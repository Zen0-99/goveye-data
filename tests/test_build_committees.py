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
    "schemas", "bundled_schema.json",
)


def make_member_item(mnis_id):
    """Committee member item as returned by /Committees/{id}/Members.

    A role with endDate=None means currently serving.
    """
    return {
        "memberInfo": {"mnisId": mnis_id, "house": "Commons"},
        "name": f"MP Test {mnis_id}",
        "roles": [{"endDate": None}],
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
        """Seed build fetches committee list, then detail + members per committee."""
        # api_get calls: 1) committee list, 2-3) detail + members for comm 100,
        # 4-5) detail + members for comm 200
        mock_list = MagicMock()
        mock_list.json.return_value = {
            "items": [
                make_committee_item(100, "Comm A"),
                make_committee_item(200, "Comm B"),
            ]
        }
        mock_list.raise_for_status = MagicMock()

        def mock_detail():
            m = MagicMock()
            m.json.return_value = {}
            m.raise_for_status = MagicMock()
            return m

        mock_mem_100 = MagicMock()
        mock_mem_100.json.return_value = {
            "items": [make_member_item(1), make_member_item(2)]
        }
        mock_mem_100.raise_for_status = MagicMock()

        mock_mem_200 = MagicMock()
        mock_mem_200.json.return_value = {"items": [make_member_item(2)]}
        mock_mem_200.raise_for_status = MagicMock()

        mock_api_get.side_effect = [
            mock_list, mock_detail(), mock_mem_100, mock_detail(), mock_mem_200,
        ]

        build_committees.build_seed(self.db_path, SCHEMA_PATH)

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
        row = build_committees.map_committee_to_entity(
            make_committee_item(100, "Comm A"), None, 1700000000000,
        )
        build_committees.insert_committees(conn, [row])
        build_committees.insert_cross_refs(conn, 100, [1], 1700000000000)
        conn.close()

        # Delta: committee 100 now has endDate → inactive
        mock_list = MagicMock()
        mock_list.json.return_value = {
            "items": [make_committee_item(100, "Comm A", end_date="2026-06-01")]
        }
        mock_list.raise_for_status = MagicMock()

        mock_detail = MagicMock()
        mock_detail.json.return_value = {}
        mock_detail.raise_for_status = MagicMock()

        mock_mem = MagicMock()
        mock_mem.json.return_value = {"items": [make_member_item(1)]}
        mock_mem.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_list, mock_detail, mock_mem]

        build_committees.build_delta(self.db_path, self.prev_db, SCHEMA_PATH)

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

    def _make_checkpoint(self, committee_ids):
        """Create a checkpoint DB with cross-refs for the given committee IDs."""
        conn = schema_module.create_database_with_tables(
            self.checkpoint_db, SCHEMA_PATH, ["committees", "mp_committee_cross_ref"],
        )
        for cid in committee_ids:
            build_committees.insert_cross_refs(conn, cid, [1], 1700000000000)
        conn.commit()
        conn.close()

    def _committee_list_mock(self, *items):
        m = MagicMock()
        m.json.return_value = {"items": list(items)}
        m.raise_for_status = MagicMock()
        return m

    def _detail_mock(self):
        m = MagicMock()
        m.json.return_value = {}
        m.raise_for_status = MagicMock()
        return m

    def _members_mock(self, *mnis_ids):
        m = MagicMock()
        m.json.return_value = {
            "items": [make_member_item(i) for i in mnis_ids]
        }
        m.raise_for_status = MagicMock()
        return m

    @patch("build_committees.api_get")
    def test_committees_checkpoint_skips_processed_committees(self, mock_api_get):
        """Checkpoint has cross-refs for committee 100; its member fetch is skipped."""
        self._make_checkpoint([100])

        # List returns committees 100 and 200; member fetch only for 200
        mock_api_get.side_effect = [
            self._committee_list_mock(
                make_committee_item(100, "Comm A"),
                make_committee_item(200, "Comm B"),
            ),
            self._detail_mock(),          # detail for 100
            self._detail_mock(),          # detail for 200
            self._members_mock(2),        # members for 200 (100 skipped)
        ]

        build_committees.build_seed(
            self.db_path, SCHEMA_PATH,
            checkpoint_db=self.checkpoint_db,
        )

        # 4 api_get calls: 1 list + 2 details + 1 members (100's members skipped)
        self.assertEqual(mock_api_get.call_count, 4)

        c = sqlite3.connect(self.db_path)
        xref_count = c.execute(
            "SELECT COUNT(*) FROM mp_committee_cross_ref"
        ).fetchone()[0]
        self.assertEqual(xref_count, 2)  # 1 from checkpoint + 1 new
        c.close()

    @patch("build_committees.api_get")
    def test_seed_with_nonexistent_checkpoint_starts_fresh(self, mock_api_get):
        """Non-existent checkpoint path -> fresh seed."""
        mock_api_get.side_effect = [
            self._committee_list_mock(make_committee_item(100, "Comm A")),
            self._detail_mock(),
            self._members_mock(1),
        ]

        build_committees.build_seed(
            self.db_path, SCHEMA_PATH,
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
        mock_api_get.side_effect = [
            self._committee_list_mock(make_committee_item(100, "Comm A")),
            self._detail_mock(),
            self._members_mock(1),
        ]

        build_committees.build_seed(self.db_path, SCHEMA_PATH)

        c = sqlite3.connect(self.db_path)
        xref_count = c.execute(
            "SELECT COUNT(*) FROM mp_committee_cross_ref"
        ).fetchone()[0]
        self.assertEqual(xref_count, 1)
        c.close()

    @patch("build_committees.api_get")
    def test_checkpoint_same_as_output(self, mock_api_get):
        """--checkpoint-db and --output same path -> no truncation, resumes."""
        self._make_checkpoint([100])
        import shutil
        shutil.copy2(self.checkpoint_db, self.db_path)

        mock_api_get.side_effect = [
            self._committee_list_mock(
                make_committee_item(100, "Comm A"),
                make_committee_item(200, "Comm B"),
            ),
            self._detail_mock(),          # detail for 100
            self._detail_mock(),          # detail for 200
            self._members_mock(2),        # members for 200 (100 skipped)
        ]

        build_committees.build_seed(
            self.db_path, SCHEMA_PATH,
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
