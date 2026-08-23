"""Unit tests for build_gov_publications.py — per-API GOV.UK Publications build script.

Uses mocked api_get to avoid hitting the real GOV.UK APIs.
Tests field mapping, HTML stripping (Pitfall 6), D-03 body discard, image_url extraction.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add parent directory to path so we can import build_gov_publications and schema
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_gov_publications

# Path to the real schema JSON for integration-style tests
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "bundled_schema.json",
)


def make_search_result(link, title, doc_type="news_article", org_slug="hm-treasury"):
    """Build a minimal GOV.UK Search API result item."""
    return {
        "link": link,
        "title": title,
        "description": f"Description for {title}",
        "public_timestamp": "2026-08-20T10:00:00.000Z",
        "document_type": doc_type,
        "organisations": [{"title": "HM Treasury", "slug": org_slug}],
    }


def make_search_response(results):
    """Build a mock GOV.UK Search API response."""
    return {
        "results": results,
        "total": len(results),
    }


def make_content_response(path, title, body_html="<p>Body text about <strong>NHS</strong> funding</p>", image=None, doc_type="news_article"):
    """Build a mock GOV.UK Content API response."""
    return {
        "title": title,
        "description": f"Description for {title}",
        "document_type": doc_type,
        "first_published_at": "2026-08-20T10:00:00.000Z",
        "public_updated_at": "2026-08-20T12:00:00.000Z",
        "base_path": path,
        "details": {
            "body": body_html,
            "image": image,
        },
        "links": {
            "primary_publishing_organisation": [{"title": "HM Treasury", "slug": "hm-treasury"}],
            "organisations": [{"title": "HM Treasury", "slug": "hm-treasury"}],
        },
    }


def make_org_response(slugs):
    """Build a mock GOV.UK Search API aggregation response for organisations."""
    return {
        "aggregates": {
            "organisations": {
                "options": [
                    {"value": {"slug": slug, "title": slug.replace("-", " ").title()}}
                    for slug in slugs
                ]
            }
        }
    }


class TestCreateGovPublicationsDb(unittest.TestCase):
    """Test that build_gov_publications creates a DB with only government_publications table."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "gov_publications.db")

    def test_create_db(self):
        """create_database_with_tables creates only government_publications + room_master_table."""
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["government_publications"],
        )
        c = sqlite3.connect(self.db_path)
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("government_publications", tables)
        self.assertIn("room_master_table", tables)
        self.assertNotIn("mps", tables)
        self.assertNotIn("divisions", tables)
        c.close()
        conn.close()


class TestStripHtml(unittest.TestCase):
    """Test strip_html_for_tag_matching (Pitfall 6)."""

    def test_strips_html_tags(self):
        html = "<p>Body text about <strong>NHS</strong> funding</p>"
        result = build_gov_publications.strip_html_for_tag_matching(html)
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)
        self.assertIn("NHS", result)
        self.assertIn("funding", result)

    def test_empty_input(self):
        self.assertEqual(build_gov_publications.strip_html_for_tag_matching(""), "")
        self.assertEqual(build_gov_publications.strip_html_for_tag_matching(None), "")

    def test_complex_html(self):
        html = """
        <div class="govspeak">
            <h2>Section Title</h2>
            <p>The government announced new <a href="/policy">taxation</a> policies.</p>
            <ul><li>Point 1</li><li>Point 2</li></ul>
        </div>
        """
        result = build_gov_publications.strip_html_for_tag_matching(html)
        self.assertIn("Section Title", result)
        self.assertIn("taxation", result)
        self.assertIn("Point 1", result)
        self.assertNotIn("<div", result)
        self.assertNotIn("href", result)


