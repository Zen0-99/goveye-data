"""Unit tests for build_legislation.py — per-API legislation.gov.uk build script.

Uses mocked api_get to avoid hitting the real legislation.gov.uk API.
Tests XML parsing, field extraction, pagination via next link, and Pitfall 5.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add parent directory to path so we can import build_legislation and schema
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_legislation

# Path to the real schema JSON for integration-style tests
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "bundled_schema.json",
)

# Atom XML namespace constants
ATOM_NS = "http://www.w3.org/2005/Atom"
LEG_NS = "http://www.legislation.gov.uk/namespaces/metadata"


def make_atom_feed(entries, next_link=None):
    """Build a mock Atom feed XML string.

    Args:
        entries: List of dicts with id, title, type, year, number, date, updated, published.
        next_link: URL for the next page (or None if no more pages).
    """
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(f'<feed xmlns="{ATOM_NS}" xmlns:leg="{LEG_NS}">')
    lines.append(f'<id>https://www.legislation.gov.uk/new/data.feed</id>')
    lines.append('<title>New Legislation</title>')
    lines.append('<updated>2026-08-20T10:00:00Z</updated>')

    if next_link:
        lines.append(f'<link rel="next" href="{next_link}"/>')

    for e in entries:
        lines.append('<entry>')
        lines.append(f'<id>{e.get("id", "")}</id>')
        lines.append(f'<title>{e.get("title", "")}</title>')
        lines.append(f'<updated>{e.get("updated", "2026-08-20T10:00:00Z")}</updated>')
        lines.append(f'<published>{e.get("published", "2026-08-20T10:00:00Z")}</published>')
        if e.get("type"):
            lines.append(f'<leg:type>{e["type"]}</leg:type>')
        if e.get("year"):
            lines.append(f'<leg:year>{e["year"]}</leg:year>')
        if e.get("number"):
            lines.append(f'<leg:number>{e["number"]}</leg:number>')
        if e.get("date"):
            lines.append(f'<leg:date>{e["date"]}</leg:date>')
        lines.append('</entry>')

    lines.append('</feed>')
    return "\n".join(lines)


def make_legislation_entry(entry_id, title, leg_type="uksi", year=2026, number=500, date="2026-08-20"):
    """Build a minimal legislation entry dict."""
    return {
        "id": entry_id,
        "title": title,
        "type": leg_type,
        "year": year,
        "number": number,
        "date": date,
        "updated": "2026-08-20T10:00:00Z",
        "published": "2026-08-20T10:00:00Z",
    }


class TestCreateLegislationDb(unittest.TestCase):
    """Test that build_legislation creates a DB with only legislation table."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "legislation.db")

    def test_create_db(self):
        """create_database_with_tables creates only legislation + room_master_table."""
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["legislation"],
        )
        c = sqlite3.connect(self.db_path)
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("legislation", tables)
        self.assertIn("room_master_table", tables)
        self.assertNotIn("mps", tables)
        self.assertNotIn("divisions", tables)
        c.close()
        conn.close()


class TestFetchNewLegislation(unittest.TestCase):
    """Test fetch_new_legislation XML parsing and pagination."""

    @patch("build_legislation.api_get")
    def test_single_page(self, mock_api_get):
        """Fetch a single page with 2 entries."""
        entries = [
            make_legislation_entry("http://www.legislation.gov.uk/uksi/2026/500", "SI 500", "uksi"),
            make_legislation_entry("http://www.legislation.gov.uk/ukpga/2026/10", "Act 10", "ukpga"),
        ]
        mock_response = MagicMock()
        mock_response.text = make_atom_feed(entries, next_link=None)
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        result = build_legislation.fetch_new_legislation(max_pages=5)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "http://www.legislation.gov.uk/uksi/2026/500")
        self.assertEqual(result[0]["title"], "SI 500")
        self.assertEqual(result[0]["type"], "uksi")
        self.assertEqual(result[0]["year"], 2026)
        self.assertEqual(result[0]["number"], 500)
        self.assertEqual(result[0]["url"], "http://www.legislation.gov.uk/uksi/2026/500")
        self.assertEqual(result[1]["type"], "ukpga")
        # Only 1 API call (no next link)
        self.assertEqual(mock_api_get.call_count, 1)

    @patch("build_legislation.api_get")
    def test_pagination_via_next_link(self, mock_api_get):
        """Follow atom:link rel='next' for pagination."""
        page1_entries = [make_legislation_entry("http://leg/1", "Entry 1")]
        page2_entries = [make_legislation_entry("http://leg/2", "Entry 2")]

        response1 = MagicMock()
        response1.text = make_atom_feed(page1_entries, next_link="https://www.legislation.gov.uk/new/data.feed?page=2")
        response1.raise_for_status = MagicMock()

        response2 = MagicMock()
        response2.text = make_atom_feed(page2_entries, next_link=None)
        response2.raise_for_status = MagicMock()

        mock_api_get.side_effect = [response1, response2]

        result = build_legislation.fetch_new_legislation(max_pages=5)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "http://leg/1")
        self.assertEqual(result[1]["id"], "http://leg/2")
        self.assertEqual(mock_api_get.call_count, 2)

    @patch("build_legislation.api_get")
    def test_max_pages_limit(self, mock_api_get):
        """Respect max_pages limit even if next links exist."""
        entries = [make_legislation_entry("http://leg/1", "Entry 1")]
        response = MagicMock()
        response.text = make_atom_feed(entries, next_link="https://www.legislation.gov.uk/new/data.feed?page=2")
        response.raise_for_status = MagicMock()
        mock_api_get.return_value = response

        result = build_legislation.fetch_new_legislation(max_pages=2)

        self.assertEqual(mock_api_get.call_count, 2)  # Exactly max_pages calls


