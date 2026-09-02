#!/usr/bin/env python3
"""Restore tag tables from a tags.db into a freshly-merged goveye.db.

After merge_dbs.py creates a fresh goveye.db (with empty tag tables), this
script downloads the previous tags.db from the seed-latest release, ATTACHes
it, and copies the 8 tag/source_rec tables into goveye.db. This enables
incremental tag processing — build_tags.py and build_mp_tags.py only need
to process new/changed rows instead of re-processing everything.

Falls back gracefully:
  - If tags.db is missing or not provided → exit 1 (caller should skip
    incremental, do full tag rebuild)
  - If tags.db identity hash doesn't match current schema → exit 1
  - If any table is missing from tags.db → warning, but continue with
    available tables

Tables restored:
  division_tags, bill_tags, tag_metadata, publication_tags,
  statement_tags, legislation_tags, mp_tags, source_recommendations

Usage:
  python restore_tags.py --output goveye.db --tags-db tags.db --schema schemas/bundled_schema.json
"""

import argparse
import logging
import os
import sqlite3
import sys

import schema as schema_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("restore_tags")

TAG_TABLES = [
    "division_tags",
    "bill_tags",
    "tag_metadata",
    "publication_tags",
    "statement_tags",
    "legislation_tags",
    "mp_tags",
    "source_recommendations",
]


def _check_identity_hash(tags_db_path, expected_hash):
    """Check if tags.db has a matching room_master_table identity hash.

    Returns True if the hash matches, False otherwise (including if
    room_master_table is missing — which means the tags.db was built
    before this feature was added, or is corrupted).
    """
    try:
        conn = sqlite3.connect(tags_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='room_master_table'"
        )
        if not cursor.fetchone():
            logger.info("tags.db has no room_master_table — cannot verify schema")
            conn.close()
            return False

        cursor.execute("SELECT identity_hash FROM room_master_table WHERE id = 42")
        row = cursor.fetchone()
        conn.close()

        if not row or row[0] != expected_hash:
            logger.info(
                "tags.db identity hash mismatch (expected %s, got %s) — schema changed",
                expected_hash,
                row[0] if row else "none",
            )
            return False

        logger.info("tags.db identity hash matches current schema")
        return True
    except Exception as e:
        logger.info("Cannot verify tags.db identity hash: %s", e)
        return False


def restore_tags(output_path, tags_db_path, schema_path):
    """Restore tag tables from tags.db into goveye.db.

    Args:
        output_path: Path to the merged goveye.db (modified in-place).
        tags_db_path: Path to the tags.db file from the previous build.
        schema_path: Path to the Room schema JSON (for identity hash check).

    Returns:
        0 on success, 1 if tags.db can't be used (caller should do full rebuild).
    """
    if not os.path.exists(output_path):
        print(f"ERROR: {output_path} does not exist — run merge_dbs.py first")
        return 1

    if not os.path.exists(tags_db_path):
        logger.info("tags.db not found — will skip restore (full tag rebuild needed)")
        return 1

    # Verify schema compatibility
    schema = schema_module.load_schema(schema_path)
    expected_hash = schema_module.get_identity_hash(schema)

    if not _check_identity_hash(tags_db_path, expected_hash):
        logger.info("tags.db schema mismatch — will skip restore (full tag rebuild needed)")
        return 1

    # ATTACH tags.db and copy tables
    conn = sqlite3.connect(output_path)
    cursor = conn.cursor()

    # Use WAL checkpoint to avoid "database is locked" on ATTACH
    cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ATTACH with a unique alias
    cursor.execute("ATTACH DATABASE ? AS tags_src", (tags_db_path,))

    total_rows = 0
    restored_tables = []

    for table_name in TAG_TABLES:
        # Check if table exists in tags.db
        cursor.execute(
            "SELECT name FROM tags_src.sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        if not cursor.fetchone():
            logger.warning("  %s not found in tags.db — skipping (will be empty)", table_name)
            continue

        # Check if table exists in goveye.db (it should — merge_dbs.py creates all tables)
        cursor.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        if not cursor.fetchone():
            logger.warning("  %s not found in goveye.db — skipping", table_name)
            continue

        # Get column names from tags.db
        cursor.execute(f"SELECT * FROM tags_src.{table_name} LIMIT 0")
        columns = [desc[0] for desc in cursor.description]
        col_list = ", ".join(columns)

        # Count rows in source
        cursor.execute(f"SELECT COUNT(*) FROM tags_src.{table_name}")
        src_count = cursor.fetchone()[0]

        # Clear destination and copy
        cursor.execute(f"DELETE FROM main.{table_name}")
        if src_count > 0:
            cursor.execute(
                f"INSERT INTO main.{table_name} ({col_list}) "
                f"SELECT {col_list} FROM tags_src.{table_name}"
            )

        total_rows += src_count
        restored_tables.append((table_name, src_count))
        logger.info("  %s: %d rows restored", table_name, src_count)

    # Commit before DETACH to release any pending statement locks
    conn.commit()
    # Use a fresh cursor for DETACH to avoid "database is locked" from
    # an unfetched result set on the previous cursor
    detach_cursor = conn.cursor()
    detach_cursor.execute("DETACH DATABASE tags_src")
    conn.commit()
    conn.close()

    logger.info("Restored %d tables, %d total rows", len(restored_tables), total_rows)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Restore tag tables from tags.db into a freshly-merged goveye.db."
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to the merged goveye.db (modified in-place).",
    )
    parser.add_argument(
        "--tags-db", required=True,
        help="Path to the tags.db file from the previous build.",
    )
    parser.add_argument(
        "--schema", required=True,
        help="Path to the Room schema JSON (for identity hash verification).",
    )
    args = parser.parse_args()

    sys.exit(restore_tags(args.output, args.tags_db, args.schema))


if __name__ == "__main__":
    main()
