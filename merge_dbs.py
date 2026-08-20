#!/usr/bin/env python3
"""Merge the 6 per-API DBs into one goveye.db (D-10a).

Combines mps.db, commons_votes.db, lords_votes.db, bills.db, committees.db,
and recess.db into a single goveye.db with all 16 tables + the correct Room
identity hash. Used for the seed release only (the first-launch download
source, D-04).

Commons and Lords votes are separate per-API DBs that both write to the
divisions + division_votes tables. Commons divisions have house=1, Lords
have house=2 — their IDs come from different API systems and do not
overlap. Both are merged via INSERT OR REPLACE into the same tables.

Implementation:
  1. Create goveye.db with ALL 16 tables + FTS triggers + room_master_table
     (full identity hash) via create_database_with_tables.
  2. ATTACH each per-API DB and copy data via INSERT OR REPLACE.
  3. The FTS4 triggers on goveye.db auto-populate mps_fts when mps data is
     copied (triggers created by create_database_with_tables).
  4. VACUUM to minimize size.

Tables not populated by any build script (hansard_contributions, interests,
remote_keys, follows, bill_follows, mp_notification_prefs) remain empty —
they're created by the schema but have no data (hansard/interests are
future use; follows/bill_follows/mp_notification_prefs are user data
created at runtime by LocalDatabase per D-10a).

Missing per-API DBs are handled gracefully (skip with warning, table empty).

Usage:
  python merge_dbs.py --output goveye.db --schema schemas/bundled_schema.json \
      --mps-db mps.db --commons-votes-db commons_votes.db --lords-votes-db lords_votes.db \
      --bills-db bills.db --committees-db committees.db --recess-db recess.db
"""

import argparse
import os
import sqlite3

import schema as schema_module
from api_helper import logger

# Maps each per-API DB arg name to (api_name, table_names)
SOURCE_MAP = [
    ("mps_db", "mps", ["mps", "mps_fts"]),
    ("commons_votes_db", "commons_votes", ["divisions", "division_votes"]),
    ("lords_votes_db", "lords_votes", ["divisions", "division_votes"]),
    ("bills_db", "bills", ["bills", "bill_stages"]),
    ("committees_db", "committees", ["committees", "mp_committee_cross_ref"]),
    ("recess_db", "recess", ["recess_dates", "recess_dates_meta"]),
]


def merge_dbs(output_path, schema_path, mps_db, commons_votes_db,
              lords_votes_db, bills_db, committees_db, recess_db):
    """Merge the 6 per-API DBs into one goveye.db.

    Args:
        output_path: Path for the merged goveye.db.
        schema_path: Path to the Room schema JSON (bundled_schema.json).
        mps_db, commons_votes_db, lords_votes_db, bills_db, committees_db,
        recess_db: Paths to the 6 per-API DBs. Any may be None or missing
        (skipped with warning).
    """
    all_table_names = schema_module.get_all_table_names(schema_path)

    # 1. Create goveye.db with ALL 16 tables + FTS triggers + room_master_table
    conn = schema_module.create_database_with_tables(
        output_path, schema_path, all_table_names,
    )
    logger.info("Created merged DB with all %d tables", len(all_table_names))

    sources = {
        "mps_db": mps_db,
        "commons_votes_db": commons_votes_db,
        "lords_votes_db": lords_votes_db,
        "bills_db": bills_db,
        "committees_db": committees_db,
        "recess_db": recess_db,
    }

    # 2. ATTACH each per-API DB and copy data
    for arg_name, api_name, table_names in SOURCE_MAP:
        db_path = sources[arg_name]
        if not db_path or not os.path.exists(db_path):
            logger.warning(
                "Missing %s DB (%s) — table(s) %s will be empty",
                api_name, db_path, table_names,
            )
            continue

        attach_alias = f"src_{api_name}"
        conn.execute(f"ATTACH DATABASE '{db_path}' AS {attach_alias}")
        logger.info("Attached %s DB: %s", api_name, db_path)

        for table_name in table_names:
            # mps_fts is a virtual FTS4 table — skip copying it directly;
            # the FTS4 triggers on goveye.db auto-populate mps_fts when mps
            # rows are copied.
            if table_name == "mps_fts":
                continue
            try:
                conn.execute(
                    f"INSERT OR REPLACE INTO {table_name} "
                    f"SELECT * FROM {attach_alias}.{table_name}"
                )
                conn.commit()  # commit before DETACH (avoids "database locked")
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table_name}"
                ).fetchone()[0]
                logger.info("Copied %d rows into %s", count, table_name)
            except sqlite3.OperationalError as e:
                logger.warning(
                    "Failed to copy %s from %s DB: %s", table_name, api_name, e,
                )

        conn.commit()  # ensure no open transaction before DETACH
        conn.execute(f"DETACH DATABASE {attach_alias}")

    conn.commit()

    # 3. VACUUM to minimize file size
    logger.info("VACUUMing merged database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Merge complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Merge the 6 per-API DBs into one goveye.db (seed release)."
    )
    parser.add_argument(
        "--output", default="goveye.db",
        help="Output path for the merged DB. Default: goveye.db.",
    )
    parser.add_argument(
        "--schema", required=True,
        help="Path to the Room exported schema JSON (bundled_schema.json).",
    )
    parser.add_argument("--mps-db", default=None, help="Path to mps.db")
    parser.add_argument("--commons-votes-db", default=None, help="Path to commons_votes.db")
    parser.add_argument("--lords-votes-db", default=None, help="Path to lords_votes.db")
    parser.add_argument("--bills-db", default=None, help="Path to bills.db")
    parser.add_argument("--committees-db", default=None, help="Path to committees.db")
    parser.add_argument("--recess-db", default=None, help="Path to recess.db")
    args = parser.parse_args()

    merge_dbs(
        args.output, args.schema,
        mps_db=args.mps_db,
        commons_votes_db=args.commons_votes_db,
        lords_votes_db=args.lords_votes_db,
        bills_db=args.bills_db,
        committees_db=args.committees_db,
        recess_db=args.recess_db,
    )


if __name__ == "__main__":
    main()
