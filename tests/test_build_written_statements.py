"""Unit tests for build_written_statements.py — per-API Written Statements build script.

Uses mocked api_get to avoid hitting the real Parliament API.
Tests field mapping, INSERT OR REPLACE, seed mode, and Pitfall 4 truncation handling.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock, call

# Add parent directory to path so we can import build_written_statements and schema
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_written_statements

# Path to the real schema JSON for integration-style tests
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "bundled_schema.json",
)


def make_statement(stmt_id, text="Short statement text.", house=1):
    """Build a minimal written statement fixture as returned by the bulk API."""
    return {
        "id": stmt_id,
        "memberId": 100 + stmt_id,
        "memberRole": "Secretary of State",
        "uin": f"HC {stmt_id}",
        "dateMade": "2026-08-20",
        "answeringBodyId": 7,
        "answeringBodyName": "Treasury",
        "title": f"Statement {stmt_id}",
        "text": text,
        "house": house,
    }


def make_api_response(statements):
    """Build a mock API response wrapping statement fixtures."""
    return {
        "totalResults": len(statements),
        "results": [{"value": s} for s in statements],
    }


class TestCreateWrittenStatementsDb(unittest.TestCase):
    """Test that build_written_statements creates a DB with only written_statements table."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "written_statements.db")

    def test_create_db(self):
        """create_database_with_tables creates only written_statements + room_master_table."""
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["written_statements"],
        )
        c = sqlite3.connect(self.db_path)
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("written_statements", tables)
        self.assertIn("room_master_table", tables)
        # No other data tables
        self.assertNotIn("mps", tables)
        self.assertNotIn("divisions", tables)
        c.close()
        conn.close()


class TestStatementInsertion(unittest.TestCase):
    """Test statement insertion and field mapping."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "written_statements.db")

    @patch("build_written_statements.api_get")
    def test_seed_inserts_statements(self, mock_api_get):
        """Seed build with 2 statements inserts them with correct field mapping."""
        stmts = [make_statement(1), make_statement(2, text="Another statement.", house=2)]
        mock_response = MagicMock()
        mock_response.json.return_value = make_api_response(stmts)
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_written_statements.build_seed(self.db_path, SCHEMA_PATH, days=90)

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM written_statements").fetchone()[0]
        self.assertEqual(count, 2)

        row = c.execute(
            "SELECT id, memberId, memberRole, uin, dateMade, answeringBodyId, "
            "answeringBodyName, title, text, house FROM written_statements WHERE id=1"
        ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 101)
        self.assertEqual(row[2], "Secretary of State")
        self.assertEqual(row[3], "HC 1")
        self.assertEqual(row[4], "2026-08-20")
        self.assertEqual(row[5], 7)
        self.assertEqual(row[6], "Treasury")
        self.assertEqual(row[7], "Statement 1")
        self.assertEqual(row[8], "Short statement text.")
        self.assertEqual(row[9], 1)
        c.close()

    @patch("build_written_statements.api_get")
    def test_insert_or_replace_upserts(self, mock_api_get):
        """INSERT OR REPLACE updates an existing statement's text."""
        # First seed with statement 1
        stmts = [make_statement(1, text="Original text")]
        mock_response = MagicMock()
        mock_response.json.return_value = make_api_response(stmts)
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_written_statements.build_seed(self.db_path, SCHEMA_PATH, days=90)

        # Delta with updated text for statement 1
        delta_path = os.path.join(self.tmpdir, "delta.db")
        stmts2 = [make_statement(1, text="Updated text")]
        mock_response2 = MagicMock()
        mock_response2.json.return_value = make_api_response(stmts2)
        mock_response2.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response2

        build_written_statements.build_delta(delta_path, self.db_path, SCHEMA_PATH, days=90)

        c = sqlite3.connect(delta_path)
        count = c.execute("SELECT COUNT(*) FROM written_statements").fetchone()[0]
        self.assertEqual(count, 1)
        text = c.execute("SELECT text FROM written_statements WHERE id=1").fetchone()[0]
        self.assertEqual(text, "Updated text")
        c.close()


