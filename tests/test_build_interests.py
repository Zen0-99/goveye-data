"""Unit tests for build_interests.py — per-API interests build script + parser.

Uses mocked api_get to avoid hitting the real Parliament API.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add parent directory to path so we can import build_interests and schema
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_interests

# Use bundled_schema.json (v2) which has the 3 new interests columns
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "bundled_schema.json",
)


def make_member_dto(mp_id):
    """Build a minimal MemberDto fixture (for MP ID fetching)."""
    return {
        "id": mp_id,
        "nameListAs": f"Test, MP {mp_id}",
        "nameDisplayAs": f"MP Test {mp_id}",
    }


def make_interest_dto(interest_id, member_id, category_number="1", fields=None):
    """Build a minimal InterestDto fixture."""
    cat_id = int(category_number.split(".")[0]) if category_number else 0
    return {
        "id": interest_id,
        "summary": f"Test interest {interest_id}",
        "category": {
            "id": cat_id,
            "number": category_number,
            "name": "Employment and earnings" if category_number.startswith("1") else "Other",
            "type": "Category",
        },
        "member": {"id": member_id},
        "registrationDate": "2024-01-15T00:00:00",
        "publishedDate": "2024-01-20T00:00:00",
        "rectified": False,
        "fields": fields or [],
    }


class TestParseAmount(unittest.TestCase):
    """Test the monetary parser in isolation."""

    def test_parse_empty_fields(self):
        """parse_amount('[]') returns (None, None)."""
        self.assertEqual(build_interests.parse_amount("[]"), (None, None))

    def test_parse_no_money(self):
        """Field with non-monetary text returns (None, None)."""
        fields = json.dumps([{"name": "Address", "value": "10 Downing St"}])
        self.assertEqual(build_interests.parse_amount(fields), (None, None))

    def test_parse_pound_amount(self):
        """Field with '£5,000' returns (500000, 'GBP')."""
        fields = json.dumps([{"name": "Amount", "value": "£5,000"}])
        self.assertEqual(build_interests.parse_amount(fields), (500000, "GBP"))

    def test_parse_pound_decimal(self):
        """Field with '£99.99' returns (9999, 'GBP')."""
        fields = json.dumps([{"name": "Amount", "value": "£99.99"}])
        self.assertEqual(build_interests.parse_amount(fields), (9999, "GBP"))

    def test_parse_pound_zero(self):
        """Field with '£0' returns (0, 'GBP')."""
        fields = json.dumps([{"name": "Amount", "value": "£0"}])
        self.assertEqual(build_interests.parse_amount(fields), (0, "GBP"))

    def test_parse_large_amount(self):
        """Field with '£1.5 million' returns (150000000, 'GBP')."""
        fields = json.dumps([{"name": "Amount", "value": "£1.5 million"}])
        self.assertEqual(build_interests.parse_amount(fields), (150000000, "GBP"))

    def test_parse_thousand_suffix(self):
        """Field with '£50k' returns (5000000, 'GBP')."""
        fields = json.dumps([{"name": "Amount", "value": "£50k"}])
        self.assertEqual(build_interests.parse_amount(fields), (5000000, "GBP"))

    def test_parse_range_takes_higher(self):
        """Range '£5,000 to £10,000' returns (1000000, 'GBP') — higher value."""
        fields = json.dumps([{"name": "Amount", "value": "£5,000 to £10,000"}])
        self.assertEqual(build_interests.parse_amount(fields), (1000000, "GBP"))

    def test_parse_range_with_dash(self):
        """Range '£5,000-£10,000' returns (1000000, 'GBP') — not negative."""
        fields = json.dumps([{"name": "Amount", "value": "£5,000-£10,000"}])
        self.assertEqual(build_interests.parse_amount(fields), (1000000, "GBP"))

    def test_parse_range_with_en_dash(self):
        """Range '£5,000–£10,000' returns (1000000, 'GBP')."""
        fields = json.dumps([{"name": "Amount", "value": "£5,000–£10,000"}])
        self.assertEqual(build_interests.parse_amount(fields), (1000000, "GBP"))

    def test_parse_hourly_rate(self):
        """Field with '£50/hour' returns (5000, 'GBP')."""
        fields = json.dumps([{"name": "Rate", "value": "£50/hour"}])
        self.assertEqual(build_interests.parse_amount(fields), (5000, "GBP"))

    def test_parse_structured_currency(self):
        """Structured field with currencyCode='USD' and value=1000 returns (100000, 'USD')."""
        fields = json.dumps([{
            "name": "Amount",
            "value": 1000,
            "typeInfo": {"currencyCode": "USD"},
        }])
        self.assertEqual(build_interests.parse_amount(fields), (100000, "USD"))

    def test_parse_structured_string_value(self):
        """Structured field with currencyCode='GBP' and value='18450.00' (string)
        returns (1845000, 'GBP'). The Parliament API returns decimals as strings."""
        fields = json.dumps([{
            "name": "Value",
            "value": "18450.00",
            "typeInfo": {"currencyCode": "GBP"},
        }])
        self.assertEqual(build_interests.parse_amount(fields), (1845000, "GBP"))

    def test_parse_summary_fallback(self):
        """When fields have no amount, the summary text 'Payment received - £18,450.00'
        is parsed as a fallback."""
        fields = json.dumps([{"name": "PaymentReceived", "value": True}])
        summary = "Payment received on 12 March 2025 - £18,450.00"
        pence, currency = build_interests.parse_amount(fields)
        # parse_amount only checks fields; the summary fallback is in
        # map_interest_to_entity. Test _regex_extract directly.
        pence, currency = build_interests._regex_extract(summary)
        self.assertEqual(pence, 1845000)
        self.assertEqual(currency, "GBP")

    def test_parse_never_fabricates(self):
        """Field with 'See attached document' returns (None, None)."""
        fields = json.dumps([{"name": "Note", "value": "See attached document"}])
        self.assertEqual(build_interests.parse_amount(fields), (None, None))

    def test_parse_negative_amount(self):
        """Field with '-£500' returns (-50000, 'GBP')."""
        fields = json.dumps([{"name": "Amount", "value": "-£500"}])
        self.assertEqual(build_interests.parse_amount(fields), (-50000, "GBP"))

    def test_parse_null_input(self):
        """parse_amount(None) returns (None, None)."""
        self.assertEqual(build_interests.parse_amount(None), (None, None))

    def test_parse_invalid_json(self):
        """parse_amount with invalid JSON returns (None, None)."""
        self.assertEqual(build_interests.parse_amount("not json"), (None, None))


class TestBucketMapping(unittest.TestCase):
    """Test get_bucket() category mapping."""

    def test_bucket_employment(self):
        self.assertEqual(build_interests.get_bucket("1"), "Employment/Earnings")

    def test_bucket_employment_subcategory(self):
        self.assertEqual(build_interests.get_bucket("1.1"), "Employment/Earnings")

    def test_bucket_employment_subcategory_2(self):
        self.assertEqual(build_interests.get_bucket("1.2"), "Employment/Earnings")

    def test_bucket_financial_support(self):
        self.assertEqual(build_interests.get_bucket("2"), "Financial Support")

    def test_bucket_gifts(self):
        self.assertEqual(build_interests.get_bucket("3"), "Gifts")

    def test_bucket_gifts_subcategory(self):
        self.assertEqual(build_interests.get_bucket("4"), "Gifts")

    def test_bucket_land_property(self):
        self.assertEqual(build_interests.get_bucket("6"), "Land/Property")

    def test_bucket_shareholdings(self):
        self.assertEqual(build_interests.get_bucket("7"), "Shareholdings")

    def test_bucket_other(self):
        self.assertEqual(build_interests.get_bucket("8"), "Other")

    def test_bucket_unknown(self):
        self.assertEqual(build_interests.get_bucket("99"), None)

    def test_bucket_unknown_subcategory_falls_back(self):
        """Unknown subcategory '99.1' falls back to parent '99' -> None."""
        self.assertEqual(build_interests.get_bucket("99.1"), None)


class TestCreateInterestsDb(unittest.TestCase):
    """Test that build_interests creates a DB with only the interests table."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "interests.db")

    def test_create_interests_db(self):
        """create_database_with_tables creates only interests + room_master_table."""
        conn = schema_module.create_database_with_tables(
            self.db_path, SCHEMA_PATH, ["interests"],
        )
        import sqlite3
        c = sqlite3.connect(self.db_path)
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("interests", tables)
        self.assertIn("room_master_table", tables)
        # No other data tables
        self.assertNotIn("mps", tables)
        self.assertNotIn("divisions", tables)
        self.assertNotIn("bills", tables)
        c.close()
        conn.close()


