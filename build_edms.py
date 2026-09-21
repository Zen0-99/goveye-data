#!/usr/bin/env python3
"""Per-API build script for Early Day Motions (early_day_motions + edm_sponsors).

Fetches from the Oral Questions & Motions API (unauthenticated — no key):

  GET /EarlyDayMotions/list   — one item per EDM: Id, Status, StatusDate,
      MemberId, PrimarySponsor{MnisId,...}, Title, MotionText,
      AmendmentToMotionId, UIN, AmendmentSuffix, DateTabled,
      PrayingAgainstNegativeStatutoryInstrumentId,
      StatutoryInstrumentNumber/Year/Title, UINWithAmendmentSuffix,
      SponsorsCount. Envelope: {"PagingInfo": {...}, "Response": [items]}.
  GET /EarlyDayMotion/{id}    — adds Sponsors[] = {Id, MemberId, Member,
      SponsoringOrder, CreatedWhen, IsWithdrawn, WithdrawnDate} and
      Amendments[] (not stored — only amendmentToMotionId linkage).

Scope (D-02): this parliament only — DateTabled >= PARLIAMENT_START
("2024-07-04"). Widening to earlier parliaments is a one-line change to
PARLIAMENT_START (D-02 reversibility).

Live-probe findings (2026-09-22) that shape behaviour:

  * `parameters.skip`/`parameters.take` page the list; take is capped at
    100 server-side (ask 500, get 100).
  * `parameters.tabledStartDate=YYYY-MM-DD` is an accepted server-side
    date filter (total drops 61,185 -> ~4,086 for 2024-07-04). The
    `tabledFrom`/`dateTabledFrom`/`tabledWhenFrom` variants are silently
    ignored. `probe_list_capabilities()` detects this at build start.
  * The list is DateTabled-descending — if no date param is accepted,
    paging can stop at the first out-of-scope item.
  * Withdrawn motions (Status=1) REMAIN in the list output — so a motion
    absent from a fresh list is genuinely removed, and the delta emits
    deletes for absent in-scope edmIds (REMOVE_ABSENT_EDMS).
  * Withdrawal blind spot: list `SponsorsCount` INCLUDES withdrawn
    signatories, and a sponsor withdrawing signature moves NEITHER
    `Status`/`StatusDate` NOR `SponsorsCount` (verified: member 4786
    withdrew from many EDMs on 2026-04-20 leaving StatusDate months
    stale). A lone withdrawal on an otherwise-unchanged EDM is invisible
    to the (status, statusDate, sponsorsCount) change detector —
    stale isWithdrawn=0 rows persist until a `--resweep-details` run
    (delta-only opt-in, ~4k detail calls, seed-like runtime; bound the
    drift monthly-ish via workflow_dispatch).

Checkpointing: `_edm_processed(edmId, hasSponsors, timestamp)` records
edmIds whose detail call COMPLETED successfully — drives seed resume and
lets delta skip unchanged EDMs. An edmId is marked ONLY after a non-None
detail response was written; a failed detail leaves it unmarked so a
resume or the next delta retries it. Pipeline-only table — never merged
or diffed (same pattern as _ch_processed in build_companies_house.py).

Modes:
  seed  — create fresh DB, fetch all in-scope EDMs + their sponsors
          (checkpoint-resumable via --checkpoint-db)
  delta — copy previous DB, upsert every in-scope motion row, refetch
          details only for new/changed/unprocessed EDMs (or all with
          --resweep-details), delete absent in-scope motions

Usage:
  python build_edms.py --output edms.db --schema schemas/bundled_schema.json \
      --mode seed
  python build_edms.py --output edms.db --schema schemas/bundled_schema.json \
      --mode seed --checkpoint-db edms.db          # resume a failed seed
  python build_edms.py --output edms.db --schema schemas/bundled_schema.json \
      --mode delta --previous-db prev_edms.db
  python build_edms.py --output edms.db --schema schemas/bundled_schema.json \
      --mode delta --previous-db prev_edms.db --resweep-details
  python build_edms.py --output edms_test.db \
      --schema schemas/bundled_schema.json --mode seed --edm-limit 15
"""

