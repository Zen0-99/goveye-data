#!/usr/bin/env python3
"""Migrate existing interests DBs to add 16 structured fields (Phase 18).

This is a derived-column migration — it does NOT re-fetch from the API.
It reads the existing summary text, parses it with parse_interest_summary(),
and UPDATEs the new columns.

Per goveye-data/AGENTS.md: derived column changes should run SQL directly
against the existing DB, not re-run the build script.

Usage:
  python migrate_interests_structured.py --db interests.db
  python migrate_interests_structured.py --db interests_historical.db
  python migrate_interests_structured.py --db goveye.db
"""

import argparse
import sqlite3
import time

from build_interests import parse_interest_summary, STRUCTURED_FIELDS


def migrate_db(db_path):
    """Add 16 structured columns to the interests table and populate them."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Check existing columns
    cur.execute("PRAGMA table_info(interests)")
    existing_cols = {row[1] for row in cur.fetchall()}
    print(f"Existing columns: {len(existing_cols)}")

    # Add missing columns (idempotent)
    added = 0
    for field in STRUCTURED_FIELDS:
        if field not in existing_cols:
            cur.execute(f"ALTER TABLE interests ADD COLUMN `{field}` TEXT")
            added += 1
    print(f"Added {added} new columns")
    conn.commit()

    # Read all rows and parse summaries
    cur.execute("SELECT id, summary, categoryNumber FROM interests")
    rows = cur.fetchall()
    print(f"Processing {len(rows)} rows...")

    updated = 0
    start = time.time()

    for i, (row_id, summary, cat_num) in enumerate(rows):
        parsed = parse_interest_summary(summary or "", cat_num or "")

        # Build UPDATE for non-null fields only
        set_clauses = []
        values = []
        for field in STRUCTURED_FIELDS:
            if parsed[field] is not None:
                set_clauses.append(f"`{field}` = ?")
                values.append(parsed[field])

        if set_clauses:
            values.append(row_id)
            sql = f"UPDATE interests SET {', '.join(set_clauses)} WHERE id = ?"
            cur.execute(sql, values)
            updated += 1

        if (i + 1) % 5000 == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(rows)} ({updated} updated)")

    conn.commit()
    elapsed = time.time() - start
    print(f"Done: {updated}/{len(rows)} rows updated in {elapsed:.1f}s")

    # Coverage check
    cur.execute(f"""
        SELECT COUNT(*) FROM interests WHERE
        {" OR ".join(f"`{f}` IS NOT NULL" for f in STRUCTURED_FIELDS)}
    """)
    structured = cur.fetchone()[0]
    total = len(rows)
    pct = 100 * structured // total if total > 0 else 0
    print(f"Coverage: {structured}/{total} ({pct}%) have at least one structured field")

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Migrate interests DB to add 16 structured fields (Phase 18)."
    )
    parser.add_argument("--db", required=True, help="Path to the SQLite DB file.")
    args = parser.parse_args()
    migrate_db(args.db)


if __name__ == "__main__":
    main()
