"""Unit tests for build_edms.py — EDM list/detail mapping, scope filter,
sponsor refresh, delta change detection, checkpoint bookkeeping.
All HTTP mocked; no live API calls."""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema as schema_module
import build_edms as edm

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "bundled_schema.json",
)

TS = 1700000000000


def make_list_item(edm_id, date_tabled="2025-01-15T00:00:00", status=0,
                   status_date="2025-01-15T09:00:00", sponsors_count=3,
                   mnis_id=4514, member_id=4514, uin=42):
    return {
        "Id": edm_id,
        "Status": status,
        "StatusDate": status_date,
        "MemberId": member_id,
        "PrimarySponsor": {"MnisId": mnis_id, "Name": "Test Member"},
        "Title": f"EDM {edm_id}",
        "MotionText": "That this House ...",
        "AmendmentToMotionId": None,
        "UIN": uin,
        "AmendmentSuffix": None,
        "DateTabled": date_tabled,
        "PrayingAgainstNegativeStatutoryInstrumentId": None,
        "StatutoryInstrumentNumber": None,
        "StatutoryInstrumentYear": None,
        "StatutoryInstrumentTitle": None,
        "UINWithAmendmentSuffix": str(uin),
        "SponsorsCount": sponsors_count,
    }


def make_sponsor(member_id, order=1, withdrawn=False,
                 created="2025-01-16T10:00:00", withdrawn_date=None):
    return {
        "Id": (member_id or 0) * 100,
        "MemberId": member_id,
        "Member": {"MnisId": member_id},
        "SponsoringOrder": order,
        "CreatedWhen": created,
        "IsWithdrawn": withdrawn,
        "WithdrawnDate": withdrawn_date,
    }


def make_detail(sponsors):
    return {"Sponsors": sponsors, "Amendments": []}


def make_edm_db(path):
    """Create an EDM per-API DB file (schema tables + _edm_processed)."""
    conn = schema_module.create_database_with_tables(
        path, SCHEMA_PATH, edm.TABLE_NAMES)
    edm.ensure_processed_table(conn)
    return conn


def insert_motion(conn, edm_id, status=0, status_date="2025-01-15T09:00:00",
                  sponsors_count=3, date_tabled="2025-01-15T00:00:00"):
    conn.execute(
        "INSERT OR REPLACE INTO early_day_motions "
        "(edmId, uin, uinDisplay, title, motionText, dateTabled, "
        "statusDate, status, primarySponsorMemberId, sponsorsCount, "
        "amendmentToMotionId, prayingAgainstSiId, siNumber, siYear, "
        "siTitle, lastUpdated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (edm_id, 1, "1", "t", "m", date_tabled, status_date, status,
         4514, sponsors_count, None, None, None, None, None, TS))


class TestMappers(unittest.TestCase):

    def test_map_edm_fields(self):
        item = make_list_item(100, mnis_id=4514, member_id=4514, uin=7)
        row = edm.map_edm(item, TS)
        (edm_id, uin, uin_display, title, motion_text, date_tabled,
         status_date, status, ps_id, sponsors_count, amend_to,
         praying_si, si_num, si_year, si_title, last_updated) = row
        self.assertEqual(edm_id, 100)
        self.assertEqual(uin, 7)
        self.assertEqual(uin_display, "7")
        self.assertEqual(title, "EDM 100")
        self.assertEqual(date_tabled, "2025-01-15T00:00:00")
        self.assertEqual(status, 0)
        self.assertEqual(ps_id, 4514)
        self.assertEqual(sponsors_count, 3)
        self.assertIsNone(amend_to)
        self.assertIsNone(praying_si)
        self.assertIsNone(si_num)
        self.assertIsNone(si_year)
        self.assertIsNone(si_title)
        self.assertEqual(last_updated, TS)

    def test_map_edm_memberid_fallback(self):
        item = make_list_item(101)
        item["PrimarySponsor"] = None
        row = edm.map_edm(item, TS)
        self.assertEqual(row[8], 4514)  # falls back to MemberId

    def test_map_sponsor_fields(self):
        s = make_sponsor(4514, order=2, created="2025-01-16T10:00:00")
        row = edm.map_sponsor(100, s, TS)
        self.assertEqual(
            row, (100, 4514, 2, "2025-01-16T10:00:00", 0, None, TS))

    def test_map_sponsor_withdrawn(self):
        s = make_sponsor(4514, order=None, withdrawn=True,
                         withdrawn_date="2025-02-01T12:00:00")
        row = edm.map_sponsor(100, s, TS)
        self.assertEqual(row[2], None)           # sponsoringOrder
        self.assertEqual(row[4], 1)              # isWithdrawn
        self.assertEqual(row[5], "2025-02-01T12:00:00")

    def test_map_sponsor_null_member_skipped(self):
        s = make_sponsor(None)
        self.assertIsNone(edm.map_sponsor(100, s, TS))


