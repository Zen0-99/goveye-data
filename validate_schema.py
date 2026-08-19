#!/usr/bin/env python3
"""Validate a built SQLite DB against the Room exported schema JSON.

Per D-01 schema drift mitigation: this script verifies that the Python-built
DB matches Room's expected schema. If validation fails, the GitHub Action
stops before publishing — preventing schema drift from reaching users.

Checks:
  1. Identity hash in room_master_table matches schema (id=42)
  2. All 16 expected tables exist in the DB
  3. All expected columns exist in each table (via PRAGMA table_info)

Usage:
  python validate_schema.py --db goveye.db --schema schemas/8.json
"""

import argparse
import json
import sqlite3
import sys

import schema as schema_module


def validate(db_path, schema_path):
    """Validate the built DB against the Room schema JSON.

    Args:
        db_path: Path to the built SQLite DB file.
        schema_path: Path to the Room exported schema JSON (8.json).

    Returns:
        True if validation passes.

    Raises:
        ValueError: If any check fails (identity hash, missing tables,
                    missing columns).
    """
    schema = schema_module.load_schema(schema_path)
    expected_hash = schema_module.get_identity_hash(schema)
    expected_tables = schema_module.get_table_names(schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Check room_master_table identity hash
    try:
        cursor.execute(
            "SELECT identity_hash FROM room_master_table WHERE id = 42"
        )
        row = cursor.fetchone()
    except sqlite3.OperationalError:
        raise ValueError(
            "room_master_table does not exist in the database. "
            "The build script must create it with the correct identity hash."
        )

    if not row:
        raise ValueError(
            "room_master_table exists but no row with id=42 found. "
            "The identity hash was not inserted."
        )

    actual_hash = row[0]
    if actual_hash != expected_hash:
        raise ValueError(
            f"Identity hash mismatch: expected {expected_hash}, "
            f"got {actual_hash}. The DB schema does not match Room's "
            f"expected schema. The Action must fail to prevent schema drift."
        )

    # 2. Check all expected tables exist
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    actual_tables = {row[0] for row in cursor.fetchall()}
    missing_tables = expected_tables - actual_tables
    if missing_tables:
        raise ValueError(
            f"Missing tables: {missing_tables}. "
            f"Expected {len(expected_tables)} tables, found {len(actual_tables)}."
        )

    # 3. Check all expected columns exist in each table
    for entity in schema_module.get_entities(schema):
        table = entity["tableName"]
        cursor.execute(f"PRAGMA table_info({table})")
        actual_cols = {row[1] for row in cursor.fetchall()}  # row[1] = col name

        for field in entity.get("fields", []):
            col_name = field["columnName"]
            if col_name not in actual_cols:
                raise ValueError(
                    f"Missing column '{col_name}' in table '{table}'. "
                    f"Expected columns: {[f['columnName'] for f in entity.get('fields', [])]}"
                )

    conn.close()
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Validate a built DB against the Room schema JSON."
    )
    parser.add_argument(
        "--db", required=True,
        help="Path to the built SQLite DB file.",
    )
    parser.add_argument(
        "--schema", required=True,
        help="Path to the Room exported schema JSON (8.json).",
    )
    args = parser.parse_args()

    try:
        validate(args.db, args.schema)
        print(f"Validation PASSED: {args.db} matches schema {args.schema}")
        sys.exit(0)
    except ValueError as e:
        print(f"Validation FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
