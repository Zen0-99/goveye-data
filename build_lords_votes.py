#!/usr/bin/env python3
"""Per-API build script for Lords voting data.

Fetches Lords divisions + votes only and builds a per-API DB
(lords_votes.db) with only the divisions + division_votes tables
(house=2 rows only).

Lords API uses PascalCase paths (Divisions/search) and camelCase field
names. Returns a bare JSON list.

Modes:
  seed  — create fresh DB, fetch all Lords divisions + votes
  delta — copy previous DB, fetch only NEW Lords divisions (D-03)

Usage:
  python build_lords_votes.py --output lords_votes.db --schema schemas/bundled_schema.json --mode seed
  python build_lords_votes.py --output lords_votes.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_lords_votes.db
"""

import argparse
import os
import shutil
import sqlite3
import time

import schema as schema_module
from api_helper import api_get, fetch_twfy_debate_url, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

LORDS_VOTES_BASE = "https://lordsvotes-api.parliament.uk/data/"

PAGE_SIZE_DIVISIONS = 25

# Lords division IDs and voter memberIds are offset by this amount to
# avoid collisions with Commons IDs. The Lords API returns division IDs
# starting from 1, which overlap with Commons division IDs (1538-2408).
# Lords voter memberIds also collide with Commons MP IDs. Adding 1,000,000
# ensures no overlap: Lords division 1 becomes 1000001, Lords voter 5131
# becomes 1005131, etc.
LORDS_ID_OFFSET = 1_000_000

TABLE_NAMES = ["divisions", "division_votes"]


# --- Lords Votes API ---

def fetch_lords_divisions(divisions_limit=None):
    """Fetch all Lords divisions from the Lords Votes API.

    Lords API uses PascalCase paths (Divisions/search) and camelCase field
    names. Returns a bare JSON list.
    """
    divisions = []
    skip = 0

    while True:
        params = {
            "itemsPerPage": PAGE_SIZE_DIVISIONS,
            "skip": skip,
        }
        logger.info("Fetching Lords divisions: skip=%d", skip)
        r = api_get(
            f"{LORDS_VOTES_BASE}Divisions/search",
            params=params,
            timeout=60,
        )
        page = r.json()  # Lords API also returns a bare list

        if not page:
            break

        divisions.extend(page)

        if divisions_limit is not None and len(divisions) >= divisions_limit:
            divisions = divisions[:divisions_limit]
            break

        if len(page) < PAGE_SIZE_DIVISIONS:
            break

        skip += PAGE_SIZE_DIVISIONS
        time.sleep(API_DELAY)

    logger.info("Fetched %d Lords divisions", len(divisions))
    return divisions


def fetch_lords_division_detail(division_id):
    """Fetch full Lords division detail with all voter lists."""
    r = api_get(
        f"{LORDS_VOTES_BASE}Divisions/{division_id}",
        timeout=60,
    )
    return r.json()


def map_lords_division_to_entity(div, timestamp_millis, twfy_debate_url=None):
    """Map a Lords division to a divisions table row tuple (house=2).

    Lords API uses camelCase field names. Content → AYE, Not Content → NO.
    Division ID is offset by LORDS_ID_OFFSET to avoid collisions with
    Commons division IDs.
    """
    return (
        div.get("divisionId", 0) + LORDS_ID_OFFSET,
        div.get("title", ""),
        div.get("date", ""),
        None,  # publicationUpdated — Lords API doesn't provide this
        div.get("number"),
        0,  # isDeferred — Lords divisions are not deferred
        div.get("memberContentCount", 0),
        div.get("memberNotContentCount", 0),
        2,  # house=2 for Lords (D-07)
        timestamp_millis,
        twfy_debate_url,
    )


