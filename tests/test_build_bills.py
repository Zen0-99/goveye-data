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


class TestSmartDelta(unittest.TestCase):
    """Test that smart delta only fetches stage details for changed/new bills."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.prev_db = os.path.join(self.tmpdir, "prev_bills.db")
        self.db_path = os.path.join(self.tmpdir, "bills.db")

    def _make_prev_db(self, bill_id, last_update):
        """Create a previous DB with one bill."""
        conn = schema_module.create_database_with_tables(
            self.prev_db, SCHEMA_PATH, ["bills", "bill_stages"],
        )
        bill = make_bill(bill_id)
        bill["lastUpdate"] = last_update
        build_bills.insert_bills(conn, [bill], 1700000000000)
        conn.close()

    @patch("build_bills.api_get")
    def test_delta_skips_unchanged_bill_stages(self, mock_api_get):
        """Bill with same lastUpdate -> stage detail fetch NOT called."""
        self._make_prev_db(1, "2024-01-01T00:00:00")

        # API returns same bill with same lastUpdate
        bill = make_bill(1)
        bill["lastUpdate"] = "2024-01-01T00:00:00"

        mock_search = MagicMock()
        mock_search.json.return_value = {"items": [bill], "totalResults": 1}
        mock_search.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search]  # Only the list fetch, no stage fetch

        build_bills.build_delta(self.db_path, self.prev_db, SCHEMA_PATH, bill_limit=1)

        # Verify fetch_bill_stages was NOT called (only 1 api_get call for the list)
        self.assertEqual(mock_api_get.call_count, 1)

    @patch("build_bills.api_get")
    def test_delta_fetches_changed_bill_stages(self, mock_api_get):
        """Bill with changed lastUpdate -> stage detail fetch IS called."""
        self._make_prev_db(1, "2024-01-01T00:00:00")

        # API returns bill with different lastUpdate
        bill = make_bill(1)
        bill["lastUpdate"] = "2024-02-01T00:00:00"

        mock_search = MagicMock()
        mock_search.json.return_value = {"items": [bill], "totalResults": 1}
        mock_search.raise_for_status = MagicMock()

        mock_stages = MagicMock()
        mock_stages.json.return_value = make_bill_stages(1)
        mock_stages.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_stages]

        build_bills.build_delta(self.db_path, self.prev_db, SCHEMA_PATH, bill_limit=1)

        # Verify fetch_bill_stages WAS called (2 api_get calls)
        self.assertEqual(mock_api_get.call_count, 2)

    @patch("build_bills.api_get")
    def test_delta_fetches_new_bill_stages(self, mock_api_get):
        """Bill not in previous DB -> stage detail fetch IS called."""
        self._make_prev_db(1, "2024-01-01T00:00:00")

        # API returns a new bill (id=2) not in previous DB
        bill = make_bill(2)
        bill["lastUpdate"] = "2024-01-01T00:00:00"

        mock_search = MagicMock()
        mock_search.json.return_value = {"items": [bill], "totalResults": 1}
        mock_search.raise_for_status = MagicMock()

        mock_stages = MagicMock()
        mock_stages.json.return_value = make_bill_stages(2)
        mock_stages.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_stages]

        build_bills.build_delta(self.db_path, self.prev_db, SCHEMA_PATH, bill_limit=1)

        # Verify fetch_bill_stages WAS called (2 api_get calls)
        self.assertEqual(mock_api_get.call_count, 2)

    @patch("build_bills.api_get")
    def test_delta_upserts_all_bills(self, mock_api_get):
        """All bills (changed and unchanged) are upserted into the DB."""
        self._make_prev_db(1, "2024-01-01T00:00:00")

        # API returns 2 bills: bill 1 (unchanged) + bill 2 (new)
        bill1 = make_bill(1)
        bill1["lastUpdate"] = "2024-01-01T00:00:00"
        bill1["shortTitle"] = "Updated Title"  # Changed title but same lastUpdate
        bill2 = make_bill(2)
        bill2["lastUpdate"] = "2024-02-01T00:00:00"

        mock_search = MagicMock()
        mock_search.json.return_value = {"items": [bill1, bill2], "totalResults": 2}
        mock_search.raise_for_status = MagicMock()

        mock_stages = MagicMock()
        mock_stages.json.return_value = make_bill_stages(2)
        mock_stages.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_stages]

        build_bills.build_delta(self.db_path, self.prev_db, SCHEMA_PATH, bill_limit=2)

        c = sqlite3.connect(self.db_path)
        # Both bills should be in the DB
        bill_count = c.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        self.assertEqual(bill_count, 2)
        # Bill 1 title should be updated (upserted)
        title = c.execute("SELECT shortTitle FROM bills WHERE id=1").fetchone()[0]
        self.assertEqual(title, "Updated Title")
        c.close()


class TestBillsCheckpoint(unittest.TestCase):
    """Test checkpoint/resume for bills seed mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "bills.db")
        self.checkpoint_db = os.path.join(self.tmpdir, "checkpoint.db")

    @patch("build_bills.api_get")
    def test_seed_with_checkpoint_resumes(self, mock_api_get):
        """Checkpoint DB has bill 1; seed resumes and only fetches bill 2+."""
        # Create checkpoint DB with bill 1
        conn = schema_module.create_database_with_tables(
            self.checkpoint_db, SCHEMA_PATH, ["bills", "bill_stages"],
        )
        build_bills.insert_bills(conn, [make_bill(1)], 1700000000000)
        conn.close()

        # API returns bills 1 and 2; only 2 should be processed
        bills = [make_bill(1), make_bill(2)]
        mock_search = MagicMock()
        mock_search.json.return_value = {"items": bills, "totalResults": 2}
        mock_search.raise_for_status = MagicMock()

        mock_stages = MagicMock()
        mock_stages.json.return_value = make_bill_stages(2)
        mock_stages.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_stages]

        build_bills.build_seed(
            self.db_path, SCHEMA_PATH, bill_limit=2,
            checkpoint_db=self.checkpoint_db,
        )

        c = sqlite3.connect(self.db_path)
        # Both bills should be in the DB (1 from checkpoint, 2 from fetch)
        bill_count = c.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        self.assertEqual(bill_count, 2)
        # Only bill 2's stages should be fetched (1 api_get for list + 1 for stages)
        self.assertEqual(mock_api_get.call_count, 2)
        c.close()

    @patch("build_bills.api_get")
    def test_seed_with_nonexistent_checkpoint_starts_fresh(self, mock_api_get):
        """Non-existent checkpoint path -> fresh seed."""
        bills = [make_bill(1)]
        mock_search = MagicMock()
        mock_search.json.return_value = {"items": bills, "totalResults": 1}
        mock_search.raise_for_status = MagicMock()

        mock_stages = MagicMock()
        mock_stages.json.return_value = make_bill_stages(1)
        mock_stages.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_stages]

        build_bills.build_seed(
            self.db_path, SCHEMA_PATH, bill_limit=1,
            checkpoint_db=os.path.join(self.tmpdir, "nonexistent.db"),
        )

        c = sqlite3.connect(self.db_path)
        bill_count = c.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        self.assertEqual(bill_count, 1)
        c.close()

    @patch("build_bills.api_get")
    def test_seed_without_checkpoint_starts_fresh(self, mock_api_get):
        """No checkpoint_db -> fresh seed (backward compatible)."""
        bills = [make_bill(1)]
        mock_search = MagicMock()
        mock_search.json.return_value = {"items": bills, "totalResults": 1}
        mock_search.raise_for_status = MagicMock()

        mock_stages = MagicMock()
        mock_stages.json.return_value = make_bill_stages(1)
        mock_stages.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_stages]

        build_bills.build_seed(self.db_path, SCHEMA_PATH, bill_limit=1)

        c = sqlite3.connect(self.db_path)
        bill_count = c.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        self.assertEqual(bill_count, 1)
        c.close()

    @patch("build_bills.api_get")
    def test_checkpoint_same_as_output(self, mock_api_get):
        """--checkpoint-db and --output are the same path -> no truncation, resumes."""
        # Create the DB file with bill 1
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["bills", "bill_stages"],
        )
        build_bills.insert_bills(conn, [make_bill(1)], 1700000000000)
        conn.close()

        # API returns bills 1 and 2; only 2 should be processed
        bills = [make_bill(1), make_bill(2)]
        mock_search = MagicMock()
        mock_search.json.return_value = {"items": bills, "totalResults": 2}
        mock_search.raise_for_status = MagicMock()

        mock_stages = MagicMock()
        mock_stages.json.return_value = make_bill_stages(2)
        mock_stages.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_stages]

        build_bills.build_seed(
            self.db_path, SCHEMA_PATH, bill_limit=2,
            checkpoint_db=self.db_path,  # Same path
        )

        c = sqlite3.connect(self.db_path)
        # Both bills should be in the DB (1 from checkpoint, 2 from fetch)
        bill_count = c.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        self.assertEqual(bill_count, 2)
        c.close()


if __name__ == "__main__":
    unittest.main()
