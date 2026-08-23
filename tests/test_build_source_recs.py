"""Unit tests for build_source_recs.py — hybrid tag→department recommendation mapping.

Tests:
- Hardcoded departments have isRecommended=1
- Data-driven departments with high hit counts have isRecommended=1
- Data-driven departments with low hit counts have isRecommended=0
- TAG_TO_DEPARTMENTS has entries for all 26 tags
- source_recommendations table is populated
"""

import os
import sqlite3
import sys
import tempfile
import unittest

# Add parent directory to path so we can import build_source_recs
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_source_recs
import build_tags


def create_test_db(db_path):
    """Create a temp SQLite DB with publication_tags + government_publications + source_recommendations."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS government_publications (
            id INTEGER PRIMARY KEY,
            title TEXT,
            summary TEXT,
            url TEXT,
            documentType TEXT,
            organisation TEXT,
            organisationSlug TEXT,
            firstPublishedAt TEXT,
            publicUpdatedAt TEXT,
            imageUrl TEXT,
            lastUpdated INTEGER
        );
        CREATE TABLE IF NOT EXISTS publication_tags (
            publicationId INTEGER NOT NULL,
            tag TEXT NOT NULL,
            hitCount INTEGER NOT NULL,
            PRIMARY KEY(publicationId, tag)
        );
    """)
    build_source_recs.create_source_recs_table(conn)
    conn.commit()
    return conn


class TestTagToDepartments(unittest.TestCase):
    """Verify TAG_TO_DEPARTMENTS covers all 26 tags."""

    def test_all_26_tags_mapped(self):
        """TAG_TO_DEPARTMENTS must have entries for all 26 tags in TAG_DICTIONARY."""
        for tag_name in build_tags.TAG_DICTIONARY:
            self.assertIn(tag_name, build_source_recs.TAG_TO_DEPARTMENTS,
                          f"Tag '{tag_name}' missing from TAG_TO_DEPARTMENTS")

    def test_departments_are_tuples(self):
        """Each department entry must be a (slug, name) tuple."""
        for tag_name, departments in build_source_recs.TAG_TO_DEPARTMENTS.items():
            for dept in departments:
                self.assertIsInstance(dept, tuple)
                self.assertEqual(len(dept), 2)
                self.assertIsInstance(dept[0], str)  # slug
                self.assertIsInstance(dept[1], str)  # name


class TestBuildSourceRecs(unittest.TestCase):
    """Test build_source_recs hybrid mapping."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_source_recs.db")
        self.conn = create_test_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_publication(self, pub_id, org_slug, org_name):
        self.conn.execute(
            """INSERT OR REPLACE INTO government_publications
               (id, title, summary, url, documentType, organisation, organisationSlug,
                firstPublishedAt, publicUpdatedAt, imageUrl, lastUpdated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pub_id, "Test Pub", "Summary", "http://example.com", "news_article",
             org_name, org_slug, "2026-01-01", "2026-01-01", None, 0),
        )
        self.conn.commit()

    def _insert_pub_tag(self, pub_id, tag, hit_count):
        self.conn.execute(
            "INSERT OR REPLACE INTO publication_tags (publicationId, tag, hitCount) VALUES (?, ?, ?)",
            (pub_id, tag, hit_count),
        )
        self.conn.commit()

    def test_hardcoded_departments_recommended(self):
        """Hardcoded departments have isRecommended=1."""
        self.conn.execute("DELETE FROM source_recommendations")
        self.conn.commit()
        build_source_recs.build_source_recs(self.conn)

        cursor = self.conn.cursor()
        # Taxation → HMRC should be recommended
        cursor.execute("SELECT isRecommended FROM source_recommendations WHERE tag = 'Taxation' AND organisationSlug = 'hm-revenue-customs'")
        row = cursor.fetchone()
        self.assertIsNotNone(row, "HMRC should be in source_recommendations for Taxation")
        self.assertEqual(row[0], 1, "Hardcoded departments should have isRecommended=1")

    def test_data_driven_high_hits_recommended(self):
        """Data-driven department with high hit counts gets isRecommended=1."""
        # Insert publications from a department not in the hardcoded mapping for NHS
        # NHS hardcoded → DHSC. Let's add publications from "home-office" with NHS tag
        # and high hit counts to trigger data-driven recommendation.
        for i in range(5):
            self._insert_publication(1000 + i, "home-office", "Home Office")
            self._insert_pub_tag(1000 + i, "NHS", 3)

        self.conn.execute("DELETE FROM source_recommendations")
        self.conn.commit()
        build_source_recs.build_source_recs(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT isRecommended, hitCount FROM source_recommendations WHERE tag = 'NHS' AND organisationSlug = 'home-office'")
        row = cursor.fetchone()
        self.assertIsNotNone(row, "Home Office should appear for NHS due to data-driven hits")
        self.assertEqual(row[0], 1, "High hit count department should be recommended")
        self.assertGreaterEqual(row[1], build_source_recs.DATA_DRIVEN_THRESHOLD)

    def test_data_driven_low_hits_not_recommended(self):
        """Data-driven department with low hit counts gets isRecommended=0."""
        # Insert 1 publication from a non-hardcoded department with low hit count
        self._insert_publication(2000, "home-office", "Home Office")
        self._insert_pub_tag(2000, "NHS", 1)

        self.conn.execute("DELETE FROM source_recommendations")
        self.conn.commit()
        build_source_recs.build_source_recs(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT isRecommended FROM source_recommendations WHERE tag = 'NHS' AND organisationSlug = 'home-office'")
        row = cursor.fetchone()
        self.assertIsNotNone(row, "Home Office should appear for NHS (low hits)")
        self.assertEqual(row[0], 0, "Low hit count department should NOT be recommended")

    def test_source_recs_populated(self):
        """source_recommendations table has entries after build_source_recs runs."""
        self.conn.execute("DELETE FROM source_recommendations")
        self.conn.commit()
        build_source_recs.build_source_recs(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM source_recommendations")
        count = cursor.fetchone()[0]
        self.assertGreater(count, 0, "source_recommendations should have entries")

    def test_hardcoded_hit_count_updated(self):
        """Hardcoded department hit count is updated from publication_tags data."""
        # DHSC is hardcoded for NHS. Insert publications from DHSC with NHS tags.
        self._insert_publication(3000, "department-of-health-and-social-care", "DHSC")
        self._insert_pub_tag(3000, "NHS", 5)

        self.conn.execute("DELETE FROM source_recommendations")
        self.conn.commit()
        build_source_recs.build_source_recs(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT hitCount, isRecommended FROM source_recommendations WHERE tag = 'NHS' AND organisationSlug = 'department-of-health-and-social-care'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 5, "Hit count should be updated from publication_tags data")
        self.assertEqual(row[1], 1, "Hardcoded department should remain recommended")


class TestMain(unittest.TestCase):
    """Test main() argument parsing."""

    def test_main_requires_output(self):
        """main() must have --output required argument."""
        import inspect
        source = inspect.getsource(build_source_recs.main)
        self.assertIn("--output", source)
        self.assertIn("required=True", source)


if __name__ == "__main__":
    unittest.main()
