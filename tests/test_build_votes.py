"""Unit tests for build_votes.py — per-API votes build script.

Tests Commons/Lords division insertion, the Lords failure resilience
design, and delta mode. Uses mocked api_get.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import requests

# Add parent directory to path so we can import build_votes and schema
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_votes

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "8.json",
)


def make_commons_division(div_id):
    return {
        "DivisionId": div_id,
        "Title": f"Commons Division {div_id}",
        "Date": "2026-01-01T00:00:00",
        "PublicationUpdated": "2026-01-01T00:00:00",
        "Number": div_id,
        "IsDeferred": False,
        "AyeCount": 300,
        "NoCount": 200,
    }


def make_commons_detail(div_id):
    return {
        "Ayes": [{"MemberId": 1, "Name": "MP A", "Party": "Lab",
                  "PartyColour": "d50000", "MemberFrom": "North"}],
        "Noes": [{"MemberId": 2, "Name": "MP B", "Party": "Con",
                  "PartyColour": "0087dc", "MemberFrom": "South"}],
        "AyeTellers": [],
        "NoTellers": [],
    }


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


class TestCreateVotesDb(unittest.TestCase):
    """Test that build_votes creates a DB with only divisions + division_votes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "votes.db")

    def test_create_votes_db(self):
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["divisions", "division_votes"],
        )
        c = sqlite3.connect(self.db_path)
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("divisions", tables)
        self.assertIn("division_votes", tables)
        self.assertIn("room_master_table", tables)
        self.assertNotIn("mps", tables)
        self.assertNotIn("bills", tables)
        c.close()
        conn.close()


class TestCommonsDivisionInsert(unittest.TestCase):
    """Test Commons division + votes insertion (house=1)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "votes.db")

    @patch("build_votes.api_get")
    def test_commons_division_insert(self, mock_api_get):
        """Mock api_get to return a Commons division + detail; verify house=1 + votes."""
        # api_get calls: 1) divisions search list, 2) division detail
        search_page = [make_commons_division(100)]
        detail = make_commons_detail(100)

        mock_search = MagicMock()
        mock_search.json.return_value = search_page
        mock_search.raise_for_status = MagicMock()

        mock_detail = MagicMock()
        mock_detail.json.return_value = detail
        mock_detail.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_search, mock_detail]

        build_votes.build_seed(self.db_path, SCHEMA_PATH, divisions_limit=1)

        c = sqlite3.connect(self.db_path)
        div_count = c.execute("SELECT COUNT(*) FROM divisions").fetchone()[0]
        self.assertEqual(div_count, 1)
        house = c.execute("SELECT house FROM divisions WHERE id=100").fetchone()[0]
        self.assertEqual(house, 1)
        vote_count = c.execute("SELECT COUNT(*) FROM division_votes").fetchone()[0]
        self.assertEqual(vote_count, 2)  # 1 Aye + 1 No
        c.close()


class TestLordsDivisionInsert(unittest.TestCase):
    """Test Lords division + votes insertion (house=2)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "votes.db")

    @patch("build_votes.api_get")
    def test_lords_division_insert(self, mock_api_get):
        """Mock api_get: empty Commons, one Lords division + detail; verify house=2."""
        commons_search = []  # no Commons divisions
        lords_search = [make_lords_division(200)]
        lords_detail = make_lords_detail(200)

        def make_mock(payload):
            m = MagicMock()
            m.json.return_value = payload
            m.raise_for_status = MagicMock()
            return m

        mock_api_get.side_effect = [
            make_mock(commons_search),
            make_mock(lords_search),
            make_mock(lords_detail),
        ]

        build_votes.build_seed(self.db_path, SCHEMA_PATH, divisions_limit=1)

        c = sqlite3.connect(self.db_path)
        div_count = c.execute("SELECT COUNT(*) FROM divisions").fetchone()[0]
        self.assertEqual(div_count, 1)
        house = c.execute("SELECT house FROM divisions WHERE id=200").fetchone()[0]
        self.assertEqual(house, 2)
        vote_count = c.execute("SELECT COUNT(*) FROM division_votes").fetchone()[0]
        self.assertEqual(vote_count, 2)  # 1 Content + 1 NotContent
        c.close()