class TestScopeFilter(unittest.TestCase):

    def _items_to_ids(self, caps, pages):
        """Drive iter_in_scope_list_items with canned pages."""
        def fake_page(skip, take, date_param=None):
            return pages.get(skip, [])
        with patch.object(edm, "fetch_edm_list_page",
                          side_effect=fake_page):
            return [i["Id"] for i in edm.iter_in_scope_list_items(caps)]

    def test_sorted_desc_early_exit(self):
        caps = {"date_param": None, "sorted_desc": True}
        pages = {0: [make_list_item(1),
                     make_list_item(2, date_tabled="2024-07-03T00:00:00"),
                     make_list_item(3)]}
        # Stops at the out-of-scope item — item 3 never reached.
        self.assertEqual(self._items_to_ids(caps, pages), [1])

    def test_unsorted_scans_all_pages(self):
        caps = {"date_param": None, "sorted_desc": False}
        page = [make_list_item(1, date_tabled="2024-07-03T00:00:00")]
        page += [make_list_item(100 + i) for i in range(99)]
        pages = {0: page}  # full page → continues, next page empty
        ids = self._items_to_ids(caps, pages)
        self.assertEqual(len(ids), 99)
        self.assertNotIn(1, ids)

    def test_date_param_still_filters_client_side(self):
        caps = {"date_param": "parameters.tabledStartDate",
                "sorted_desc": True}
        pages = {0: [make_list_item(1),
                     make_list_item(2, date_tabled="2024-07-03T00:00:00")]}
        self.assertEqual(self._items_to_ids(caps, pages), [1])


class TestSponsorRefresh(unittest.TestCase):

    def test_refresh_replaces_only_target_edm(self):
        conn = sqlite3.connect(":memory:")
        edm.ensure_edm_tables(conn)
        edm.insert_sponsors(conn, [
            (1, 10, 1, "a", 0, None, TS),
            (1, 11, 2, "b", 0, None, TS),
            (2, 20, 1, "c", 0, None, TS),
        ])
        conn.commit()

        n = edm.refresh_sponsors_for_edm(
            conn, 1, [make_sponsor(30), make_sponsor(31, withdrawn=True,
                      withdrawn_date="2025-02-01T00:00:00")], TS + 1)
        self.assertEqual(n, 2)

        edm1 = conn.execute(
            "SELECT memberId, isWithdrawn FROM edm_sponsors WHERE edmId=1 "
            "ORDER BY memberId").fetchall()
        self.assertEqual(edm1, [(30, 0), (31, 1)])
        edm2 = conn.execute(
            "SELECT memberId FROM edm_sponsors WHERE edmId=2").fetchall()
        self.assertEqual(edm2, [(20,)])
        conn.close()

    def test_sponsor_pk_roundtrip_nonnull(self):
        conn = sqlite3.connect(":memory:")
        edm.ensure_edm_tables(conn)
        edm.refresh_sponsors_for_edm(
            conn, 5, [make_sponsor(10), make_sponsor(None), make_sponsor(12)],
            TS)
        rows = conn.execute(
            "SELECT edmId, memberId FROM edm_sponsors ORDER BY memberId"
        ).fetchall()
        self.assertEqual(rows, [(5, 10), (5, 12)])  # null memberId skipped
        self.assertTrue(all(r[0] is not None and r[1] is not None
                            for r in rows))
        conn.close()


