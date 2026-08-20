"""Unit tests for build_committees.py — per-API committees build script.

Uses mocked api_get to avoid hitting the real Committees API.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_committees

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "8.json",
)


def make_mp(mp_id):
    return {
        "id": mp_id,
        "nameListAs": f"Test, MP {mp_id}",
        "nameDisplayAs": f"MP Test {mp_id}",
        "latestParty": {"id": 15, "name": "Labour", "abbreviation": "Lab"},
        "latestHouseMembership": {
            "membershipFromId": 100,
            "membershipFrom": "Test North",
            "house": 1,
            "membershipStatus": {"statusIsActive": True},
        },
    }


def make_committee_item(cid, name="Test Committee", end_date=None):
    return {
        "id": cid,
        "name": name,
        "house": "Commons",
        "category": {"id": 1, "name": "Departmental"},
        "startDate": "2020-01-01",
        "endDate": end_date,
    }


class TestCreateCommitteesDb(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "committees.db")

    def test_create_committees_db(self):
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["committees", "mp_committee_cross_ref"],
        )
        c = sqlite3.connect(self.db_path)
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("committees", tables)
        self.assertIn("mp_committee_cross_ref", tables)
        self.assertIn("room_master_table", tables)
        self.assertNotIn("mps", tables)
        self.assertNotIn("bills", tables)
        c.close()
        conn.close()


class TestCommitteeInsertion(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "committees.db")

    @patch("build_committees.api_get")
    def test_committee_insertion(self, mock_api_get):
        """Seed build with --mp-limit 2 fetches committees per MP."""
        # api_get calls: 1) MP search, 2) committees for MP 1, 3) committees for MP 2
        mp_page = {"items": [{"value": make_mp(1)}, {"value": make_mp(2)}]}

        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_comm_1 = MagicMock()
        mock_comm_1.json.return_value = {
            "items": [make_committee_item(100, "Comm A")]
        }
        mock_comm_1.raise_for_status = MagicMock()

        mock_comm_2 = MagicMock()
        mock_comm_2.json.return_value = {
            "items": [make_committee_item(200, "Comm B"), make_committee_item(100, "Comm A")]
        }
        mock_comm_2.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_comm_1, mock_comm_2]

        build_committees.build_seed(self.db_path, SCHEMA_PATH, mp_limit=2)

        c = sqlite3.connect(self.db_path)
        # Committee 100 appears for both MPs but deduped by PK → 2 unique committees
        comm_count = c.execute("SELECT COUNT(*) FROM committees").fetchone()[0]
        self.assertEqual(comm_count, 2)
        # 1 cross-ref for MP 1, 2 cross-refs for MP 2 → 3 total
        xref_count = c.execute(
            "SELECT COUNT(*) FROM mp_committee_cross_ref"
        ).fetchone()[0]
        self.assertEqual(xref_count, 3)

        # Verify isActive derived from endDate == null
        is_active = c.execute(
            "SELECT isActive FROM committees WHERE id=100"
        ).fetchone()[0]
        self.assertEqual(is_active, 1)  # end_date=None → active
        c.close()


class TestDeltaUpsert(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.prev_db = os.path.join(self.tmpdir, "prev_committees.db")
        self.db_path = os.path.join(self.tmpdir, "committees.db")

    @patch("build_committees.api_get")
    def test_delta_upsert(self, mock_api_get):
        """Delta mode updates a committee when it gets an endDate (now inactive)."""
        # Previous DB with committee 100 (active, no endDate)
        conn = schema_module.create_database_with_tables(
            self.prev_db, SCHEMA_PATH, ["committees", "mp_committee_cross_ref"],
        )
        build_committees.insert_committees(
            conn, [make_committee_item(100, "Comm A")], 1700000000000,
        )
        build_committees.insert_cross_refs(conn, 1, [100], 1700000000000)
        conn.close()

        # Delta: committee 100 now has endDate → inactive
        mp_page = {"items": [{"value": make_mp(1)}]}
        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_comm = MagicMock()
        mock_comm.json.return_value = {
            "items": [make_committee_item(100, "Comm A", end_date="2026-06-01")]
        }
        mock_comm.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_comm]

        build_committees.build_delta(
            self.db_path, self.prev_db, SCHEMA_PATH, mp_limit=1,
        )

        c = sqlite3.connect(self.db_path)
        is_active = c.execute(
            "SELECT isActive FROM committees WHERE id=100"
        ).fetchone()[0]
        self.assertEqual(is_active, 0)  # now inactive
        c.close()


class TestCommitteesCheckpoint(unittest.TestCase):
    """Test checkpoint/resume for committees seed mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "committees.db")
        self.checkpoint_db = os.path.join(self.tmpdir, "checkpoint.db")

    def _make_checkpoint(self, mp_ids):
        """Create a checkpoint DB with cross-refs for the given MP IDs."""
        conn = schema_module.create_database_with_tables(
            self.checkpoint_db, SCHEMA_PATH, ["committees", "mp_committee_cross_ref"],
        )
        for mp_id in mp_ids:
            build_committees.insert_cross_refs(conn, mp_id, [100], 1700000000000)
        conn.commit()
        conn.close()

    @patch("build_committees.api_get")
    def test_committees_checkpoint_skips_processed_mps(self, mock_api_get):
        """Checkpoint DB has cross-refs for MP 1; only MP 2 is fetched."""
        self._make_checkpoint([1])

        # API returns MPs 1 and 2; only MP 2 should be fetched
        mp_page = {"items": [{"value": make_mp(1)}, {"value": make_mp(2)}]}

        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_comm_2 = MagicMock()
        mock_comm_2.json.return_value = {
            "items": [make_committee_item(200, "Comm B")]
        }
        mock_comm_2.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_comm_2]

        build_committees.build_seed(
            self.db_path, SCHEMA_PATH, mp_limit=2,
            checkpoint_db=self.checkpoint_db,
        )

        # Only 2 api_get calls: 1 for MP list + 1 for MP 2's committees
        # MP 1 was skipped (in checkpoint)
        self.assertEqual(mock_api_get.call_count, 2)

        c = sqlite3.connect(self.db_path)
        # MP 1's cross-ref from checkpoint + MP 2's new cross-ref
        xref_count = c.execute(
            "SELECT COUNT(*) FROM mp_committee_cross_ref"
        ).fetchone()[0]
        self.assertEqual(xref_count, 2)  # 1 from checkpoint + 1 new
        c.close()

    @patch("build_committees.api_get")
    def test_seed_with_nonexistent_checkpoint_starts_fresh(self, mock_api_get):
        """Non-existent checkpoint path -> fresh seed."""
        mp_page = {"items": [{"value": make_mp(1)}]}

        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_comm = MagicMock()
        mock_comm.json.return_value = {
            "items": [make_committee_item(100, "Comm A")]
        }
        mock_comm.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_comm]

        build_committees.build_seed(
            self.db_path, SCHEMA_PATH, mp_limit=1,
            checkpoint_db=os.path.join(self.tmpdir, "nonexistent.db"),
        )

        c = sqlite3.connect(self.db_path)
        xref_count = c.execute(
            "SELECT COUNT(*) FROM mp_committee_cross_ref"
        ).fetchone()[0]
        self.assertEqual(xref_count, 1)
        c.close()

    @patch("build_committees.api_get")
    def test_seed_without_checkpoint_starts_fresh(self, mock_api_get):
        """No checkpoint_db -> fresh seed (backward compatible)."""
        mp_page = {"items": [{"value": make_mp(1)}]}

        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_comm = MagicMock()
        mock_comm.json.return_value = {
            "items": [make_committee_item(100, "Comm A")]
        }
        mock_comm.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_comm]

        build_committees.build_seed(self.db_path, SCHEMA_PATH, mp_limit=1)

        c = sqlite3.connect(self.db_path)
        xref_count = c.execute(
            "SELECT COUNT(*) FROM mp_committee_cross_ref"
        ).fetchone()[0]
        self.assertEqual(xref_count, 1)
        c.close()

    @patch("build_committees.api_get")
    def test_checkpoint_same_as_output(self, mock_api_get):
        """--checkpoint-db and --output same path -> no truncation, resumes."""
        self._make_checkpoint([1])
        import shutil
        shutil.copy2(self.checkpoint_db, self.db_path)

        # API returns MPs 1 and 2; only MP 2 should be fetched
        mp_page = {"items": [{"value": make_mp(1)}, {"value": make_mp(2)}]}

        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_comm_2 = MagicMock()
        mock_comm_2.json.return_value = {
            "items": [make_committee_item(200, "Comm B")]
        }
        mock_comm_2.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_comm_2]

        build_committees.build_seed(
            self.db_path, SCHEMA_PATH, mp_limit=2,
            checkpoint_db=self.db_path,  # Same path
        )

        c = sqlite3.connect(self.db_path)
        xref_count = c.execute(
            "SELECT COUNT(*) FROM mp_committee_cross_ref"
        ).fetchone()[0]
        self.assertEqual(xref_count, 2)  # 1 from checkpoint + 1 new
        c.close()


if __name__ == "__main__":
    unittest.main()