def map_lords_voter_to_entity(voter, division_id, vote, is_teller=0):
    """Map a Lords voter to a division_votes row tuple (camelCase fields).

    Both division_id and voter memberId are offset by LORDS_ID_OFFSET.
    The division_id passed in is already offset (from map_lords_division_to_entity).
    The voter memberId is offset here.
    """
    return (
        division_id,
        (voter.get("memberId") or 0) + LORDS_ID_OFFSET,
        vote,
        voter.get("name") or "",
        voter.get("party", "") or "",
        voter.get("partyColour", "") or "",
        voter.get("memberFrom", "") or "",
        is_teller,
        None,  # proxyName — Lords API doesn't provide this
    )


def insert_lords_division(conn, div, timestamp_millis):
    """Insert a Lords division and all its votes into the DB.

    Lords members are NOT inserted into the mps table — only Commons MPs
    are in the directory (House=1). Lords votes are self-contained in
    division_votes with denormalized memberName/partyName.
    """
    cursor = conn.cursor()
    raw_division_id = div.get("divisionId", 0)
    division_id = raw_division_id + LORDS_ID_OFFSET  # offset for storage

    # Fetch the TWFY debate URL (scrape the TWFY division page for the GID)
    date_only = (div.get("date") or "").split("T")[0]
    twfy_url = fetch_twfy_debate_url(date_only, div.get("number"), 2)

    cursor.execute(
        """INSERT OR REPLACE INTO divisions
           (id, title, date, publicationUpdated, number, isDeferred,
            ayeCount, noCount, house, lastUpdated, twfyDebateUrl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        map_lords_division_to_entity(div, timestamp_millis, twfy_url),
    )

    # Fetch detail using the RAW (unoffset) ID — the API doesn't know about our offset
    detail = fetch_lords_division_detail(raw_division_id)

    votes = []
    for voter in detail.get("contents", []):
        votes.append(map_lords_voter_to_entity(voter, division_id, "AYE", 0))
    for voter in detail.get("notContents", []):
        votes.append(map_lords_voter_to_entity(voter, division_id, "NO", 0))
    for teller in detail.get("contentTellers", []) or []:
        votes.append(map_lords_voter_to_entity(teller, division_id, "AYE", 1))
    for teller in detail.get("notContentTellers", []) or []:
        votes.append(map_lords_voter_to_entity(teller, division_id, "NO", 1))

    vote_sql = """INSERT OR REPLACE INTO division_votes
        (divisionId, memberId, vote, memberName, partyName,
         partyColour, constituencyName, isTeller, proxyName)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""

    for i in range(0, len(votes), BATCH_SIZE):
        batch = votes[i:i + BATCH_SIZE]
        cursor.executemany(vote_sql, batch)
        conn.commit()

    logger.info(
        "Lords division %d: %d votes inserted", division_id, len(votes),
    )


# --- Delta mode helpers ---

def get_max_division_id(conn, house):
    """Get the maximum division ID for a given house from the DB (D-03)."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MAX(id) FROM divisions WHERE house = ?", (house,)
    )
    result = cursor.fetchone()
    return result[0] if result and result[0] is not None else 0


def fetch_lords_divisions_since(max_id, divisions_limit=None):
    """Fetch Lords divisions with divisionId > max_id (delta mode).

    max_id is the stored (offset) ID from the DB. Subtract LORDS_ID_OFFSET
    to get the raw API ID for comparison.
    """
    raw_max_id = max_id - LORDS_ID_OFFSET if max_id > LORDS_ID_OFFSET else 0
    divisions = []
    skip = 0

    while True:
        params = {
            "itemsPerPage": PAGE_SIZE_DIVISIONS,
            "skip": skip,
        }
        logger.info("Fetching Lords divisions (delta): skip=%d", skip)
        r = api_get(
            f"{LORDS_VOTES_BASE}Divisions/search",
            params=params,
            timeout=60,
        )
        page = r.json()

        if not page:
            break

        new_count = 0
        for div in page:
            div_id = div.get("divisionId", 0)
            if div_id > raw_max_id:
                divisions.append(div)
                new_count += 1

        if new_count == 0:
            break

        if divisions_limit is not None and len(divisions) >= divisions_limit:
            divisions = divisions[:divisions_limit]
            break

        if len(page) < PAGE_SIZE_DIVISIONS:
            break

        skip += PAGE_SIZE_DIVISIONS
        time.sleep(API_DELAY)

    logger.info("Fetched %d new Lords divisions (delta)", len(divisions))
    return divisions


# --- Build modes ---

def build_seed(output_path, schema_path, divisions_limit=None, checkpoint_db=None):
    """Seed mode: full historical fetch of Lords divisions + votes.

    If checkpoint_db exists and has data, resume from MAX(division id).
    """
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        max_id = get_max_division_id(conn, 2)
        logger.info("Resuming from checkpoint: max_lords_id=%d", max_id)
        lords_divisions = fetch_lords_divisions_since(max_id, divisions_limit)
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        lords_divisions = fetch_lords_divisions(divisions_limit=divisions_limit)

    lords_count = 0
    for div in lords_divisions:
        insert_lords_division(conn, div, timestamp_millis)
        lords_count += 1
        time.sleep(API_DELAY)
    conn.commit()
    logger.info("Lords data committed: %d divisions", lords_count)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, divisions_limit=None):
    """Delta mode: incremental fetch (D-03).

    1. Copy previous DB to output path
    2. Apply schema migrations (add missing columns)
    3. Fetch only new Lords divisions (divisionId > max_lords_id)
    4. Backfill twfyDebateUrl for existing divisions that don't have it
    5. VACUUM

    Past voting data is never re-fetched — the past is immutable (D-03).
    """
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    # --- Schema migration: add twfyDebateUrl column if missing ---
    cols = [r[1] for r in conn.execute("PRAGMA table_info(divisions)").fetchall()]
    if "twfyDebateUrl" not in cols:
        conn.execute("ALTER TABLE divisions ADD COLUMN twfyDebateUrl TEXT")
        conn.commit()
        logger.info("Schema migration: added twfyDebateUrl column to divisions")

    max_lords_id = get_max_division_id(conn, 2)
    logger.info("Delta mode: max_lords_id=%d", max_lords_id)

    new_lords = fetch_lords_divisions_since(max_lords_id, divisions_limit)
    for div in new_lords:
        insert_lords_division(conn, div, timestamp_millis)
        time.sleep(API_DELAY)
    conn.commit()
    logger.info("Lords delta committed: %d new divisions", len(new_lords))

    # --- Backfill twfyDebateUrl for existing divisions that don't have it ---
    missing = conn.execute(
        "SELECT id, date, number FROM divisions WHERE house = 2 AND twfyDebateUrl IS NULL"
    ).fetchall()
    if missing:
        logger.info("Backfilling twfyDebateUrl for %d existing divisions...", len(missing))
        backfilled = 0
        for div_id, div_date, div_number in missing:
            date_only = div_date.split("T")[0] if div_date else ""
            twfy_url = fetch_twfy_debate_url(date_only, div_number, 2)
            if twfy_url:
                conn.execute(
                    "UPDATE divisions SET twfyDebateUrl = ? WHERE id = ?",
                    (twfy_url, div_id),
                )
                backfilled += 1
            time.sleep(API_DELAY)
        conn.commit()
        logger.info("Backfilled twfyDebateUrl for %d/%d divisions", backfilled, len(missing))
    else:
        logger.info("All divisions already have twfyDebateUrl")

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Lords votes per-API SQLite database (lords_votes.db)."
    )
    parser.add_argument(
        "--output", default="lords_votes.db",
        help="Output path for the SQLite DB file. Default: lords_votes.db.",
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
        "--divisions-limit", type=int, default=None,
        help="Limit number of divisions fetched (for testing).",
    )
    parser.add_argument(
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only). If it has data, resume from MAX(id).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, divisions_limit=args.divisions_limit, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(
            args.output, args.previous_db, args.schema,
            divisions_limit=args.divisions_limit,
        )


if __name__ == "__main__":
    main()
