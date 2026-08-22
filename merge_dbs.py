#!/usr/bin/env python3
"""Merge 7 per-API SQLite DBs into a single goveye.db.

Each per-API build script (build_mps.py, build_commons_votes.py, etc.)
creates a DB with only its own tables but the FULL schema's identity hash.
This script combines them into one goveye.db that Room accepts.

Per D-10: creates goveye.db with ALL tables from the schema, then copies
data from each per-API DB. FTS4 triggers are created BEFORE data insertion
so the FTS index populates automatically (Pitfall 2).

Missing per-API DBs are skipped — their tables exist but are empty.

Usage:
  python merge_dbs.py --output goveye.db --schema schemas/bundled_schema.json \
    --mps-db mps.db --commons-votes-db commons_votes.db \
    --lords-votes-db lords_votes.db --bills-db bills.db \
    --committees-db committees.db --recess-db recess.db \
    --interests-db interests.db
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
logger = logging.getLogger("merge_dbs")

# Maps each per-API DB argument to the tables it owns.
# FTS tables are excluded — they auto-populate via triggers.
PER_API_TABLES = {
    "mps_db": ["mps"],
    "commons_votes_db": ["divisions", "division_votes"],
    "lords_votes_db": ["divisions", "division_votes"],
    "bills_db": ["bills", "bill_stages"],
    "committees_db": ["committees", "mp_committee_cross_ref"],
    "recess_db": ["recess_dates", "recess_dates_meta"],
    "interests_db": ["interests"],
    "party_stats_db": ["party_stats"],
}


def merge_dbs(output_path, schema_path, mps_db=None, commons_votes_db=None,
              lords_votes_db=None, bills_db=None, committees_db=None,
              recess_db=None, interests_db=None, party_stats_db=None):
    """Merge per-API DBs into a single goveye.db.

    Args:
        output_path: Path for the merged goveye.db file.
        schema_path: Path to the Room exported schema JSON.
        mps_db: Path to mps.db (or None if not available).
        commons_votes_db: Path to commons_votes.db (or None).
        lords_votes_db: Path to lords_votes.db (or None).
        bills_db: Path to bills.db (or None).
        committees_db: Path to committees.db (or None).
        recess_db: Path to recess.db (or None).
        interests_db: Path to interests.db (or None).
        party_stats_db: Path to party_stats.db (or None).
    """
    schema = schema_module.load_schema(schema_path)
    all_table_names = schema_module.get_table_names(schema)

    # Remove existing output file
    if os.path.exists(output_path):
        os.remove(output_path)

    # Create goveye.db with ALL tables + FTS triggers + room_master_table
    conn = schema_module.create_database_with_tables(
        output_path, schema_path, all_table_names
    )
    logger.info("Created %s with %d tables", output_path, len(all_table_names))

    # Map argument names to provided paths
    source_dbs = {
        "mps_db": mps_db,
        "commons_votes_db": commons_votes_db,
        "lords_votes_db": lords_votes_db,
        "bills_db": bills_db,
        "committees_db": committees_db,
        "recess_db": recess_db,
        "interests_db": interests_db,
        "party_stats_db": party_stats_db,
    }

    for arg_name, db_path in source_dbs.items():
        if db_path is None or not os.path.exists(db_path):
            logger.info("Skipping %s (not provided)", arg_name)
            continue

        tables = PER_API_TABLES[arg_name]
        logger.info("Merging %s (%d tables: %s)", arg_name, len(tables), ", ".join(tables))

        src_conn = sqlite3.connect(db_path)
        src_conn.row_factory = sqlite3.Row
        src_cursor = src_conn.cursor()

        for table_name in tables:
            # Get column names from the source table
            try:
                src_cursor.execute(f"SELECT * FROM {table_name} LIMIT 0")
                columns = [desc[0] for desc in src_cursor.description]
            except sqlite3.OperationalError:
                logger.warning("  Table %s not found in %s — skipping", table_name, db_path)
                continue

            if not columns:
                continue

            # Read all rows from source
            col_list = ", ".join(columns)
            src_cursor.execute(f"SELECT {col_list} FROM {table_name}")
            rows = src_cursor.fetchall()

            if not rows:
                logger.info("  %s: 0 rows", table_name)
                continue

            # Insert into destination (INSERT OR REPLACE to handle commons+lords
            # both writing to divisions/division_votes)
            placeholders = ", ".join("?" * len(columns))
            conn.executemany(
                f"INSERT OR REPLACE INTO {table_name} ({col_list}) VALUES ({placeholders})",
                [tuple(row) for row in rows],
            )
            logger.info("  %s: %d rows copied", table_name, len(rows))

        src_conn.close()

    conn.commit()
    conn.close()
    logger.info("Merge complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Merge 7 per-API DBs into a single goveye.db."
    )
    parser.add_argument("--output", required=True, help="Path for merged goveye.db")
    parser.add_argument("--schema", required=True, help="Path to Room schema JSON")
    parser.add_argument("--mps-db", default=None, help="Path to mps.db")
    parser.add_argument("--commons-votes-db", default=None, help="Path to commons_votes.db")
    parser.add_argument("--lords-votes-db", default=None, help="Path to lords_votes.db")
    parser.add_argument("--bills-db", default=None, help="Path to bills.db")
    parser.add_argument("--committees-db", default=None, help="Path to committees.db")
    parser.add_argument("--recess-db", default=None, help="Path to recess.db")
    parser.add_argument("--interests-db", default=None, help="Path to interests.db")
    parser.add_argument("--party-stats-db", default=None, help="Path to party_stats.db")
    args = parser.parse_args()

    merge_dbs(
        args.output, args.schema,
        mps_db=args.mps_db,
        commons_votes_db=args.commons_votes_db,
        lords_votes_db=args.lords_votes_db,
        bills_db=args.bills_db,
        committees_db=args.committees_db,
        recess_db=args.recess_db,
        interests_db=args.interests_db,
        party_stats_db=args.party_stats_db,
    )


if __name__ == "__main__":
    main()