class TestProcessedTable(unittest.TestCase):

    def test_get_processed_ids(self):
        conn = sqlite3.connect(":memory:")
        edm.ensure_processed_table(conn)
        self.assertEqual(edm.get_processed_ids(conn), set())
        edm.mark_processed(conn, 7, has_sponsors=True, timestamp=TS)
        edm.mark_processed(conn, 9, has_sponsors=False, timestamp=TS)
        self.assertEqual(edm.get_processed_ids(conn), {7, 9})
        conn.close()


class TestSeedCheckpointResume(unittest.TestCase):

    def test_checkpoint_skips_processed_edms(self):
        with tempfile.TemporaryDirectory() as td:
            ckpt = os.path.join(td, "ckpt.db")
            out = os.path.join(td, "out.db")

            conn = make_edm_db(ckpt)
            insert_motion(conn, 1)
            edm.mark_processed(conn, 1, has_sponsors=True, timestamp=TS)
            conn.commit()
            conn.close()

            pages = {0: [make_list_item(1), make_list_item(2)]}
            detail_calls = []

            def fake_page(skip, take, date_param=None):
                return pages.get(skip, [])

            def fake_detail(edm_id):
                detail_calls.append(edm_id)
                return make_detail([make_sponsor(10)])

            with patch.object(edm, "probe_list_capabilities",
                              return_value={"date_param": "x",
                                            "sorted_desc": True}), \
                 patch.object(edm, "fetch_edm_list_page",
                              side_effect=fake_page), \
                 patch.object(edm, "fetch_edm_detail",
                              side_effect=fake_detail):
                edm.build_seed(out, SCHEMA_PATH, checkpoint_db=ckpt)

            self.assertEqual(detail_calls, [2])  # edm 1 skipped via checkpoint
            conn = sqlite3.connect(out)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM early_day_motions")
                .fetchone()[0], 2)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM edm_sponsors")
                .fetchone()[0], 1)
            conn.close()

    def test_failed_detail_left_unprocessed(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.db")
            pages = {0: [make_list_item(1)]}

            with patch.object(edm, "probe_list_capabilities",
                              return_value={"date_param": "x",
                                            "sorted_desc": True}), \
                 patch.object(edm, "fetch_edm_list_page",
                              side_effect=lambda s, t, date_param=None:
                              pages.get(s, [])), \
                 patch.object(edm, "fetch_edm_detail", return_value=None):
                edm.build_seed(out, SCHEMA_PATH)

            conn = sqlite3.connect(out)
            # Motion row exists, but edmId is NOT marked processed.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM early_day_motions")
                .fetchone()[0], 1)
            self.assertEqual(edm.get_processed_ids(conn), set())
            conn.close()


