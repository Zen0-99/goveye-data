"""Unit tests for diff_db.py and manifest.py.

Tests diff generation (empty diff, new division, updated MP) and
manifest generation (version increment, hash format).
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

# Add parent directory to path so we can import diff_db and manifest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diff_db
import manifest

# Path to the real schema JSON
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "8.json",
)


def create_test_db(db_path):
    """Create a minimal test DB with mps, divisions, and division_votes tables."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute(
        """CREATE TABLE IF NOT EXISTS mps (
            id INTEGER PRIMARY KEY,
            nameListAs TEXT NOT NULL,
            nameDisplayAs TEXT NOT NULL,
            nameFullTitle TEXT,
            nameAddressAs TEXT,
            gender TEXT,
            partyId INTEGER NOT NULL,
            partyName TEXT NOT NULL,
            partyAbbreviation TEXT NOT NULL,
            partyBackgroundColour TEXT NOT NULL,
            partyForegroundColour TEXT NOT NULL,
            constituencyId INTEGER NOT NULL,
            constituencyName TEXT NOT NULL,
            house INTEGER NOT NULL,
            membershipStartDate TEXT,
            membershipEndDate TEXT,
            isActive INTEGER NOT NULL,
            thumbnailUrl TEXT,
            lastUpdated INTEGER NOT NULL
        )"""
    )

    cursor.execute(
        """CREATE TABLE IF NOT EXISTS divisions (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            publicationUpdated TEXT,
            number INTEGER,
            isDeferred INTEGER NOT NULL,
            ayeCount INTEGER NOT NULL,
            noCount INTEGER NOT NULL,
            house INTEGER NOT NULL,
            lastUpdated INTEGER NOT NULL
        )"""
    )

    cursor.execute(
        """CREATE TABLE IF NOT EXISTS division_votes (
            divisionId INTEGER NOT NULL,
            memberId INTEGER NOT NULL,
            vote TEXT NOT NULL,
            memberName TEXT NOT NULL,
            partyName TEXT NOT NULL,
            partyColour TEXT NOT NULL,
            constituencyName TEXT NOT NULL,
            isTeller INTEGER NOT NULL,
            proxyName TEXT,
            PRIMARY KEY(divisionId, memberId)
        )"""
    )

    # Create remaining empty tables that the schema expects
    for table_sql in [
        "CREATE TABLE IF NOT EXISTS remote_keys (label TEXT PRIMARY KEY, lastUpdated INTEGER)",
        "CREATE TABLE IF NOT EXISTS committees (id INTEGER PRIMARY KEY, name TEXT)",
        "CREATE TABLE IF NOT EXISTS mp_committee_cross_ref (mpId INTEGER, committeeId INTEGER, PRIMARY KEY(mpId, committeeId))",
        "CREATE TABLE IF NOT EXISTS bills (id INTEGER PRIMARY KEY, title TEXT)",
        "CREATE TABLE IF NOT EXISTS bill_stages (id INTEGER PRIMARY KEY, name TEXT)",
        "CREATE TABLE IF NOT EXISTS bill_follows (id INTEGER PRIMARY KEY, name TEXT)",
        "CREATE TABLE IF NOT EXISTS hansard_contributions (id INTEGER PRIMARY KEY, text TEXT)",
        "CREATE TABLE IF NOT EXISTS interests (id INTEGER PRIMARY KEY, description TEXT)",
        "CREATE TABLE IF NOT EXISTS follows (id INTEGER PRIMARY KEY, name TEXT)",
        "CREATE TABLE IF NOT EXISTS recess_dates (id INTEGER PRIMARY KEY, date TEXT)",
        "CREATE TABLE IF NOT EXISTS recess_dates_meta (id INTEGER PRIMARY KEY, key TEXT)",
        "CREATE TABLE IF NOT EXISTS mp_notification_prefs (mpId INTEGER PRIMARY KEY, enabled INTEGER)",
        "CREATE VIRTUAL TABLE IF NOT EXISTS mps_fts USING FTS4(nameListAs, nameDisplayAs, constituencyName, partyName)",
        "CREATE TABLE IF NOT EXISTS room_master_table (id INTEGER PRIMARY KEY, identity_hash TEXT)",
    ]:
        cursor.execute(table_sql)

    cursor.execute(
        "INSERT OR REPLACE INTO room_master_table (id, identity_hash) VALUES(42, 'test_hash')"
    )

    conn.commit()
    return conn


