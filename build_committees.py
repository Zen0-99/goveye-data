#!/usr/bin/env python3
"""Per-API build script for committees (D-10, D-11).

Fetches committees + cross-refs for all 650 MPs from the Committees API
and builds a per-API DB (committees.db) with only the committees +
mp_committee_cross_ref tables.

This script makes 650 per-MP API calls (one per MP) plus the initial MP
fetch — rate limiting (API_DELAY) between each call is critical. Committees
may appear for multiple MPs; INSERT OR REPLACE deduplicates by primary key.

Modes:
  seed  — create fresh DB, fetch all MPs, per-MP committees, insert
  delta — copy previous DB, fetch all MPs, per-MP committees, upsert

Usage:
  python build_committees.py --output committees.db --schema schemas/8.json --mode seed
  python build_committees.py --output committees.db --schema schemas/8.json --mode delta --previous-db prev_committees.db
"""

import argparse
import shutil
import sqlite3
import time

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

MEMBERS_BASE = "https://members-api.parliament.uk/api/"
COMMITTEES_BASE = "https://committees-api.parliament.uk/api/"
PAGE_SIZE_MEMBERS = 20

TABLE_NAMES = ["committees", "mp_committee_cross_ref"]


# --- Members API (need MP IDs to query committees per MP) ---

def fetch_all_mps(mp_limit=None):
    """Fetch all current Commons MPs from the Members API.

    Returns a list of MemberDto dicts (we need their ids to query
    committees per MP).
    """
    mps = []
    skip = 0

    while True:
        params = {
            "House": 1,
            "IsCurrentMember": "true",
            "itemsPerPage": PAGE_SIZE_MEMBERS,
            "skip": skip,
        }
        logger.info("Fetching MPs: skip=%d", skip)
        r = api_get(
            f"{MEMBERS_BASE}Members/Search",
            params=params,
            timeout=60,
        )
        data = r.json()
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            mps.append(item.get("value", item))

        if mp_limit is not None and len(mps) >= mp_limit:
            mps = mps[:mp_limit]
            break

        if len(items) < PAGE_SIZE_MEMBERS:
            break

        skip += PAGE_SIZE_MEMBERS
        time.sleep(API_DELAY)

    logger.info("Fetched %d MPs", len(mps))
    return mps


# --- Committees API ---

def fetch_committees_for_mp(mp_id):
    """Fetch committees for a single MP from the Committees API.

    The API returns {"items": [...]} where each item is a CommitteeItem
    with `id`, `name`, `house`, `category` (with `name`), `startDate`,
    `endDate`.
    """
    r = api_get(
        f"{COMMITTEES_BASE}Committees",
        params={"MemberId": mp_id},
        timeout=60,
    )
    data = r.json()
    return data.get("items", [])


def map_committee_to_entity(item, timestamp_millis):
    """Map a CommitteeItem to a committees table row tuple.

    isActive is derived from endDate == null (per CommitteeMapper).
    house is a TEXT column (nullable).
    """
    end_date = item.get("endDate")
    category = item.get("category") or {}
    return (
        item.get("id") or 0,
        item.get("name") or "",
        item.get("house"),
        category.get("name"),
        item.get("startDate"),
        end_date,
        1 if end_date is None else 0,  # isActive = (endDate == null)
        timestamp_millis,
    )


def insert_committees(conn, items, timestamp_millis):
    """Batch insert committees via INSERT OR REPLACE (dedup by PK id)."""
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO committees (
            id, name, house, categoryName, startDate, endDate,
            isActive, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = [map_committee_to_entity(item, timestamp_millis) for item in items]

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()


def insert_cross_refs(conn, mp_id, committee_ids, timestamp_millis):
    """Insert mp_committee_cross_ref rows for one MP."""
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO mp_committee_cross_ref
            (memberId, committeeId, lastUpdated)
        VALUES (?, ?, ?)
    """
    rows = [(mp_id, cid, timestamp_millis) for cid in committee_ids]
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()


# --- Build modes ---

def fetch_and_insert_committees(conn, timestamp_millis, mp_limit=None):
    """Fetch all MPs, then per-MP committees, insert committees + cross-refs."""
    mps = fetch_all_mps(mp_limit=mp_limit)
    total_committees = 0
    total_cross_refs = 0

    for mp in mps:
        mp_id = mp.get("id") or 0
        items = fetch_committees_for_mp(mp_id)
        if items:
            insert_committees(conn, items, timestamp_millis)
            committee_ids = [item.get("id") or 0 for item in items]
            insert_cross_refs(conn, mp_id, committee_ids, timestamp_millis)
            total_committees += len(items)
            total_cross_refs += len(committee_ids)
        time.sleep(API_DELAY)  # Rate limit critical — 650 per-MP calls

    logger.info(
        "Committees: %d entries, %d cross-refs across %d MPs",
        total_committees, total_cross_refs, len(mps),
    )


def build_seed(output_path, schema_path, mp_limit=None):
    """Seed mode: create fresh DB, fetch all MPs + per-MP committees."""
    timestamp_millis = int(time.time() * 1000)

    conn = schema_module.create_database_with_tables(
        output_path, schema_path, TABLE_NAMES,
    )

    fetch_and_insert_committees(conn, timestamp_millis, mp_limit=mp_limit)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mp_limit=None):
    """Delta mode: copy previous DB, fetch all MPs + per-MP committees, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    fetch_and_insert_committees(conn, timestamp_millis, mp_limit=mp_limit)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye committees per-API SQLite database (committees.db)."
    )
    parser.add_argument(
        "--output", default="committees.db",
        help="Output path for the SQLite DB file. Default: committees.db.",
    )
    parser.add_argument(
        "--schema", required=True,
        help="Path to the Room exported schema JSON (8.json).",
    )
    parser.add_argument(
        "--mode", choices=["seed", "delta"], default="seed",
        help="Build mode: seed (full) or delta (incremental). Default: seed.",
    )
    parser.add_argument(
        "--previous-db",
        help="Path to previous DB file (required for delta mode).",
    )
    parser.add_argument(
        "--mp-limit", type=int, default=None,
        help="Limit number of MPs fetched (for testing).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, mp_limit=args.mp_limit)
    else:
        build_delta(args.output, args.previous_db, args.schema, mp_limit=args.mp_limit)


if __name__ == "__main__":
    main()
