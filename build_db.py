#!/usr/bin/env python3
"""Build script for the GovEye bundled SQLite database.

Fetches UK Parliament data (Members API + Commons Votes API + Lords Votes API)
and builds a Room-compatible SQLite DB. Uses Python stdlib sqlite3 + requests
only (D-01). No JVM, no Room dependency in CI.

Data sources (D-08 — MNIS deferred to Phase 11):
  - Members API: https://members-api.parliament.uk/api/
  - Commons Votes API: https://commonsvotes-api.parliament.uk/data/
  - Lords Votes API: https://lordsvotes-api.parliament.uk/data/

Modes:
  seed  — full historical fetch (all 650 MPs + all divisions + all votes)
  delta — incremental fetch (only new divisions since last run, per D-03)

Usage:
  python build_db.py --output goveye.db --schema schemas/8.json --mode seed
  python build_db.py --output goveye.db --schema schemas/8.json --mode delta --previous-db prev.db
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time

import requests

import schema as schema_module

# --- Constants ---

MEMBERS_BASE = "https://members-api.parliament.uk/api/"
COMMONS_VOTES_BASE = "https://commonsvotes-api.parliament.uk/data/"
LORDS_VOTES_BASE = "https://lordsvotes-api.parliament.uk/data/"

API_DELAY = 0.2  # seconds between API calls for rate limiting (A3)
PAGE_SIZE_MEMBERS = 20
PAGE_SIZE_DIVISIONS = 25
BATCH_SIZE = 1000  # rows per transaction for batch inserts (Pitfall 7)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("build_db")


# --- Schema / DB Setup ---

def create_database(output_path, schema_path):
    """Create a new SQLite DB with all 16 Room tables, FTS4 triggers,
    and room_master_table with the correct identity hash.

    Per Pitfall 2: FTS4 sync triggers are created BEFORE any data insertion
    so the FTS index populates automatically when rows are inserted.
    """
    schema = schema_module.load_schema(schema_path)
    identity_hash = schema_module.get_identity_hash(schema)
    db_version = schema_module.get_version(schema)

    # Remove existing DB file if it exists
    if os.path.exists(output_path):
        os.remove(output_path)

    conn = sqlite3.connect(output_path)
    cursor = conn.cursor()

    # 1. Create all 16 tables using createSql from schema JSON
    for table_name, create_sql in schema_module.get_create_sql(schema):
        logger.info("Creating table: %s", table_name)
        cursor.execute(create_sql)

    # 2. Create FTS4 content sync triggers BEFORE inserting any data (Pitfall 2)
    for trigger_sql in schema_module.get_fts_triggers(schema):
        logger.info("Creating FTS trigger: %s", trigger_sql[:80])
        cursor.execute(trigger_sql)

    # 3. Execute setupQueries to create room_master_table with identity hash
    for setup_query in schema_module.get_setup_queries(schema):
        cursor.execute(setup_query)

    conn.commit()
    logger.info(
        "Database created: %s (identity_hash=%s, version=%d)",
        output_path, identity_hash, db_version,
    )
    return conn


# --- Members API (House=1, current Commons MPs) ---

def fetch_all_mps(mp_limit=None):
    """Fetch all current Commons MPs from the Members API.

    The API returns {"items": [...]} with MemberItem objects containing
    a "value" field with the MemberDto.

    Args:
        mp_limit: Optional limit on number of MPs to fetch (for testing).

    Returns:
        List of MemberDto dicts.
    """
    mps = []
    skip = 0

    while True:
        params = {
            "House": 1,
            "IsCurrentMember": "true",
            "itemsPerPage": PAGE_SIZE_MEMBERS,
            "skip": skip,
        }
        logger.info("Fetching MPs: skip=%d", skip)
        r = requests.get(
            f"{MEMBERS_BASE}Members/Search",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            mps.append(item.get("value", item))

        if mp_limit is not None and len(mps) >= mp_limit:
            mps = mps[:mp_limit]
            break

        if len(items) < PAGE_SIZE_MEMBERS:
            break

        skip += PAGE_SIZE_MEMBERS
        time.sleep(API_DELAY)

    logger.info("Fetched %d MPs", len(mps))
    return mps


def map_mp_to_entity(member_dto, timestamp_millis):
    """Map a MemberDto to an MpEntity row tuple.

    Matches the MemberMapper.toEntity field mapping:
    - id from MemberDto.id
    - nameListAs, nameDisplayAs, nameFullTitle from MemberDto fields
    - gender from MemberDto.gender
    - partyId/partyName/partyAbbreviation/partyBackgroundColour/partyForegroundColour
      from latestParty
    - constituencyId/constituencyName from latestHouseMembership.membershipFromId/membershipFrom
    - house from latestHouseMembership.house
    - membershipStartDate from latestHouseMembership.membershipStartDate
    - isActive from latestHouseMembership.membershipStatus.statusIsActive
    - thumbnailUrl from MemberDto.thumbnailUrl (URL text only, DATA-04)
    - lastUpdated as current timestamp

    Labour/Co-op edge case: if party name contains "(Co-op)", append
    " Co-op" to the abbreviation (per MemberMapper).
    """
    latest_party = member_dto.get("latestParty") or {}
    latest_membership = member_dto.get("latestHouseMembership") or {}
    membership_status = latest_membership.get("membershipStatus") or {}

    party_name = latest_party.get("name", "")
    party_abbrev = latest_party.get("abbreviation", "")

    # Labour/Co-op edge case from MemberMapper
    if "(Co-op)" in party_name:
        party_abbrev = f"{party_abbrev} Co-op"

    return (
        member_dto.get("id", 0),
        member_dto.get("nameListAs", ""),
        member_dto.get("nameDisplayAs", ""),
        member_dto.get("nameFullTitle"),
        member_dto.get("nameAddressAs"),
        member_dto.get("gender"),
        latest_party.get("id", 0),
        party_name,
        party_abbrev,
        latest_party.get("backgroundColour", ""),
        latest_party.get("foregroundColour", ""),
        latest_membership.get("membershipFromId", 0),
        latest_membership.get("membershipFrom", ""),
        latest_membership.get("house", 1),
        latest_membership.get("membershipStartDate"),
        latest_membership.get("membershipEndDate"),
        1 if membership_status.get("statusIsActive", False) else 0,
        member_dto.get("thumbnailUrl"),  # URL text only, not downloaded (DATA-04)
        timestamp_millis,
    )


def insert_mps(conn, mps, timestamp_millis):
    """Insert MPs into the mps table using batch executemany.

    Uses INSERT OR REPLACE so this works for both seed and delta modes
    (MPs can change party/constituency/active status between runs).
    """
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO mps (
            id, nameListAs, nameDisplayAs, nameFullTitle, nameAddressAs,
            gender, partyId, partyName, partyAbbreviation,
            partyBackgroundColour, partyForegroundColour,
            constituencyId, constituencyName, house,
            membershipStartDate, membershipEndDate, isActive,
            thumbnailUrl, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = [map_mp_to_entity(mp, timestamp_millis) for mp in mps]

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted MPs: %d/%d", min(i + BATCH_SIZE, len(rows)), len(rows))


# --- Commons Votes API ---

def fetch_commons_divisions(divisions_limit=None):
    """Fetch all Commons divisions from the Commons Votes API.

    The API returns a bare JSON list (NOT an object with items).
    Paginated 25/page, newest first.

    Args:
        divisions_limit: Optional limit on total divisions fetched (for testing).

    Returns:
        List of division metadata dicts.
    """
    divisions = []
    skip = 0

    while True:
        params = {
            "itemsPerPage": PAGE_SIZE_DIVISIONS,
            "skip": skip,
        }
        logger.info("Fetching Commons divisions: skip=%d", skip)
        r = requests.get(
            f"{COMMONS_VOTES_BASE}divisions.json/search",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
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
    """Fetch full Commons division detail with all voter lists.

    Returns a dict with Ayes[], Noes[], AyeTellers[], NoTellers[] arrays.
    """
    r = requests.get(
        f"{COMMONS_VOTES_BASE}division/{division_id}.json",
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def map_commons_division_to_entity(div, timestamp_millis):
    """Map a Commons DivisionDto to a divisions table row tuple (house=1).

    Fields from the API use PascalCase (DivisionId, Title, Date, etc.).
    """
    return (
        div.get("DivisionId", 0),
        div.get("Title", ""),
        div.get("Date", ""),
        div.get("PublicationUpdated"),
        div.get("Number"),
        1 if div.get("IsDeferred", False) else 0,  # Store as 0/1 integer
        div.get("AyeCount", 0),
        div.get("NoCount", 0),
        1,  # house=1 for Commons
        timestamp_millis,
    )


def map_commons_voter_to_entity(voter, division_id, vote, is_teller=0):
    """Map a Commons VoterDto to a division_votes row tuple.

    Args:
        voter: The voter dict from Ayes/Noes/AyeTellers/NoTellers.
        division_id: The division ID.
        vote: "AYE" or "NO".
        is_teller: 0 for regular voters, 1 for tellers.
    """
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
    """Insert a Commons division and all its votes into the DB.

    Fetches the full division detail (voter lists) and inserts:
    - 1 row into divisions table (house=1)
    - N rows into division_votes (Ayes as AYE, Noes as NO)
    - Teller rows from AyeTellers/NoTellers arrays (isTeller=1)
    """
    cursor = conn.cursor()
    division_id = div.get("DivisionId", 0)

    # Insert division metadata
    cursor.execute(
        """INSERT OR REPLACE INTO divisions
           (id, title, date, publicationUpdated, number, isDeferred,
            ayeCount, noCount, house, lastUpdated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        map_commons_division_to_entity(div, timestamp_millis),
    )

    # Fetch full division detail with voter lists
    detail = fetch_commons_division_detail(division_id)

    votes = []

    # Ayes → AYE
    for voter in detail.get("Ayes", []):
        votes.append(map_commons_voter_to_entity(voter, division_id, "AYE", 0))

    # Noes → NO
    for voter in detail.get("Noes", []):
        votes.append(map_commons_voter_to_entity(voter, division_id, "NO", 0))

    # AyeTellers → AYE with isTeller=1
    for teller in detail.get("AyeTellers", []) or []:
        votes.append(map_commons_voter_to_entity(teller, division_id, "AYE", 1))

    # NoTellers → NO with isTeller=1
    for teller in detail.get("NoTellers", []) or []:
        votes.append(map_commons_voter_to_entity(teller, division_id, "NO", 1))

    # Batch insert votes (Pitfall 7: 1000 rows per transaction)
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

    Note: Lords API uses PascalCase paths (Divisions/search) and
    camelCase field names. Returns {"items": [...]}.

    Args:
        divisions_limit: Optional limit on total divisions fetched (for testing).

    Returns:
        List of Lords division metadata dicts.
    """
    divisions = []
    skip = 0

    while True:
        params = {
            "itemsPerPage": PAGE_SIZE_DIVISIONS,
            "skip": skip,
        }
        logger.info("Fetching Lords divisions: skip=%d", skip)
        r = requests.get(
            f"{LORDS_VOTES_BASE}Divisions/search",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
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
    """Fetch full Lords division detail with all voter lists.

    Returns a dict with contents[], notContents[], contentTellers[],
    notContentTellers[] arrays.
    """
    r = requests.get(
        f"{LORDS_VOTES_BASE}Divisions/{division_id}",
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def map_lords_division_to_entity(div, timestamp_millis):
    """Map a Lords division to a divisions table row tuple (house=2).

    Lords API uses camelCase field names (divisionId, title, date, etc.).
    Content → AYE, Not Content → NO (per DivisionMapper convention).
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
    """Map a Lords voter to a division_votes row tuple.

    Lords voters use camelCase field names (memberId, name, party, etc.).
    """
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

    Fetches the full division detail and inserts:
    - 1 row into divisions table (house=2)
    - N rows into division_votes (Contents as AYE, NotContents as NO)
    - Teller rows from contentTellers/notContentTellers (isTeller=1)

    Lords members are NOT inserted into the mps table — only Commons MPs
    are in the directory (House=1). Lords votes are self-contained in
    division_votes with denormalized memberName/partyName.
    """
    cursor = conn.cursor()
    division_id = div.get("divisionId", 0)

    # Insert division metadata
    cursor.execute(
        """INSERT OR REPLACE INTO divisions
           (id, title, date, publicationUpdated, number, isDeferred,
            ayeCount, noCount, house, lastUpdated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        map_lords_division_to_entity(div, timestamp_millis),
    )

    # Fetch full division detail with voter lists
    detail = fetch_lords_division_detail(division_id)

    votes = []

    # Contents → AYE (Content)
    for voter in detail.get("contents", []):
        votes.append(map_lords_voter_to_entity(voter, division_id, "AYE", 0))

    # NotContents → NO (Not Content)
    for voter in detail.get("notContents", []):
        votes.append(map_lords_voter_to_entity(voter, division_id, "NO", 0))

    # ContentTellers → AYE with isTeller=1
    for teller in detail.get("contentTellers", []) or []:
        votes.append(map_lords_voter_to_entity(teller, division_id, "AYE", 1))

    # NotContentTellers → NO with isTeller=1
    for teller in detail.get("notContentTellers", []) or []:
        votes.append(map_lords_voter_to_entity(teller, division_id, "NO", 1))

    # Batch insert votes
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


