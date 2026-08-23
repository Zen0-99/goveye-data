"""Unit tests for build_party_leaders.py — party leader identification from MNIS bio_data.

Tests:
- Leader identified from bio_data postsJson containing a leader title
- Fallback: party with no leader in bio_data uses HARDCODED_LEADERS
- LEADER_TITLES has at least 10 entries
- HARDCODED_LEADERS dict exists with major party entries
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

# Add parent directory to path so we can import build_party_leaders
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_party_leaders


def create_test_db(db_path):
    """Create a temp SQLite DB with bio_data + mps + party_leaders tables."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mps (
            id INTEGER PRIMARY KEY,
            nameListAs TEXT,
            partyId INTEGER,
            partyName TEXT,
            house INTEGER
        );
        CREATE TABLE IF NOT EXISTS bio_data (
            mpId INTEGER PRIMARY KEY,
            maidenSpeechDate TEXT,
            dateOfBirth TEXT,
            townOfBirth TEXT,
            countryOfBirth TEXT,
            honoursJson TEXT,
            postsJson TEXT,
            committeesJson TEXT,
            lastUpdated INTEGER
        );
    """)
    build_party_leaders.create_party_leaders_table(conn)
    conn.commit()
    return conn


class TestLeaderTitles(unittest.TestCase):
    """Verify LEADER_TITLES and HARDCODED_LEADERS constants."""

    def test_leader_titles_has_at_least_10(self):
        """LEADER_TITLES must contain at least 10 entries (D-07)."""
        self.assertGreaterEqual(len(build_party_leaders.LEADER_TITLES), 10)

    def test_leader_titles_contains_prime_minister(self):
        """LEADER_TITLES must contain 'Prime Minister'."""
        self.assertIn("Prime Minister", build_party_leaders.LEADER_TITLES)

    def test_leader_titles_contains_leader_of_opposition(self):
        """LEADER_TITLES must contain 'Leader of the Opposition'."""
        self.assertIn("Leader of the Opposition", build_party_leaders.LEADER_TITLES)

    def test_hardcoded_leaders_exists(self):
        """HARDCODED_LEADERS dict must exist."""
        self.assertTrue(hasattr(build_party_leaders, "HARDCODED_LEADERS"))

    def test_hardcoded_leaders_has_major_parties(self):
        """HARDCODED_LEADERS must have entries for major parties (Labour, Conservative, Lib Dem)."""
        hd = build_party_leaders.HARDCODED_LEADERS
        self.assertIn(15, hd)  # Labour
        self.assertIn(4, hd)   # Conservative
        self.assertIn(17, hd)  # Lib Dem


class TestBuildPartyLeaders(unittest.TestCase):
    """Test build_party_leaders leader identification and fallback."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_party_leaders.db")
        self.conn = create_test_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_mp(self, mp_id, party_id, party_name="Test Party"):
        self.conn.execute(
            "INSERT OR REPLACE INTO mps (id, nameListAs, partyId, partyName, house) VALUES (?, ?, ?, ?, ?)",
            (mp_id, f"MP {mp_id}", party_id, party_name, 1),
        )
        self.conn.commit()

    def _insert_bio_data(self, mp_id, posts_json):
        self.conn.execute(
            "INSERT OR REPLACE INTO bio_data (mpId, postsJson, lastUpdated) VALUES (?, ?, ?)",
            (mp_id, posts_json, 0),
        )
        self.conn.commit()

    def test_leader_identified_from_bio_data(self):
        """Party leader is identified from bio_data postsJson with 'Prime Minister' title."""
        self._insert_mp(100, 15, "Labour")
        posts = [{"type": "Government Post", "title": "Prime Minister",
                  "department": "Prime Minister's Office", "startDate": "2024-07-05", "endDate": None}]
        self._insert_bio_data(100, json.dumps(posts))

        # Clear and rebuild
        self.conn.execute("DELETE FROM party_leaders")
        self.conn.commit()
        build_party_leaders.build_party_leaders(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT memberId, title FROM party_leaders WHERE partyId = 15")
        row = cursor.fetchone()
        self.assertIsNotNone(row, "Labour party should have a leader")
        self.assertEqual(row[0], 100)
        self.assertEqual(row[1], "Prime Minister")

    def test_fallback_used_when_no_leader_in_bio_data(self):
        """Fallback HARDCODED_LEADERS used when bio_data has no leader post."""
        self._insert_mp(200, 4, "Conservative")
        posts = [{"type": "Opposition Post", "title": "Shadow Chancellor",
                  "department": "", "startDate": "2024-01-01", "endDate": None}]
        self._insert_bio_data(200, json.dumps(posts))

        self.conn.execute("DELETE FROM party_leaders")
        self.conn.commit()
        build_party_leaders.build_party_leaders(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT memberId, title FROM party_leaders WHERE partyId = 4")
        row = cursor.fetchone()
        self.assertIsNotNone(row, "Conservative party should have a fallback leader")
        # Should be the hardcoded memberId, not 200
        self.assertNotEqual(row[0], 200)

    def test_no_bio_data_uses_fallback(self):
        """When bio_data table is empty, all hardcoded leaders are used."""
        self._insert_mp(300, 15, "Labour")
        # No bio_data inserted

        self.conn.execute("DELETE FROM party_leaders")
        self.conn.commit()
        build_party_leaders.build_party_leaders(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM party_leaders")
        count = cursor.fetchone()[0]
        self.assertGreaterEqual(count, len(build_party_leaders.HARDCODED_LEADERS),
                                "All hardcoded leaders should be present when no bio_data leaders found")

    def test_case_insensitive_match(self):
        """Leader title matching is case-insensitive."""
        self._insert_mp(400, 15, "Labour")
        posts = [{"type": "Government Post", "title": "PRIME MINISTER",
                  "department": "", "startDate": "2024-07-05", "endDate": None}]
        self._insert_bio_data(400, json.dumps(posts))

        self.conn.execute("DELETE FROM party_leaders")
        self.conn.commit()
        build_party_leaders.build_party_leaders(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT memberId FROM party_leaders WHERE partyId = 15")
        row = cursor.fetchone()
        self.assertIsNotNone(row, "Case-insensitive match should find 'PRIME MINISTER'")
        self.assertEqual(row[0], 400)

    def test_invalid_json_skipped(self):
        """Invalid JSON in postsJson is skipped gracefully."""
        self._insert_mp(500, 15, "Labour")
        self._insert_bio_data(500, "not valid json{{")

        self.conn.execute("DELETE FROM party_leaders")
        self.conn.commit()
        build_party_leaders.build_party_leaders(self.conn)

        # Fallback should be used
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM party_leaders WHERE partyId = 15")
        self.assertEqual(cursor.fetchone()[0], 1, "Fallback should be used for invalid JSON")


class TestMain(unittest.TestCase):
    """Test main() argument parsing."""

    def test_main_requires_output(self):
        """main() must have --output required argument."""
        import inspect
        source = inspect.getsource(build_party_leaders.main)
        self.assertIn("--output", source)
        self.assertIn("required=True", source)


if __name__ == "__main__":
    unittest.main()