class TestPitfall4Truncation(unittest.TestCase):
    """Test Pitfall 4: text truncated at 255 chars triggers individual endpoint fetch."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "written_statements.db")

    @patch("build_written_statements.api_get")
    def test_truncated_text_triggers_full_fetch(self, mock_api_get):
        """Statement with len(text)==255 triggers fetch_full_statement_text."""
        truncated_text = "A" * 255
        full_text = "A" * 255 + " and the rest of the full text."
        stmts = [make_statement(1, text=truncated_text), make_statement(2, text="Short text.")]
        bulk_response = MagicMock()
        bulk_response.json.return_value = make_api_response(stmts)
        bulk_response.raise_for_status = MagicMock()

        # Individual endpoint response for statement 1
        individual_response = MagicMock()
        individual_response.json.return_value = {
            "value": {"id": 1, "text": full_text}
        }
        individual_response.raise_for_status = MagicMock()

        mock_api_get.side_effect = [bulk_response, individual_response]

        build_written_statements.build_seed(self.db_path, SCHEMA_PATH, days=90)

        c = sqlite3.connect(self.db_path)
        text1 = c.execute("SELECT text FROM written_statements WHERE id=1").fetchone()[0]
        self.assertEqual(text1, full_text)
        text2 = c.execute("SELECT text FROM written_statements WHERE id=2").fetchone()[0]
        self.assertEqual(text2, "Short text.")
        c.close()

        # Verify api_get was called twice: once for bulk, once for individual
        self.assertEqual(mock_api_get.call_count, 2)

    @patch("build_written_statements.api_get")
    def test_non_truncated_text_does_not_trigger_fetch(self, mock_api_get):
        """Statement with len(text) < 255 does NOT trigger individual fetch."""
        stmts = [make_statement(1, text="Short text.")]
        mock_response = MagicMock()
        mock_response.json.return_value = make_api_response(stmts)
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_written_statements.build_seed(self.db_path, SCHEMA_PATH, days=90)

        # Only 1 call — the bulk API
        self.assertEqual(mock_api_get.call_count, 1)


class TestCheckpointResume(unittest.TestCase):
    """Test checkpoint/resume for written statements seed mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "written_statements.db")
        self.checkpoint_db = os.path.join(self.tmpdir, "checkpoint.db")

    @patch("build_written_statements.api_get")
    def test_seed_with_checkpoint_upserts(self, mock_api_get):
        """Checkpoint DB has statement 1; seed upserts statement 1 + inserts statement 2."""
        # Create checkpoint with statement 1
        conn = schema_module.create_database_with_tables(
            self.checkpoint_db, SCHEMA_PATH, ["written_statements"],
        )
        build_written_statements.insert_statements(
            conn, [make_statement(1)], 1700000000000,
        )
        conn.close()

        # API returns statements 1 and 2
        stmts = [make_statement(1, text="Updated text"), make_statement(2)]
        mock_response = MagicMock()
        mock_response.json.return_value = make_api_response(stmts)
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_written_statements.build_seed(
            self.db_path, SCHEMA_PATH, days=90,
            checkpoint_db=self.checkpoint_db,
        )

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM written_statements").fetchone()[0]
        self.assertEqual(count, 2)
        text = c.execute("SELECT text FROM written_statements WHERE id=1").fetchone()[0]
        self.assertEqual(text, "Updated text")
        c.close()

    @patch("build_written_statements.api_get")
    def test_seed_without_checkpoint_starts_fresh(self, mock_api_get):
        """No checkpoint_db -> fresh seed."""
        stmts = [make_statement(1)]
        mock_response = MagicMock()
        mock_response.json.return_value = make_api_response(stmts)
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_written_statements.build_seed(self.db_path, SCHEMA_PATH, days=90)

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM written_statements").fetchone()[0]
        self.assertEqual(count, 1)
        c.close()


class TestMapStatementToEntity(unittest.TestCase):
    """Test the map_statement_to_entity function directly."""

    def test_full_mapping(self):
        stmt = make_statement(42, text="Some text.", house=2)
        row = build_written_statements.map_statement_to_entity(stmt, 1700000000000)
        self.assertEqual(row[0], 42)       # id
        self.assertEqual(row[1], 142)      # memberId
        self.assertEqual(row[2], "Secretary of State")  # memberRole
        self.assertEqual(row[3], "HC 42")  # uin
        self.assertEqual(row[4], "2026-08-20")  # dateMade
        self.assertEqual(row[5], 7)        # answeringBodyId
        self.assertEqual(row[6], "Treasury")  # answeringBodyName
        self.assertEqual(row[7], "Statement 42")  # title
        self.assertEqual(row[8], "Some text.")  # text
        self.assertEqual(row[9], 2)        # house
        self.assertEqual(row[10], 1700000000000)  # lastUpdated

    def test_missing_fields_use_defaults(self):
        stmt = {"id": 1}
        row = build_written_statements.map_statement_to_entity(stmt, 1700000000000)
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 0)        # memberId default
        self.assertEqual(row[2], "")       # memberRole default
        self.assertEqual(row[9], 1)        # house default


if __name__ == "__main__":
    unittest.main()