class TestLordsFailureResilience(unittest.TestCase):
    """Test that a Lords API failure does NOT crash the build (resilience fix)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "votes.db")

    @patch("build_votes.api_get")
    def test_lords_failure_resilience(self, mock_api_get):
        """Commons succeeds, Lords raises ReadTimeout — Commons data preserved, no crash."""
        commons_search = [make_commons_division(100)]
        commons_detail = make_commons_detail(100)

        def make_mock(payload):
            m = MagicMock()
            m.json.return_value = payload
            m.raise_for_status = MagicMock()
            return m

        # Commons search + detail succeed; Lords search raises ReadTimeout
        mock_api_get.side_effect = [
            make_mock(commons_search),
            make_mock(commons_detail),
            requests.exceptions.ReadTimeout("Lords API down"),
        ]

        # Should NOT raise
        build_votes.build_seed(self.db_path, SCHEMA_PATH, divisions_limit=1)

        c = sqlite3.connect(self.db_path)
        # Commons data is preserved
        commons = c.execute(
            "SELECT COUNT(*) FROM divisions WHERE house=1"
        ).fetchone()[0]
        self.assertEqual(commons, 1)
        votes = c.execute("SELECT COUNT(*) FROM division_votes").fetchone()[0]
        self.assertEqual(votes, 2)
        # No Lords data
        lords = c.execute(
            "SELECT COUNT(*) FROM divisions WHERE house=2"
        ).fetchone()[0]
        self.assertEqual(lords, 0)
        c.close()


class TestDeltaNewDivisions(unittest.TestCase):
    """Test delta mode fetches only new divisions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.prev_db = os.path.join(self.tmpdir, "prev_votes.db")
        self.db_path = os.path.join(self.tmpdir, "votes.db")

    @patch("build_votes.api_get")
    def test_delta_new_divisions(self, mock_api_get):
        """Previous DB has 2 Commons divisions; delta adds 1 new → 3 total."""
        # Build previous DB with 2 Commons divisions
        conn = schema_module.create_database_with_tables(
            self.prev_db, SCHEMA_PATH, ["divisions", "division_votes"],
        )
        ts = 1700000000000
        conn.execute(
            "INSERT OR REPLACE INTO divisions (id, title, date, publicationUpdated, "
            "number, isDeferred, ayeCount, noCount, house, lastUpdated) "
            "VALUES (100, 'D100', '2026-01-01', NULL, 100, 0, 300, 200, 1, ?)", (ts,))
        conn.execute(
            "INSERT OR REPLACE INTO divisions (id, title, date, publicationUpdated, "
            "number, isDeferred, ayeCount, noCount, house, lastUpdated) "
            "VALUES (101, 'D101', '2026-01-01', NULL, 101, 0, 300, 200, 1, ?)", (ts,))
        conn.commit()
        conn.close()

        # Delta: API returns divisions 101 (old) and 102 (new); only 102 is new
        search_page = [make_commons_division(102), make_commons_division(101)]
        detail = make_commons_detail(102)

        def make_mock(payload):
            m = MagicMock()
            m.json.return_value = payload
            m.raise_for_status = MagicMock()
            return m

        # Commons delta search (returns 102, 101 — 102 is new), detail for 102,
        # Lords delta search (empty)
        mock_api_get.side_effect = [
            make_mock(search_page),
            make_mock(detail),
            make_mock([]),  # Lords: empty
        ]

        build_votes.build_delta(self.db_path, self.prev_db, SCHEMA_PATH)

        c = sqlite3.connect(self.db_path)
        total = c.execute("SELECT COUNT(*) FROM divisions WHERE house=1").fetchone()[0]
        self.assertEqual(total, 3)  # 2 old + 1 new
        has_102 = c.execute(
            "SELECT COUNT(*) FROM divisions WHERE id=102"
        ).fetchone()[0]
        self.assertEqual(has_102, 1)
        c.close()


if __name__ == "__main__":
    unittest.main()