import argparse
import os
import shutil
import sqlite3
import time

import requests

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

EDM_BASE = "https://oralquestionsandmotions-api.parliament.uk"
LIST_URL = f"{EDM_BASE}/EarlyDayMotions/list"
DETAIL_URL = f"{EDM_BASE}/EarlyDayMotion"          # /{id}
PARLIAMENT_START = "2024-07-04"                     # D-02 — this parliament only
LIST_PAGE_SIZE = 100                                # probed: take caps at 100

TABLE_NAMES = ["early_day_motions", "edm_sponsors"]

# Pipeline-only bookkeeping (never merged/diffed — see _ch_processed)
PROCESSED_TABLE = "_edm_processed"

# Candidate server-side date filters, tried in order by
# probe_list_capabilities(). Verified live 2026-09-22: only
# parameters.tabledStartDate is honoured (the others return the
# unfiltered total).
DATE_PARAM_CANDIDATES = [
    "parameters.tabledStartDate",
    "parameters.dateTabledFrom",
    "parameters.tabledFrom",
    "parameters.tabledWhenFrom",
]

# Probe result (2026-09-22): withdrawn motions (Status=1) REMAIN in the
# list output — 31 Status=1 EDMs present in the in-scope listing. An
# in-scope edmId absent from a fresh list is therefore a genuine removal,
# and the delta may emit deletes for it. If this ever changes (withdrawn
# motions silently vanish from the list), set this to False — deleting
# on absence would then wipe withdrawn history from devices, and
# keep-withdrawn-as-facts must win (a truly-removed motion would linger
# until the next seed rebuild).
REMOVE_ABSENT_EDMS = True


# --- Fallback DDL (byte-for-byte from Room 38.json createSql) ---

# Protects builds against a stale bundled_schema.json — same pattern as
# ensure_mp_career_events_table in build_member_details.py. Column order,
# affinities, PKs and index names must match Room exactly or
# validate_schema.py's TableInfo parity check fails.
EARLY_DAY_MOTIONS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS early_day_motions (
    `edmId` INTEGER NOT NULL,
    `uin` INTEGER,
    `uinDisplay` TEXT,
    `title` TEXT,
    `motionText` TEXT,
    `dateTabled` TEXT,
    `statusDate` TEXT,
    `status` INTEGER,
    `primarySponsorMemberId` INTEGER,
    `sponsorsCount` INTEGER,
    `amendmentToMotionId` INTEGER,
    `prayingAgainstSiId` INTEGER,
    `siNumber` INTEGER,
    `siYear` TEXT,
    `siTitle` TEXT,
    `lastUpdated` INTEGER NOT NULL,
    PRIMARY KEY(`edmId`)
)
"""

EARLY_DAY_MOTIONS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS `index_early_day_motions_primarySponsorMemberId` "
    "ON `early_day_motions` (`primarySponsorMemberId`)"
)

EDM_SPONSORS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS edm_sponsors (
    `edmId` INTEGER NOT NULL,
    `memberId` INTEGER NOT NULL,
    `sponsoringOrder` INTEGER,
    `signedAt` TEXT,
    `isWithdrawn` INTEGER NOT NULL,
    `withdrawnDate` TEXT,
    `lastUpdated` INTEGER NOT NULL,
    PRIMARY KEY(`edmId`, `memberId`)
)
"""

EDM_SPONSORS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS `index_edm_sponsors_memberId` "
    "ON `edm_sponsors` (`memberId`)"
)


