#!/usr/bin/env python3
"""One-time migration: offset Lords division IDs in debates.db by 1,000,000.

The debates DB (debate_speeches table) stores divisionId references that
point to the divisions table. Lords division IDs were just offset by
1,000,000 in lords_votes.db, so any debate_speeches rows that reference
Lords divisions need their divisionId updated too.

We determine which divisionIds are Lords by checking against the Commons
votes DB — any divisionId NOT in the Commons divisions table is a Lords
division and needs offsetting.

Usage:
  python migrate_debates_lords_ids.py --db debates.db --commons-db commons_votes.db
  python migrate_debates_lords_ids.py --db debates.db --commons-db commons_votes.db --dry-run
"""

import argparse
import os
import sqlite3
import shutil
import time

LORDS_ID_OFFSET = 1_000_000


def migrate(db_path, commons_db_path, dry_run=False):
    if not os.path.exists(db_path):
        print(f"ERROR: {db_path} does not exist")
        return
    if not os.path.exists(commons_db_path):
        print(f"ERROR: {commons_db_path} does not exist")
        return

    # Backup before modifying
    if not dry_run:
        backup_path = db_path + ".pre-migration.bak"
        shutil.copy2(db_path, backup_path)
        print(f"Backed up to {backup_path}")

    # Get the set of Commons division IDs
    commons_conn = sqlite3.connect(commons_db_path)
    commons_cursor = commons_conn.cursor()
    commons_cursor.execute("SELECT id FROM divisions WHERE house = 1")
    commons_ids = {row[0] for row in commons_cursor.fetchall()}
    commons_conn.close()
    print(f"Found {len(commons_ids)} Commons division IDs")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all distinct divisionIds in debates
    cursor.execute("SELECT DISTINCT divisionId FROM debate_speeches")
    all_division_ids = [row[0] for row in cursor.fetchall()]
    print(f"Debates DB references {len(all_division_ids)} distinct division IDs")

    # Determine which are Lords (not in Commons set)
    lords_ids = [did for did in all_division_ids if did not in commons_ids]
    commons_in_debates = [did for did in all_division_ids if did in commons_ids]
    print(f"  Commons divisions: {len(commons_in_debates)}")
    print(f"  Lords divisions: {len(lords_ids)} (need offset)")

    # Count affected rows
    if lords_ids:
        placeholders = ",".join("?" * len(lords_ids))
        cursor.execute(
            f"SELECT COUNT(*) FROM debate_speeches WHERE divisionId IN ({placeholders})",
            lords_ids,
        )
        affected_rows = cursor.fetchone()[0]
        print(f"  Affected debate_speeches rows: {affected_rows}")

    if dry_run:
        print(f"\n[DRY RUN] Would add {LORDS_ID_OFFSET} to divisionId for {len(lords_ids)} Lords divisions")
        conn.close()
        return

    if not lords_ids:
        print("No Lords divisions found — nothing to migrate.")
        conn.close()
        return

    start = time.time()
    cursor.execute("BEGIN TRANSACTION")

    # Update all debate_speeches rows that reference Lords divisions
    # Do it in batches to avoid SQLite parameter limits
    batch_size = 500
    total_updated = 0
    for i in range(0, len(lords_ids), batch_size):
        batch = lords_ids[i:i + batch_size]
        placeholders = ",".join("?" * len(batch))
        cursor.execute(
            f"""UPDATE debate_speeches
                SET divisionId = divisionId + {LORDS_ID_OFFSET}
                WHERE divisionId IN ({placeholders})""",
            batch,
        )
        total_updated += cursor.rowcount

    cursor.execute("COMMIT")

    # Verify
    cursor.execute(f"SELECT MIN(divisionId), MAX(divisionId) FROM debate_speeches")
    new_min, new_max = cursor.fetchone()
    print(f"\nAfter: divisionId range {new_min} to {new_max}")
    print(f"Updated {total_updated} debate_speeches rows")

    elapsed = time.time() - start
    print(f"Migration completed in {elapsed:.1f}s")

    conn.execute("VACUUM")
    conn.close()
    print("VACUUMed database")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Lords division IDs in debates.db by +1,000,000 (one-time)."
    )
    parser.add_argument("--db", required=True, help="Path to debates.db")
    parser.add_argument("--commons-db", required=True, help="Path to commons_votes.db (for ID reference)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying")
    args = parser.parse_args()

    migrate(args.db, args.commons_db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
