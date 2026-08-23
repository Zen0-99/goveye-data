"""Unit tests for build_mp_tags.py — MP tag aggregation with recency weighting.

Tests:
- build_tags.py can be imported and TAG_DICTIONARY has 26 entries
- Recency weighting: recent speech has higher hitCount than old speech for same tag
- memberId=0 speeches are skipped
- isIntervention=1 speeches are skipped
- mp_tags table is populated with correct composite PK
"""

import datetime
import math
import os
import sqlite3
import sys
import tempfile
import unittest

# Add parent directory to path so we can import build_mp_tags and build_tags
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_tags
import build_mp_tags


def create_test_db(db_path):
    """Create a temp SQLite DB with debate_speeches + divisions tables and mp_tags."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS divisions (
            id INTEGER PRIMARY KEY,
            title TEXT,
            date TEXT,
            house INTEGER
        );
        CREATE TABLE IF NOT EXISTS debate_speeches (
            debateGid TEXT,
            speechGid TEXT,
            divisionId INTEGER,
            speakerName TEXT,
            memberId INTEGER,
            twfyPersonId INTEGER,
            speakerPosition TEXT,
            speechText TEXT,
            speechOrder INTEGER,
            isIntervention INTEGER,
            lastUpdated INTEGER,
            PRIMARY KEY(debateGid, speechGid)
        );
    """)
    build_mp_tags.create_mp_tags_table(conn)
    conn.commit()
    return conn


class TestBuildTagsDictionary(unittest.TestCase):
    """Verify build_tags.py is importable and TAG_DICTIONARY is intact."""

    def test_tag_dictionary_has_26_entries(self):
        """TAG_DICTIONARY must have exactly 26 tags (unchanged — no new tags added)."""
        self.assertEqual(len(build_tags.TAG_DICTIONARY), 26)

    def test_build_publication_tags_exists(self):
        """build_publication_tags function must exist."""
        self.assertTrue(callable(getattr(build_tags, "build_publication_tags", None)))

    def test_build_statement_tags_exists(self):
        """build_statement_tags function must exist."""
        self.assertTrue(callable(getattr(build_tags, "build_statement_tags", None)))

    def test_build_legislation_tags_exists(self):
        """build_legislation_tags function must exist."""
        self.assertTrue(callable(getattr(build_tags, "build_legislation_tags", None)))

    def test_publication_tag_threshold_exists(self):
        """PUBLICATION_TAG_THRESHOLD constant must exist."""
        self.assertTrue(hasattr(build_tags, "PUBLICATION_TAG_THRESHOLD"))

    def test_statement_tag_threshold_exists(self):
        """STATEMENT_TAG_THRESHOLD constant must exist."""
        self.assertTrue(hasattr(build_tags, "STATEMENT_TAG_THRESHOLD"))

    def test_legislation_tag_threshold_exists(self):
        """LEGISLATION_TAG_THRESHOLD constant must exist."""
        self.assertTrue(hasattr(build_tags, "LEGISLATION_TAG_THRESHOLD"))


class TestParseDate(unittest.TestCase):
    """Test parse_date handles various ISO date formats."""

    def test_parse_date_only(self):
        d = build_mp_tags.parse_date("2026-01-15")
        self.assertEqual(d, datetime.date(2026, 1, 15))

    def test_parse_datetime(self):
        d = build_mp_tags.parse_date("2026-01-15T10:30:00")
        self.assertEqual(d, datetime.date(2026, 1, 15))

    def test_parse_none(self):
        self.assertIsNone(build_mp_tags.parse_date(None))

    def test_parse_empty(self):
        self.assertIsNone(build_mp_tags.parse_date(""))

    def test_parse_invalid(self):
        self.assertIsNone(build_mp_tags.parse_date("not-a-date"))