class TestInterestInsertion(unittest.TestCase):
    """Test seed build with mocked API."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "interests.db")

    @patch("build_interests.api_get")
    def test_seed_insertion(self, mock_api_get):
        """Seed build with --mp-limit 2 inserts interests with correct mapping."""
        # MP search response (1 page with 2 MPs)
        mp_page = {
            "items": [
                {"value": make_member_dto(1)},
                {"value": make_member_dto(2)},
            ]
        }
        # Interests responses: MP 1 has 2 interests, MP 2 has 1 interest
        mp1_interests = {
            "items": [
                make_interest_dto(101, 1, "1", [{"name": "Amount", "value": "£5,000"}]),
                make_interest_dto(102, 1, "3"),
            ],
            "totalResults": 2,
        }
        mp2_interests = {
            "items": [make_interest_dto(201, 2, "7")],
            "totalResults": 1,
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mp_page
        mock_response.raise_for_status = MagicMock()

        mock_int1 = MagicMock()
        mock_int1.json.return_value = mp1_interests
        mock_int1.raise_for_status = MagicMock()

        mock_int2 = MagicMock()
        mock_int2.json.return_value = mp2_interests
        mock_int2.raise_for_status = MagicMock()

        # First call: MP search. Then 2 calls for interests (MP 1, MP 2).
        # MP 1 has 2 interests in one page (20/page, so single page).
        # MP 2 has 1 interest in one page.
        mock_api_get.side_effect = [mock_response, mock_int1, mock_int2]

        build_interests.build_seed(self.db_path, SCHEMA_PATH, mp_limit=2)

        import sqlite3
        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM interests").fetchone()[0]
        self.assertEqual(count, 3)

        # Verify interest 101 has parsed amount
        row = c.execute(
            "SELECT parsedAmountPence, currencyCode, bucket FROM interests WHERE id=101"
        ).fetchone()
        self.assertEqual(row[0], 500000)
        self.assertEqual(row[1], "GBP")
        self.assertEqual(row[2], "Employment/Earnings")

        # Verify interest 102 (Gifts, no amount)
        row = c.execute(
            "SELECT parsedAmountPence, currencyCode, bucket FROM interests WHERE id=102"
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertEqual(row[2], "Gifts")

        # Verify interest 201 (Shareholdings)
        row = c.execute(
            "SELECT bucket FROM interests WHERE id=201"
        ).fetchone()
        self.assertEqual(row[0], "Shareholdings")

        c.close()

    @patch("build_interests.api_get")
    def test_parsed_amount_stored(self, mock_api_get):
        """Interest with '£5,000' in fields has parsedAmountPence=500000."""
        mp_page = {"items": [{"value": make_member_dto(1)}]}
        interest = make_interest_dto(101, 1, "1", [{"name": "Amount", "value": "£5,000"}])
        interests_page = {"items": [interest], "totalResults": 1}

        mock_response = MagicMock()
        mock_response.json.return_value = mp_page
        mock_response.raise_for_status = MagicMock()

        mock_int = MagicMock()
        mock_int.json.return_value = interests_page
        mock_int.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_response, mock_int]

        build_interests.build_seed(self.db_path, SCHEMA_PATH, mp_limit=1)

        import sqlite3
        c = sqlite3.connect(self.db_path)
        row = c.execute(
            "SELECT parsedAmountPence, currencyCode FROM interests WHERE id=101"
        ).fetchone()
        self.assertEqual(row[0], 500000)
        self.assertEqual(row[1], "GBP")
        c.close()

    @patch("build_interests.api_get")
    def test_null_amount_stored(self, mock_api_get):
        """Interest with no monetary data has NULL parsedAmountPence and currencyCode."""
        mp_page = {"items": [{"value": make_member_dto(1)}]}
        interest = make_interest_dto(101, 1, "1", [{"name": "Description", "value": "Consultant role"}])
        interests_page = {"items": [interest], "totalResults": 1}

        mock_response = MagicMock()
        mock_response.json.return_value = mp_page
        mock_response.raise_for_status = MagicMock()

        mock_int = MagicMock()
        mock_int.json.return_value = interests_page
        mock_int.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_response, mock_int]

        build_interests.build_seed(self.db_path, SCHEMA_PATH, mp_limit=1)

        import sqlite3
        c = sqlite3.connect(self.db_path)
        row = c.execute(
            "SELECT parsedAmountPence, currencyCode FROM interests WHERE id=101"
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        c.close()


class TestDeltaUpsert(unittest.TestCase):
    """Test delta mode upsert behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "interests.db")
        self.prev_db = os.path.join(self.tmpdir, "prev_interests.db")

    @patch("build_interests.api_get")
    def test_delta_upsert(self, mock_api_get):
        """Delta build upserts — amended interest gets new summary, no duplicates."""
        # Create previous DB with 2 interests
        conn = schema_module.create_database_with_tables(
            self.prev_db, SCHEMA_PATH, ["interests"],
        )
        ts = 1700000000000
        conn.execute(
            "INSERT OR REPLACE INTO interests (id, memberId, summary, categoryId, "
            "categoryNumber, categoryName, registrationDate, publishedDate, "
            "rectified, fieldsJson, lastUpdated, parsedAmountPence, currencyCode, "
            "bucket) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (101, 1, "Old summary", 1, "1", "Employment", "2024-01-15",
             "2024-01-20", 0, "[]", ts, None, None, "Employment/Earnings"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO interests (id, memberId, summary, categoryId, "
            "categoryNumber, categoryName, registrationDate, publishedDate, "
            "rectified, fieldsJson, lastUpdated, parsedAmountPence, currencyCode, "
            "bucket) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (102, 1, "Unchanged", 3, "3", "Gifts", "2024-01-15",
             "2024-01-20", 0, "[]", ts, None, None, "Gifts"),
        )
        conn.commit()
        conn.close()

        # Delta build: MP 1 now has 2 interests, interest 101 has amended summary
        mp_page = {"items": [{"value": make_member_dto(1)}]}
        amended_interest = make_interest_dto(101, 1, "1", [{"name": "Amount", "value": "£10,000"}])
        amended_interest["summary"] = "Amended summary"
        unchanged_interest = make_interest_dto(102, 1, "3")
        interests_page = {
            "items": [amended_interest, unchanged_interest],
            "totalResults": 2,
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mp_page
        mock_response.raise_for_status = MagicMock()

        mock_int = MagicMock()
        mock_int.json.return_value = interests_page
        mock_int.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_response, mock_int]

        build_interests.build_delta(self.db_path, self.prev_db, SCHEMA_PATH, mp_limit=1)

        import sqlite3
        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM interests").fetchone()[0]
        self.assertEqual(count, 2)  # No duplicates — INSERT OR REPLACE

        # Amended interest has new summary and parsed amount
        row = c.execute(
            "SELECT summary, parsedAmountPence FROM interests WHERE id=101"
        ).fetchone()
        self.assertEqual(row[0], "Amended summary")
        self.assertEqual(row[1], 1000000)

        c.close()


