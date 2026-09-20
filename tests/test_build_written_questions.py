"""Unit tests for build_written_questions.py full-text detection.

Covers the _wq_fulltext_verified marker: the bulk API truncates
questionText at exactly 255 chars / answerText at exactly 258, but some
questions are *genuinely* that length. Once the detail endpoint confirms
a boundary-length row is full, it must not be re-fetched.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_written_questions as wq

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "bundled_schema.json",
)

TS = 1700000000000


def make_question(qid, qtext_len=255, atext=""):
    return {
        "id": qid,
        "askingMemberId": 1,
        "uin": f"UIN{qid}",
        "dateTabled": "2026-01-01",
        "questionText": "Q" * qtext_len,
        "answerText": atext,
    }


class TestFullTextVerified(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "written_questions.db")
        schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["written_questions"],
        )
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()

    def _insert(self, questions):
        wq.insert_questions(self.conn, questions, TS)
        self.conn.commit()

    def _candidate_ids(self):
        """Reproduce the _fetch_full_text_batch candidate query."""
        wq._ensure_verified_table(self.conn)
        return [
            r[0] for r in self.conn.execute(
                "SELECT id FROM written_questions "
                "WHERE (length(questionText) = 255 OR length(answerText) = 258) "
                f"AND id NOT IN (SELECT id FROM {wq.VERIFIED_TABLE}) "
                "ORDER BY id"
            ).fetchall()
        ]

    @patch("build_written_questions.fetch_full_question_text")
    def test_genuine_255_marked_and_not_refetched(self, mock_fetch):
        """A 255-char question the detail endpoint confirms is skipped next run."""
        self._insert([make_question(1, qtext_len=255)])
        mock_fetch.return_value = ("Q" * 255, "")  # detail returns same length

        wq._fetch_full_text_batch(self.conn, workers=1)
        self.assertEqual(mock_fetch.call_count, 1)

        # Second batch: nothing to fetch
        mock_fetch.reset_mock()
        wq._fetch_full_text_batch(self.conn, workers=1)
        self.assertEqual(mock_fetch.call_count, 0)

    @patch("build_written_questions.fetch_full_question_text")
    def test_failed_fetch_not_marked(self, mock_fetch):
        """A failed detail fetch stays in the candidate set."""
        self._insert([make_question(1, qtext_len=255)])
        mock_fetch.return_value = ("", "")

        wq._fetch_full_text_batch(self.conn, workers=1)
        self.assertEqual(self._candidate_ids(), [1])

    @patch("build_written_questions.fetch_full_question_text")
    def test_full_text_grows_row_out_of_candidates(self, mock_fetch):
        """Detail returns longer text -> row no longer matches the detector."""
        self._insert([make_question(1, qtext_len=255)])
        mock_fetch.return_value = ("Q" * 400, "")

        wq._fetch_full_text_batch(self.conn, workers=1)
        self.assertEqual(self._candidate_ids(), [])
        self.assertEqual(
            self.conn.execute(
                "SELECT length(questionText) FROM written_questions WHERE id=1"
            ).fetchone()[0],
            400,
        )

    def test_marker_cleared_when_upstream_text_changes(self):
        """Verified marker is invalidated when an upsert brings different text."""
        self._insert([make_question(1, qtext_len=255)])
        wq._mark_verified(self.conn, 1, TS)
        self.conn.commit()

        # Re-upsert identical text: marker survives
        self._insert([make_question(1, qtext_len=255)])
        self.assertEqual(self._candidate_ids(), [])

        # Upstream edit: different text at the boundary length -> marker cleared
        self._insert([make_question(1, qtext_len=255) | {"questionText": "Z" * 255}])
        self.assertEqual(self._candidate_ids(), [1])

    @patch("build_written_questions.fetch_full_question_text")
    def test_answer_text_boundary_detected(self, mock_fetch):
        """answerText at exactly 258 chars is a candidate."""
        self._insert([make_question(1, qtext_len=10, atext="A" * 258)])
        self.assertEqual(self._candidate_ids(), [1])

        mock_fetch.return_value = ("Q" * 10, "A" * 500)
        wq._fetch_full_text_batch(self.conn, workers=1)
        self.assertEqual(self._candidate_ids(), [])
        self.assertEqual(
            self.conn.execute(
                "SELECT length(answerText) FROM written_questions WHERE id=1"
            ).fetchone()[0],
            500,
        )


if __name__ == "__main__":
    unittest.main()
