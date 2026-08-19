"""Unit tests for build_db.py — schema parsing, DB creation, MP insertion.

Uses Python stdlib unittest with mocked API calls (unittest.mock.patch)
to avoid hitting the real Parliament API in tests.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add parent directory to path so we can import build_db and schema
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_db


# Path to the real schema JSON for integration-style tests
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "8.json",
)

# Minimal fixture schema for unit tests
FIXTURE_SCHEMA = {
    "formatVersion": 1,
    "database": {
        "version": 8,
        "identityHash": "test_hash_12345",
        "entities": [
            {
                "tableName": "mps",
                "createSql": (
                    "CREATE TABLE IF NOT EXISTS `${TABLE_NAME}` "
                    "(`id` INTEGER NOT NULL, `nameListAs` TEXT NOT NULL, "
                    "`nameDisplayAs` TEXT NOT NULL, `nameFullTitle` TEXT, "
                    "`nameAddressAs` TEXT, `gender` TEXT, "
                    "`partyId` INTEGER NOT NULL, `partyName` TEXT NOT NULL, "
                    "`partyAbbreviation` TEXT NOT NULL, "
                    "`partyBackgroundColour` TEXT NOT NULL, "
                    "`partyForegroundColour` TEXT NOT NULL, "
                    "`constituencyId` INTEGER NOT NULL, "
                    "`constituencyName` TEXT NOT NULL, "
                    "`house` INTEGER NOT NULL, "
                    "`membershipStartDate` TEXT, `membershipEndDate` TEXT, "
                    "`isActive` INTEGER NOT NULL, `thumbnailUrl` TEXT, "
                    "`lastUpdated` INTEGER NOT NULL, PRIMARY KEY(`id`))"
                ),
                "fields": [
                    {"columnName": "id", "affinity": "INTEGER", "notNull": True},
                    {"columnName": "nameListAs", "affinity": "TEXT", "notNull": True},
                    {"columnName": "nameDisplayAs", "affinity": "TEXT", "notNull": True},
                    {"columnName": "nameFullTitle", "affinity": "TEXT"},
                    {"columnName": "nameAddressAs", "affinity": "TEXT"},
                    {"columnName": "gender", "affinity": "TEXT"},
                    {"columnName": "partyId", "affinity": "INTEGER", "notNull": True},
                    {"columnName": "partyName", "affinity": "TEXT", "notNull": True},
                    {"columnName": "partyAbbreviation", "affinity": "TEXT", "notNull": True},
                    {"columnName": "partyBackgroundColour", "affinity": "TEXT", "notNull": True},
                    {"columnName": "partyForegroundColour", "affinity": "TEXT", "notNull": True},
                    {"columnName": "constituencyId", "affinity": "INTEGER", "notNull": True},
                    {"columnName": "constituencyName", "affinity": "TEXT", "notNull": True},
                    {"columnName": "house", "affinity": "INTEGER", "notNull": True},
                    {"columnName": "membershipStartDate", "affinity": "TEXT"},
                    {"columnName": "membershipEndDate", "affinity": "TEXT"},
                    {"columnName": "isActive", "affinity": "INTEGER", "notNull": True},
                    {"columnName": "thumbnailUrl", "affinity": "TEXT"},
                    {"columnName": "lastUpdated", "affinity": "INTEGER", "notNull": True},
                ],
                "primaryKey": {"columnNames": ["id"]},
            },
            {
                "tableName": "mps_fts",
                "createSql": (
                    "CREATE VIRTUAL TABLE IF NOT EXISTS `${TABLE_NAME}` "
                    "USING FTS4(`nameListAs` TEXT, `nameDisplayAs` TEXT, "
                    "`constituencyName` TEXT, `partyName` TEXT, "
                    "content=`mps`)"
                ),
                "fields": [
                    {"columnName": "nameListAs", "affinity": "TEXT"},
                    {"columnName": "nameDisplayAs", "affinity": "TEXT"},
                    {"columnName": "constituencyName", "affinity": "TEXT"},
                    {"columnName": "partyName", "affinity": "TEXT"},
                ],
                "contentSyncTriggers": [
                    "CREATE TRIGGER IF NOT EXISTS room_fts_content_sync_mps_fts_BEFORE_UPDATE "
                    "BEFORE UPDATE ON `mps` BEGIN DELETE FROM `mps_fts` WHERE `docid`=OLD.`rowid`; END",
                    "CREATE TRIGGER IF NOT EXISTS room_fts_content_sync_mps_fts_BEFORE_DELETE "
                    "BEFORE DELETE ON `mps` BEGIN DELETE FROM `mps_fts` WHERE `docid`=OLD.`rowid`; END",
                    "CREATE TRIGGER IF NOT EXISTS room_fts_content_sync_mps_fts_AFTER_UPDATE "
                    "AFTER UPDATE ON `mps` BEGIN INSERT INTO `mps_fts`(`docid`, `nameListAs`, "
                    "`nameDisplayAs`, `constituencyName`, `partyName`) VALUES (NEW.`rowid`, "
                    "NEW.`nameListAs`, NEW.`nameDisplayAs`, NEW.`constituencyName`, NEW.`partyName`); END",
                    "CREATE TRIGGER IF NOT EXISTS room_fts_content_sync_mps_fts_AFTER_INSERT "
                    "AFTER INSERT ON `mps` BEGIN INSERT INTO `mps_fts`(`docid`, `nameListAs`, "
                    "`nameDisplayAs`, `constituencyName`, `partyName`) VALUES (NEW.`rowid`, "
                    "NEW.`nameListAs`, NEW.`nameDisplayAs`, NEW.`constituencyName`, NEW.`partyName`); END",
                ],
            },
        ],
        "setupQueries": [
            "CREATE TABLE IF NOT EXISTS room_master_table (id INTEGER PRIMARY KEY,identity_hash TEXT)",
            "INSERT OR REPLACE INTO room_master_table (id,identity_hash) VALUES(42, 'test_hash_12345')",
        ],
    },
}


class TestSchemaParsing(unittest.TestCase):
    """Test schema.py correctly extracts identity hash, version, table count, createSql."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.fixture_path = os.path.join(self.tmpdir, "fixture.json")
        with open(self.fixture_path, "w") as f:
            json.dump(FIXTURE_SCHEMA, f)

    def test_get_identity_hash(self):
        schema = schema_module.load_schema(self.fixture_path)
        self.assertEqual(schema_module.get_identity_hash(schema), "test_hash_12345")

    def test_get_version(self):
        schema = schema_module.load_schema(self.fixture_path)
        self.assertEqual(schema_module.get_version(schema), 8)

    def test_get_table_names(self):
        schema = schema_module.load_schema(self.fixture_path)
        names = schema_module.get_table_names(schema)
        self.assertEqual(names, {"mps", "mps_fts"})

    def test_get_create_sql(self):
        schema = schema_module.load_schema(self.fixture_path)
        create_sqls = schema_module.get_create_sql(schema)
        self.assertEqual(len(create_sqls), 2)
        # Check ${TABLE_NAME} is replaced
        for table_name, sql in create_sqls:
            self.assertNotIn("${TABLE_NAME}", sql)
            self.assertIn(table_name, sql)

    def test_get_fts_triggers(self):
        schema = schema_module.load_schema(self.fixture_path)
        triggers = schema_module.get_fts_triggers(schema)
        self.assertEqual(len(triggers), 4)

    def test_get_setup_queries(self):
        schema = schema_module.load_schema(self.fixture_path)
        queries = schema_module.get_setup_queries(schema)
        self.assertEqual(len(queries), 2)
        self.assertIn("room_master_table", queries[0])
        self.assertIn("test_hash_12345", queries[1])

    def test_real_schema_identity_hash(self):
        """Verify the real schema JSON has the correct identity hash."""
        if not os.path.exists(SCHEMA_PATH):
            self.skipTest("Real schema JSON not found")
        schema = schema_module.load_schema(SCHEMA_PATH)
        self.assertEqual(
            schema_module.get_identity_hash(schema),
            "187aeb854a2e69de65200c666d6555d1",
        )
        self.assertEqual(schema_module.get_version(schema), 8)
        self.assertEqual(len(schema_module.get_table_names(schema)), 16)


