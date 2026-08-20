"""Unit tests for build_mps.py — per-API MPs build script.

Uses mocked api_get to avoid hitting the real Parliament API.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add parent directory to path so we can import build_mps and schema
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_mps

# Path to the real schema JSON for integration-style tests
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "8.json",
)


def make_member_dto(mp_id, party_name="Labour", party_abbrev="Lab"):
    """Build a minimal MemberDto fixture."""
    return {
        "id": mp_id,
        "nameListAs": f"Test, MP {mp_id}",
        "nameDisplayAs": f"MP Test {mp_id}",
        "nameFullTitle": f"Mr MP Test {mp_id}",
        "gender": "M",
        "latestParty": {
            "id": 15,
            "name": party_name,
            "abbreviation": party_abbrev,
            "backgroundColour": "d50000",
            "foregroundColour": "ffffff",
        },
        "latestHouseMembership": {
            "membershipFromId": 100 + mp_id,
            "membershipFrom": f"Test North {mp_id}",
            "house": 1,
            "membershipStartDate": "2020-01-01",
            "membershipStatus": {"statusIsActive": True},
        },
        "thumbnailUrl": f"http://example.com/photo{mp_id}.jpg",
    }


class TestCreateMpsDb(unittest.TestCase):
    """Test that build_mps creates a DB with only mps + mps_fts tables."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "mps.db")

    def test_create_mps_db(self):
        """create_database_with_tables creates only mps + mps_fts + room_master_table."""
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["mps", "mps_fts"],
        )
        c = sqlite3_conn(self.db_path)
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        # mps + mps_fts (+ FTS shadow tables) + room_master_table
        self.assertIn("mps", tables)
        self.assertIn("mps_fts", tables)
        self.assertIn("room_master_table", tables)
        # No other data tables
        self.assertNotIn("divisions", tables)
        self.assertNotIn("bills", tables)
        self.assertNotIn("committees", tables)
        c.close()
        conn.close()


def sqlite3_conn(path):
    import sqlite3
    return sqlite3.connect(path)


