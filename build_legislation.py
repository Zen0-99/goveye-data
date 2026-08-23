#!/usr/bin/env python3
"""Per-API build script for legislation.gov.uk.

Fetches new legislation from the legislation.gov.uk Atom feed
(www.legislation.gov.uk/new/data.feed) and builds a per-API DB
(legislation.db) with only the legislation table + the full schema's
Room identity hash.

Pitfall 5: uses /new/data.feed specifically (not the default feed) to
get genuinely new legislation, not modifications to old legislation.

Pagination via <atom:link rel="next"> element in the Atom XML.

Modes:
  seed  — create fresh DB, fetch new legislation, insert
  delta — copy previous DB, fetch new legislation, upsert

Usage:
  python build_legislation.py --output legislation.db --schema schemas/bundled_schema.json --mode seed
  python build_legislation.py --output legislation.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_legislation.db
"""

import argparse
import os
import shutil
import sqlite3
import time
import xml.etree.ElementTree as ET

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

LEGISLATION_NEW = "https://www.legislation.gov.uk/new/data.feed"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
LEG_NS = "{http://www.legislation.gov.uk/namespaces/metadata}"
TABLE_NAMES = ["legislation"]


# --- legislation.gov.uk Atom feed ---

def fetch_new_legislation(max_pages=20):
    """Fetch new legislation from legislation.gov.uk Atom feed.

    Parses Atom XML with xml.etree.ElementTree. For each entry, extracts
    id, title, updated, published from ATOM_NS elements, and type, year,
    number, date from LEG_NS elements.

    Pagination via <atom:link rel="next"> — follow until no next link
    or max_pages reached.

    Pitfall 5: uses /new/data.feed specifically for genuinely new legislation.

    Args:
        max_pages: Maximum number of pages to fetch (default 20).

    Returns:
        List of dicts with keys: id, title, type, year, number, date, url,
        updated, published.
    """
    all_entries = []
    url = LEGISLATION_NEW

    for page in range(max_pages):
        logger.info("Fetching legislation feed: page %d (%s)", page + 1, url)
        r = api_get(url, timeout=60)
        root = ET.fromstring(r.text)

        for entry in root.findall(f"{ATOM_NS}entry"):
            entry_data = {
                "id": entry.findtext(f"{ATOM_NS}id", ""),
                "title": entry.findtext(f"{ATOM_NS}title", ""),
                "updated": entry.findtext(f"{ATOM_NS}updated", ""),
                "published": entry.findtext(f"{ATOM_NS}published", ""),
                "type": "",
                "year": 0,
                "number": 0,
                "date": "",
                "url": "",
            }

            # Extract legislation metadata from LEG_NS elements
            for elem in entry:
                if elem.tag.startswith(LEG_NS):
                    tag_name = elem.tag.replace(LEG_NS, "")
                    if tag_name in ("type", "date"):
                        entry_data[tag_name] = elem.text or ""
                    elif tag_name in ("year", "number"):
                        try:
                            entry_data[tag_name] = int(elem.text) if elem.text else 0
                        except (ValueError, TypeError):
                            entry_data[tag_name] = 0

            # URL is the entry id (legislation.gov.uk URL)
            entry_data["url"] = entry_data["id"]

            all_entries.append(entry_data)

        # Find next page link
        next_link = None
        for link in root.findall(f"{ATOM_NS}link"):
            if link.get("rel") == "next":
                next_link = link.get("href")
                break
        if not next_link:
            break
        url = next_link
        time.sleep(API_DELAY)

    logger.info("Fetched %d legislation entries (%d pages)", len(all_entries), page + 1)
    return all_entries


# --- Mapping + insertion ---

def map_legislation_to_entity(entry_data, timestamp_millis, leg_id):
    """Map a legislation entry dict to a legislation row tuple.

    Matches LegislationEntity fields:
    (id, title, type, year, number, date, url, lastUpdated)

    Args:
        entry_data: Dict from fetch_new_legislation.
        timestamp_millis: Build timestamp.
        leg_id: Sequential integer ID assigned by the build script.
    """
    return (
        leg_id,
        entry_data.get("title") or "",
        entry_data.get("type") or "",
        entry_data.get("year") or 0,
        entry_data.get("number") or 0,
        entry_data.get("date") or "",
        entry_data.get("url") or entry_data.get("id") or "",
        timestamp_millis,
    )


def insert_legislation(conn, legislation, timestamp_millis):
    """Insert legislation into the legislation table using batch executemany.

    Uses INSERT OR REPLACE so this works for both seed and delta modes.
    """
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO legislation (
            id, title, type, year, number, date, url, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = [map_legislation_to_entity(entry, timestamp_millis, i + 1) for i, entry in enumerate(legislation)]

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted legislation: %d/%d", min(i + BATCH_SIZE, len(rows)), len(rows))


# --- Build modes ---

def build_seed(output_path, schema_path, max_pages=20, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch new legislation, insert."""
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        logger.info("Resuming from checkpoint")
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )

    legislation = fetch_new_legislation(max_pages=max_pages)
    if legislation:
        insert_legislation(conn, legislation, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s (%d entries)", output_path, len(legislation))


def build_delta(output_path, previous_db, schema_path, max_pages=20):
    """Delta mode: copy previous DB, fetch new legislation, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    legislation = fetch_new_legislation(max_pages=max_pages)
    if legislation:
        insert_legislation(conn, legislation, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s (%d entries)", output_path, len(legislation))


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Legislation per-API SQLite database (legislation.db)."
    )
    parser.add_argument(
        "--output", default="legislation.db",
        help="Output path for the SQLite DB file. Default: legislation.db.",
    )
    parser.add_argument(
        "--schema", required=True,
        help="Path to the Room exported schema JSON (bundled_schema.json).",
    )
    parser.add_argument(
        "--mode", choices=["seed", "delta"], default="seed",
        help="Build mode: seed (full) or delta (incremental). Default: seed.",
    )
    parser.add_argument(
        "--previous-db",
        help="Path to previous DB file (required for delta mode).",
    )
    parser.add_argument(
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only). Upserts on top of existing data.",
    )
    parser.add_argument(
        "--max-pages", type=int, default=20,
        help="Maximum number of Atom feed pages to fetch (default: 20).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, max_pages=args.max_pages, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema, max_pages=args.max_pages)


if __name__ == "__main__":
    main()
