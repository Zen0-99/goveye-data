#!/usr/bin/env python3
"""Per-API build script for Parliament Written Statements.

Fetches written ministerial statements from the Parliament Written Statements
API (questions-statements-api.parliament.uk) and builds a per-API DB
(written_statements.db) with only the written_statements table + the full
schema's Room identity hash.

Per D-02: fetches the last 90 days of statements (hybrid storage — recent
data in bundled DB, historical on-demand).

Pitfall 4: the bulk API truncates text fields at 255 characters. For any
statement where len(text) == 255, we fetch the full text from the individual
endpoint GET {STATEMENTS_API}/{id}.

Modes:
  seed  — create fresh DB, fetch last 90 days, insert
  delta — copy previous DB, fetch last 90 days, upsert

Usage:
  python build_written_statements.py --output written_statements.db --schema schemas/bundled_schema.json --mode seed
  python build_written_statements.py --output written_statements.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_written_statements.db
"""

import argparse
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

STATEMENTS_API = "https://questions-statements-api.parliament.uk/api/writtenstatements/statements"
TABLE_NAMES = ["written_statements"]


# --- Written Statements API ---

def fetch_written_statements(start_date, end_date):
    """Fetch all written statements within a date range.

    The API returns results sorted by dateMade descending. Both Commons and
    Lords statements are returned (the house field distinguishes them).

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.

    Returns:
        List of statement dicts with keys: id, memberId, memberRole, uin,
        dateMade, answeringBodyId, answeringBodyName, title, text, house.
    """
    params = {"start": start_date, "end": end_date}
    logger.info("Fetching written statements: %s to %s", start_date, end_date)
    r = api_get(STATEMENTS_API, params=params, timeout=60)
    data = r.json()
    statements = []
    for item in data.get("results", []):
        val = item.get("value", {})
        statements.append({
            "id": val.get("id"),
            "memberId": val.get("memberId"),
            "memberRole": val.get("memberRole", ""),
            "uin": val.get("uin", ""),
            "dateMade": val.get("dateMade", ""),
            "answeringBodyId": val.get("answeringBodyId"),
            "answeringBodyName": val.get("answeringBodyName", ""),
            "title": val.get("title", ""),
            "text": val.get("text", ""),
            "house": val.get("house", 1),
        })
    logger.info("Fetched %d written statements", len(statements))
    return statements


def fetch_full_statement_text(statement_id):
    """Fetch the full text of a single statement from the individual endpoint.

    Pitfall 4: the bulk API truncates text at 255 chars. When len(text) == 255,
    we fetch the full text from GET {STATEMENTS_API}/{id}.

    Args:
        statement_id: The Parliament statement ID.

    Returns:
        The full text string, or empty string if the fetch fails.
    """
    try:
        r = api_get(f"{STATEMENTS_API}/{statement_id}", timeout=30)
        data = r.json()
        val = data.get("value", data)
        return val.get("text", "")
    except Exception as e:
        logger.warning("Failed to fetch full text for statement %s: %s", statement_id, e)
        return ""


# --- Mapping + insertion ---

def map_statement_to_entity(stmt, timestamp_millis):
    """Map a statement dict to a written_statements row tuple.

    Matches WrittenStatementEntity fields:
    (id, memberId, memberRole, uin, dateMade, answeringBodyId,
     answeringBodyName, title, text, house, lastUpdated)
    """
    return (
        stmt.get("id") or 0,
        stmt.get("memberId") or 0,
        stmt.get("memberRole") or "",
        stmt.get("uin") or "",
        stmt.get("dateMade") or "",
        stmt.get("answeringBodyId") or 0,
        stmt.get("answeringBodyName") or "",
        stmt.get("title") or "",
        stmt.get("text") or "",
        stmt.get("house") or 1,
        timestamp_millis,
    )


def insert_statements(conn, statements, timestamp_millis):
    """Insert statements into the written_statements table using batch executemany.

    Uses INSERT OR REPLACE so this works for both seed and delta modes.
    """
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO written_statements (
            id, memberId, memberRole, uin, dateMade, answeringBodyId,
            answeringBodyName, title, text, house, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = [map_statement_to_entity(stmt, timestamp_millis) for stmt in statements]

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted statements: %d/%d", min(i + BATCH_SIZE, len(rows)), len(rows))


# --- Build modes ---

def get_processed_statement_ids(conn):
    """Get the set of statement IDs already in the checkpoint DB."""
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM written_statements")
    return {row[0] for row in cursor.fetchall()}


def build_seed(output_path, schema_path, days=90, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch last 90 days of statements, insert.

    If checkpoint_db exists and has data, upserts on top of it (INSERT OR REPLACE
    handles dedup).
    """
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        existing = get_processed_statement_ids(conn)
        logger.info("Resuming from checkpoint: %d statements already in DB", len(existing))
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    statements = fetch_written_statements(start_date, end_date)

    # Pitfall 4: fetch full text for truncated statements
    for stmt in statements:
        text = stmt.get("text", "")
        if len(text) == 255:
            logger.info("Statement %s text truncated at 255 chars — fetching full text", stmt.get("id"))
            full_text = fetch_full_statement_text(stmt["id"])
            if full_text:
                stmt["text"] = full_text
            time.sleep(API_DELAY)

    if statements:
        insert_statements(conn, statements, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, days=90):
    """Delta mode: copy previous DB, fetch last 90 days, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    # Ensure schema is up-to-date (previous DB may be from an older schema version)
    schema_module.ensure_schema(conn, schema_path, TABLE_NAMES)
    logger.info("Schema ensured for delta build")

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    statements = fetch_written_statements(start_date, end_date)

    # Pitfall 4: fetch full text for truncated statements
    for stmt in statements:
        text = stmt.get("text", "")
        if len(text) == 255:
            logger.info("Statement %s text truncated at 255 chars — fetching full text", stmt.get("id"))
            full_text = fetch_full_statement_text(stmt["id"])
            if full_text:
                stmt["text"] = full_text
            time.sleep(API_DELAY)

    if statements:
        insert_statements(conn, statements, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Written Statements per-API SQLite database (written_statements.db)."
    )
    parser.add_argument(
        "--output", default="written_statements.db",
        help="Output path for the SQLite DB file. Default: written_statements.db.",
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
        help="Path to a checkpoint DB to resume from (seed mode only). Upserts on top of existing data.",
    )
    parser.add_argument(
        "--days", type=int, default=90,
        help="Number of days of statements to fetch (default: 90 per D-02).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, days=args.days, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema, days=args.days)


if __name__ == "__main__":
    main()