def insert_test_mp(conn, mp_id, name, party="Labour", party_abbrev="Lab"):
    """Insert a test MP into the DB."""
    conn.execute(
        """INSERT OR REPLACE INTO mps (id, nameListAs, nameDisplayAs, nameFullTitle,
           nameAddressAs, gender, partyId, partyName, partyAbbreviation,
           partyBackgroundColour, partyForegroundColour, constituencyId,
           constituencyName, house, membershipStartDate, membershipEndDate,
           isActive, thumbnailUrl, lastUpdated)
           VALUES (?, ?, ?, NULL, NULL, NULL, 15, ?, ?, 'd50000', 'ffffff',
                   100, 'Test North', 1, '2020-01-01', NULL, 1, NULL, 1700000000000)""",
        (mp_id, name, name, party, party_abbrev),
    )
    conn.commit()


def insert_test_division(conn, div_id, title="Test Division", house=1):
    """Insert a test division into the DB."""
    conn.execute(
        """INSERT OR REPLACE INTO divisions (id, title, date, publicationUpdated,
           number, isDeferred, ayeCount, noCount, house, lastUpdated)
           VALUES (?, ?, '2026-01-01', NULL, 1, 0, 300, 200, ?, 1700000000000)""",
        (div_id, title, house),
    )
    conn.commit()


def insert_test_vote(conn, div_id, member_id, vote="AYE"):
    """Insert a test vote into the DB."""
    conn.execute(
        """INSERT OR REPLACE INTO division_votes (divisionId, memberId, vote,
           memberName, partyName, partyColour, constituencyName, isTeller, proxyName)
           VALUES (?, ?, ?, 'Test MP', 'Labour', 'd50000', 'Test North', 0, NULL)""",
        (div_id, member_id, vote),
    )
    conn.commit()


class TestDiffDb(unittest.TestCase):
    """Test diff_db.py diff generation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.prev_db = os.path.join(self.tmpdir, "prev.db")
        self.new_db = os.path.join(self.tmpdir, "new.db")
        self.patch_path = os.path.join(self.tmpdir, "patch.json")

    def test_empty_diff(self):
        """Identical DBs produce empty upsert/delete arrays."""
        # Create identical DBs
        conn1 = create_test_db(self.prev_db)
        insert_test_mp(conn1, 1, "Test, A")
        insert_test_division(conn1, 100)
        conn1.close()

        conn2 = create_test_db(self.new_db)
        insert_test_mp(conn2, 1, "Test, A")
        insert_test_division(conn2, 100)
        conn2.close()

        diff_db.generate_diff(
            self.new_db, self.prev_db, SCHEMA_PATH, self.patch_path
        )

        with open(self.patch_path) as f:
            patch = json.load(f)

        # All tables should have empty upsert and delete arrays
        for table_name, changes in patch["changes"].items():
            if table_name in ("mps", "divisions", "division_votes"):
                self.assertEqual(len(changes["upsert"]), 0,
                                 f"{table_name} should have 0 upserts")
                self.assertEqual(len(changes["delete"]), 0,
                                 f"{table_name} should have 0 deletes")

    def test_new_division(self):
        """Adding a division to new DB produces one upsert in divisions."""
        # Previous DB with no divisions
        conn1 = create_test_db(self.prev_db)
        insert_test_mp(conn1, 1, "Test, A")
        conn1.close()

        # New DB with a new division and votes
        conn2 = create_test_db(self.new_db)
        insert_test_mp(conn2, 1, "Test, A")
        insert_test_division(conn2, 100)
        insert_test_vote(conn2, 100, 1, "AYE")
        conn2.close()

        diff_db.generate_diff(
            self.new_db, self.prev_db, SCHEMA_PATH, self.patch_path
        )

        with open(self.patch_path) as f:
            patch = json.load(f)

        # divisions should have 1 upsert
        self.assertEqual(len(patch["changes"]["divisions"]["upsert"]), 1)
        self.assertEqual(patch["changes"]["divisions"]["upsert"][0]["id"], 100)

        # division_votes should have 1 upsert
        self.assertEqual(len(patch["changes"]["division_votes"]["upsert"]), 1)
        self.assertEqual(
            patch["changes"]["division_votes"]["upsert"][0]["divisionId"], 100
        )

    def test_updated_mp(self):
        """Changing an MP's party produces one upsert in mps."""
        # Previous DB with MP in Labour
        conn1 = create_test_db(self.prev_db)
        insert_test_mp(conn1, 1, "Test, A", party="Labour", party_abbrev="Lab")
        conn1.close()

        # New DB with same MP but party changed to Conservative
        conn2 = create_test_db(self.new_db)
        insert_test_mp(conn2, 1, "Test, A", party="Conservative", party_abbrev="Con")
        conn2.close()

        diff_db.generate_diff(
            self.new_db, self.prev_db, SCHEMA_PATH, self.patch_path
        )

        with open(self.patch_path) as f:
            patch = json.load(f)

        # mps should have 1 upsert (the changed MP)
        self.assertEqual(len(patch["changes"]["mps"]["upsert"]), 1)
        self.assertEqual(patch["changes"]["mps"]["upsert"][0]["partyName"], "Conservative")

        # No deletes (MP still exists)
        self.assertEqual(len(patch["changes"]["mps"]["delete"]), 0)

    def test_deleted_mp(self):
        """Removing an MP from new DB produces one delete in mps."""
        # Previous DB with 2 MPs
        conn1 = create_test_db(self.prev_db)
        insert_test_mp(conn1, 1, "Test, A")
        insert_test_mp(conn1, 2, "Test, B")
        conn1.close()

        # New DB with only 1 MP (MP 2 removed)
        conn2 = create_test_db(self.new_db)
        insert_test_mp(conn2, 1, "Test, A")
        conn2.close()

        diff_db.generate_diff(
            self.new_db, self.prev_db, SCHEMA_PATH, self.patch_path
        )

        with open(self.patch_path) as f:
            patch = json.load(f)

        # mps should have 1 delete (MP 2)
        self.assertEqual(len(patch["changes"]["mps"]["delete"]), 1)
        self.assertEqual(patch["changes"]["mps"]["delete"][0]["id"], 2)

    def test_first_run_no_previous(self):
        """First run (no previous DB) produces all rows as upserts."""
        conn = create_test_db(self.new_db)
        insert_test_mp(conn, 1, "Test, A")
        insert_test_division(conn, 100)
        conn.close()

        diff_db.generate_diff(
            self.new_db, None, SCHEMA_PATH, self.patch_path
        )

        with open(self.patch_path) as f:
            patch = json.load(f)

        # mps should have 1 upsert (all rows)
        self.assertEqual(len(patch["changes"]["mps"]["upsert"]), 1)
        # divisions should have 1 upsert
        self.assertEqual(len(patch["changes"]["divisions"]["upsert"]), 1)
        # No deletes on first run
        self.assertEqual(len(patch["changes"]["mps"]["delete"]), 0)


