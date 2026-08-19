"""Unit tests for build_bills.py — per-API bills build script.

Uses mocked api_get to avoid hitting the real Bills API.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_bills

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "8.json",
)


def make_bill(bill_id, title="Test Bill"):
    return {
        "billId": bill_id,
        "shortTitle": title,
        "longTitle": "A long title",
        "summary": "A summary",
        "currentHouse": "Commons",
        "originatingHouse": "Commons",
        "lastUpdate": "2026-01-01T00:00:00",
        "billWithdrawn": None,
        "isDefeated": False,
        "isAct": False,
        "billTypeId": 1,
        "currentStage": {
            "description": "Second Reading",
            "abbreviation": "2R",
        },
    }


def make_bill_stages(bill_id):
    return {
        "items": [
            {
                "id": 1,
                "stageId": 10,
                "sessionId": 2024,
                "description": "First Reading",
                "abbreviation": "1R",
                "house": "Commons",
                "sortOrder": 1,
                "stageSittings": [{"date": "2026-01-01"}, {"date": "2026-01-02"}],
            },
            {
                "id": 2,
                "stageId": 20,
                "sessionId": 2024,
                "description": "Second Reading",
                "abbreviation": "2R",
                "house": "Commons",
                "sortOrder": 2,
                "stageSittings": [{"date": "2026-01-10"}],
            },
        ],
        "totalResults": 2,
    }


class TestCreateBillsDb(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "bills.db")

    def test_create_bills_db(self):
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["bills", "bill_stages"],
        )
        c = sqlite3.connect(self.db_path)
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("bills", tables)
        self.assertIn("bill_stages", tables)
        self.assertIn("room_master_table", tables)
        self.assertNotIn("mps", tables)
        self.assertNotIn("divisions", tables)
        c.close()
        conn.close()


class TestBillInsertion(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "bills.db")

    @patch("build_bills.api_get")
    def test_bill_insertion(self, mock_api_get):
        """Seed build with --bill-limit 2 inserts 2 bills + their stages."""
        bills = [make_bill(1, "Bill One"), make_bill(2, "Bill Two")]

        mock_search = MagicMock()
        mock_search.json.return_value = {"items": bills, "totalResults": 2}
        mock_search.raise_for_status = MagicMock()

        mock_stages_1 = MagicMock()
        mock_stages_1.json.return_value = make_bill_stages(1)
        mock_stages_1.raise_for_status = MagicMock()

        mock_stages_2 = MagicMock()
        mock_stages_2.json.return_value = make_bill_stages(2)
        mock_stages_2.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_stages_1, mock_stages_2]

        build_bills.build_seed(self.db_path, SCHEMA_PATH, bill_limit=2)

        c = sqlite3.connect(self.db_path)
        bill_count = c.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        self.assertEqual(bill_count, 2)
        stage_count = c.execute("SELECT COUNT(*) FROM bill_stages").fetchone()[0]
        self.assertEqual(stage_count, 4)  # 2 stages per bill

        # Verify sittingDates is JSON-encoded
        sitting = c.execute(
            "SELECT sittingDates FROM bill_stages WHERE billId=1 AND stageId=10"
        ).fetchone()[0]
        dates = json.loads(sitting)
        self.assertEqual(dates, ["2026-01-01", "2026-01-02"])
        c.close()


class TestDeltaUpsert(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.prev_db = os.path.join(self.tmpdir, "prev_bills.db")
        self.db_path = os.path.join(self.tmpdir, "bills.db")

    @patch("build_bills.api_get")
    def test_delta_upsert(self, mock_api_get):
        """Delta mode updates a bill's isAct status when it changes."""
        # Previous DB with bill 1 (isAct=0)
        conn = schema_module.create_database_with_tables(
            self.prev_db, SCHEMA_PATH, ["bills", "bill_stages"],
        )
        build_bills.insert_bills(conn, [make_bill(1)], 1700000000000)
        conn.close()

        # Delta: bill 1 now isAct=True
        updated_bill = make_bill(1)
        updated_bill["isAct"] = True

        mock_search = MagicMock()
        mock_search.json.return_value = {"items": [updated_bill], "totalResults": 1}
        mock_search.raise_for_status = MagicMock()

        mock_stages = MagicMock()
        mock_stages.json.return_value = make_bill_stages(1)
        mock_stages.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_stages]

        build_bills.build_delta(self.db_path, self.prev_db, SCHEMA_PATH, bill_limit=1)

        c = sqlite3.connect(self.db_path)
        is_act = c.execute("SELECT isAct FROM bills WHERE id=1").fetchone()[0]
        self.assertEqual(is_act, 1)
        c.close()


if __name__ == "__main__":
    unittest.main()
