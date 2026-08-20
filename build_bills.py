#!/usr/bin/env python3
"""Per-API build script for bills (D-10, D-11).

Fetches all bills + bill stages from the Bills API and builds a per-API DB
(bills.db) with only the bills + bill_stages tables.

Modes:
  seed  — create fresh DB, fetch all bills + stages, insert
  delta — copy previous DB, fetch all bills + stages, upsert (bills can
          change stage/status — INSERT OR REPLACE)

Usage:
  python build_bills.py --output bills.db --schema schemas/8.json --mode seed
  python build_bills.py --output bills.db --schema schemas/8.json --mode delta --previous-db prev_bills.db
"""

import argparse
import json
import os
import shutil
import sqlite3
import time

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

BILLS_BASE = "https://bills-api.parliament.uk/api/v1/"
PAGE_SIZE_BILLS = 20

TABLE_NAMES = ["bills", "bill_stages"]


# --- Bills API ---

def fetch_all_bills(bill_limit=None):
    """Fetch all bills from the Bills API.

    The API returns {"items": [...], "totalResults": N}. Each item is a
    BillDto with `billId` (not `id`).
    """
    bills = []
    skip = 0

    while True:
        params = {
            "itemsPerPage": PAGE_SIZE_BILLS,
            "skip": skip,
        }
        logger.info("Fetching bills: skip=%d", skip)
        r = api_get(
            f"{BILLS_BASE}Bills",
            params=params,
            timeout=60,
        )
        data = r.json()
        items = data.get("items", [])
        if not items:
            break

        bills.extend(items)

        if bill_limit is not None and len(bills) >= bill_limit:
            bills = bills[:bill_limit]
            break

        if len(items) < PAGE_SIZE_BILLS:
            break

        skip += PAGE_SIZE_BILLS
        time.sleep(API_DELAY)

    logger.info("Fetched %d bills", len(bills))
    return bills


def fetch_bill_stages(bill_id):
    """Fetch bill stages for a single bill.

    The API returns {"items": [...], "totalResults": N}. Each item is a
    BillStageDto with `id`, `stageId`, `description`, `abbreviation`,
    `house`, `sortOrder`, `sessionId`, `stageSittings` (list with `date`).
    """
    r = api_get(
        f"{BILLS_BASE}Bills/{bill_id}/Stages",
        timeout=60,
    )
    data = r.json()
    # Handle both {"items": [...]} and bare list responses
    if isinstance(data, dict):
        return data.get("items", [])
    return data


def map_bill_to_entity(bill, timestamp_millis):
    """Map a BillDto to a bills table row tuple.

    BillDto uses `billId` (not `id`). currentStage is a BillStageDto with
    `description` and `abbreviation`. isDefeated/isAct are booleans → 0/1.
    """
    current_stage = bill.get("currentStage") or {}
    return (
        bill.get("billId") or 0,
        bill.get("shortTitle") or "",
        bill.get("longTitle"),
        bill.get("summary"),
        bill.get("currentHouse") or "",
        bill.get("originatingHouse") or "",
        bill.get("lastUpdate") or "",
        bill.get("billWithdrawn"),
        1 if bill.get("isDefeated", False) else 0,
        1 if bill.get("isAct", False) else 0,
        bill.get("billTypeId"),
        current_stage.get("description"),
        current_stage.get("abbreviation"),
        timestamp_millis,
    )


def map_bill_stage_to_entity(stage, bill_id, timestamp_millis):
    """Map a BillStageDto to a bill_stages table row tuple.

    stageId from `dto.stageId` (per BillMapper). sittingDates is a list of
    date strings from stageSittings — JSON-encoded as a string (the
    BillStageEntity uses List<String> with a TypeConverter).
    """
    sitting_dates = [
        s.get("date") for s in (stage.get("stageSittings") or []) if s.get("date")
    ]
    return (
        bill_id,
        stage.get("stageId") or 0,
        stage.get("description") or "",
        stage.get("abbreviation") or "",
        stage.get("house") or "",
        stage.get("sortOrder") or 0,
        stage.get("sessionId"),
        json.dumps(sitting_dates),
        timestamp_millis,
    )


