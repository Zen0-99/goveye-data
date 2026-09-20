"""Unit tests for build_companies_house.py — CH officer matching, schema
writes, partial-DOB backfill. All HTTP mocked; no live API calls."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_companies_house as ch

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "bundled_schema.json",
)

MP = {"id": 5030, "name": "Dr Simon Opher", "constituency": "Stroud",
      "elected": "2024-07-04"}


def make_search_item(title, oid, dob=None, snippet=""):
    item = {
        "title": title,
        "address_snippet": snippet,
        "links": {"self": f"/officers/{oid}/appointments"},
    }
    if dob:
        item["date_of_birth"] = dob
    return item


def make_appt(company, number="00000001", role="director",
              appointed="2020-01-01", resigned=None, status="active",
              nationality="British", country="England"):
    return {
        "appointed_to": {"company_name": company,
                         "company_number": number,
                         "company_status": status},
        "officer_role": role,
        "appointed_on": appointed,
        "resigned_on": resigned,
        "nationality": nationality,
        "country_of_residence": country,
    }


class TestHelpers(unittest.TestCase):

    def test_officer_id_from_appointments_link(self):
        item = make_search_item("X", "abc123")
        self.assertEqual(ch.officer_id_of(item), "abc123")

    def test_officer_id_missing(self):
        self.assertIsNone(ch.officer_id_of({"links": {}}))
        self.assertIsNone(ch.officer_id_of({}))

    def test_plain_name_strips_titles(self):
        self.assertEqual(ch.plain_name("Dr Simon Opher"), "Simon Opher")
        self.assertEqual(ch.plain_name("Sir John Whittingdale"),
                         "John Whittingdale")
        self.assertEqual(ch.plain_name("Rt Hon Sir Keir Starmer"),
                         "Keir Starmer")
        self.assertEqual(ch.plain_name("Dame Meg Hillier"), "Meg Hillier")
        self.assertEqual(ch.plain_name("Joe Morris"), "Joe Morris")

    def test_normalise_company_strips_suffixes(self):
        n = ch.normalise_company("May Lane Surgery Limited")
        self.assertEqual(n, frozenset({"may", "lane", "surgery"}))
        self.assertIsNone(ch.normalise_company("Ltd"))
        self.assertIsNone(ch.normalise_company("!"))

    def test_companies_overlap_threshold_rounds_up(self):
        # 3-token company name needs ceil(0.6*3)=2 token hits — one stray
        # shared token must NOT match, two must.
        self.assertFalse(
            ch.companies_overlap("High Definition Installations Ltd",
                                 {frozenset({"high", "street"})}))
        self.assertTrue(
            ch.companies_overlap("High Definition Installations Ltd",
                                 {frozenset({"high", "definition",
                                             "other"})}))
        # 2-token name needs both tokens.
        self.assertFalse(ch.companies_overlap("High Definition Ltd",
                                              {frozenset({"high"})}))
        self.assertTrue(ch.companies_overlap("High Definition Ltd",
                                             {frozenset({"high",
                                                         "definition"})}))

    def test_companies_overlap_single_token_needs_full_hit(self):
        declared = {frozenset({"prema", "arts"})}
        self.assertTrue(ch.companies_overlap("PREMA", declared))
        self.assertFalse(ch.companies_overlap("PREMARKET", declared))


class TestScoring(unittest.TestCase):

    def test_declared_company_match(self):
        declared = {frozenset({"may", "lane", "surgery"})}
        appts = [make_appt("May Lane Surgery Limited")]
        score, reasons = ch.score_candidate(MP, {}, appts, declared)
        self.assertEqual(score, ch.W_INTEREST_MATCH)
        self.assertTrue(any(r.startswith("declared:") for r in reasons))

    def test_resignation_at_election(self):
        appts = [make_appt("Some Co", resigned="2024-08-01")]
        score, reasons = ch.score_candidate(MP, {}, appts, set())
        self.assertEqual(score, ch.W_RESIGN_AT_ELECTION)

    def test_resignation_outside_window_no_score(self):
        appts = [make_appt("Some Co", resigned="2020-01-01")]
        score, _ = ch.score_candidate(MP, {}, appts, set())
        self.assertEqual(score, 0)

    def test_region_match(self):
        item = make_search_item("X", "o1", snippet="Stroud, Gloucestershire")
        score, reasons = ch.score_candidate(MP, item, [], set())
        self.assertEqual(score, ch.W_REGION)


class TestMatchMp(unittest.TestCase):

    def _mp_search(self, items):
        return {"items": items, "total_results": len(items)}

    @patch.object(ch, "fetch_all_appointments")
    @patch.object(ch, "ch_get")
    def test_deterministic_accept(self, mock_get, mock_appts):
        mock_get.return_value = self._mp_search([
            make_search_item("Dr Simon OPHER", "real",
                             dob={"month": 1, "year": 1964}),
            make_search_item("Simon OTHER", "decoy",
                             dob={"month": 5, "year": 1980}),
        ])
        mock_appts.side_effect = lambda k, oid: (
            [make_appt("PREMA"), make_appt("X", resigned="2024-08-01")]
            if oid == "real" else [])
        declared = {frozenset({"prema"})}
        officer, appts, method, conf = ch.match_mp(
            MP, declared, "k", "j", {})
        self.assertEqual(officer["title"], "Dr Simon OPHER")
        self.assertEqual(method, "interests")
        self.assertGreater(conf, 0)

    @patch.object(ch, "fetch_all_appointments")
    @patch.object(ch, "ch_get")
    def test_no_match_below_threshold(self, mock_get, mock_appts):
        mock_get.return_value = self._mp_search([
            make_search_item("Simon OPHER", "c1", dob={"month": 1,
                                                       "year": 1964}),
        ])
        mock_appts.return_value = []
        officer, _, method, _ = ch.match_mp(MP, set(), "k", "j", {})
        self.assertIsNone(officer)
        self.assertEqual(method, "none")

    @patch.object(ch, "fetch_all_appointments")
    @patch.object(ch, "ch_get")
    def test_ambiguous_without_jev_key_is_none(self, mock_get, mock_appts):
        # Two candidates each scoring 2 (resignation timing) — top >=
        # JEV_MIN_SCORE but no clear margin, and no Jev key -> none.
        mock_get.return_value = self._mp_search([
            make_search_item("Simon OPHER A", "c1"),
            make_search_item("Simon OPHER B", "c2"),
        ])
        mock_appts.return_value = [make_appt("Y", resigned="2024-07-20")]
        officer, _, method, _ = ch.match_mp(MP, set(), "k", None, {})
        self.assertIsNone(officer)
        self.assertEqual(method, "none")

    @patch.object(ch, "jev_choice", return_value=("candidate_0", 0.9))
    @patch.object(ch, "fetch_all_appointments")
    @patch.object(ch, "ch_get")
    def test_jev_resolves_ambiguity(self, mock_get, mock_appts, mock_jev):
        mock_get.return_value = self._mp_search([
            make_search_item("Simon OPHER A", "c1"),
            make_search_item("Simon OPHER B", "c2"),
        ])
        mock_appts.return_value = [make_appt("Y", resigned="2024-07-20")]
        officer, _, method, conf = ch.match_mp(MP, set(), "k", "j", {})
        self.assertEqual(method, "jev")
        self.assertEqual(conf, 0.9)
        self.assertEqual(officer["title"], "Simon OPHER A")

    @patch.object(ch, "jev_choice", return_value=("none", 0.95))
    @patch.object(ch, "fetch_all_appointments")
    @patch.object(ch, "ch_get")
    def test_jev_none_means_no_match(self, mock_get, mock_appts, mock_jev):
        mock_get.return_value = self._mp_search([
            make_search_item("Simon OPHER A", "c1"),
            make_search_item("Simon OPHER B", "c2"),
        ])
        mock_appts.return_value = [make_appt("Y", resigned="2024-07-20")]
        officer, _, method, _ = ch.match_mp(MP, set(), "k", "j", {})
        self.assertIsNone(officer)
        self.assertEqual(method, "none")

    @patch.object(ch, "fetch_all_appointments")
    @patch.object(ch, "ch_get")
    def test_corporate_officers_skipped(self, mock_get, mock_appts):
        mock_get.return_value = self._mp_search([
            make_search_item("SIMON OPHER LIMITED", "corp"),
            make_search_item("Dr Simon OPHER", "real",
                             dob={"month": 1, "year": 1964}),
        ])
        mock_appts.return_value = []
        ch.match_mp(MP, set(), "k", None, {})
        fetched_ids = [c.args[1] for c in mock_appts.call_args_list]
        self.assertNotIn("corp", fetched_ids)
        self.assertIn("real", fetched_ids)


class TestDbWrites(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = schema_module.create_database_with_tables(
            self.tmp.name, SCHEMA_PATH, ch.TABLE_NAMES)
        ch.ensure_processed_table(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_upsert_identity_and_appointments(self):
        officer = make_search_item("Dr Simon OPHER", "oid",
                                   dob={"month": 1, "year": 1964})
        appts = [make_appt("PREMA", number="111",
                           nationality="British", country="England"),
                 make_appt("MAY LANE SURGERY LIMITED", number="222",
                           resigned="2024-08-08")]
        with patch.object(ch, "fetch_company",
                          return_value={"company_status": "active",
                                        "type": "ltd",
                                        "sic_codes": ["85520"],
                                        "date_of_creation": "1991-03-06"}), \
             patch.object(ch, "fetch_psc_natures",
                          return_value=["ownership-of-shares-25-to-50"]):
            ch.upsert_identity(self.conn, 5030, officer, "oid", appts,
                               "interests", 0.83, None, 1234)
            n = ch.upsert_appointments(self.conn, 5030, "oid", appts,
                                       "k", {}, {}, 1234)
        self.assertEqual(n, 2)
        row = self.conn.execute(
            "SELECT officerId, dobMonth, dobYear, nationality, "
            "countryOfResidence, matchMethod, isDisqualified "
            "FROM mp_officer_identity WHERE mpId=5030").fetchone()
        self.assertEqual(row, ("oid", 1, 1964, "British", "England",
                               "interests", 0))
        appt = self.conn.execute(
            "SELECT companyNumber, isCurrent, companySicCodes, pscNatures "
            "FROM mp_appointments WHERE companyNumber='111'").fetchone()
        self.assertEqual(appt[0], "111")
        self.assertEqual(appt[1], 1)
        self.assertEqual(json.loads(appt[2]), ["85520"])
        self.assertIn("ownership-of-shares", appt[3])

    def test_appointments_replaced_on_rematch(self):
        officer = make_search_item("X", "oid")
        appts = [make_appt("A Ltd", number="1")]
        with patch.object(ch, "fetch_company", return_value=None), \
             patch.object(ch, "fetch_psc_natures", return_value=[]):
            ch.upsert_appointments(self.conn, 1, "oid", appts, "k", {}, {}, 1)
            ch.upsert_appointments(self.conn, 1, "oid",
                                   [make_appt("B Ltd", number="2")],
                                   "k", {}, {}, 2)
        rows = self.conn.execute(
            "SELECT companyNumber FROM mp_appointments WHERE mpId=1"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], ["2"])

    def test_checkpoint_marks_processed(self):
        ch.ensure_processed_table(self.conn)
        self.conn.execute(
            f"INSERT INTO {ch.PROCESSED_TABLE} (mpId, matched, timestamp) "
            "VALUES (5030, 1, 0)")
        self.assertEqual(ch.get_processed_ids(self.conn), {5030})


class TestDobBackfill(unittest.TestCase):

    def _mk_bio(self, rows):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        c = sqlite3.connect(f.name)
        c.execute("CREATE TABLE bio_data (mpId INTEGER PRIMARY KEY, "
                  "dateOfBirth TEXT)")
        c.executemany("INSERT INTO bio_data VALUES (?,?)", rows)
        c.commit()
        c.close()
        return f.name

    def test_backfill_fills_nulls_only(self):
        ch_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        ch_db.close()
        c = sqlite3.connect(ch_db.name)
        c.execute("CREATE TABLE mp_officer_identity "
                  "(mpId INTEGER PRIMARY KEY, dobMonth INT, dobYear INT)")
        c.executemany("INSERT INTO mp_officer_identity VALUES (?,?,?)",
                      [(1, 3, 1970), (2, None, None), (3, 7, 1965)])
        c.commit()
        c.close()

        bio = self._mk_bio([(1, None), (2, None), (3, "1965-07-15")])
        try:
            ch.backfill_bio_from_ch(ch_db.name, bio)
            conn = sqlite3.connect(bio)
            got = dict(conn.execute(
                "SELECT mpId, dateOfBirth FROM bio_data").fetchall())
            conn.close()
            self.assertEqual(got[1], "1970-03")
            self.assertIsNone(got[2])          # no CH dob -> untouched
            self.assertEqual(got[3], "1965-07-15")  # full date preserved
        finally:
            os.unlink(ch_db.name)
            os.unlink(bio)


if __name__ == "__main__":
    unittest.main()
