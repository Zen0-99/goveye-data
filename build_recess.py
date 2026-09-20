#!/usr/bin/env python3
"""Per-API build script for recess dates (D-10, D-11).

Fetches recess dates from the Egg Timer API (HTML scraping) and builds a
per-API DB (recess.db) with only the recess_dates + recess_dates_meta
tables.

The Egg Timer API returns HTML-rendered tables, not JSON. This script
parses the HTML to extract (description, startDate, endDate) tuples,
matching EggTimerApi.kt's parseRecessDates() logic.

Modes:
  seed  — create fresh DB, fetch + parse recess dates for both houses
  delta — copy previous DB, delete old rows, re-fetch + insert

Usage:
  python build_recess.py --output recess.db --schema schemas/8.json --mode seed
  python build_recess.py --output recess.db --schema schemas/8.json --mode delta --previous-db prev_recess.db
"""

import argparse
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime

import schema as schema_module
from api_helper import api_get, API_DELAY, logger

# --- Constants ---

EGG_TIMER_BASE = "https://api.parliament.uk"
# houseId: 1 = Commons, 2 = Lords
HOUSES = [1, 2]

TABLE_NAMES = ["recess_dates", "recess_dates_meta"]


# --- Egg Timer API (HTML scraping) ---

def fetch_recess_html(house_id):
    """Fetch the recess dates HTML page for a given house."""
    r = api_get(
        f"{EGG_TIMER_BASE}/egg-timer/houses/{house_id}/recess-dates",
        timeout=60,
    )
    # The API returns HTML, not JSON — use .text not .json()
    return r.text


def parse_recess_dates(html):
    """Parse the recess dates HTML page.

    The Egg Timer API returns an HTML page with a <table> of recess dates.
    Each row is <tr><td>description</td><td>start date</td><td>end date</td></tr>
    with dates in format "Friday 18 December 2026".

    Also handles the legacy │/|-delimited text format (per EggTimerApi.kt's
    parseRecessDates) as a fallback.

    Returns:
        List of (description, start_date_iso, end_date_iso) tuples
        (dates in ISO yyyy-MM-dd format).
    """
    results = []

    # Primary: parse HTML <tr><td>...</td></tr> rows
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    cell_pattern = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)

    for row_match in row_pattern.finditer(html):
        row_html = row_match.group(1)
        cells = cell_pattern.findall(row_html)
        # Strip HTML tags/whitespace from each cell
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        cells = [c for c in cells if c]
        if len(cells) < 3:
            continue
        # Only rows whose first cell mentions "recess"
        if "recess" not in cells[0].lower():
            continue
        description = cells[0]
        start_date = parse_date(cells[1])
        end_date = parse_date(cells[2])
        if start_date is None or end_date is None:
            continue
        results.append((description, start_date, end_date))

    if results:
        return results

    # Fallback: legacy │/|-delimited text format (EggTimerApi.kt style)
    lines = html.split("\n")
    for line in lines:
        if "recess" not in line.lower():
            continue
        cells = []
        for cell in line.replace("│", "|").split("|"):
            cell = cell.strip()
            if cell:
                cells.append(cell)
        if len(cells) < 3:
            continue
        description = cells[0]
        start_date = parse_date(cells[1])
        end_date = parse_date(cells[2])
        if start_date is None or end_date is None:
            continue
        results.append((description, start_date, end_date))

    return results


def parse_date(text):
    """Parse a date string in format 'Friday 18 December 2026' → 'yyyy-MM-dd'.

    Returns None if parsing fails.
    """
    text = text.strip()
    for fmt in ("%A %d %B %Y", "%A %d %b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def insert_recess_dates(conn, house_id, recess_dates, timestamp_millis):
    """Upsert recess dates for a house, keyed on (description, startDate).

    recess_dates.id is AUTOINCREMENT — delete+reinsert re-keys every row each
    run and turns every patch into a full-table dump. Reuse existing ids,
    update endDate in place, and delete ids whose rows vanished upstream.
    """
    cursor = conn.cursor()
    existing = {}
    for rid, desc, start, end in cursor.execute(
        "SELECT id, description, startDate, endDate FROM recess_dates WHERE house = ?",
        (house_id,),
    ):
        existing.setdefault((desc, start), []).append((rid, end))

    next_id = cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM recess_dates").fetchone()[0]
    for desc, start, end in recess_dates:
        bucket = existing.get((desc, start))
        if bucket:
            rid, old_end = bucket.pop()
            if old_end != end:
                cursor.execute("UPDATE recess_dates SET endDate = ? WHERE id = ?", (end, rid))
        else:
            cursor.execute(
                "INSERT INTO recess_dates (id, house, description, startDate, endDate)"
                " VALUES (?, ?, ?, ?, ?)",
                (next_id, house_id, desc, start, end),
            )
            next_id += 1

    stale = [rid for ids in existing.values() for rid, _ in ids]
    cursor.executemany("DELETE FROM recess_dates WHERE id = ?", [(rid,) for rid in stale])
    conn.commit()
    logger.info("House %d: %d recess dates upserted, %d deleted", house_id, len(recess_dates), len(stale))


def update_recess_meta(conn, house_id, timestamp_millis):
    """Insert/replace the recess_dates_meta row for a house."""
    cursor = conn.cursor()
    cursor.execute(
        """INSERT OR REPLACE INTO recess_dates_meta
           (house, lastRefreshedAt) VALUES (?, ?)""",
        (house_id, timestamp_millis),
    )
    conn.commit()


# --- Build modes ---

def fetch_and_insert_recess(conn, timestamp_millis):
    """Fetch + parse + insert recess dates for both houses."""
    for house_id in HOUSES:
        try:
            html = fetch_recess_html(house_id)
            recess_dates = parse_recess_dates(html)
            if not recess_dates:
                # Empty parse means the page format changed, not that the
                # house has no recesses — keep stored rows rather than wiping.
                logger.warning("House %d: parsed 0 recess dates, keeping stored rows", house_id)
                continue
            insert_recess_dates(conn, house_id, recess_dates, timestamp_millis)
            update_recess_meta(conn, house_id, timestamp_millis)
        except Exception as e:
            logger.warning("Recess fetch failed for house %d: %s", house_id, e)
        time.sleep(API_DELAY)


def build_seed(output_path, schema_path, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch + insert recess dates for both houses.

    If checkpoint_db exists, copies it and re-fetches (recess dates are tiny
    and can change, so we always re-fetch both houses — the checkpoint just
    preserves the DB structure and any previously fetched data).
    """
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        logger.info("Resuming from checkpoint: upserting fresh recess dates")
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )

    fetch_and_insert_recess(conn, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path):
    """Delta mode: copy previous DB, delete old rows, re-fetch + insert.

    Recess dates are small and can change, so delta mode re-fetches all
    rather than diffing.
    """
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    fetch_and_insert_recess(conn, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye recess dates per-API SQLite database (recess.db)."
    )
    parser.add_argument(
        "--output", default="recess.db",
        help="Output path for the SQLite DB file. Default: recess.db.",
    )
    parser.add_argument(
        "--schema", required=True,
        help="Path to the Room exported schema JSON (8.json).",
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
        help="Path to a checkpoint DB to resume from (seed mode only). Re-fetches recess dates on top of existing DB.",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema)


if __name__ == "__main__":
    main()
