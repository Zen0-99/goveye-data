"""Unit tests for build_recess.py — per-API recess dates build script.

Uses mocked api_get to avoid hitting the real Egg Timer API. Mocks the
HTML response and verifies parsing.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_recess

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "8.json",
)


# Sample HTML mimicking the Egg Timer API output format (HTML table rows)
SAMPLE_HTML = (
    "<html><body><table>\n"
    "<tr><th>Recess</th><th>Start date</th><th>End date</th></tr>\n"
    "<tr>\n\t<td>Christmas recess 2026</td>\n\t<td>Friday 18 December 2026</td>\n"
    "\t<td>Sunday 3 January 2027</td>\n</tr>\n"
    "<tr>\n\t<td>Easter recess 2027</td>\n\t<td>Wednesday 24 March 2027</td>\n"
    "\t<td>Tuesday 13 April 2027</td>\n</tr>\n"
    "</table></body></html>\n"
)

# Legacy │/|-delimited text format (fallback parser)
LEGACY_HTML = (
    "Some header line\n"
    "Christmas recess 2026 │Friday 18 December 2026 │Sunday 3 January 2027\n"
    "Easter recess 2027 |Wednesday 24 March 2027 |Tuesday 13 April 2027\n"
    "Some footer line\n"
)


class TestCreateRecessDb(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "recess.db")

    def test_create_recess_db(self):
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["recess_dates", "recess_dates_meta"],
        )
        c = sqlite3.connect(self.db_path)
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("recess_dates", tables)
        self.assertIn("recess_dates_meta", tables)
        self.assertIn("room_master_table", tables)
        self.assertNotIn("mps", tables)
        self.assertNotIn("bills", tables)
        c.close()
        conn.close()


class TestParseRecessDates(unittest.TestCase):
    """Test the HTML parsing logic (mirrors EggTimerApi.kt)."""

    def test_parse_basic(self):
        """Parse two recess rows from sample HTML."""
        results = build_recess.parse_recess_dates(SAMPLE_HTML)
        self.assertEqual(len(results), 2)
        desc0, start0, end0 = results[0]
        self.assertEqual(desc0, "Christmas recess 2026")
        self.assertEqual(start0, "2026-12-18")
        self.assertEqual(end0, "2027-01-03")

    def test_parse_pipe_delimiter(self):
        """Lines using | delimiter are parsed (legacy fallback format)."""
        results = build_recess.parse_recess_dates(LEGACY_HTML)
        # Easter row uses | delimiter
        desc1, start1, end1 = results[1]
        self.assertEqual(desc1, "Easter recess 2027")
        self.assertEqual(start1, "2027-03-24")
        self.assertEqual(end1, "2027-04-13")

    def test_parse_skips_non_recess_lines(self):
        """Rows without 'recess' in the description are skipped."""
        html = (
            "<table>"
            "<tr><td>Some other event</td><td>Friday 18 December 2026</td><td>Sunday 3 January 2027</td></tr>"
            "<tr><td>Summer recess 2027</td><td>Monday 20 July 2027</td><td>Tuesday 1 September 2027</td></tr>"
            "</table>"
        )
        results = build_recess.parse_recess_dates(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "Summer recess 2027")

    def test_parse_empty(self):
        """Empty HTML returns empty list."""
        self.assertEqual(build_recess.parse_recess_dates(""), [])

    def test_parse_date_formats(self):
        """parse_date handles full day/month names (the format the API returns)."""
        self.assertEqual(build_recess.parse_date("Friday 18 December 2026"), "2026-12-18")
        self.assertEqual(build_recess.parse_date("Wednesday 24 March 2027"), "2027-03-24")
        self.assertIsNone(build_recess.parse_date("not a date"))


class TestRecessInsertion(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "recess.db")

    @patch("build_recess.api_get")
    def test_recess_insertion(self, mock_api_get):
        """Seed build fetches HTML for both houses and inserts recess dates."""
        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_HTML
        mock_resp.raise_for_status = MagicMock()
        # Two houses → two API calls
        mock_api_get.side_effect = [mock_resp, mock_resp]

        build_recess.build_seed(self.db_path, SCHEMA_PATH)

        c = sqlite3.connect(self.db_path)
        # 2 recess rows per house × 2 houses = 4
        recess_count = c.execute("SELECT COUNT(*) FROM recess_dates").fetchone()[0]
        self.assertEqual(recess_count, 4)
        # 2 meta rows (one per house)
        meta_count = c.execute("SELECT COUNT(*) FROM recess_dates_meta").fetchone()[0]
        self.assertEqual(meta_count, 2)

        # Verify house values
        houses = sorted(
            r[0] for r in c.execute("SELECT DISTINCT house FROM recess_dates").fetchall()
        )
        self.assertEqual(houses, [1, 2])
        c.close()


class TestDeltaRecess(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.prev_db = os.path.join(self.tmpdir, "prev_recess.db")
        self.db_path = os.path.join(self.tmpdir, "recess.db")

    @patch("build_recess.api_get")
    def test_delta_replaces_old_data(self, mock_api_get):
        """Delta mode deletes old rows and inserts fresh data."""
        # Previous DB with 1 recess date
        conn = schema_module.create_database_with_tables(
            self.prev_db, SCHEMA_PATH, ["recess_dates", "recess_dates_meta"],
        )
        build_recess.insert_recess_dates(
            conn, 1, [("Old recess", "2025-01-01", "2025-01-10")], 1700000000000,
        )
        build_recess.update_recess_meta(conn, 1, 1700000000000)
        conn.close()

        # Delta: fresh HTML with 2 rows per house
        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_HTML
        mock_resp.raise_for_status = MagicMock()
        mock_api_get.side_effect = [mock_resp, mock_resp]

        build_recess.build_delta(self.db_path, self.prev_db, SCHEMA_PATH)

        c = sqlite3.connect(self.db_path)
        # Old row deleted; 4 new rows (2 per house)
        recess_count = c.execute("SELECT COUNT(*) FROM recess_dates").fetchone()[0]
        self.assertEqual(recess_count, 4)
        # No "Old recess" row remains
        old = c.execute(
            "SELECT COUNT(*) FROM recess_dates WHERE description='Old recess'"
        ).fetchone()[0]
        self.assertEqual(old, 0)
        c.close()


class TestRecessCheckpoint(unittest.TestCase):
    """Test checkpoint/resume for recess seed mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "recess.db")
        self.checkpoint_db = os.path.join(self.tmpdir, "checkpoint.db")

    def _make_checkpoint(self):
        """Create a checkpoint DB with old recess data."""
        conn = schema_module.create_database_with_tables(
            self.checkpoint_db, SCHEMA_PATH, ["recess_dates", "recess_dates_meta"],
        )
        build_recess.insert_recess_dates(
            conn, 1, [("Old recess", "2025-01-01", "2025-01-10")], 1700000000000,
        )
        build_recess.update_recess_meta(conn, 1, 1700000000000)
        conn.close()

    @patch("build_recess.api_get")
    def test_seed_with_checkpoint_refetches(self, mock_api_get):
        """Checkpoint DB has old data; seed clears and re-fetches fresh data."""
        self._make_checkpoint()

        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_HTML
        mock_resp.raise_for_status = MagicMock()
        mock_api_get.side_effect = [mock_resp, mock_resp]

        build_recess.build_seed(
            self.db_path, SCHEMA_PATH,
            checkpoint_db=self.checkpoint_db,
        )

        c = sqlite3.connect(self.db_path)
        # Old "Old recess" row should be gone; 4 new rows (2 per house)
        recess_count = c.execute("SELECT COUNT(*) FROM recess_dates").fetchone()[0]
        self.assertEqual(recess_count, 4)
        old = c.execute(
            "SELECT COUNT(*) FROM recess_dates WHERE description='Old recess'"
        ).fetchone()[0]
        self.assertEqual(old, 0)
        c.close()

    @patch("build_recess.api_get")
    def test_seed_with_nonexistent_checkpoint_starts_fresh(self, mock_api_get):
        """Non-existent checkpoint path -> fresh seed."""
        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_HTML
        mock_resp.raise_for_status = MagicMock()
        mock_api_get.side_effect = [mock_resp, mock_resp]

        build_recess.build_seed(
            self.db_path, SCHEMA_PATH,
            checkpoint_db=os.path.join(self.tmpdir, "nonexistent.db"),
        )

        c = sqlite3.connect(self.db_path)
        recess_count = c.execute("SELECT COUNT(*) FROM recess_dates").fetchone()[0]
        self.assertEqual(recess_count, 4)
        c.close()

    @patch("build_recess.api_get")
    def test_seed_without_checkpoint_starts_fresh(self, mock_api_get):
        """No checkpoint_db -> fresh seed (backward compatible)."""
        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_HTML
        mock_resp.raise_for_status = MagicMock()
        mock_api_get.side_effect = [mock_resp, mock_resp]

        build_recess.build_seed(self.db_path, SCHEMA_PATH)

        c = sqlite3.connect(self.db_path)
        recess_count = c.execute("SELECT COUNT(*) FROM recess_dates").fetchone()[0]
        self.assertEqual(recess_count, 4)
        c.close()

    @patch("build_recess.api_get")
    def test_checkpoint_same_as_output(self, mock_api_get):
        """--checkpoint-db and --output same path -> no truncation, re-fetches."""
        self._make_checkpoint()
        import shutil
        shutil.copy2(self.checkpoint_db, self.db_path)

        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_HTML
        mock_resp.raise_for_status = MagicMock()
        mock_api_get.side_effect = [mock_resp, mock_resp]

        build_recess.build_seed(
            self.db_path, SCHEMA_PATH,
            checkpoint_db=self.db_path,  # Same path
        )

        c = sqlite3.connect(self.db_path)
        recess_count = c.execute("SELECT COUNT(*) FROM recess_dates").fetchone()[0]
        self.assertEqual(recess_count, 4)
        old = c.execute(
            "SELECT COUNT(*) FROM recess_dates WHERE description='Old recess'"
        ).fetchone()[0]
        self.assertEqual(old, 0)
        c.close()


if __name__ == "__main__":
    unittest.main()
