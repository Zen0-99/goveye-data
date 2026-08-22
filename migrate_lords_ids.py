#!/usr/bin/env python3
"""One-time migration: offset Lords division IDs and voter memberIds by 1,000,000.

The Lords Votes API returns division IDs starting from 1, which overlap with
Commons division IDs (1538-2408). Lords voter memberIds also collide with
Commons MP IDs. This script patches an existing lords_votes.db in-place by
adding 1,000,000 to all Lords division IDs and voter memberIds.

This replaces a full 40-minute API re-fetch with a seconds-long SQL migration.

Usage:
  python migrate_lords_ids.py --db lords_votes.db
  python migrate_lords_ids.py --db lords_votes.db --dry-run
"""

import argparse
import os
import sqlite3
import shutil
import time

LORDS_ID_OFFSET = 1_000_000


def migrate(db_path, dry_run=False):
    if not os.path.exists(db_path):
        print(f"ERROR: {db_path} does not exist")
        return

    # Backup before modifying
    if not dry_run:
        backup_path = db_path + ".pre-migration.bak"
        shutil.copy2(db_path, backup_path)
        print(f"Backed up to {backup_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check current state
    cursor.execute("SELECT MIN(id), MAX(id), COUNT(*) FROM divisions WHERE house = 2")
    min_id, max_id, count = cursor.fetchone()
    print(f"Before: {count} Lords divisions, IDs {min_id}–{max_id}")

    cursor.execute("SELECT COUNT(*) FROM division_votes WHERE divisionId < ?", (LORDS_ID_OFFSET,))
    old_votes = cursor.fetchone()[0]
    print(f"Before: {old_votes} division_votes with unoffset divisionId")

    if min_id is not None and min_id >= LORDS_ID_OFFSET:
        print("Already migrated — all Lords IDs are already offset. Nothing to do.")
        conn.close()
        return

    if dry_run:
        # Show what would change
        cursor.execute(
            "SELECT COUNT(DISTINCT memberId) FROM division_votes WHERE divisionId < ?",
            (LORDS_ID_OFFSET,),
        )
        unique_voters = cursor.fetchone()[0]
        print(f"\n[DRY RUN] Would update:")
        print(f"  {count} divisions: id = id + {LORDS_ID_OFFSET}")
        print(f"  {old_votes} division_votes: divisionId = divisionId + {LORDS_ID_OFFSET}")
        print(f"  {unique_voters} voter memberIds: memberId = memberId + {LORDS_ID_OFFSET}")
        conn.close()
        return

    start = time.time()

    # Use a transaction so the migration is atomic
    cursor.execute("BEGIN TRANSACTION")

    # 1. Update division_votes first (foreign key references)
    # Update divisionId for all Lords votes
    cursor.execute(
        f"""UPDATE division_votes
            SET divisionId = divisionId + {LORDS_ID_OFFSET}
            WHERE divisionId < {LORDS_ID_OFFSET}""",
    )
    updated_votes = cursor.rowcount
    print(f"Updated {updated_votes} division_votes.divisionId")

    # Update memberId for all Lords votes (Lords voter IDs also need offset)
    cursor.execute(
        f"""UPDATE division_votes
            SET memberId = memberId + {LORDS_ID_OFFSET}
            WHERE memberId < {LORDS_ID_OFFSET}""",
    )
    updated_voters = cursor.rowcount
    print(f"Updated {updated_voters} division_votes.memberId")

    # 2. Update divisions table
    cursor.execute(
        f"""UPDATE divisions
            SET id = id + {LORDS_ID_OFFSET}
            WHERE house = 2 AND id < {LORDS_ID_OFFSET}""",
    )
    updated_divisions = cursor.rowcount
    print(f"Updated {updated_divisions} divisions.id")

    cursor.execute("COMMIT")

    # Verify
    cursor.execute("SELECT MIN(id), MAX(id), COUNT(*) FROM divisions WHERE house = 2")
    new_min, new_max, new_count = cursor.fetchone()
    print(f"\nAfter: {new_count} Lords divisions, IDs {new_min}–{new_max}")

    cursor.execute(f"SELECT COUNT(*) FROM division_votes WHERE divisionId < {LORDS_ID_OFFSET}")
    remaining_old = cursor.fetchone()[0]
    if remaining_old > 0:
        print(f"WARNING: {remaining_old} votes still have unoffset divisionId!")
    else:
        print("All division_votes have offset divisionId ✓")

    elapsed = time.time() - start
    print(f"\nMigration completed in {elapsed:.1f}s")

    conn.execute("VACUUM")
    conn.close()
    print("VACUUMed database")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Lords division IDs and voter memberIds by +1,000,000 (one-time)."
    )
    parser.add_argument("--db", required=True, help="Path to lords_votes.db")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying")
    args = parser.parse_args()

    migrate(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