# --- Delta Mode ---

def get_max_division_id(conn, house):
    """Get the maximum division ID for a given house from the DB.

    Used in delta mode to fetch only new divisions (D-03).
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MAX(id) FROM divisions WHERE house = ?", (house,)
    )
    result = cursor.fetchone()
    return result[0] if result and result[0] is not None else 0


def fetch_commons_divisions_since(max_id, divisions_limit=None):
    """Fetch Commons divisions with DivisionId > max_id (delta mode).

    Fetches all divisions and filters client-side since the API doesn't
    support ID-based filtering. Stops early once we've passed the max_id
    boundary (divisions are returned newest first).
    """
    divisions = []
    skip = 0

    while True:
        params = {
            "itemsPerPage": PAGE_SIZE_DIVISIONS,
            "skip": skip,
        }
        logger.info("Fetching Commons divisions (delta): skip=%d", skip)
        r = requests.get(
            f"{COMMONS_VOTES_BASE}divisions.json/search",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        page = r.json()

        if not page:
            break

        new_count = 0
        for div in page:
            div_id = div.get("DivisionId", 0)
            if div_id > max_id:
                divisions.append(div)
                new_count += 1

        # If no new divisions on this page, we've reached the boundary
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
    """Fetch Lords divisions with divisionId > max_id (delta mode).

    Fetches all divisions and filters client-side. Lords divisions are
    returned newest first.
    """
    divisions = []
    skip = 0

    while True:
        params = {
            "itemsPerPage": PAGE_SIZE_DIVISIONS,
            "skip": skip,
        }
        logger.info("Fetching Lords divisions (delta): skip=%d", skip)
        r = requests.get(
            f"{LORDS_VOTES_BASE}Divisions/search",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        page = r.json()  # Lords API returns a bare list

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


# --- Main Build Logic ---

def build_seed(output_path, schema_path, mp_limit=None, divisions_limit=None):
    """Seed mode: full historical fetch.

    1. Create DB with all 16 tables + FTS4 triggers + room_master_table
    2. Fetch all 650 current Commons MPs and insert
    3. Fetch all Commons divisions + votes and insert
    4. Fetch all Lords divisions + votes and insert
    5. VACUUM to minimize file size
    """
    timestamp_millis = int(time.time() * 1000)

    conn = create_database(output_path, schema_path)

    # Fetch and insert MPs
    mps = fetch_all_mps(mp_limit=mp_limit)
    if mps:
        insert_mps(conn, mps, timestamp_millis)

    # Fetch and insert Commons divisions + votes
    commons_divisions = fetch_commons_divisions(divisions_limit=divisions_limit)
    for div in commons_divisions:
        insert_commons_division(conn, div, timestamp_millis)
        time.sleep(API_DELAY)  # Rate limit between detail fetches

    # Fetch and insert Lords divisions + votes
    lords_divisions = fetch_lords_divisions(divisions_limit=divisions_limit)
    for div in lords_divisions:
        insert_lords_division(conn, div, timestamp_millis)
        time.sleep(API_DELAY)

    # VACUUM to minimize file size
    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mp_limit=None,
                divisions_limit=None):
    """Delta mode: incremental fetch (D-03).

    1. Copy previous DB to output path
    2. Open it
    3. Fetch all MPs and upsert (MPs can change party/constituency)
    4. Fetch only new Commons divisions (DivisionId > max_commons_id)
    5. Fetch only new Lords divisions (divisionId > max_lords_id)
    6. For each new division, fetch full detail and insert votes
    7. VACUUM to minimize file size

    Past voting data is never re-fetched — the past is immutable (D-03).
    """
    timestamp_millis = int(time.time() * 1000)

    # Copy previous DB to output path
    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)
    cursor = conn.cursor()

    # Fetch and upsert MPs (they can change party/constituency/active status)
    mps = fetch_all_mps(mp_limit=mp_limit)
    if mps:
        insert_mps(conn, mps, timestamp_millis)

    # Find last fetched division IDs for each house
    max_commons_id = get_max_division_id(conn, 1)
    max_lords_id = get_max_division_id(conn, 2)
    logger.info(
        "Delta mode: max_commons_id=%d, max_lords_id=%d",
        max_commons_id, max_lords_id,
    )

    # Fetch only new Commons divisions
    new_commons = fetch_commons_divisions_since(max_commons_id, divisions_limit)
    for div in new_commons:
        insert_commons_division(conn, div, timestamp_millis)
        time.sleep(API_DELAY)

    # Fetch only new Lords divisions
    new_lords = fetch_lords_divisions_since(max_lords_id, divisions_limit)
    for div in new_lords:
        insert_lords_division(conn, div, timestamp_millis)
        time.sleep(API_DELAY)

    # VACUUM to minimize file size
    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye bundled SQLite database from Parliament APIs."
    )
    parser.add_argument(
        "--output", required=True,
        help="Output path for the SQLite DB file.",
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
        "--mp-limit", type=int, default=None,
        help="Limit number of MPs fetched (for testing).",
    )
    parser.add_argument(
        "--divisions-limit", type=int, default=None,
        help="Limit number of divisions fetched per house (for testing).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(
            args.output, args.schema,
            mp_limit=args.mp_limit,
            divisions_limit=args.divisions_limit,
        )
    else:
        build_delta(
            args.output, args.previous_db, args.schema,
            mp_limit=args.mp_limit,
            divisions_limit=args.divisions_limit,
        )


if __name__ == "__main__":
    main()