class TestMpInsertion(unittest.TestCase):
    """Test MP insertion and FTS population."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "mps.db")

    @patch("build_mps.api_get")
    def test_mp_insertion(self, mock_api_get):
        """Seed build with --mp-limit 2 inserts 2 MPs with correct mapping."""
        page = {"items": [{"value": make_member_dto(1)}, {"value": make_member_dto(2)}]}
        mock_response = MagicMock()
        mock_response.json.return_value = page
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_mps.build_seed(self.db_path, SCHEMA_PATH, mp_limit=2)

        import sqlite3
        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM mps").fetchone()[0]
        self.assertEqual(count, 2)

        row = c.execute(
            "SELECT id, nameListAs, partyName, partyAbbreviation FROM mps WHERE id=1"
        ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "Test, MP 1")
        self.assertEqual(row[2], "Labour")
        self.assertEqual(row[3], "Lab")
        c.close()

    @patch("build_mps.api_get")
    def test_fts_populated(self, mock_api_get):
        """After inserting MPs, mps_fts has corresponding rows (triggers)."""
        page = {"items": [{"value": make_member_dto(1)}, {"value": make_member_dto(2)}]}
        mock_response = MagicMock()
        mock_response.json.return_value = page
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_mps.build_seed(self.db_path, SCHEMA_PATH, mp_limit=2)

        import sqlite3
        c = sqlite3.connect(self.db_path)
        fts_count = c.execute("SELECT COUNT(*) FROM mps_fts").fetchone()[0]
        self.assertEqual(fts_count, 2)
        c.close()


class TestDeltaUpsert(unittest.TestCase):
    """Test delta mode upserts MPs (INSERT OR REPLACE)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.prev_db = os.path.join(self.tmpdir, "prev_mps.db")
        self.db_path = os.path.join(self.tmpdir, "mps.db")

    @patch("build_mps.api_get")
    def test_delta_upsert(self, mock_api_get):
        """Delta mode updates an MP's party when it changes."""
        import sqlite3

        # Create previous DB with 2 MPs (Labour)
        conn = schema_module.create_database_with_tables(
            self.prev_db, SCHEMA_PATH, ["mps", "mps_fts"],
        )
        build_mps.insert_mps(
            conn,
            [make_member_dto(1), make_member_dto(2)],
            1700000000000,
        )
        conn.close()

        # Delta: MP 1 changes to Conservative
        page = {
            "items": [
                {"value": make_member_dto(1, "Conservative", "Con")},
                {"value": make_member_dto(2)},
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = page
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_mps.build_delta(self.db_path, self.prev_db, SCHEMA_PATH, mp_limit=2)

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM mps").fetchone()[0]
        self.assertEqual(count, 2)
        party = c.execute(
            "SELECT partyName FROM mps WHERE id=1"
        ).fetchone()[0]
        self.assertEqual(party, "Conservative")
        c.close()


class TestMpsCheckpoint(unittest.TestCase):
    """Test checkpoint/resume for MPs seed mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "mps.db")
        self.checkpoint_db = os.path.join(self.tmpdir, "checkpoint.db")

    def _make_checkpoint(self, mp_ids):
        """Create a checkpoint DB with the given MP IDs."""
        conn = schema_module.create_database_with_tables(
            self.checkpoint_db, SCHEMA_PATH, ["mps", "mps_fts"],
        )
        mps = [make_member_dto(mid) for mid in mp_ids]
        build_mps.insert_mps(conn, mps, 1700000000000)
        conn.close()

    @patch("build_mps.api_get")
    def test_seed_with_checkpoint_upserts(self, mock_api_get):
        """Checkpoint DB has MP 1; seed upserts MP 1 + inserts MP 2."""
        self._make_checkpoint([1])

        # API returns MPs 1 and 2
        page = {"items": [{"value": make_member_dto(1)}, {"value": make_member_dto(2)}]}
        mock_response = MagicMock()
        mock_response.json.return_value = page
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_mps.build_seed(
            self.db_path, SCHEMA_PATH, mp_limit=2,
            checkpoint_db=self.checkpoint_db,
        )

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM mps").fetchone()[0]
        self.assertEqual(count, 2)  # MP 1 (upserted) + MP 2 (new)
        c.close()

    @patch("build_mps.api_get")
    def test_seed_with_nonexistent_checkpoint_starts_fresh(self, mock_api_get):
        """Non-existent checkpoint path -> fresh seed."""
        page = {"items": [{"value": make_member_dto(1)}]}
        mock_response = MagicMock()
        mock_response.json.return_value = page
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_mps.build_seed(
            self.db_path, SCHEMA_PATH, mp_limit=1,
            checkpoint_db=os.path.join(self.tmpdir, "nonexistent.db"),
        )

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM mps").fetchone()[0]
        self.assertEqual(count, 1)
        c.close()

    @patch("build_mps.api_get")
    def test_seed_without_checkpoint_starts_fresh(self, mock_api_get):
        """No checkpoint_db -> fresh seed (backward compatible)."""
        page = {"items": [{"value": make_member_dto(1)}]}
        mock_response = MagicMock()
        mock_response.json.return_value = page
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_mps.build_seed(self.db_path, SCHEMA_PATH, mp_limit=1)

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM mps").fetchone()[0]
        self.assertEqual(count, 1)
        c.close()

    @patch("build_mps.api_get")
    def test_checkpoint_same_as_output(self, mock_api_get):
        """--checkpoint-db and --output same path -> no truncation, upserts."""
        self._make_checkpoint([1])
        import shutil
        shutil.copy2(self.checkpoint_db, self.db_path)

        # API returns MPs 1 and 2
        page = {"items": [{"value": make_member_dto(1)}, {"value": make_member_dto(2)}]}
        mock_response = MagicMock()
        mock_response.json.return_value = page
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_mps.build_seed(
            self.db_path, SCHEMA_PATH, mp_limit=2,
            checkpoint_db=self.db_path,  # Same path
        )

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM mps").fetchone()[0]
        self.assertEqual(count, 2)
        c.close()


if __name__ == "__main__":
    unittest.main()
