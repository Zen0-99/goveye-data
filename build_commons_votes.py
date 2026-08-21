#!/usr/bin/env python3
"""Per-API build script for Commons voting data.

Fetches Commons divisions + votes only and builds a per-API DB
(commons_votes.db) with only the divisions + division_votes tables
(house=1 rows only).

Modes:
  seed  — create fresh DB, fetch all Commons divisions + votes
  delta — copy previous DB, fetch only NEW Commons divisions (D-03)

Usage:
  python build_commons_votes.py --output commons_votes.db --schema schemas/bundled_schema.json --mode seed
  python build_commons_votes.py --output commons_votes.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_commons_votes.db
"""

import argparse
import os
import shutil
import sqlite3
import time

import schema as schema_module
from api_helper import api_get, fetch_twfy_debate_url, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

COMMONS_VOTES_BASE = "https://commonsvotes-api.parliament.uk/data/"

PAGE_SIZE_DIVISIONS = 25

TABLE_NAMES = ["divisions", "division_votes"]


# --- Commons Votes API ---

def fetch_commons_divisions(divisions_limit=None):
    """Fetch all Commons divisions from the Commons Votes API.

    The API returns a bare JSON list (NOT an object with items).
    Paginated 25/page, newest first.
    """
    divisions = []
    skip = 0

    while True:
        params = {
            "itemsPerPage": PAGE_SIZE_DIVISIONS,
            "skip": skip,
        }
        logger.info("Fetching Commons divisions: skip=%d", skip)
        r = api_get(
            f"{COMMONS_VOTES_BASE}divisions.json/search",
            params=params,
            timeout=60,
        )
        page = r.json()  # Returns a list, not an object

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

    logger.info("Fetched %d Commons divisions", len(divisions))
    return divisions


def fetch_commons_division_detail(division_id):
    """Fetch full Commons division detail with all voter lists."""
    r = api_get(
        f"{COMMONS_VOTES_BASE}division/{division_id}.json",
        timeout=60,
    )
    return r.json()


def map_commons_division_to_entity(div, timestamp_millis, twfy_debate_url=None):
    """Map a Commons DivisionDto to a divisions table row tuple (house=1)."""
    return (
        div.get("DivisionId", 0),
        div.get("Title", ""),
        div.get("Date", ""),
        div.get("PublicationUpdated"),
        div.get("Number"),
        1 if div.get("IsDeferred", False) else 0,
        div.get("AyeCount", 0),
        div.get("NoCount", 0),
        1,  # house=1 for Commons
        timestamp_millis,
        twfy_debate_url,
    )


def map_commons_voter_to_entity(voter, division_id, vote, is_teller=0):
    """Map a Commons VoterDto to a division_votes row tuple."""
    return (
        division_id,
        voter.get("MemberId", 0),
        vote,
        voter.get("Name", ""),
        voter.get("Party", "") or "",
        voter.get("PartyColour", "") or "",
        voter.get("MemberFrom", "") or "",
        is_teller,
        voter.get("ProxyName"),
    )


def insert_commons_division(conn, div, timestamp_millis):
    """Insert a Commons division and all its votes into the DB."""
    cursor = conn.cursor()
    division_id = div.get("DivisionId", 0)

    # Fetch the TWFY debate URL (scrape the TWFY division page for the GID)
    date_only = (div.get("Date") or "").split("T")[0]
    twfy_url = fetch_twfy_debate_url(date_only, div.get("Number"), 1)

    cursor.execute(
        """INSERT OR REPLACE INTO divisions
           (id, title, date, publicationUpdated, number, isDeferred,
            ayeCount, noCount, house, lastUpdated, twfyDebateUrl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        map_commons_division_to_entity(div, timestamp_millis, twfy_url),
    )

    detail = fetch_commons_division_detail(division_id)

    votes = []
    for voter in detail.get("Ayes", []):
        votes.append(map_commons_voter_to_entity(voter, division_id, "AYE", 0))
    for voter in detail.get("Noes", []):
        votes.append(map_commons_voter_to_entity(voter, division_id, "NO", 0))
    for teller in detail.get("AyeTellers", []) or []:
        votes.append(map_commons_voter_to_entity(teller, division_id, "AYE", 1))
    for teller in detail.get("NoTellers", []) or []:
        votes.append(map_commons_voter_to_entity(teller, division_id, "NO", 1))

    vote_sql = """INSERT OR REPLACE INTO division_votes
        (divisionId, memberId, vote, memberName, partyName,
         partyColour, constituencyName, isTeller, proxyName)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""

    for i in range(0, len(votes), BATCH_SIZE):
        batch = votes[i:i + BATCH_SIZE]
        cursor.executemany(vote_sql, batch)
        conn.commit()

    logger.info(
        "Commons division %d: %d votes inserted", division_id, len(votes),
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


def fetch_commons_divisions_since(max_id, divisions_limit=None):
    """Fetch Commons divisions with DivisionId > max_id (delta mode).

    Fetches all divisions and filters client-side since the API doesn't
    support ID-based filtering. Stops early once past the max_id boundary
    (divisions returned newest first).
    """
    divisions = []
    skip = 0

    while True:
        params = {
            "itemsPerPage": PAGE_SIZE_DIVISIONS,
            "skip": skip,
        }
        logger.info("Fetching Commons divisions (delta): skip=%d", skip)
        r = api_get(
            f"{COMMONS_VOTES_BASE}divisions.json/search",
            params=params,
            timeout=60,
        )
        page = r.json()

        if not page:
            break

        new_count = 0
        for div in page:
            div_id = div.get("DivisionId", 0)
            if div_id > max_id:
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

    logger.info("Fetched %d new Commons divisions (delta)", len(divisions))
    return divisions


# --- Build modes ---

def build_seed(output_path, schema_path, divisions_limit=None, checkpoint_db=None):
    """Seed mode: full historical fetch of Commons divisions + votes.

    If checkpoint_db exists and has data, resume from MAX(division id).
    """
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        max_id = get_max_division_id(conn, 1)
        logger.info("Resuming from checkpoint: max_commons_id=%d", max_id)
        commons_divisions = fetch_commons_divisions_since(max_id, divisions_limit)
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        commons_divisions = fetch_commons_divisions(divisions_limit=divisions_limit)

    commons_count = 0
    for div in commons_divisions:
        insert_commons_division(conn, div, timestamp_millis)
        commons_count += 1
        time.sleep(API_DELAY)  # Rate limit between detail fetches
    conn.commit()
    logger.info("Commons data committed: %d divisions", commons_count)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, divisions_limit=None):
    """Delta mode: incremental fetch (D-03).

    1. Copy previous DB to output path
    2. Fetch only new Commons divisions (DivisionId > max_commons_id)
    3. VACUUM

    Past voting data is never re-fetched — the past is immutable (D-03).
    """
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    max_commons_id = get_max_division_id(conn, 1)
    logger.info("Delta mode: max_commons_id=%d", max_commons_id)

    new_commons = fetch_commons_divisions_since(max_commons_id, divisions_limit)
    for div in new_commons:
        insert_commons_division(conn, div, timestamp_millis)
        time.sleep(API_DELAY)
    conn.commit()
    logger.info("Commons delta committed: %d new divisions", len(new_commons))

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Commons votes per-API SQLite database (commons_votes.db)."
    )
    parser.add_argument(
        "--output", default="commons_votes.db",
        help="Output path for the SQLite DB file. Default: commons_votes.db.",
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