def insert_bills(conn, bills, timestamp_millis):
    """Batch insert bills via INSERT OR REPLACE."""
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO bills (
            id, shortTitle, longTitle, summary, currentHouse,
            originatingHouse, lastUpdate, billWithdrawn, isDefeated,
            isAct, billTypeId, currentStageDescription,
            currentStageAbbreviation, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = [map_bill_to_entity(bill, timestamp_millis) for bill in bills]

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted bills: %d/%d", min(i + BATCH_SIZE, len(rows)), len(rows))


def insert_bill_stages(conn, stages, bill_id, timestamp_millis):
    """Batch insert bill stages for one bill via INSERT OR REPLACE."""
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO bill_stages (
            billId, stageId, description, abbreviation, house,
            sortOrder, sessionId, sittingDates, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = [map_bill_stage_to_entity(s, bill_id, timestamp_millis) for s in stages]

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()

    logger.info("Bill %d: %d stages inserted", bill_id, len(rows))


# --- Build modes ---

def get_max_bill_id(conn):
    """Get the maximum bill ID from the DB (for checkpoint resume)."""
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(id) FROM bills")
    result = cursor.fetchone()
    return result[0] if result and result[0] is not None else 0


def get_previous_bill_updates(conn):
    """Build a dict of {billId: lastUpdate} from the previous DB (for smart delta)."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, lastUpdate FROM bills")
    return {row[0]: row[1] for row in cursor.fetchall()}


def build_seed(output_path, schema_path, bill_limit=None, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch all bills + stages, insert.

    If checkpoint_db exists and has data, resume from MAX(bill id).
    """
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        max_bill_id = get_max_bill_id(conn)
        logger.info("Resuming from checkpoint: max_bill_id=%d", max_bill_id)
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        max_bill_id = 0

    bills = fetch_all_bills(bill_limit=bill_limit)
    # Filter to only bills with billId > max_bill_id (for resume)
    if max_bill_id > 0:
        bills = [b for b in bills if (b.get("billId") or 0) > max_bill_id]

    if bills:
        insert_bills(conn, bills, timestamp_millis)

        for bill in bills:
            bill_id = bill.get("billId") or 0
            stages = fetch_bill_stages(bill_id)
            if stages:
                insert_bill_stages(conn, stages, bill_id, timestamp_millis)
            time.sleep(API_DELAY)  # Rate limit between stage fetches

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, bill_limit=None):
    """Delta mode: copy previous DB, fetch all bills + stages, upsert.

    Smart delta: only fetches stage details for bills whose lastUpdate
    changed (or new bills). All bills are upserted (list data is cheap).
    """
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    # Build a map of previous bill lastUpdate values for smart delta
    prev_updates = get_previous_bill_updates(conn)
    logger.info("Delta mode: %d bills in previous DB", len(prev_updates))

    bills = fetch_all_bills(bill_limit=bill_limit)
    if bills:
        # Always upsert all bills (list data is cheap)
        insert_bills(conn, bills, timestamp_millis)

        # Only fetch stage details for changed or new bills
        changed_count = 0
        for bill in bills:
            bill_id = bill.get("billId") or 0
            api_last_update = bill.get("lastUpdate") or ""
            prev_last_update = prev_updates.get(bill_id, "")
            if api_last_update != prev_last_update:
                stages = fetch_bill_stages(bill_id)
                if stages:
                    insert_bill_stages(conn, stages, bill_id, timestamp_millis)
                changed_count += 1
            time.sleep(API_DELAY)

        logger.info("Delta: %d/%d bills had stage detail updates", changed_count, len(bills))

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye bills per-API SQLite database (bills.db)."
    )
    parser.add_argument(
        "--output", default="bills.db",
        help="Output path for the SQLite DB file. Default: bills.db.",
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
        "--bill-limit", type=int, default=None,
        help="Limit number of bills fetched (for testing).",
    )
    parser.add_argument(
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only). If it has data, resume from MAX(billId).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, bill_limit=args.bill_limit, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema, bill_limit=args.bill_limit)


if __name__ == "__main__":
    main()