def ensure_edm_tables(conn):
    """Create both EDM tables + indices idempotently (stale-schema fallback)."""
    conn.execute(EARLY_DAY_MOTIONS_CREATE_SQL)
    conn.execute(EARLY_DAY_MOTIONS_INDEX_SQL)
    conn.execute(EDM_SPONSORS_CREATE_SQL)
    conn.execute(EDM_SPONSORS_INDEX_SQL)
    conn.commit()


# --- HTTP fetchers ---

def fetch_edm_list_page(skip, take, date_param=None):
    """GET /EarlyDayMotions/list. Returns the raw item list.

    OQM paging convention: params keys are 'parameters.skip' /
    'parameters.take' (verified live — parameters.take works, capped at
    100). date_param, when provided, is the accepted filter key found by
    probe_list_capabilities() (e.g. 'parameters.tabledStartDate').
    """
    params = {"parameters.skip": skip, "parameters.take": take}
    if date_param:
        params[date_param] = PARLIAMENT_START
    r = api_get(LIST_URL, params=params, timeout=60)
    time.sleep(API_DELAY)
    data = r.json()
    return data.get("Response") or []


def fetch_edm_detail(edm_id):
    """GET /EarlyDayMotion/{id} → dict with Sponsors[]/Amendments[].
    Returns None on failure (logged warning) — callers must NOT mark the
    edmId processed, so a retry happens on resume/next delta."""
    try:
        r = api_get(f"{DETAIL_URL}/{edm_id}", timeout=60)
    except requests.exceptions.RequestException as e:
        logger.warning("EDM %s detail fetch failed: %s", edm_id, e)
        return None
    time.sleep(API_DELAY)
    try:
        data = r.json()
    except ValueError as e:
        logger.warning("EDM %s detail returned non-JSON: %s", edm_id, e)
        return None
    detail = data.get("Response")
    if not data.get("Success", True) or detail is None:
        logger.warning("EDM %s detail: Success=%s errors=%s",
                       edm_id, data.get("Success"), data.get("Errors"))
        return None
    return detail


# --- List capability probe ---

def _list_page(params):
    """One list call for the probe. Returns (PagingInfo.Total, items)."""
    r = api_get(LIST_URL, params=params, timeout=60)
    time.sleep(API_DELAY)
    data = r.json()
    return (data.get("PagingInfo") or {}).get("Total"), \
        (data.get("Response") or [])


def probe_list_capabilities():
    """Probe the list endpoint once at build start (logged).

    Detects:
      * a working server-side date filter — a candidate param is
        "accepted" when sending it shrinks PagingInfo.Total below the
        unfiltered total;
      * whether the list is DateTabled-descending (enables early exit
        when no date param works).

    Returns {"date_param": str|None, "sorted_desc": bool,
             "total": int|None, "filtered_total": int|None}.
    The cheapest correct fetch path is then:
      date_param accepted → pass it, client filter is belt-and-braces
      sorted_desc         → stop paging at first out-of-scope item
      neither             → page all ~61k, filter client-side (list
                            calls only — still cheap)
    """
    caps = {"date_param": None, "sorted_desc": False,
            "total": None, "filtered_total": None}
    try:
        total_all, items = _list_page({"parameters.take": LIST_PAGE_SIZE})
        caps["total"] = total_all
        dates = [(i.get("DateTabled") or "")[:10] for i in items]
        caps["sorted_desc"] = len(dates) > 1 and all(
            dates[i] >= dates[i + 1] for i in range(len(dates) - 1))
    except Exception as e:
        logger.warning("List probe failed (%s) — assuming no date param "
                       "and unknown ordering", e)
        return caps

    for cand in DATE_PARAM_CANDIDATES:
        try:
            tot, _ = _list_page(
                {"parameters.take": 5, cand: PARLIAMENT_START})
        except Exception as e:
            logger.warning("Date-param probe %s failed: %s", cand, e)
            continue
        if isinstance(tot, int) and isinstance(total_all, int) \
                and tot < total_all:
            caps["date_param"] = cand
            caps["filtered_total"] = tot
            break

    logger.info(
        "List probe: date_param=%s sorted_desc=%s total=%s filtered_total=%s",
        caps["date_param"], caps["sorted_desc"], caps["total"],
        caps["filtered_total"])
    return caps


