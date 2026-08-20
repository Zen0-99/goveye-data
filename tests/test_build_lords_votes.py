"""Unit tests for build_lords_votes.py — Lords votes build script.

Tests Lords division insertion and delta mode. Uses mocked api_get.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add parent directory to path so we can import build_lords_votes and schema
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_lords_votes

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "bundled_schema.json",
)


def make_lords_division(div_id):
    return {
        "divisionId": div_id,
        "title": f"Lords Division {div_id}",
        "date": "2026-01-02T00:00:00",
        "number": div_id,
        "memberContentCount": 100,
        "memberNotContentCount": 50,
    }


def make_lords_detail(div_id):
    return {
        "contents": [{"memberId": 10, "name": "Lord A", "party": "Lab",
                      "partyColour": "d50000", "memberFrom": ""}],
        "notContents": [{"memberId": 11, "name": "Lord B", "party": "Con",
                         "partyColour": "0087dc", "memberFrom": ""}],
        "contentTellers": [],
        "notContentTellers": [],
    }


class TestLordsDivisionInsert(unittest.TestCase):
    """Test Lords division + votes insertion (house=2)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "lords_votes.db")

    @patch("build_lords_votes.api_get")
    def test_lords_division_insert(self, mock_api_get):
        """Mock api_get: one Lords division + detail; verify house=2."""
        lords_search = [make_lords_division(200)]
        lords_detail = make_lords_detail(200)

        def make_mock(payload):
            m = MagicMock()
            m.json.return_value = payload
            m.raise_for_status = MagicMock()
            return m

        mock_api_get.side_effect = [
            make_mock(lords_search),
            make_mock(lords_detail),
        ]

        build_lords_votes.build_seed(self.db_path, SCHEMA_PATH, divisions_limit=1)

        c = sqlite3.connect(self.db_path)
        div_count = c.execute("SELECT COUNT(*) FROM divisions").fetchone()[0]
        self.assertEqual(div_count, 1)
        house = c.execute("SELECT house FROM divisions WHERE id=200").fetchone()[0]
        self.assertEqual(house, 2)
        vote_count = c.execute("SELECT COUNT(*) FROM division_votes").fetchone()[0]
        self.assertEqual(vote_count, 2)  # 1 Content + 1 NotContent
        c.close()