class TestMapPublicationToEntity(unittest.TestCase):
    """Test map_publication_to_entity field mapping and D-03 body discard."""

    def test_full_mapping_with_content(self):
        search_item = make_search_result("/government/news/budget", "Budget 2026")
        content = make_content_response(
            "/government/news/budget", "Budget 2026",
            body_html="<p>Budget details</p>",
            image="https://assets.publishing.service.gov.uk/budget.jpg",
        )
        row = build_gov_publications.map_publication_to_entity(search_item, content, 1700000000000, 1)

        self.assertEqual(row[0], 1)  # id
        self.assertEqual(row[1], "Budget 2026")  # title
        self.assertEqual(row[2], "Description for Budget 2026")  # summary
        self.assertEqual(row[3], "https://www.gov.uk/government/news/budget")  # url
        self.assertEqual(row[4], "news_article")  # documentType
        self.assertEqual(row[5], "HM Treasury")  # organisation
        self.assertEqual(row[6], "hm-treasury")  # organisationSlug
        self.assertEqual(row[7], "2026-08-20T10:00:00.000Z")  # firstPublishedAt
        self.assertEqual(row[8], "2026-08-20T12:00:00.000Z")  # publicUpdatedAt
        self.assertEqual(row[9], "https://assets.publishing.service.gov.uk/budget.jpg")  # imageUrl
        self.assertEqual(row[10], 1700000000000)  # lastUpdated

    def test_entity_tuple_does_not_contain_body(self):
        """D-03: entity tuple must NOT contain body text field."""
        search_item = make_search_result("/government/news/test", "Test")
        content = make_content_response("/government/news/test", "Test", body_html="<p>Body text</p>")
        row = build_gov_publications.map_publication_to_entity(search_item, content, 1700000000000, 1)
        # Entity tuple has 11 fields (matching GovernmentPublicationEntity)
        self.assertEqual(len(row), 11)
        # None of the fields should be the raw body text
        self.assertNotIn("Body text", row)

    def test_fallback_to_search_item_when_content_none(self):
        search_item = make_search_result("/government/news/fallback", "Fallback Title")
        row = build_gov_publications.map_publication_to_entity(search_item, None, 1700000000000, 5)

        self.assertEqual(row[0], 5)
        self.assertEqual(row[1], "Fallback Title")
        self.assertEqual(row[3], "https://www.gov.uk/government/news/fallback")
        self.assertIsNone(row[9])  # imageUrl — None when no content details

    def test_image_url_extraction(self):
        """D-10: image_url from Content API details.image field."""
        search_item = make_search_result("/government/news/with-image", "With Image")
        content = make_content_response(
            "/government/news/with-image", "With Image",
            image="https://assets.publishing.service.gov.uk/image.jpg",
        )
        row = build_gov_publications.map_publication_to_entity(search_item, content, 1700000000000, 1)
        self.assertEqual(row[9], "https://assets.publishing.service.gov.uk/image.jpg")

    def test_image_url_null_when_no_image(self):
        search_item = make_search_result("/government/news/no-image", "No Image")
        content = make_content_response("/government/news/no-image", "No Image", image=None)
        row = build_gov_publications.map_publication_to_entity(search_item, content, 1700000000000, 1)
        self.assertIsNone(row[9])


class TestSeedBuild(unittest.TestCase):
    """Test seed build with mocked APIs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "gov_publications.db")

    @patch("build_gov_publications.api_get")
    def test_seed_inserts_publications(self, mock_api_get):
        """Seed build fetches orgs, publications, and content — inserts into DB."""
        org_response = MagicMock()
        org_response.json.return_value = make_org_response(["hm-treasury"])
        org_response.raise_for_status = MagicMock()

        search_response = MagicMock()
        search_response.json.return_value = make_search_response([
            make_search_result("/government/news/budget", "Budget 2026", "news_article"),
            make_search_result("/government/news/tax", "Tax Update", "press_release"),
        ])
        search_response.raise_for_status = MagicMock()

        content1 = MagicMock()
        content1.json.return_value = make_content_response(
            "/government/news/budget", "Budget 2026",
            body_html="<p>Budget details about taxation</p>",
            image="https://example.com/budget.jpg",
        )
        content1.raise_for_status = MagicMock()

        content2 = MagicMock()
        content2.json.return_value = make_content_response(
            "/government/news/tax", "Tax Update",
            body_html="<p>Tax policy changes</p>",
        )
        content2.raise_for_status = MagicMock()

        mock_api_get.side_effect = [org_response, search_response, content1, content2]

        build_gov_publications.build_seed(self.db_path, SCHEMA_PATH, days=90)

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM government_publications").fetchone()[0]
        self.assertEqual(count, 2)

        # Verify D-03: body text NOT in government_publications table
        cols = [desc[1] for desc in c.execute("SELECT * FROM government_publications LIMIT 0").description]
        self.assertNotIn("body", cols)

        # Verify body text IS in _publication_bodies temp table
        body_count = c.execute("SELECT COUNT(*) FROM _publication_bodies").fetchone()[0]
        self.assertEqual(body_count, 2)

        # Verify image_url extraction
        row = c.execute(
            "SELECT imageUrl FROM government_publications WHERE title='Budget 2026'"
        ).fetchone()
        self.assertEqual(row[0], "https://example.com/budget.jpg")
        c.close()

    @patch("build_gov_publications.api_get")
    def test_seed_different_document_types(self, mock_api_get):
        """D-01: all document types captured (no filtering)."""
        org_response = MagicMock()
        org_response.json.return_value = make_org_response(["hm-treasury"])
        org_response.raise_for_status = MagicMock()

        search_response = MagicMock()
        search_response.json.return_value = make_search_response([
            make_search_result("/gov/news", "News", "news_article"),
            make_search_result("/gov/speech", "Speech", "speech"),
            make_search_result("/gov/guidance", "Guidance", "guidance"),
        ])
        search_response.raise_for_status = MagicMock()

        content_responses = []
        for path, title, doc_type in [("/gov/news", "News", "news_article"), ("/gov/speech", "Speech", "speech"), ("/gov/guidance", "Guidance", "guidance")]:
            resp = MagicMock()
            resp.json.return_value = make_content_response(path, title, doc_type=doc_type)
            resp.raise_for_status = MagicMock()
            content_responses.append(resp)

        mock_api_get.side_effect = [org_response, search_response] + content_responses

        build_gov_publications.build_seed(self.db_path, SCHEMA_PATH, days=90)

        c = sqlite3.connect(self.db_path)
        types = [r[0] for r in c.execute("SELECT documentType FROM government_publications").fetchall()]
        self.assertIn("news_article", types)
        self.assertIn("speech", types)
        self.assertIn("guidance", types)
        c.close()


if __name__ == "__main__":
    unittest.main()