class TestDeltaDetection(unittest.TestCase):

    def _prev_db(self, path):
        conn = make_edm_db(path)
        insert_motion(conn, 1, sponsors_count=2)
        insert_motion(conn, 2, sponsors_count=2)
        insert_motion(conn, 3, sponsors_count=1)  # will go absent
        for eid in (1, 2, 3):
            edm.mark_processed(conn, eid, has_sponsors=True, timestamp=TS)
        conn.execute(
            "INSERT INTO edm_sponsors VALUES (1,10,1,'a',0,NULL,?)", (TS,))
        conn.execute(
            "INSERT INTO edm_sponsors VALUES (3,30,1,'a',0,NULL,?)", (TS,))
        conn.commit()
        conn.close()

    def test_delta_refetches_only_changed(self):
        with tempfile.TemporaryDirectory() as td:
            prev = os.path.join(td, "prev.db")
            out = os.path.join(td, "out.db")
            self._prev_db(prev)

            # edm 2's sponsorsCount changed 2→3; edm 3 absent from list.
            items = [make_list_item(1, sponsors_count=2),
                     make_list_item(2, sponsors_count=3)]
            detail_calls = []

            def fake_page(skip, take, date_param=None):
                return items if skip == 0 else []

            def fake_detail(edm_id):
                detail_calls.append(edm_id)
                return make_detail([make_sponsor(10), make_sponsor(11)])

            with patch.object(edm, "probe_list_capabilities",
                              return_value={"date_param": "x",
                                            "sorted_desc": True}), \
                 patch.object(edm, "fetch_edm_list_page",
                              side_effect=fake_page), \
                 patch.object(edm, "fetch_edm_detail",
                              side_effect=fake_detail):
                edm.build_delta(out, prev, SCHEMA_PATH)

            self.assertEqual(detail_calls, [2])  # unchanged edm 1 skipped
            conn = sqlite3.connect(out)
            # edm 3 was in-scope in the DB but absent from the list → deleted
            self.assertEqual(
                conn.execute("SELECT edmId FROM early_day_motions "
                             "ORDER BY edmId").fetchall(), [(1,), (2,)])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM edm_sponsors WHERE edmId=3")
                .fetchone()[0], 0)
            # edm 2's sponsors refreshed
            self.assertEqual(
                conn.execute("SELECT memberId FROM edm_sponsors WHERE edmId=2 "
                             "ORDER BY memberId").fetchall(), [(10,), (11,)])
            conn.close()

    def test_delta_refetches_unprocessed(self):
        """An unchanged EDM absent from _edm_processed still gets a detail
        call (heals the silent-sponsor hole)."""
        with tempfile.TemporaryDirectory() as td:
            prev = os.path.join(td, "prev.db")
            out = os.path.join(td, "out.db")

            conn = make_edm_db(prev)
            insert_motion(conn, 1, sponsors_count=2)
            insert_motion(conn, 2, sponsors_count=2)
            edm.mark_processed(conn, 1, has_sponsors=True, timestamp=TS)
            # edm 2 deliberately NOT marked processed
            conn.commit()
            conn.close()

            items = [make_list_item(1, sponsors_count=2),
                     make_list_item(2, sponsors_count=2)]
            detail_calls = []

            with patch.object(edm, "probe_list_capabilities",
                              return_value={"date_param": "x",
                                            "sorted_desc": True}), \
                 patch.object(edm, "fetch_edm_list_page",
                              side_effect=lambda s, t, date_param=None:
                              items if s == 0 else []), \
                 patch.object(edm, "fetch_edm_detail",
                              side_effect=lambda eid: (
                                  detail_calls.append(eid),
                                  make_detail([make_sponsor(10)]))[1]):
                edm.build_delta(out, prev, SCHEMA_PATH)

            self.assertEqual(detail_calls, [2])

    def test_resweep_refetches_everything(self):
        with tempfile.TemporaryDirectory() as td:
            prev = os.path.join(td, "prev.db")
            out = os.path.join(td, "out.db")

            conn = make_edm_db(prev)
            insert_motion(conn, 1, sponsors_count=2)
            insert_motion(conn, 2, sponsors_count=2)
            for eid in (1, 2):
                edm.mark_processed(conn, eid, has_sponsors=True,
                                   timestamp=TS)
            conn.commit()
            conn.close()

            items = [make_list_item(1, sponsors_count=2),
                     make_list_item(2, sponsors_count=2)]
            detail_calls = []

            with patch.object(edm, "probe_list_capabilities",
                              return_value={"date_param": "x",
                                            "sorted_desc": True}), \
                 patch.object(edm, "fetch_edm_list_page",
                              side_effect=lambda s, t, date_param=None:
                              items if s == 0 else []), \
                 patch.object(edm, "fetch_edm_detail",
                              side_effect=lambda eid: (
                                  detail_calls.append(eid),
                                  make_detail([]))[1]):
                edm.build_delta(out, prev, SCHEMA_PATH,
                                resweep_details=True)

            self.assertEqual(sorted(detail_calls), [1, 2])


class TestSummaries(unittest.TestCase):

    def test_load_existing_edm_summaries(self):
        conn = sqlite3.connect(":memory:")
        edm.ensure_edm_tables(conn)
        insert_motion(conn, 1, status=0,
                      status_date="2025-01-15T09:00:00", sponsors_count=5)
        insert_motion(conn, 2, status=1,
                      status_date="2025-02-01T00:00:00", sponsors_count=1)
        conn.commit()
        sums = edm.load_existing_edm_summaries(conn)
        self.assertEqual(sums[1], (0, "2025-01-15T09:00:00", 5))
        self.assertEqual(sums[2], (1, "2025-02-01T00:00:00", 1))
        conn.close()


if __name__ == "__main__":
    unittest.main()
