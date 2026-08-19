#!/usr/bin/env python3
"""Per-API build script for MPs (D-10).

Fetches all 650 current Commons MPs from the Members API and builds a
per-API DB (mps.db) with only the mps + mps_fts tables + the full schema's
Room identity hash. The FTS4 sync triggers auto-populate mps_fts when
rows are inserted into mps (Pitfall 2 — triggers created before data).

Modes:
  seed  — create fresh DB, fetch all MPs, insert
  delta — copy previous DB, fetch all MPs, upsert (MPs can change
          party/constituency/active status — INSERT OR REPLACE)

Usage:
  python build_mps.py --output mps.db --schema schemas/8.json --mode seed
  python build_mps.py --output mps.db --schema schemas/8.json --mode delta --previous-db prev_mps.db
"""

import argparse
import shutil
import sqlite3
import time

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

MEMBERS_BASE = "https://members-api.parliament.uk/api/"
PAGE_SIZE_MEMBERS = 20

TABLE_NAMES = ["mps", "mps_fts"]


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
        r = api_get(
            f"{MEMBERS_BASE}Members/Search",
            params=params,
            timeout=30,
        )
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
        member_dto.get("id") or 0,
        member_dto.get("nameListAs") or "",
        member_dto.get("nameDisplayAs") or "",
        member_dto.get("nameFullTitle"),
        member_dto.get("nameAddressAs"),
        member_dto.get("gender"),
        latest_party.get("id", 0) or 0,
        party_name or "",
        party_abbrev or "",
        latest_party.get("backgroundColour") or "",
        latest_party.get("foregroundColour") or "",
        latest_membership.get("membershipFromId") or 0,
        latest_membership.get("membershipFrom") or "",
        latest_membership.get("house") or 1,
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


# --- Build modes ---

def build_seed(output_path, schema_path, mp_limit=None):
    """Seed mode: create fresh DB, fetch all MPs, insert."""
    timestamp_millis = int(time.time() * 1000)

    conn = schema_module.create_database_with_tables(
        output_path, schema_path, TABLE_NAMES,
    )

    mps = fetch_all_mps(mp_limit=mp_limit)
    if mps:
        insert_mps(conn, mps, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mp_limit=None):
    """Delta mode: copy previous DB, fetch all MPs, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    mps = fetch_all_mps(mp_limit=mp_limit)
    if mps:
        insert_mps(conn, mps, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye MPs per-API SQLite database (mps.db)."
    )
    parser.add_argument(
        "--output", default="mps.db",
        help="Output path for the SQLite DB file. Default: mps.db.",
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
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, mp_limit=args.mp_limit)
    else:
        build_delta(args.output, args.previous_db, args.schema, mp_limit=args.mp_limit)


if __name__ == "__main__":
    main()