class TestLordsDeltaNewDivisions(unittest.TestCase):
    """Test delta mode fetches only new Lords divisions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.prev_db = os.path.join(self.tmpdir, "prev_lords_votes.db")
        self.db_path = os.path.join(self.tmpdir, "lords_votes.db")

    @patch("build_lords_votes.api_get")
    def test_delta_new_divisions(self, mock_api_get):
        """Previous DB has 1 Lords division; delta adds 1 new -> 2 total."""
        conn = schema_module.create_database_with_tables(
            self.prev_db, SCHEMA_PATH, ["divisions", "division_votes"],
        )
        ts = 1700000000000
        conn.execute(
            "INSERT OR REPLACE INTO divisions (id, title, date, publicationUpdated, "
            "number, isDeferred, ayeCount, noCount, house, lastUpdated) "
            "VALUES (200, 'D200', '2026-01-01', NULL, 200, 0, 100, 50, 2, ?)", (ts,))
        conn.commit()
        conn.close()

        # Delta: API returns divisions 201 (new) and 200 (old); only 201 is new
        search_page = [make_lords_division(201), make_lords_division(200)]
        detail = make_lords_detail(201)

        def make_mock(payload):
            m = MagicMock()
            m.json.return_value = payload
            m.raise_for_status = MagicMock()
            return m

        mock_api_get.side_effect = [
            make_mock(search_page),
            make_mock(detail),
        ]

        build_lords_votes.build_delta(self.db_path, self.prev_db, SCHEMA_PATH)

        c = sqlite3.connect(self.db_path)
        total = c.execute("SELECT COUNT(*) FROM divisions WHERE house=2").fetchone()[0]
        self.assertEqual(total, 2)  # 1 old + 1 new
        has_201 = c.execute(
            "SELECT COUNT(*) FROM divisions WHERE id=201"
        ).fetchone()[0]
        self.assertEqual(has_201, 1)
        c.close()


class TestLordsCheckpoint(unittest.TestCase):
    """Test checkpoint/resume for Lords seed mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "lords_votes.db")
        self.checkpoint_db = os.path.join(self.tmpdir, "checkpoint.db")

    def _make_checkpoint(self, div_id):
        """Create a checkpoint DB with one Lords division."""
        conn = schema_module.create_database_with_tables(
            self.checkpoint_db, SCHEMA_PATH, ["divisions", "division_votes"],
        )
        ts = 1700000000000
        conn.execute(
            "INSERT OR REPLACE INTO divisions (id, title, date, publicationUpdated, "
            "number, isDeferred, ayeCount, noCount, house, lastUpdated) "
            "VALUES (?, 'D', '2026-01-01', NULL, ?, 0, 100, 50, 2, ?)",
            (div_id, div_id, ts,))
        conn.commit()
        conn.close()

    @patch("build_lords_votes.api_get")
    def test_seed_with_checkpoint_resumes(self, mock_api_get):
        """Checkpoint DB has division 200; seed resumes and only fetches 201+."""
        self._make_checkpoint(200)

        # API returns 200 (old) and 201 (new); only 201 should be processed
        search_page = [make_lords_division(201), make_lords_division(200)]
        detail = make_lords_detail(201)

        def make_mock(payload):
            m = MagicMock()
            m.json.return_value = payload
            m.raise_for_status = MagicMock()
            return m

        mock_api_get.side_effect = [make_mock(search_page), make_mock(detail)]

        build_lords_votes.build_seed(
            self.db_path, SCHEMA_PATH,
            checkpoint_db=self.checkpoint_db,
        )

        c = sqlite3.connect(self.db_path)
        total = c.execute("SELECT COUNT(*) FROM divisions WHERE house=2").fetchone()[0]
        self.assertEqual(total, 2)  # 1 from checkpoint + 1 new
        c.close()

    @patch("build_lords_votes.api_get")
    def test_seed_with_nonexistent_checkpoint_starts_fresh(self, mock_api_get):
        """Non-existent checkpoint path -> fresh seed."""
        search_page = [make_lords_division(200)]
        detail = make_lords_detail(200)

        def make_mock(payload):
            m = MagicMock()
            m.json.return_value = payload
            m.raise_for_status = MagicMock()
            return m

        mock_api_get.side_effect = [make_mock(search_page), make_mock(detail)]

        build_lords_votes.build_seed(
            self.db_path, SCHEMA_PATH, divisions_limit=1,
            checkpoint_db=os.path.join(self.tmpdir, "nonexistent.db"),
        )

        c = sqlite3.connect(self.db_path)
        div_count = c.execute("SELECT COUNT(*) FROM divisions").fetchone()[0]
        self.assertEqual(div_count, 1)
        c.close()

    @patch("build_lords_votes.api_get")
    def test_seed_without_checkpoint_starts_fresh(self, mock_api_get):
        """No checkpoint_db -> fresh seed (backward compatible)."""
        search_page = [make_lords_division(200)]
        detail = make_lords_detail(200)

        def make_mock(payload):
            m = MagicMock()
            m.json.return_value = payload
            m.raise_for_status = MagicMock()
            return m

        mock_api_get.side_effect = [make_mock(search_page), make_mock(detail)]

        build_lords_votes.build_seed(self.db_path, SCHEMA_PATH, divisions_limit=1)

        c = sqlite3.connect(self.db_path)
        div_count = c.execute("SELECT COUNT(*) FROM divisions").fetchone()[0]
        self.assertEqual(div_count, 1)
        c.close()

    @patch("build_lords_votes.api_get")
    def test_checkpoint_same_as_output(self, mock_api_get):
        """--checkpoint-db and --output same path -> no truncation, resumes."""
        self._make_checkpoint(200)
        # Copy checkpoint to output path
        import shutil
        shutil.copy2(self.checkpoint_db, self.db_path)

        # API returns 200 (old) and 201 (new); only 201 should be processed
        search_page = [make_lords_division(201), make_lords_division(200)]
        detail = make_lords_detail(201)

        def make_mock(payload):
            m = MagicMock()
            m.json.return_value = payload
            m.raise_for_status = MagicMock()
            return m

        mock_api_get.side_effect = [make_mock(search_page), make_mock(detail)]

        build_lords_votes.build_seed(
            self.db_path, SCHEMA_PATH,
            checkpoint_db=self.db_path,  # Same path
        )

        c = sqlite3.connect(self.db_path)
        total = c.execute("SELECT COUNT(*) FROM divisions WHERE house=2").fetchone()[0]
        self.assertEqual(total, 2)
        c.close()


if __name__ == "__main__":
    unittest.main()
