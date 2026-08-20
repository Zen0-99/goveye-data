"""Unit tests for merge_dbs.py — combines 7 per-API DBs into goveye.db.

Uses create_database_with_tables to build small per-API DBs with test data,
runs merge_dbs, and verifies all 16 tables exist, data is copied, the
identity hash is correct, FTS is populated, and missing sources are handled.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import merge_dbs

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "bundled_schema.json",
)


def make_mps_db(path, count=2):
    """Create an mps.db with a few test MPs."""
    conn = schema_module.create_database_with_tables(
        path, SCHEMA_PATH, ["mps", "mps_fts"],
    )
    ts = 1700000000000
    for i in range(1, count + 1):
        conn.execute(
            "INSERT OR REPLACE INTO mps (id, nameListAs, nameDisplayAs, "
            "nameFullTitle, nameAddressAs, gender, partyId, partyName, "
            "partyAbbreviation, partyBackgroundColour, partyForegroundColour, "
            "constituencyId, constituencyName, house, membershipStartDate, "
            "membershipEndDate, isActive, thumbnailUrl, lastUpdated) "
            "VALUES (?, ?, ?, NULL, NULL, NULL, 15, 'Labour', 'Lab', "
            "'d50000', 'ffffff', 100, 'Test North', 1, '2020-01-01', NULL, "
            "1, NULL, ?)",
            (i, f"Test, MP {i}", f"MP Test {i}", ts),
        )
    conn.commit()
    conn.close()


def make_commons_votes_db(path, count=2):
    """Create a commons_votes.db with a few test Commons divisions (house=1)."""
    conn = schema_module.create_database_with_tables(
        path, SCHEMA_PATH, ["divisions", "division_votes"],
    )
    ts = 1700000000000
    for i in range(1, count + 1):
        conn.execute(
            "INSERT OR REPLACE INTO divisions (id, title, date, "
            "publicationUpdated, number, isDeferred, ayeCount, noCount, "
            "house, lastUpdated) VALUES (?, ?, '2026-01-01', NULL, ?, 0, "
            "300, 200, 1, ?)",
            (100 + i, f"Commons Division {i}", 100 + i, ts),
        )
    conn.commit()
    conn.close()


def make_lords_votes_db(path, count=2):
    """Create a lords_votes.db with a few test Lords divisions (house=2)."""
    conn = schema_module.create_database_with_tables(
        path, SCHEMA_PATH, ["divisions", "division_votes"],
    )
    ts = 1700000000000
    for i in range(1, count + 1):
        conn.execute(
            "INSERT OR REPLACE INTO divisions (id, title, date, "
            "publicationUpdated, number, isDeferred, ayeCount, noCount, "
            "house, lastUpdated) VALUES (?, ?, '2026-01-02', NULL, ?, 0, "
            "100, 50, 2, ?)",
            (200 + i, f"Lords Division {i}", 200 + i, ts),
        )
    conn.commit()
    conn.close()


def make_bills_db(path, count=2):
    """Create a bills.db with a few test bills."""
    conn = schema_module.create_database_with_tables(
        path, SCHEMA_PATH, ["bills", "bill_stages"],
    )
    ts = 1700000000000
    for i in range(1, count + 1):
        conn.execute(
            "INSERT OR REPLACE INTO bills (id, shortTitle, longTitle, "
            "summary, currentHouse, originatingHouse, lastUpdate, "
            "billWithdrawn, isDefeated, isAct, billTypeId, "
            "currentStageDescription, currentStageAbbreviation, lastUpdated) "
            "VALUES (?, ?, NULL, NULL, 'Commons', 'Commons', '2026-01-01', "
            "NULL, 0, 0, 1, 'Second Reading', '2R', ?)",
            (i, f"Bill {i}", ts),
        )
    conn.commit()
    conn.close()


def make_committees_db(path, count=2):
    """Create a committees.db with a few test committees."""
    conn = schema_module.create_database_with_tables(
        path, SCHEMA_PATH, ["committees", "mp_committee_cross_ref"],
    )
    ts = 1700000000000
    for i in range(1, count + 1):
        conn.execute(
            "INSERT OR REPLACE INTO committees (id, name, house, "
            "categoryName, startDate, endDate, isActive, lastUpdated) "
            "VALUES (?, ?, 'Commons', 'Dept', '2020-01-01', NULL, 1, ?)",
            (i, f"Committee {i}", ts),
        )
        conn.execute(
            "INSERT OR REPLACE INTO mp_committee_cross_ref (memberId, "
            "committeeId, lastUpdated) VALUES (?, ?, ?)",
            (i, i, ts),
        )
    conn.commit()
    conn.close()


def make_recess_db(path, count=2):
    """Create a recess.db with a few test recess dates."""
    conn = schema_module.create_database_with_tables(
        path, SCHEMA_PATH, ["recess_dates", "recess_dates_meta"],
    )
    ts = 1700000000000
    for i in range(1, count + 1):
        conn.execute(
            "INSERT OR REPLACE INTO recess_dates (house, description, "
            "startDate, endDate) VALUES (?, ?, '2026-12-18', '2027-01-03')",
            (i, f"Recess {i}"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO recess_dates_meta (house, "
            "lastRefreshedAt) VALUES (?, ?)",
            (i, ts),
        )
    conn.commit()
    conn.close()


def make_interests_db(path, count=2):
    """Create an interests.db with a few test interests."""
    conn = schema_module.create_database_with_tables(
        path, SCHEMA_PATH, ["interests"],
    )
    ts = 1700000000000
    for i in range(1, count + 1):
        conn.execute(
            "INSERT OR REPLACE INTO interests (id, memberId, summary, "
            "categoryId, categoryNumber, categoryName, registrationDate, "
            "publishedDate, rectified, fieldsJson, lastUpdated, "
            "parsedAmountPence, currencyCode, bucket) "
            "VALUES (?, ?, ?, 1, '1', 'Employment and earnings', "
            "'2024-01-15', '2024-01-20', 0, '[]', ?, 500000, 'GBP', "
            "'Employment/Earnings')",
            (i, 100 + i, f"Test interest {i}", ts),
        )
    conn.commit()
    conn.close()


class TestMergeDbs(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mps_db = os.path.join(self.tmpdir, "mps.db")
        self.commons_votes_db = os.path.join(self.tmpdir, "commons_votes.db")
        self.lords_votes_db = os.path.join(self.tmpdir, "lords_votes.db")
        self.bills_db = os.path.join(self.tmpdir, "bills.db")
        self.committees_db = os.path.join(self.tmpdir, "committees.db")
        self.recess_db = os.path.join(self.tmpdir, "recess.db")
        self.interests_db = os.path.join(self.tmpdir, "interests.db")
        self.goveye_db = os.path.join(self.tmpdir, "goveye.db")

    def test_merge_creates_all_tables(self):
        """Merged goveye.db has all 16 tables."""
        make_mps_db(self.mps_db)
        make_commons_votes_db(self.commons_votes_db)
        make_lords_votes_db(self.lords_votes_db)
        make_bills_db(self.bills_db)
        make_committees_db(self.committees_db)
        make_recess_db(self.recess_db)
        make_interests_db(self.interests_db)

        merge_dbs.merge_dbs(
            self.goveye_db, SCHEMA_PATH,
            mps_db=self.mps_db,
            commons_votes_db=self.commons_votes_db,
            lords_votes_db=self.lords_votes_db,
            bills_db=self.bills_db, committees_db=self.committees_db,
            recess_db=self.recess_db,
            interests_db=self.interests_db,
        )

        c = sqlite3.connect(self.goveye_db)
        tables = {
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = schema_module.get_table_names(
            schema_module.load_schema(SCHEMA_PATH)
        )
        missing = expected - tables
        self.assertEqual(missing, set(), f"Missing tables: {missing}")
        c.close()

    def test_merge_copies_data(self):
        """Data from per-API DBs appears in merged goveye.db."""
        make_mps_db(self.mps_db, count=3)
        make_commons_votes_db(self.commons_votes_db, count=2)
        make_lords_votes_db(self.lords_votes_db, count=1)
        make_interests_db(self.interests_db, count=2)

        merge_dbs.merge_dbs(
            self.goveye_db, SCHEMA_PATH,
            mps_db=self.mps_db,
            commons_votes_db=self.commons_votes_db,
            lords_votes_db=self.lords_votes_db,
            bills_db=None, committees_db=None, recess_db=None,
            interests_db=self.interests_db,
        )

        c = sqlite3.connect(self.goveye_db)
        mp_count = c.execute("SELECT COUNT(*) FROM mps").fetchone()[0]
        self.assertEqual(mp_count, 3)
        div_count = c.execute("SELECT COUNT(*) FROM divisions").fetchone()[0]
        self.assertEqual(div_count, 3)  # 2 Commons + 1 Lords
        interest_count = c.execute("SELECT COUNT(*) FROM interests").fetchone()[0]
        self.assertEqual(interest_count, 2)
        c.close()

    def test_merge_identity_hash(self):
        """Merged goveye.db has correct identity hash in room_master_table."""
        make_mps_db(self.mps_db)

        merge_dbs.merge_dbs(
            self.goveye_db, SCHEMA_PATH,
            mps_db=self.mps_db,
            commons_votes_db=None, lords_votes_db=None,
            bills_db=None, committees_db=None, recess_db=None,
            interests_db=None,
        )

        c = sqlite3.connect(self.goveye_db)
        hash_val = c.execute(
            "SELECT identity_hash FROM room_master_table WHERE id=42"
        ).fetchone()[0]
        self.assertEqual(hash_val, "aed5eaea3808b15abb72f1c1e89835f9")
        c.close()

    def test_merge_fts_populated(self):
        """FTS4 triggers auto-populate mps_fts when mps data is copied."""
        make_mps_db(self.mps_db, count=2)

        merge_dbs.merge_dbs(
            self.goveye_db, SCHEMA_PATH,
            mps_db=self.mps_db,
            commons_votes_db=None, lords_votes_db=None,
            bills_db=None, committees_db=None, recess_db=None,
            interests_db=None,
        )

        c = sqlite3.connect(self.goveye_db)
        fts_count = c.execute("SELECT COUNT(*) FROM mps_fts").fetchone()[0]
        self.assertEqual(fts_count, 2)
        c.close()

    def test_merge_missing_source(self):
        """Missing per-API DBs are skipped; their tables exist but are empty."""
        # Only provide mps.db; recess.db is missing
        make_mps_db(self.mps_db)

        merge_dbs.merge_dbs(
            self.goveye_db, SCHEMA_PATH,
            mps_db=self.mps_db,
            commons_votes_db=None, lords_votes_db=None,
            bills_db=None, committees_db=None, recess_db=None,
            interests_db=None,
        )

        c = sqlite3.connect(self.goveye_db)
        # mps has data
        self.assertEqual(c.execute("SELECT COUNT(*) FROM mps").fetchone()[0], 2)
        # recess_dates table exists but is empty
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM recess_dates").fetchone()[0], 0
        )
        # Table still exists
        tables = {
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='recess_dates'"
            ).fetchall()
        }
        self.assertIn("recess_dates", tables)
        c.close()


if __name__ == "__main__":
    unittest.main()
