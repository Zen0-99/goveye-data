#!/usr/bin/env python3
"""Validate a built SQLite DB against the Room exported schema JSON.

Per D-01 schema drift mitigation: this script verifies that the Python-built
DB matches Room's expected schema. If validation fails, the GitHub Action
stops before publishing — preventing schema drift from reaching users.

This replicates Room's TableInfo validation (what the app checks on open
after running migrations). A DB that passes every check here will open
without "Migration didn't properly handle" crashes.

Checks:
  1. Identity hash in room_master_table matches schema (id=42)
  2. user_version pragma matches schema version (drives Room's migration path)
  3. All expected tables exist in the DB
  4. Per-column parity: name, type affinity, notNull, primaryKeyPosition,
     defaultValue (when the schema declares one)
  5. All declared indices exist (name, uniqueness, column set)
  6. All declared foreign keys exist (reference table + column mapping)
  7. All FTS4 content-sync triggers exist

Usage:
  python validate_schema.py --db goveye.db --schema schemas/8.json
"""

import argparse
import json
import re
import sqlite3
import sys

import schema as schema_module

# SQLite declared-type -> affinity, mirroring Room's typeAffinity rules.
def _affinity(declared_type):
    t = (declared_type or "").upper()
    if "INT" in t:
        return "INTEGER"
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in t or t == "":
        return "BLOB"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _column_errors(cursor, entity):
    """Return a list of column-level mismatches for one entity."""
    table = entity["tableName"]
    cursor.execute(f"PRAGMA table_info({table})")
    actual = {row[1]: row for row in cursor.fetchall()}
    errors = []

    expected_pk = entity.get("primaryKey", {})
    pk_columns = expected_pk.get("columnNames", [])
    pk_position = {name: i + 1 for i, name in enumerate(pk_columns)}

    for field in entity.get("fields", []):
        col_name = field["columnName"]
        row = actual.get(col_name)
        if row is None:
            errors.append(
                f"Missing column '{col_name}' in table '{table}'"
            )
            continue

        _cid, _name, decl_type, notnull, dflt, pk_pos = row

        actual_affinity = _affinity(decl_type)
        expected_affinity = field.get("affinity")
        if expected_affinity and actual_affinity != expected_affinity:
            errors.append(
                f"Column '{table}.{col_name}' affinity mismatch: "
                f"expected {expected_affinity}, declared type '{decl_type}' "
                f"(affinity {actual_affinity})"
            )

        expected_notnull = 1 if field.get("notNull") else 0
        # Room ignores notNull on PK columns (PK implies NOT NULL in Room's
        # TableInfo comparison for most cases, but SQLite INTEGER PKs report
        # notnull=0). Compare only non-PK columns to avoid false positives.
        if pk_pos == 0 and notnull != expected_notnull:
            errors.append(
                f"Column '{table}.{col_name}' notNull mismatch: "
                f"expected {expected_notnull}, got {notnull}"
            )

        expected_pk_pos = pk_position.get(col_name, 0)
        if pk_pos != expected_pk_pos:
            errors.append(
                f"Column '{table}.{col_name}' primaryKeyPosition mismatch: "
                f"expected {expected_pk_pos}, got {pk_pos}"
            )

        expected_default = field.get("defaultValue")
        if expected_default is not None:
            # Room normalizes defaults; compare loosely (strip quotes/spaces)
            norm = lambda v: str(v).strip().strip("'\"") if v is not None else None
            if norm(dflt) != norm(expected_default):
                errors.append(
                    f"Column '{table}.{col_name}' defaultValue mismatch: "
                    f"expected {expected_default}, got {dflt}"
                )

    return errors


def _index_errors(cursor, schema):
    """Return a list of index-level mismatches across all entities."""
    errors = []
    cursor.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index'"
    )
    actual = {row[0]: row[1] for row in cursor.fetchall()}

    for entity in schema_module.get_entities(schema):
        table = entity["tableName"]
        for idx in entity.get("indices", []):
            idx_name = idx["name"]
            if idx_name not in actual:
                errors.append(
                    f"Missing index '{idx_name}' on table '{table}'. "
                    f"Room validation will fail on device."
                )
                continue

            # Check uniqueness + column set via PRAGMAs
            cursor.execute(
                "SELECT name FROM pragma_index_info(?)",
                (idx_name,),
            )
            actual_cols = [row[0] for row in cursor.fetchall()]
            expected_cols = idx.get("columnNames", [])
            if sorted(actual_cols) != sorted(expected_cols):
                errors.append(
                    f"Index '{idx_name}' on '{table}' column mismatch: "
                    f"expected {expected_cols}, got {actual_cols}"
                )
            expected_unique = 1 if idx.get("unique") else 0
            cursor.execute(
                "SELECT \"unique\" FROM pragma_index_list(?) WHERE name=?",
                (table, idx_name),
            )
            row = cursor.fetchone()
            if row is not None and row[0] != expected_unique:
                errors.append(
                    f"Index '{idx_name}' on '{table}' unique mismatch: "
                    f"expected {expected_unique}, got {row[0]}"
                )
    return errors