class TestMapLegislationToEntity(unittest.TestCase):
    """Test map_legislation_to_entity field mapping."""

    def test_full_mapping(self):
        entry = make_legislation_entry(
            "http://www.legislation.gov.uk/uksi/2026/500", "SI 500",
            leg_type="uksi", year=2026, number=500, date="2026-08-20",
        )
        row = build_legislation.map_legislation_to_entity(entry, 1700000000000, 1)

        self.assertEqual(row[0], 1)  # id (sequential)
        self.assertEqual(row[1], "SI 500")  # title
        self.assertEqual(row[2], "uksi")  # type
        self.assertEqual(row[3], 2026)  # year
        self.assertEqual(row[4], 500)  # number
        self.assertEqual(row[5], "2026-08-20")  # date
        self.assertEqual(row[6], "http://www.legislation.gov.uk/uksi/2026/500")  # url
        self.assertEqual(row[7], 1700000000000)  # lastUpdated

    def test_missing_fields_use_defaults(self):
        entry = {"id": "http://leg/1", "title": "Test"}
        row = build_legislation.map_legislation_to_entity(entry, 1700000000000, 5)

        self.assertEqual(row[0], 5)
        self.assertEqual(row[1], "Test")
        self.assertEqual(row[2], "")  # type default
        self.assertEqual(row[3], 0)   # year default
        self.assertEqual(row[4], 0)   # number default
        self.assertEqual(row[5], "")  # date default
        self.assertEqual(row[6], "http://leg/1")  # url = id


class TestSeedBuild(unittest.TestCase):
    """Test seed build with mocked API."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "legislation.db")

    @patch("build_legislation.api_get")
    def test_seed_inserts_legislation(self, mock_api_get):
        """Seed build fetches legislation and inserts into DB."""
        entries = [
            make_legislation_entry("http://leg/1", "SI 100", "uksi", 2026, 100),
            make_legislation_entry("http://leg/2", "Act 5", "ukpga", 2026, 5),
        ]
        mock_response = MagicMock()
        mock_response.text = make_atom_feed(entries, next_link=None)
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        build_legislation.build_seed(self.db_path, SCHEMA_PATH, max_pages=5)

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM legislation").fetchone()[0]
        self.assertEqual(count, 2)

        row = c.execute(
            "SELECT title, type, year, number, url FROM legislation WHERE id=1"
        ).fetchone()
        self.assertEqual(row[0], "SI 100")
        self.assertEqual(row[1], "uksi")
        self.assertEqual(row[2], 2026)
        self.assertEqual(row[3], 100)
        self.assertEqual(row[4], "http://leg/1")
        c.close()

    @patch("build_legislation.api_get")
    def test_delta_upserts(self, mock_api_get):
        """Delta mode copies previous DB and upserts new entries."""
        # Create previous DB with 1 entry
        prev_path = os.path.join(self.tmpdir, "prev.db")
        conn = schema_module.create_database_with_tables(
            prev_path, SCHEMA_PATH, ["legislation"],
        )
        build_legislation.insert_legislation(
            conn, [make_legislation_entry("http://leg/1", "Old Entry")], 1700000000000,
        )
        conn.close()

        # Delta with 2 entries (old + new)
        entries = [
            make_legislation_entry("http://leg/1", "Updated Entry"),
            make_legislation_entry("http://leg/2", "New Entry"),
        ]
        mock_response = MagicMock()
        mock_response.text = make_atom_feed(entries, next_link=None)
        mock_response.raise_for_status = MagicMock()
        mock_api_get.return_value = mock_response

        delta_path = os.path.join(self.tmpdir, "delta.db")
        build_legislation.build_delta(delta_path, prev_path, SCHEMA_PATH, max_pages=5)

        c = sqlite3.connect(delta_path)
        count = c.execute("SELECT COUNT(*) FROM legislation").fetchone()[0]
        self.assertEqual(count, 2)
        c.close()


if __name__ == "__main__":
    unittest.main()