def iter_in_scope_list_items(caps):
    """Yield raw list items with DateTabled >= PARLIAMENT_START.

    Fetch path per probe result: server-side date param when accepted,
    early exit at the first out-of-scope item when the list is
    DateTabled-descending, otherwise full pagination with client-side
    filtering.
    """
    date_param = caps.get("date_param")
    sorted_desc = caps.get("sorted_desc")
    skip = 0
    while True:
        items = fetch_edm_list_page(skip, LIST_PAGE_SIZE,
                                    date_param=date_param)
        if not items:
            break
        for item in items:
            dt = (item.get("DateTabled") or "")[:10]
            if dt < PARLIAMENT_START:
                if date_param or sorted_desc:
                    # Server filtered (shouldn't happen) or ordering
                    # guarantees nothing older follows — stop paging.
                    return
                continue  # unknown ordering — keep scanning
            yield item
        if len(items) < LIST_PAGE_SIZE:
            break
        skip += LIST_PAGE_SIZE


# --- Mappers (column order matches the INSERT statements) ---

def map_edm(dto, timestamp_millis):
    """Map a list item to an early_day_motions row tuple.

    primarySponsorMemberId: prefer PrimarySponsor.MnisId, fall back to
    MemberId (verified equal on probe — warns if they ever disagree).
    """
    ps = dto.get("PrimarySponsor") or {}
    member_id = dto.get("MemberId")
    sponsor_id = ps.get("MnisId") or member_id
    if ps.get("MnisId") is not None and member_id is not None \
            and ps["MnisId"] != member_id:
        logger.warning("EDM %s: PrimarySponsor.MnisId %s != MemberId %s "
                       "— using MnisId", dto.get("Id"), ps["MnisId"],
                       member_id)
    return (
        dto.get("Id"),                                     # edmId
        dto.get("UIN"),                                    # uin (int)
        dto.get("UINWithAmendmentSuffix"),                 # uinDisplay
        dto.get("Title"),                                  # title
        dto.get("MotionText"),                             # motionText
        dto.get("DateTabled"),                             # dateTabled
        dto.get("StatusDate"),                             # statusDate
        dto.get("Status"),                                 # status (int enum)
        sponsor_id,                                        # primarySponsorMemberId
        dto.get("SponsorsCount"),                          # sponsorsCount
        dto.get("AmendmentToMotionId"),                    # amendmentToMotionId
        dto.get("PrayingAgainstNegativeStatutoryInstrumentId"),  # prayingAgainstSiId
        dto.get("StatutoryInstrumentNumber"),              # siNumber
        dto.get("StatutoryInstrumentYear"),                # siYear (string)
        dto.get("StatutoryInstrumentTitle"),               # siTitle
        timestamp_millis,                                  # lastUpdated
    )


def map_sponsor(edm_id, sponsor, timestamp_millis):
    """Map a detail Sponsors[] entry to an edm_sponsors row tuple.

    (edmId, memberId, sponsoringOrder, signedAt, isWithdrawn,
     withdrawnDate, lastUpdated)

    memberId None → returns None (caller skips + PK can't be NULL).
    Withdrawn sponsors keep their row with isWithdrawn=1 — never dropped.
    """
    member_id = sponsor.get("MemberId")
    if member_id is None:
        logger.warning("EDM %s: sponsor entry lacks MemberId — skipped",
                       edm_id)
        return None
    return (
        edm_id,
        member_id,
        sponsor.get("SponsoringOrder"),      # null for withdrawn sponsors
        sponsor.get("CreatedWhen"),          # signedAt
        1 if sponsor.get("IsWithdrawn") else 0,
        sponsor.get("WithdrawnDate"),
        timestamp_millis,
    )