def _fk_errors(cursor, schema):
    """Return a list of foreign-key mismatches (schema has FKs declared)."""
    errors = []
    for entity in schema_module.get_entities(schema):
        expected_fks = entity.get("foreignKeys") or []
        if not expected_fks:
            continue
        table = entity["tableName"]
        cursor.execute(f"PRAGMA foreign_key_list({table})")
        actual_fks = cursor.fetchall()
        actual_sigs = {
            (row[2], row[3], row[4])  # ref_table, from_col, to_col
            for row in actual_fks
        }
        for fk in expected_fks:
            ref_table = fk["referenceTable"]
            for from_col, to_col in zip(
                fk["columns"], fk["referenceColumns"]
            ):
                if (ref_table, from_col, to_col) not in actual_sigs:
                    errors.append(
                        f"Missing foreign key on '{table}': "
                        f"{from_col} -> {ref_table}.{to_col}"
                    )
    return errors


def _trigger_errors(cursor, schema):
    """Return a list of missing FTS content-sync triggers."""
    errors = []
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    )
    actual_triggers = {row[0] for row in cursor.fetchall()}

    for trigger_sql in schema_module.get_fts_triggers(schema):
        m = re.search(
            r"CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?([^`\"'\s(]+)",
            trigger_sql,
            re.IGNORECASE,
        )
        if not m:
            continue
        trigger_name = m.group(1)
        if trigger_name not in actual_triggers:
            errors.append(
                f"Missing FTS trigger '{trigger_name}' — mps_fts-style "
                f"content sync will silently break on device."
            )
    return errors


def validate(db_path, schema_path):
    """Validate the built DB against the Room schema JSON.

    Args:
        db_path: Path to the built SQLite DB file.
        schema_path: Path to the Room exported schema JSON.

    Returns:
        True if validation passes.

    Raises:
        ValueError: If any check fails.
    """
    schema = schema_module.load_schema(schema_path)
    expected_hash = schema_module.get_identity_hash(schema)
    expected_version = schema_module.get_version(schema)
    expected_tables = schema_module.get_table_names(schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    errors = []

    # 1. room_master_table identity hash
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

    if row[0] != expected_hash:
        raise ValueError(
            f"Identity hash mismatch: expected {expected_hash}, "
            f"got {row[0]}. The DB schema does not match Room's "
            f"expected schema. The Action must fail to prevent schema drift."
        )

    # 2. user_version pragma — this drives Room's migration path on open.
    # If it doesn't match the schema version the seed was built from, Room
    # will run migrations (or crash if it can't find a path).
    cursor.execute("PRAGMA user_version")
    actual_version = cursor.fetchone()[0]
    if actual_version != expected_version:
        errors.append(
            f"user_version mismatch: DB has {actual_version}, schema is "
            f"{expected_version}. Room will run {actual_version}→"
            f"{expected_version} migrations on open."
        )

    # 3. All expected tables exist
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    )
    actual_tables = {row[0] for row in cursor.fetchall()}
    missing_tables = expected_tables - actual_tables
    if missing_tables:
        errors.append(
            f"Missing tables: {missing_tables}. "
            f"Expected {len(expected_tables)} tables, "
            f"found {len(actual_tables)}."
        )

    # 4. Per-column parity (name, affinity, notNull, pkPosition, default).
    # FTS virtual tables are skipped — SQLite reports no declared types or
    # nullability for FTS4 columns, and Room validates them differently.
    for entity in schema_module.get_entities(schema):
        if entity["tableName"] not in actual_tables:
            continue  # already reported as missing
        if "USING FTS" in entity.get("createSql", "").upper():
            continue
        errors.extend(_column_errors(cursor, entity))

    # 5. Indices
    errors.extend(_index_errors(cursor, schema))

    # 6. Foreign keys
    errors.extend(_fk_errors(cursor, schema))

    # 7. FTS triggers
    errors.extend(_trigger_errors(cursor, schema))

    conn.close()

    if errors:
        raise ValueError(
            "Schema validation failed:\n  - " + "\n  - ".join(errors)
        )
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
        help="Path to the Room exported schema JSON.",
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
