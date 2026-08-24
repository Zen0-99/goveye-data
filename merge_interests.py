"""Merge live and historical interests databases.

Merges the live interests DB (from Parliament API via build_interests.py)
with the historical interests DB (from mySociety CSV via
build_historical_interests.py) into a single output DB.

Strategy:
- Start with the live DB (copy it as the output)
- Insert historical interests using INSERT OR IGNORE — live data takes
  precedence (live data is more current and authoritative)
- Historical data fills in gaps for MPs/periods not covered by the live API

Usage:
    python merge_interests.py --live interests.db --historical interests_historical.db --output interests_merged.db
"""

import argparse
import logging
import os
import shutil
import sqlite3
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def merge_interests(live_db, historical_db, output_db):
    """Merge historical interests into the live DB.

    Args:
        live_db: Path to the live interests DB (Parliament API data).
        historical_db: Path to the historical interests DB (mySociety CSV data).
        output_db: Path for the merged output DB.
    """
    # Start with a copy of the live DB
    shutil.copy2(live_db, output_db)
    logger.info("Copied live DB to %s", output_db)

    # Checkpoint the historical DB's WAL file to avoid "database is locked"
    # when attaching. The build_historical_interests.py script may leave a
    # -wal file that hasn't been checkpointed.
    hist_conn = sqlite3.connect(historical_db)
    hist_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    hist_conn.close()

    conn = sqlite3.connect(output_db)
    cursor = conn.cursor()

    # Count existing (live) interests
    cursor.execute("SELECT COUNT(*) FROM interests")
    live_count = cursor.fetchone()[0]
    logger.info("Live interests: %d", live_count)

    # Attach the historical DB and merge
    conn.execute("ATTACH DATABASE ? AS hist", (historical_db,))

    # Check if historical DB has interests table
    cursor.execute(
        "SELECT name FROM hist.sqlite_master WHERE type='table' AND name='interests'"
    )
    if cursor.fetchone() is None:
        logger.warning("Historical DB has no interests table — skipping merge")
        conn.execute("DETACH DATABASE hist")
        conn.close()
        return

    cursor.execute("SELECT COUNT(*) FROM hist.interests")
    hist_count = cursor.fetchone()[0]
    logger.info("Historical interests: %d", hist_count)

    # Insert historical interests that don't conflict with live data
    # INSERT OR IGNORE skips rows where the primary key (id) already exists
    insert_sql = """
        INSERT OR IGNORE INTO interests (
            id, memberId, summary, categoryId, categoryNumber, categoryName,
            registrationDate, publishedDate, rectified, fieldsJson, lastUpdated,
            parsedAmountPence, currencyCode, bucket
        )
        SELECT id, memberId, summary, categoryId, categoryNumber, categoryName,
               registrationDate, publishedDate, rectified, fieldsJson, lastUpdated,
               parsedAmountPence, currencyCode, bucket
        FROM hist.interests
    """
    cursor.execute(insert_sql)
    inserted = cursor.rowcount
    logger.info("Merged %d historical interests (INSERT OR IGNORE)", inserted)

    conn.execute("DETACH DATABASE hist")

    # Final count
    cursor.execute("SELECT COUNT(*) FROM interests")
    final_count = cursor.fetchone()[0]
    logger.info("Final merged interests: %d (live=%d, historical_added=%d)",
                final_count, live_count, inserted)

    # VACUUM to minimize file size
    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.commit()
    conn.close()
    logger.info("Merge complete: %s", output_db)


def main():
    parser = argparse.ArgumentParser(
        description="Merge live and historical interests databases."
    )
    parser.add_argument("--live", required=True, help="Path to live interests DB")
    parser.add_argument("--historical", required=True, help="Path to historical interests DB")
    parser.add_argument("--output", required=True, help="Path for merged output DB")
    args = parser.parse_args()

    if not os.path.exists(args.live):
        logger.error("Live DB not found: %s", args.live)
        return 1
    if not os.path.exists(args.historical):
        logger.error("Historical DB not found: %s", args.historical)
        return 1

    merge_interests(args.live, args.historical, args.output)
    return 0


if __name__ == "__main__":
    exit(main())
