#!/usr/bin/env python3
"""Per-API build script for voting data (D-10).

Fetches Commons divisions + votes AND Lords divisions + votes and builds a
per-API DB (votes.db) with only the divisions + division_votes tables.

CRITICAL RESILIENCE DESIGN (fix for the first build attempt crash):
In seed mode, Commons divisions + votes are fetched FIRST and committed to
the DB, THEN Lords divisions + votes are fetched. If the Lords API fails
(timeout), votes.db still has Commons data and can be published. The Lords
fetch is wrapped in try/except — on failure it logs a warning and continues
to VACUUM/close. Commons data is preserved.

Modes:
  seed  — create fresh DB, fetch Commons (commit), then Lords (resilient)
  delta — copy previous DB, fetch only NEW divisions per house (D-03)

Usage:
  python build_votes.py --output votes.db --schema schemas/8.json --mode seed
  python build_votes.py --output votes.db --schema schemas/8.json --mode delta --previous-db prev_votes.db
"""

import argparse
import shutil
import sqlite3
import time

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

COMMONS_VOTES_BASE = "https://commonsvotes-api.parliament.uk/data/"
LORDS_VOTES_BASE = "https://lordsvotes-api.parliament.uk/data/"

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
            timeout=30,
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
        timeout=30,
    )
    return r.json()


def map_commons_division_to_entity(div, timestamp_millis):
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

    cursor.execute(
        """INSERT OR REPLACE INTO divisions
           (id, title, date, publicationUpdated, number, isDeferred,
            ayeCount, noCount, house, lastUpdated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        map_commons_division_to_entity(div, timestamp_millis),
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
            timeout=30,
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
        timeout=30,
    )
    return r.json()


def map_lords_division_to_entity(div, timestamp_millis):
    """Map a Lords division to a divisions table row tuple (house=2).

    Lords API uses camelCase field names. Content → AYE, Not Content → NO.
    """
    return (
        div.get("divisionId", 0),
        div.get("title", ""),
        div.get("date", ""),
        None,  # publicationUpdated — Lords API doesn't provide this
        div.get("number"),
        0,  # isDeferred — Lords divisions are not deferred
        div.get("memberContentCount", 0),
        div.get("memberNotContentCount", 0),
        2,  # house=2 for Lords (D-07)
        timestamp_millis,
    )


def map_lords_voter_to_entity(voter, division_id, vote, is_teller=0):
    """Map a Lords voter to a division_votes row tuple (camelCase fields)."""
    return (
        division_id,
        voter.get("memberId", 0),
        vote,
        voter.get("name", ""),
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
    division_id = div.get("divisionId", 0)

    cursor.execute(
        """INSERT OR REPLACE INTO divisions
           (id, title, date, publicationUpdated, number, isDeferred,
            ayeCount, noCount, house, lastUpdated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        map_lords_division_to_entity(div, timestamp_millis),
    )

    detail = fetch_lords_division_detail(division_id)

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
            timeout=30,
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


def fetch_lords_divisions_since(max_id, divisions_limit=None):
    """Fetch Lords divisions with divisionId > max_id (delta mode)."""
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
            timeout=30,
        )
        page = r.json()

        if not page:
            break

        new_count = 0
        for div in page:
            div_id = div.get("divisionId", 0)
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

    logger.info("Fetched %d new Lords divisions (delta)", len(divisions))
    return divisions


# --- Build modes ---

def build_seed(output_path, schema_path, divisions_limit=None):
    """Seed mode: full historical fetch with resilience.

    1. Create DB with divisions + division_votes tables
    2. Fetch Commons divisions + votes, COMMIT
    3. Fetch Lords divisions + votes (try/except — Lords failure is
       non-fatal; Commons data is preserved)
    4. VACUUM
    """
    timestamp_millis = int(time.time() * 1000)

    conn = schema_module.create_database_with_tables(
        output_path, schema_path, TABLE_NAMES,
    )

    # --- Commons first (commit so Commons data survives a Lords failure) ---
    commons_divisions = fetch_commons_divisions(divisions_limit=divisions_limit)
    commons_count = 0
    commons_votes = 0
    for div in commons_divisions:
        insert_commons_division(conn, div, timestamp_millis)
        commons_count += 1
        time.sleep(API_DELAY)  # Rate limit between detail fetches
    conn.commit()
    logger.info(
        "Commons data committed: %d divisions", commons_count,
    )

    # --- Lords second (resilient — failure does NOT crash the build) ---
    try:
        lords_divisions = fetch_lords_divisions(divisions_limit=divisions_limit)
        lords_count = 0
        for div in lords_divisions:
            insert_lords_division(conn, div, timestamp_millis)
            lords_count += 1
            time.sleep(API_DELAY)
        conn.commit()
        logger.info("Lords data committed: %d divisions", lords_count)
    except Exception as e:
        logger.warning(
            "Lords fetch failed: %s. Publishing with Commons-only data.",
            e,
        )

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, divisions_limit=None):
    """Delta mode: incremental fetch (D-03).

    1. Copy previous DB to output path
    2. Fetch only new Commons divisions (DivisionId > max_commons_id), COMMIT
    3. Try: fetch only new Lords divisions (divisionId > max_lords_id)
       Except: log warning and continue
    4. VACUUM

    Past voting data is never re-fetched — the past is immutable (D-03).
    """
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    max_commons_id = get_max_division_id(conn, 1)
    max_lords_id = get_max_division_id(conn, 2)
    logger.info(
        "Delta mode: max_commons_id=%d, max_lords_id=%d",
        max_commons_id, max_lords_id,
    )

    # Commons first (commit)
    new_commons = fetch_commons_divisions_since(max_commons_id, divisions_limit)
    for div in new_commons:
        insert_commons_division(conn, div, timestamp_millis)
        time.sleep(API_DELAY)
    conn.commit()
    logger.info("Commons delta committed: %d new divisions", len(new_commons))

    # Lords second (resilient)
    try:
        new_lords = fetch_lords_divisions_since(max_lords_id, divisions_limit)
        for div in new_lords:
            insert_lords_division(conn, div, timestamp_millis)
            time.sleep(API_DELAY)
        conn.commit()
        logger.info("Lords delta committed: %d new divisions", len(new_lords))
    except Exception as e:
        logger.warning(
            "Lords delta fetch failed: %s. Commons data already committed.",
            e,
        )

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye votes per-API SQLite database (votes.db)."
    )
    parser.add_argument(
        "--output", default="votes.db",
        help="Output path for the SQLite DB file. Default: votes.db.",
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
        "--divisions-limit", type=int, default=None,
        help="Limit number of divisions fetched per house (for testing).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, divisions_limit=args.divisions_limit)
    else:
        build_delta(
            args.output, args.previous_db, args.schema,
            divisions_limit=args.divisions_limit,
        )


if __name__ == "__main__":
    main()