class TestInterestsCheckpoint(unittest.TestCase):
    """Test checkpoint/resume for interests seed mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "interests.db")
        self.checkpoint_db = os.path.join(self.tmpdir, "checkpoint.db")

    def _make_checkpoint(self, mp_ids):
        """Create a checkpoint DB with interests for the given MP IDs."""
        conn = schema_module.create_database_with_tables(
            self.checkpoint_db, SCHEMA_PATH, ["interests"],
        )
        ts = 1700000000000
        for mp_id in mp_ids:
            interest = make_interest_dto(mp_id * 100, mp_id, "1")
            build_interests.insert_interests(conn, [interest], ts)
        conn.close()

    @patch("build_interests.api_get")
    def test_checkpoint_skips_processed_mps(self, mock_api_get):
        """Checkpoint DB has interests for MP 1; only MP 2 is fetched."""
        self._make_checkpoint([1])

        # API returns MPs 1 and 2; only MP 2 should be fetched
        mp_page = {"items": [{"value": make_member_dto(1)}, {"value": make_member_dto(2)}]}
        mp2_interests = {"items": [make_interest_dto(201, 2, "7")], "totalResults": 1}

        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_int = MagicMock()
        mock_int.json.return_value = mp2_interests
        mock_int.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_int]

        build_interests.build_seed(
            self.db_path, SCHEMA_PATH, mp_limit=2,
            checkpoint_db=self.checkpoint_db,
        )

        # Only 2 api_get calls: 1 for MP list + 1 for MP 2's interests
        # MP 1 was skipped (in checkpoint)
        self.assertEqual(mock_api_get.call_count, 2)

        c = sqlite3.connect(self.db_path)
        # MP 1's interest from checkpoint + MP 2's new interest
        count = c.execute("SELECT COUNT(*) FROM interests").fetchone()[0]
        self.assertEqual(count, 2)
        c.close()

    @patch("build_interests.api_get")
    def test_seed_with_nonexistent_checkpoint_starts_fresh(self, mock_api_get):
        """Non-existent checkpoint path -> fresh seed."""
        mp_page = {"items": [{"value": make_member_dto(1)}]}
        interests_page = {"items": [make_interest_dto(101, 1, "1")], "totalResults": 1}

        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_int = MagicMock()
        mock_int.json.return_value = interests_page
        mock_int.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_int]

        build_interests.build_seed(
            self.db_path, SCHEMA_PATH, mp_limit=1,
            checkpoint_db=os.path.join(self.tmpdir, "nonexistent.db"),
        )

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM interests").fetchone()[0]
        self.assertEqual(count, 1)
        c.close()

    @patch("build_interests.api_get")
    def test_seed_without_checkpoint_starts_fresh(self, mock_api_get):
        """No checkpoint_db -> fresh seed (backward compatible)."""
        mp_page = {"items": [{"value": make_member_dto(1)}]}
        interests_page = {"items": [make_interest_dto(101, 1, "1")], "totalResults": 1}

        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_int = MagicMock()
        mock_int.json.return_value = interests_page
        mock_int.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_int]

        build_interests.build_seed(self.db_path, SCHEMA_PATH, mp_limit=1)

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM interests").fetchone()[0]
        self.assertEqual(count, 1)
        c.close()

    @patch("build_interests.api_get")
    def test_checkpoint_same_as_output(self, mock_api_get):
        """--checkpoint-db and --output same path -> no truncation, resumes."""
        self._make_checkpoint([1])
        import shutil
        shutil.copy2(self.checkpoint_db, self.db_path)

        # API returns MPs 1 and 2; only MP 2 should be fetched
        mp_page = {"items": [{"value": make_member_dto(1)}, {"value": make_member_dto(2)}]}
        mp2_interests = {"items": [make_interest_dto(201, 2, "7")], "totalResults": 1}

        mock_mp = MagicMock()
        mock_mp.json.return_value = mp_page
        mock_mp.raise_for_status = MagicMock()

        mock_int = MagicMock()
        mock_int.json.return_value = mp2_interests
        mock_int.raise_for_status = MagicMock()

        mock_api_get.side_effect = [mock_mp, mock_int]

        build_interests.build_seed(
            self.db_path, SCHEMA_PATH, mp_limit=2,
            checkpoint_db=self.db_path,  # Same path
        )

        c = sqlite3.connect(self.db_path)
        count = c.execute("SELECT COUNT(*) FROM interests").fetchone()[0]
        self.assertEqual(count, 2)  # 1 from checkpoint + 1 new
        c.close()


if __name__ == "__main__":
    unittest.main()