class TestManifestGeneration(unittest.TestCase):
    """Test manifest.py manifest generation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "goveye.db")
        self.patch_path = os.path.join(self.tmpdir, "patch.json")
        self.manifest_path = os.path.join(self.tmpdir, "manifest.json")

        # Create minimal test files
        conn = create_test_db(self.db_path)
        conn.close()

        with open(self.patch_path, "w") as f:
            json.dump({"changes": {}}, f)

    def test_manifest_first_run(self):
        """First run (no previous manifest) produces version=1."""
        manifest.generate_manifest(
            self.db_path, self.patch_path, self.manifest_path, SCHEMA_PATH
        )

        with open(self.manifest_path) as f:
            m = json.load(f)

        self.assertEqual(m["version"], 1)
        self.assertIsNone(m["previousVersion"])
        self.assertEqual(m["schemaVersion"], 8)
        self.assertIn("dbHash", m)
        self.assertIn("dbSize", m)
        self.assertIn("patchHash", m)
        self.assertIn("patchSize", m)
        self.assertIn("generatedAt", m)

    def test_manifest_version_increment(self):
        """Previous manifest causes version to increment by 1."""
        # Create previous manifest with version=5
        prev_manifest_path = os.path.join(self.tmpdir, "prev_manifest.json")
        with open(prev_manifest_path, "w") as f:
            json.dump({"version": 5}, f)

        manifest.generate_manifest(
            self.db_path, self.patch_path, self.manifest_path, SCHEMA_PATH,
            previous_manifest_path=prev_manifest_path,
        )

        with open(self.manifest_path) as f:
            m = json.load(f)

        self.assertEqual(m["version"], 6)
        self.assertEqual(m["previousVersion"], 5)

    def test_manifest_hash_format(self):
        """Manifest hashes are valid SHA-256 hex strings (64 chars)."""
        manifest.generate_manifest(
            self.db_path, self.patch_path, self.manifest_path, SCHEMA_PATH
        )

        with open(self.manifest_path) as f:
            m = json.load(f)

        # SHA-256 hex digest is 64 characters
        self.assertEqual(len(m["dbHash"]), 64)
        self.assertEqual(len(m["patchHash"]), 64)

        # All hex characters
        int(m["dbHash"], 16)  # Raises if not valid hex
        int(m["patchHash"], 16)

    def test_manifest_db_size(self):
        """Manifest dbSize matches actual file size."""
        manifest.generate_manifest(
            self.db_path, self.patch_path, self.manifest_path, SCHEMA_PATH
        )

        with open(self.manifest_path) as f:
            m = json.load(f)

        self.assertEqual(m["dbSize"], os.path.getsize(self.db_path))
        self.assertEqual(m["patchSize"], os.path.getsize(self.patch_path))


if __name__ == "__main__":
    unittest.main()
