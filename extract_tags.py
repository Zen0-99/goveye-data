#!/usr/bin/env python3
"""Extract tag tables from goveye.db into a standalone tags.db.

Produces a small (~2MB) SQLite file containing the 8 derived tag/source_rec
tables. This file is published alongside goveye.db in the seed-latest release
so that the next seed build can restore the tag tables without downloading
the full 1GB goveye.db.

Tables extracted:
  division_tags, bill_tags, tag_metadata, publication_tags,
  statement_tags, legislation_tags, mp_tags, source_recommendations

Usage:
  python extract_tags.py --input goveye.db --output tags.db
"""

import argparse
import os
import sqlite3
import sys

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


def extract_tags(input_path, output_path):
    """Extract tag tables from goveye.db into tags.db.

    Args:
        input_path: Path to the merged goveye.db.
        output_path: Path for the output tags.db file.

    Returns:
        0 on success, non-zero on error.
    """
    if not os.path.exists(input_path):
        print(f"ERROR: {input_path} does not exist")
        return 1

    # Remove existing output file
    if os.path.exists(output_path):
        os.remove(output_path)

    src_conn = sqlite3.connect(input_path)
    src_conn.row_factory = sqlite3.Row
    src_cursor = src_conn.cursor()

    dst_conn = sqlite3.connect(output_path)
    dst_cursor = dst_conn.cursor()

    total_rows = 0
    extracted_tables = []

    for table_name in TAG_TABLES:
        # Check if table exists in source
        src_cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        if not src_cursor.fetchone():
            print(f"  WARNING: {table_name} not found in source DB — skipping")
            continue

        # Get column names and create SQL from source
        src_cursor.execute(
            f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table_name}'"
        )
        create_sql_row = src_cursor.fetchone()
        if not create_sql_row or not create_sql_row[0]:
            print(f"  WARNING: {table_name} has no CREATE SQL — skipping")
            continue

        # Get column names
        src_cursor.execute(f"SELECT * FROM {table_name} LIMIT 0")
        columns = [desc[0] for desc in src_cursor.description]
        col_list = ", ".join(columns)
        placeholders = ", ".join("?" * len(columns))

        # Create table in destination with same schema
        dst_cursor.execute(create_sql_row[0])

        # Copy all rows
        src_cursor.execute(f"SELECT {col_list} FROM {table_name}")
        rows = src_cursor.fetchall()
        if rows:
            dst_cursor.executemany(
                f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})",
                [tuple(row) for row in rows],
            )

        dst_conn.commit()
        total_rows += len(rows)
        extracted_tables.append((table_name, len(rows)))
        print(f"  {table_name}: {len(rows)} rows")

    # Also copy room_master_table so restore_tags.py can verify identity hash
    src_cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='room_master_table'"
    )
    if src_cursor.fetchone():
        src_cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='room_master_table'"
        )
        create_sql_row = src_cursor.fetchone()
        if create_sql_row and create_sql_row[0]:
            dst_cursor.execute("DROP TABLE IF EXISTS room_master_table")
            dst_cursor.execute(create_sql_row[0])
            src_cursor.execute("SELECT * FROM room_master_table")
            rm_rows = src_cursor.fetchall()
            rm_cols = [desc[0] for desc in src_cursor.description]
            rm_col_list = ", ".join(rm_cols)
            rm_placeholders = ", ".join("?" * len(rm_cols))
            if rm_rows:
                dst_cursor.executemany(
                    f"INSERT INTO room_master_table ({rm_col_list}) VALUES ({rm_placeholders})",
                    [tuple(row) for row in rm_rows],
                )
            dst_conn.commit()
            print(f"  room_master_table: {len(rm_rows)} rows (for identity hash verification)")

    dst_conn.close()
    src_conn.close()

    output_size = os.path.getsize(output_path)
    print(f"\nExtracted {len(extracted_tables)} tables, {total_rows} total rows")
    print(f"tags.db size: {output_size / 1024:.0f} KB")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Extract tag tables from goveye.db into a standalone tags.db."
    )
    parser.add_argument(
        "--input", required=True,
        help="Path to the merged goveye.db.",
    )
    parser.add_argument(
        "--output", required=True,
        help="Path for the output tags.db file.",
    )
    args = parser.parse_args()

    sys.exit(extract_tags(args.input, args.output))


if __name__ == "__main__":
    main()