# --- DB writes ---

def ensure_processed_table(conn):
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {PROCESSED_TABLE} "
        "(edmId INTEGER PRIMARY KEY, hasSponsors INTEGER, "
        "timestamp INTEGER NOT NULL)")
    conn.commit()


def get_processed_ids(conn):
    ensure_processed_table(conn)
    return {r[0] for r in
            conn.execute(f"SELECT edmId FROM {PROCESSED_TABLE}")}


def mark_processed(conn, edm_id, has_sponsors, timestamp):
    """Record a successfully-completed detail fetch. Callers must invoke
    this ONLY after a non-None detail response was written — a transient
    failure left marked would leave sponsors silently absent forever."""
    conn.execute(
        f"INSERT OR REPLACE INTO {PROCESSED_TABLE} "
        "(edmId, hasSponsors, timestamp) VALUES (?,?,?)",
        (edm_id, 1 if has_sponsors else 0, timestamp))


def insert_edms(conn, rows):
    """INSERT OR REPLACE early_day_motions rows, batched at BATCH_SIZE."""
    for i in range(0, len(rows), BATCH_SIZE):
        conn.executemany(
            "INSERT OR REPLACE INTO early_day_motions "
            "(edmId, uin, uinDisplay, title, motionText, dateTabled, "
            "statusDate, status, primarySponsorMemberId, sponsorsCount, "
            "amendmentToMotionId, prayingAgainstSiId, siNumber, siYear, "
            "siTitle, lastUpdated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows[i:i + BATCH_SIZE])


def insert_sponsors(conn, rows):
    """INSERT OR REPLACE edm_sponsors rows, batched at BATCH_SIZE."""
    for i in range(0, len(rows), BATCH_SIZE):
        conn.executemany(
            "INSERT OR REPLACE INTO edm_sponsors "
            "(edmId, memberId, sponsoringOrder, signedAt, isWithdrawn, "
            "withdrawnDate, lastUpdated) "
            "VALUES (?,?,?,?,?,?,?)",
            rows[i:i + BATCH_SIZE])


def refresh_sponsors_for_edm(conn, edm_id, sponsors, timestamp_millis):
    """DELETE existing sponsor rows for the EDM, then insert the fresh
    list. Delete+reinsert is patch-neutral: diff_db compares final
    contents, so unchanged rows produce no churn. Withdrawn sponsors
    stay as isWithdrawn=1 rows — never dropped."""
    conn.execute("DELETE FROM edm_sponsors WHERE edmId = ?", (edm_id,))
    rows = []
    for s in sponsors or []:
        row = map_sponsor(edm_id, s, timestamp_millis)
        if row is not None:
            rows.append(row)
    insert_sponsors(conn, rows)
    return len(rows)


def load_existing_edm_summaries(conn):
    """{edmId: (status, statusDate, sponsorsCount)} from the DB — the
    list payload carries all three, so a sponsor/status change is
    detectable without a detail call.

    Known blind spot (live probe, 2026-09-22): a lone sponsor withdrawal
    moves NONE of the three fields — SponsorsCount counts withdrawn
    signatories, and Status/StatusDate don't move on signature
    withdrawal. Stale isWithdrawn=0 rows persist until a
    --resweep-details run; this detector alone does not bound them."""
    rows = conn.execute(
        "SELECT edmId, status, statusDate, sponsorsCount "
        "FROM early_day_motions").fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def item_summary(item):
    """The (status, statusDate, sponsorsCount) detector tuple from a
    list item — same order as load_existing_edm_summaries values."""
    return (item.get("Status"), item.get("StatusDate"),
            item.get("SponsorsCount"))


def fetch_and_store_sponsors(conn, edm_id, timestamp_millis):
    """Detail call + store for one EDM. Returns True on success (caller
    then marks _edm_processed), False on failure (left unmarked for
    retry on resume / next delta)."""
    detail = fetch_edm_detail(edm_id)
    if detail is None:
        return False
    n = refresh_sponsors_for_edm(conn, edm_id, detail.get("Sponsors"),
                                 timestamp_millis)
    mark_processed(conn, edm_id, has_sponsors=n > 0,
                   timestamp=timestamp_millis)
    return True


# --- Build modes ---

def build_seed(output_path, schema_path, edm_limit=None, checkpoint_db=None):
    """Seed build: fresh DB (or checkpoint resume), all in-scope EDMs
    + their sponsor lists. ~4k detail calls at parliament scope."""
    caps = probe_list_capabilities()

    skip_ids = set()
    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        skip_ids = get_processed_ids(conn)
        logger.info("Resuming: %d EDMs already processed", len(skip_ids))
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES)
        ensure_processed_table(conn)
        ensure_edm_tables(conn)  # stale-schema fallback (no-op if v38)

    timestamp = int(time.time() * 1000)
    in_scope = details_ok = details_failed = 0
    edm_buffer = []

    for item in iter_in_scope_list_items(caps):
        if edm_limit is not None and in_scope >= edm_limit:
            break
        edm_id = item.get("Id")
        if edm_id is None:
            logger.warning("List item lacks Id — skipped: %s",
                           str(item.get("Title"))[:60])
            continue
        in_scope += 1
        edm_buffer.append(map_edm(item, timestamp))
        if len(edm_buffer) >= BATCH_SIZE:
            insert_edms(conn, edm_buffer)
            edm_buffer = []

        if edm_id in skip_ids:
            continue
        if fetch_and_store_sponsors(conn, edm_id, timestamp):
            details_ok += 1
        else:
            details_failed += 1
            logger.warning("EDM %s: detail failed — left unprocessed for "
                           "retry", edm_id)

        if in_scope % 50 == 0:
            conn.commit()
            logger.info("Progress: %d EDMs (%d sponsor fetches ok, "
                        "%d failed)", in_scope, details_ok, details_failed)

    insert_edms(conn, edm_buffer)
    conn.commit()
    logger.info("Seed: %d in-scope EDMs, %d sponsor fetches ok, %d failed",
                in_scope, details_ok, details_failed)

    logger.info("VACUUMing database...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, edm_limit=None,
                resweep_details=False):
    """Delta build: copy previous DB, upsert every in-scope motion row,
    refetch details only where needed, delete absent in-scope motions.

    Detail refetch for an edmId when ANY of:
      (a) it's new to the DB,
      (b) its (status, statusDate, sponsorsCount) tuple changed,
      (c) it's absent from _edm_processed (detail failed/never ran —
          heals the silent-sponsor hole),
      (d) --resweep-details is set (all in-scope EDMs — the bounding
          mechanism for the withdrawal blind spot; opt-in because it's
          ~4k detail calls, invoked via workflow_dispatch).

    Removals: enabled only because the probe confirmed withdrawn motions
    REMAIN in the list (REMOVE_ABSENT_EDMS). Rows with
    dateTabled >= PARLIAMENT_START absent from the fresh list are deleted
    (with their sponsors) — the only way diff_db emits deletes for
    genuinely-removed motions. If withdrawn motions ever disappear from
    the list this MUST be switched off (keep-withdrawn-as-facts beats
    emitting deletes).
    """
    caps = probe_list_capabilities()

    shutil.copy2(previous_db, output_path)
    conn = sqlite3.connect(output_path)
    schema_module.ensure_schema(conn, schema_path, TABLE_NAMES)
    ensure_processed_table(conn)
    ensure_edm_tables(conn)  # stale-schema fallback (no-op if v38)

    existing = load_existing_edm_summaries(conn)
    processed = get_processed_ids(conn)
    timestamp = int(time.time() * 1000)

    in_scope = details_ok = details_failed = refetches = 0
    seen_ids = set()
    edm_buffer = []

    for item in iter_in_scope_list_items(caps):
        if edm_limit is not None and in_scope >= edm_limit:
            break
        edm_id = item.get("Id")
        if edm_id is None:
            logger.warning("List item lacks Id — skipped: %s",
                           str(item.get("Title"))[:60])
            continue
        in_scope += 1
        seen_ids.add(edm_id)
        edm_buffer.append(map_edm(item, timestamp))
        if len(edm_buffer) >= BATCH_SIZE:
            insert_edms(conn, edm_buffer)
            edm_buffer = []

        prev = existing.get(edm_id)
        need_detail = (
            resweep_details
            or prev is None                            # (a) new EDM
            or prev != item_summary(item)              # (b) changed tuple
            or edm_id not in processed                 # (c) never fetched
        )
        if not need_detail:
            continue
        refetches += 1
        if fetch_and_store_sponsors(conn, edm_id, timestamp):
            details_ok += 1
        else:
            details_failed += 1
            logger.warning("EDM %s: detail failed — left unprocessed for "
                           "retry", edm_id)
        if refetches % 50 == 0:
            conn.commit()
            logger.info("Progress: %d detail refetches (%d ok, %d failed)",
                        refetches, details_ok, details_failed)

    insert_edms(conn, edm_buffer)

    # Remove in-scope motions absent from the fresh list (probe-gated —
    # see REMOVE_ABSENT_EDMS). Skipped under --edm-limit: a capped list
    # would make every uncapped row look "absent".
    removed = 0
    if REMOVE_ABSENT_EDMS and edm_limit is None:
        stale_ids = [
            r[0] for r in conn.execute(
                "SELECT edmId FROM early_day_motions "
                "WHERE substr(dateTabled, 1, 10) >= ?", (PARLIAMENT_START,))
            if r[0] not in seen_ids
        ]
        for i in range(0, len(stale_ids), BATCH_SIZE):
            chunk = stale_ids[i:i + BATCH_SIZE]
            ph = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM early_day_motions WHERE edmId IN ({ph})",
                chunk)
            conn.execute(
                f"DELETE FROM edm_sponsors WHERE edmId IN ({ph})", chunk)
            conn.execute(
                f"DELETE FROM {PROCESSED_TABLE} WHERE edmId IN ({ph})",
                chunk)
        removed = len(stale_ids)
        if removed:
            logger.info("Removed %d motions absent from the list", removed)

    conn.commit()
    logger.info("Delta: %d in-scope EDMs, %d detail refetches "
                "(%d ok, %d failed), %d removed",
                in_scope, refetches, details_ok, details_failed, removed)

    logger.info("VACUUMing database...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Early Day Motions per-API DB.")
    parser.add_argument("--output", default="edms.db")
    parser.add_argument("--schema")
    parser.add_argument("--mode", choices=["seed", "delta"], default="seed")
    parser.add_argument("--previous-db")
    parser.add_argument("--edm-limit", type=int, default=None,
                        help="Cap in-scope EDMs processed (testing only)")
    parser.add_argument("--checkpoint-db",
                        help="Resume seed from checkpoint DB")
    parser.add_argument("--resweep-details", action="store_true",
                        help="Delta only: refetch sponsors for EVERY "
                             "in-scope EDM — bounds the sponsor-withdrawal "
                             "blind spot (status/statusDate/sponsorsCount "
                             "don't move on withdrawal). Costly; run "
                             "monthly-ish via workflow_dispatch.")
    args = parser.parse_args()

    if not args.schema:
        parser.error("--schema is required")
    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")
    if args.resweep_details and args.mode != "delta":
        parser.error("--resweep-details only applies to delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, edm_limit=args.edm_limit,
                   checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema,
                    edm_limit=args.edm_limit,
                    resweep_details=args.resweep_details)


if __name__ == "__main__":
    main()