class TestBuildMpTags(unittest.TestCase):
    """Test build_mp_tags recency weighting and filtering logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_mp_tags.db")
        self.conn = create_test_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_division(self, div_id, date_str, title="Test Division"):
        self.conn.execute(
            "INSERT OR REPLACE INTO divisions (id, title, date, house) VALUES (?, ?, ?, ?)",
            (div_id, title, date_str, 1),
        )
        self.conn.commit()

    def _insert_speech(self, gid, div_id, member_id, text, is_intervention=0):
        self.conn.execute(
            """INSERT OR REPLACE INTO debate_speeches
               (debateGid, speechGid, divisionId, speakerName, memberId,
                twfyPersonId, speakerPosition, speechText, speechOrder,
                isIntervention, lastUpdated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"debate-{gid}", f"speech-{gid}", div_id, "Test MP", member_id,
             0, "", text, 1, is_intervention, 0),
        )
        self.conn.commit()

    def test_mp_tags_populated(self):
        """mp_tags table has entries after build_mp_tags runs."""
        # Recent division with NHS content
        recent_date = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        self._insert_division(1, recent_date)
        self._insert_speech("s1", 1, 100, "The NHS needs more funding for hospitals and GP services.")

        scores = build_mp_tags.build_mp_tags(self.conn)
        self.assertIn(100, scores)
        self.assertTrue(len(scores[100]) > 0)

        build_mp_tags.populate_mp_tags(self.conn, scores)

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM mp_tags WHERE memberId = 100")
        self.assertGreater(cursor.fetchone()[0], 0)

    def test_recency_weighting(self):
        """Recent speech has higher hitCount than old speech for the same tag (D-08)."""
        # MP 1: recent speech about NHS
        recent_date = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        self._insert_division(1, recent_date)
        self._insert_speech("s1", 1, 200, "The NHS needs more funding for hospitals and GP services.")

        # MP 2: old speech about NHS (same content, different member)
        old_date = (datetime.date.today() - datetime.timedelta(days=700)).isoformat()
        self._insert_division(2, old_date)
        self._insert_speech("s2", 2, 201, "The NHS needs more funding for hospitals and GP services.")

        scores = build_mp_tags.build_mp_tags(self.conn)
        build_mp_tags.populate_mp_tags(self.conn, scores)

        cursor = self.conn.cursor()
        # Find the NHS tag for both MPs
        cursor.execute("SELECT hitCount FROM mp_tags WHERE memberId = 200 AND tag = 'NHS'")
        recent_row = cursor.fetchone()
        cursor.execute("SELECT hitCount FROM mp_tags WHERE memberId = 201 AND tag = 'NHS'")
        old_row = cursor.fetchone()

        self.assertIsNotNone(recent_row, "Recent MP should have an NHS tag")
        self.assertIsNotNone(old_row, "Old MP should have an NHS tag")
        self.assertGreater(recent_row[0], old_row[0],
                           "Recent speech hitCount must be higher than old speech (D-08 recency weighting)")

    def test_member_id_zero_skipped(self):
        """Speeches with memberId=0 are skipped (unmatched speeches)."""
        recent_date = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
        self._insert_division(1, recent_date)
        self._insert_speech("s1", 1, 0, "The NHS needs more funding for hospitals.")

        scores = build_mp_tags.build_mp_tags(self.conn)
        self.assertNotIn(0, scores, "memberId=0 speeches must be skipped")

    def test_intervention_speeches_skipped(self):
        """Intervention speeches (isIntervention=1) are skipped."""
        recent_date = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
        self._insert_division(1, recent_date)
        self._insert_speech("s1", 1, 300, "The NHS needs more funding.", is_intervention=1)

        scores = build_mp_tags.build_mp_tags(self.conn)
        self.assertNotIn(300, scores, "Intervention speeches must be skipped")

    def test_composite_primary_key(self):
        """mp_tags table has composite PK (memberId, tag) — no duplicate rows."""
        recent_date = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
        self._insert_division(1, recent_date)
        # Two speeches for same MP in same division — tags should aggregate, not duplicate
        self._insert_speech("s1", 1, 400, "The NHS needs more funding for hospitals.")
        self._insert_speech("s2", 1, 400, "More NHS hospital funding is needed.")

        scores = build_mp_tags.build_mp_tags(self.conn)
        build_mp_tags.populate_mp_tags(self.conn, scores)

        cursor = self.conn.cursor()
        cursor.execute("SELECT memberId, tag, COUNT(*) FROM mp_tags GROUP BY memberId, tag HAVING COUNT(*) > 1")
        duplicates = cursor.fetchall()
        self.assertEqual(len(duplicates), 0, "No duplicate (memberId, tag) rows should exist")


class TestBuildMpTagsMain(unittest.TestCase):
    """Test main() argument parsing."""

    def test_main_requires_output(self):
        """main() must have --output required argument."""
        import inspect
        sig = inspect.signature(build_mp_tags.main)
        # main() uses argparse internally, so we check the source contains --output
        source = inspect.getsource(build_mp_tags.main)
        self.assertIn("--output", source)
        self.assertIn("required=True", source)


if __name__ == "__main__":
    unittest.main()
