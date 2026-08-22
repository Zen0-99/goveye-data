#!/usr/bin/env python3
"""Per-API build script for committees (D-10, D-11).

Fetches all committees from the Committees API, then for each committee
fetches its current members via /Committees/{id}/Members. Builds a per-API
DB (committees.db) with the committees + mp_committee_cross_ref tables.

The Committees API's ?MemberId= parameter is ignored — it returns all
committees regardless. The correct approach is to query per-committee
members and filter to isCurrent=true.

Modes:
  seed  — create fresh DB, fetch all committees + per-committee members
  delta — copy previous DB, re-fetch all, upsert

Usage:
  python build_committees.py --output committees.db --schema schemas/bundled_schema.json --mode seed
  python build_committees.py --output committees.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_committees.db
"""

import argparse
import os
import shutil
import sqlite3
import time

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

COMMITTEES_BASE = "https://committees-api.parliament.uk/api/"
COMMITTEES_PAGE_SIZE = 500

TABLE_NAMES = ["committees", "mp_committee_cross_ref"]


# --- Committees API ---

def fetch_all_committees():
    """Fetch all committees from the Committees API.

    Returns a list of CommitteeItem dicts with id, name, house, category,
    startDate, endDate.
    """
    committees = []
    skip = 0

    while True:
        params = {
            "Take": COMMITTEES_PAGE_SIZE,
            "Skip": skip,
        }
        logger.info("Fetching committees: skip=%d", skip)
        r = api_get(
            f"{COMMITTEES_BASE}Committees",
            params=params,
            timeout=60,
        )
        data = r.json()
        items = data.get("items", [])
        if not items:
            break

        committees.extend(items)

        total_results = data.get("totalResults", 0)
        if len(committees) >= total_results or len(items) < COMMITTEES_PAGE_SIZE:
            break

        skip += COMMITTEES_PAGE_SIZE
        time.sleep(API_DELAY)

    logger.info("Fetched %d committees", len(committees))
    return committees


def fetch_committee_members(committee_id):
    """Fetch current members for a single committee.

    Returns a list of member dicts with mnisId (the Members API ID used
    to match against our mps table) and name.
    """
    r = api_get(
        f"{COMMITTEES_BASE}Committees/{committee_id}/Members",
        timeout=60,
    )
    data = r.json()
    items = data.get("items", [])

    # Filter to current members only
    current_members = []
    for item in items:
        member_info = item.get("memberInfo") or {}
        if member_info.get("isCurrent", False):
            mnis_id = member_info.get("mnisId")
            if mnis_id:
                current_members.append({
                    "mnisId": mnis_id,
                    "name": item.get("name", ""),
                    "house": member_info.get("house", ""),
                })
    return current_members


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


def insert_cross_refs(conn, committee_id, mp_ids, timestamp_millis):
    """Insert mp_committee_cross_ref rows for one committee."""
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO mp_committee_cross_ref
            (memberId, committeeId, lastUpdated)
        VALUES (?, ?, ?)
    """
    rows = [(mp_id, committee_id, timestamp_millis) for mp_id in mp_ids]
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
    conn.commit()


# --- Build modes ---

def get_processed_committee_ids(conn):
    """Get the set of committee IDs already processed (in cross-ref table)."""
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT committeeId FROM mp_committee_cross_ref")
    return {row[0] for row in cursor.fetchall()}


def fetch_and_insert_committees(conn, timestamp_millis, skip_committee_ids=None):
    """Fetch all committees, then per-committee current members, insert + cross-refs.

    If skip_committee_ids is provided, those committees are skipped (already in checkpoint).
    """
    if skip_committee_ids is None:
        skip_committee_ids = set()

    committees = fetch_all_committees()

    # Insert all committee entities first
    insert_committees(conn, committees, timestamp_millis)
    logger.info("Inserted %d committee entities", len(committees))

    total_cross_refs = 0
    skipped = 0
    committees_with_members = 0

    for committee in committees:
        committee_id = committee.get("id") or 0
        if committee_id in skip_committee_ids:
            skipped += 1
            continue

        members = fetch_committee_members(committee_id)
        if members:
            mp_ids = [m["mnisId"] for m in members]
            insert_cross_refs(conn, committee_id, mp_ids, timestamp_millis)
            total_cross_refs += len(mp_ids)
            committees_with_members += 1

        time.sleep(API_DELAY)

    logger.info(
        "Committees: %d total, %d with current members, %d cross-refs (%d skipped from checkpoint)",
        len(committees), committees_with_members, total_cross_refs, skipped,
    )


def build_seed(output_path, schema_path, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch all committees + per-committee members.

    If checkpoint_db exists and has data, skip committees already in cross-ref table.
    """
    timestamp_millis = int(time.time() * 1000)
    skip_committee_ids = set()

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        skip_committee_ids = get_processed_committee_ids(conn)
        logger.info("Resuming from checkpoint: %d committees already processed", len(skip_committee_ids))
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )

    fetch_and_insert_committees(conn, timestamp_millis, skip_committee_ids=skip_committee_ids)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path):
    """Delta mode: copy previous DB, re-fetch all committees + members, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    # Clear old cross-refs — we'll re-fetch all current memberships
    conn.execute("DELETE FROM mp_committee_cross_ref")
    conn.commit()

    fetch_and_insert_committees(conn, timestamp_millis)

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
        help="Path to the Room exported schema JSON (bundled_schema.json).",
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
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only). Skips committees already in cross-ref table.",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema)


if __name__ == "__main__":
    main()