class TestDbCreation(unittest.TestCase):
    """Test build_db.py creates a DB with all tables and correct room_master_table hash."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.fixture_path = os.path.join(self.tmpdir, "fixture.json")
        with open(self.fixture_path, "w") as f:
            json.dump(FIXTURE_SCHEMA, f)

    def test_create_database_tables(self):
        """Verify all tables from the fixture schema are created."""
        conn = build_db.create_database(self.db_path, self.fixture_path)
        cursor = conn.cursor()

        # Check mps table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mps'"
        )
        self.assertIsNotNone(cursor.fetchone())

        # Check mps_fts virtual table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mps_fts'"
        )
        self.assertIsNotNone(cursor.fetchone())

        conn.close()

    def test_create_database_room_master_table(self):
        """Verify room_master_table has the correct identity hash."""
        conn = build_db.create_database(self.db_path, self.fixture_path)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT identity_hash FROM room_master_table WHERE id = 42"
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "test_hash_12345")

        conn.close()

    def test_create_database_fts_triggers(self):
        """Verify FTS4 sync triggers exist in the DB (Pitfall 2)."""
        conn = build_db.create_database(self.db_path, self.fixture_path)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
        triggers = [row[0] for row in cursor.fetchall()]
        self.assertEqual(len(triggers), 4)
        self.assertIn("room_fts_content_sync_mps_fts_BEFORE_UPDATE", triggers)
        self.assertIn("room_fts_content_sync_mps_fts_BEFORE_DELETE", triggers)
        self.assertIn("room_fts_content_sync_mps_fts_AFTER_UPDATE", triggers)
        self.assertIn("room_fts_content_sync_mps_fts_AFTER_INSERT", triggers)

        conn.close()

    def test_fts_populates_on_insert(self):
        """Verify FTS4 triggers populate mps_fts when MPs are inserted (Pitfall 2)."""
        conn = build_db.create_database(self.db_path, self.fixture_path)
        cursor = conn.cursor()

        # Insert a test MP
        cursor.execute(
            """INSERT INTO mps (id, nameListAs, nameDisplayAs, nameFullTitle,
               nameAddressAs, gender, partyId, partyName, partyAbbreviation,
               partyBackgroundColour, partyForegroundColour, constituencyId,
               constituencyName, house, membershipStartDate, membershipEndDate,
               isActive, thumbnailUrl, lastUpdated)
               VALUES (1, 'Test, MP', 'Test MP', 'Mr Test', NULL, 'M',
               15, 'Labour', 'Lab', 'd50000', 'ffffff', 100, 'Test North',
               1, '2020-01-01', NULL, 1, 'http://example.com/photo.jpg', 1700000000000)"""
        )
        conn.commit()

        # Check FTS table was populated by the AFTER_INSERT trigger
        cursor.execute("SELECT COUNT(*) FROM mps_fts")
        count = cursor.fetchone()[0]
        self.assertEqual(count, 1)

        conn.close()


class TestMpInsertion(unittest.TestCase):
    """Test MP insertion and column mapping."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.fixture_path = os.path.join(self.tmpdir, "fixture.json")
        with open(self.fixture_path, "w") as f:
            json.dump(FIXTURE_SCHEMA, f)

    def test_map_mp_to_entity_basic(self):
        """Test basic MP field mapping."""
        member_dto = {
            "id": 172,
            "nameListAs": "Abbott, Ms Diane",
            "nameDisplayAs": "Diane Abbott",
            "nameFullTitle": "Ms Diane Abbott",
            "gender": "F",
            "latestParty": {
                "id": 15,
                "name": "Labour",
                "abbreviation": "Lab",
                "backgroundColour": "d50000",
                "foregroundColour": "ffffff",
            },
            "latestHouseMembership": {
                "membershipFromId": 4074,
                "membershipFrom": "Hackney North and Stoke Newington",
                "house": 1,
                "membershipStartDate": "1987-06-11",
                "membershipStatus": {"statusIsActive": True},
            },
            "thumbnailUrl": "https://members-api.parliament.uk/api/Members/172/Thumbnail",
        }

        row = build_db.map_mp_to_entity(member_dto, 1700000000000)

        self.assertEqual(row[0], 172)  # id
        self.assertEqual(row[1], "Abbott, Ms Diane")  # nameListAs
        self.assertEqual(row[2], "Diane Abbott")  # nameDisplayAs
        self.assertEqual(row[3], "Ms Diane Abbott")  # nameFullTitle
        self.assertEqual(row[5], "F")  # gender
        self.assertEqual(row[6], 15)  # partyId
        self.assertEqual(row[7], "Labour")  # partyName
        self.assertEqual(row[8], "Lab")  # partyAbbreviation
        self.assertEqual(row[9], "d50000")  # partyBackgroundColour
        self.assertEqual(row[10], "ffffff")  # partyForegroundColour
        self.assertEqual(row[11], 4074)  # constituencyId
        self.assertEqual(row[12], "Hackney North and Stoke Newington")  # constituencyName
        self.assertEqual(row[13], 1)  # house
        self.assertEqual(row[14], "1987-06-11")  # membershipStartDate
        self.assertEqual(row[16], 1)  # isActive (1 for True)
        self.assertEqual(row[17], "https://members-api.parliament.uk/api/Members/172/Thumbnail")
        self.assertEqual(row[18], 1700000000000)  # lastUpdated

    def test_map_mp_coop_abbreviation(self):
        """Test Labour/Co-op abbreviation edge case from MemberMapper."""
        member_dto = {
            "id": 999,
            "nameListAs": "Test, MP",
            "nameDisplayAs": "Test MP",
            "latestParty": {
                "id": 15,
                "name": "Labour (Co-op)",
                "abbreviation": "Lab",
                "backgroundColour": "d50000",
                "foregroundColour": "ffffff",
            },
            "latestHouseMembership": {
                "membershipFromId": 1,
                "membershipFrom": "Test South",
                "house": 1,
                "membershipStatus": {"statusIsActive": True},
            },
        }

        row = build_db.map_mp_to_entity(member_dto, 1700000000000)
        # Abbreviation should have " Co-op" appended
        self.assertEqual(row[8], "Lab Co-op")

    def test_insert_mps(self):
        """Test inserting MPs into the DB and verifying columns match schema."""
        conn = build_db.create_database(self.db_path, self.fixture_path)

        mps = [
            {
                "id": 1,
                "nameListAs": "Test, A",
                "nameDisplayAs": "A Test",
                "latestParty": {
                    "id": 15, "name": "Labour", "abbreviation": "Lab",
                    "backgroundColour": "d50000", "foregroundColour": "ffffff",
                },
                "latestHouseMembership": {
                    "membershipFromId": 100, "membershipFrom": "Test North",
                    "house": 1, "membershipStatus": {"statusIsActive": True},
                },
                "thumbnailUrl": "http://example.com/photo.jpg",
            },
            {
                "id": 2,
                "nameListAs": "Test, B",
                "nameDisplayAs": "B Test",
                "latestParty": {
                    "id": 4, "name": "Conservative", "abbreviation": "Con",
                    "backgroundColour": "0087dc", "foregroundColour": "ffffff",
                },
                "latestHouseMembership": {
                    "membershipFromId": 200, "membershipFrom": "Test South",
                    "house": 1, "membershipStatus": {"statusIsActive": True},
                },
            },
        ]

        build_db.insert_mps(conn, mps, 1700000000000)

        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM mps")
        self.assertEqual(cursor.fetchone()[0], 2)

        cursor.execute("SELECT id, nameListAs, partyName FROM mps WHERE id = 1")
        row = cursor.fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "Test, A")
        self.assertEqual(row[2], "Labour")

        # Verify FTS was populated (Pitfall 2 — triggers exist before insert)
        cursor.execute("SELECT COUNT(*) FROM mps_fts")
        self.assertEqual(cursor.fetchone()[0], 2)

        conn.close()

    @patch("build_db.requests.get")
    def test_fetch_all_mps_mocked(self, mock_get):
        """Test fetch_all_mps with mocked API responses."""
        # Mock two pages: first with 20 items, second with 5 items
        page1 = {"items": [{"value": {"id": i}} for i in range(20)]}
        page2 = {"items": [{"value": {"id": i}} for i in range(20, 25)]}

        mock_response1 = MagicMock()
        mock_response1.json.return_value = page1
        mock_response1.raise_for_status = MagicMock()

        mock_response2 = MagicMock()
        mock_response2.json.return_value = page2
        mock_response2.raise_for_status = MagicMock()

        mock_get.side_effect = [mock_response1, mock_response2]

        mps = build_db.fetch_all_mps()
        self.assertEqual(len(mps), 25)
        self.assertEqual(mps[0]["id"], 0)
        self.assertEqual(mps[24]["id"], 24)

    @patch("build_db.requests.get")
    def test_fetch_all_mps_with_limit(self, mock_get):
        """Test fetch_all_mps respects mp_limit."""
        page1 = {"items": [{"value": {"id": i}} for i in range(20)]}

        mock_response = MagicMock()
        mock_response.json.return_value = page1
        mock_response.raise_for_status = MagicMock()

        mock_get.return_value = mock_response

        mps = build_db.fetch_all_mps(mp_limit=5)
        self.assertEqual(len(mps), 5)


if __name__ == "__main__":
    unittest.main()
