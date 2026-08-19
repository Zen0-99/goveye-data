#!/usr/bin/env python3
"""Generate a JSON diff patch by comparing new DB vs previous DB.

For each of the 16 tables, computes:
  - upsert array: rows in new DB that differ from previous (by primary key)
  - delete array: primary keys in previous DB but not in new DB

The patch format maps directly to Android Room DAO @Upsert and @Query DELETE
operations (JSON diff over raw SQL for type safety and no injection risk).

Usage:
  python diff_db.py --new goveye.db --previous prev_goveye.db --output patch.json
  python diff_db.py --new goveye.db --output patch.json  # first run, no previous
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import schema as schema_module

# Primary key definitions for each table.
# Maps table name to list of primary key column names.
# For tables with auto-generated primary keys or no meaningful PK for diffing,
# we use all columns as the comparison key.
TABLE_PRIMARY_KEYS = {
    "mps": ["id"],
    "divisions": ["id"],
    "division_votes": ["divisionId", "memberId"],
    "remote_keys": ["label"],
    "committees": ["id"],
    "mp_committee_cross_ref": ["memberId", "committeeId"],
    "bills": ["id"],
    "bill_stages": ["id"],
    "bill_follows": ["id"],
    "hansard_contributions": ["id"],
    "interests": ["id"],
    "follows": ["id"],
    "recess_dates": ["id"],
    "recess_dates_meta": ["id"],
    "mp_notification_prefs": ["mpId"],
}

# Tables to skip in diffing (FTS virtual tables don't need diffing)
SKIP_TABLES = {"mps_fts"}


def get_table_columns(conn, table_name):
    """Return the list of column names for a table."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def get_table_rows(conn, table_name):
    """Return all rows from a table as a list of dicts keyed by column name."""
    columns = get_table_columns(conn, table_name)
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM {table_name}")
    rows = cursor.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def make_pk_key(row, pk_columns):
    """Create a hashable key tuple from a row's primary key columns."""
    return tuple(row.get(col) for col in pk_columns)


def diff_table(new_conn, prev_conn, table_name, pk_columns):
    """Compute upsert and delete arrays for a single table.

    Args:
        new_conn: Connection to the new DB.
        prev_conn: Connection to the previous DB (or None for first run).
        table_name: The table to diff.
        pk_columns: List of primary key column names.

    Returns:
        Dict with "upsert" and "delete" arrays.
    """
    new_rows = get_table_rows(new_conn, table_name)

    # First run: no previous DB — all rows are upserts
    if prev_conn is None:
        return {"upsert": new_rows, "delete": []}

    prev_rows = get_table_rows(prev_conn, table_name)

    # Build lookup by primary key
    prev_map = {}
    for row in prev_rows:
        key = make_pk_key(row, pk_columns)
        prev_map[key] = row

    new_map = {}
    for row in new_rows:
        key = make_pk_key(row, pk_columns)
        new_map[key] = row

    upserts = []
    deletes = []

    # Upserts: rows in new that are different from previous or not in previous
    for key, new_row in new_map.items():
        if key not in prev_map:
            upserts.append(new_row)
        elif new_row != prev_map[key]:
            upserts.append(new_row)

    # Deletes: primary keys in previous but not in new
    for key, prev_row in prev_map.items():
        if key not in new_map:
            deletes.append(prev_row)

    return {"upsert": upserts, "delete": deletes}


def generate_diff(new_db_path, previous_db_path, schema_path, output_path,
                  tables=None):
    """Generate a JSON diff patch comparing new DB vs previous DB.

    Args:
        new_db_path: Path to the new DB file.
        previous_db_path: Path to the previous DB file (None for first run).
        schema_path: Path to the Room schema JSON.
        output_path: Path to write the patch JSON file.
        tables: Optional list of table names to diff (D-10). If provided,
            only those tables are diffed instead of all 16. The SKIP_TABLES
            set still applies (mps_fts is always skipped — auto-synced).
    """
    schema = schema_module.load_schema(schema_path)
    schema_version = schema_module.get_version(schema)
    table_names = schema_module.get_table_names(schema)

    # D-10: filter to only the requested tables when --tables is provided
    if tables:
        requested = {t.strip() for t in tables if t.strip()}
        table_names = table_names & requested

    new_conn = sqlite3.connect(new_db_path)
    new_conn.row_factory = sqlite3.Row  # Enable row access by column name

    prev_conn = None
    if previous_db_path and os.path.exists(previous_db_path):
        prev_conn = sqlite3.connect(previous_db_path)
        prev_conn.row_factory = sqlite3.Row

    changes = {}
    for table_name in sorted(table_names):
        if table_name in SKIP_TABLES:
            continue

        pk_columns = TABLE_PRIMARY_KEYS.get(table_name, ["id"])
        result = diff_table(new_conn, prev_conn, table_name, pk_columns)
        changes[table_name] = result

        if result["upsert"] or result["delete"]:
            print(
                f"  {table_name}: {len(result['upsert'])} upserts, "
                f"{len(result['delete'])} deletes"
            )

    new_conn.close()
    if prev_conn:
        prev_conn.close()

    patch = {
        "patchVersion": None,  # Filled by manifest.py based on previous version
        "previousVersion": None,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": schema_version,
        "changes": changes,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(patch, f, indent=2, ensure_ascii=False)

    print(f"Patch written to {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate a JSON diff patch comparing new DB vs previous DB."
    )
    parser.add_argument(
        "--new", required=True,
        help="Path to the new DB file.",
    )
    parser.add_argument(
        "--previous", default=None,
        help="Path to the previous DB file (omit for first run).",
    )
    parser.add_argument(
        "--schema", required=True,
        help="Path to the Room schema JSON (8.json).",
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to write the patch JSON file.",
    )
    parser.add_argument(
        "--tables", default=None,
        help="Comma-separated list of tables to diff (e.g. mps,mps_fts). "
             "If omitted, diffs all tables.",
    )
    args = parser.parse_args()

    tables = args.tables.split(",") if args.tables else None
    generate_diff(args.new, args.previous, args.schema, args.output, tables=tables)


if __name__ == "__main__":
    main()
