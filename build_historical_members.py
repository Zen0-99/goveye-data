#!/usr/bin/env python3
"""Per-API build script for historical members (Phase 11 follow-up).

Downloads ParlParse people.json (Popolo format), extracts all persons
with memberships active from 2000 onwards, and builds a per-API DB
(historical_members.db) with the historical_members and
historical_members_fts4 tables.

This DB serves two purposes:
1. Debate speaker matching: twfy_person_id -> parliament_member_id lookup
   for build_debates.py (replaces name-only matching against 650 current MPs)
2. Officials directory: browse former MPs and Lords in the app

ParlParse people.json URL: https://parser.theyworkforyou.com/people.json

Modes:
  seed  — create fresh DB, download JSON, parse, insert
  delta — copy previous DB, download latest JSON, upsert

Usage:
  python build_historical_members.py --output historical_members.db --schema schemas/bundled_schema.json --mode seed
  python build_historical_members.py --output historical_members.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_historical_members.db
"""

import argparse
import json
import shutil
import sqlite3
import time

import requests

import schema as schema_module
from api_helper import BATCH_SIZE, api_get, logger

# --- Constants ---

PARLPARSE_PEOPLE_URL = "https://raw.githubusercontent.com/mysociety/parlparse/master/members/people.json"
TABLE_NAMES = ["historical_members", "historical_members_fts4"]
START_YEAR = "2000"

# Prime Ministers 2000-2026: twfyPersonId → parliamentMemberId for photo download.
# Tony Blair's parliamentMemberId (313) is not in ParlParse, so hardcoded here.
# The others are in ParlParse but we list them all for clarity.
PM_PHOTOS = {
    10047: 313,    # Tony Blair
    10068: 591,    # Gordon Brown
    10777: 1467,   # David Cameron
    10426: 8,      # Theresa May
    10999: 1423,   # Boris Johnson
    24941: 4097,   # Liz Truss (Elizabeth Truss)
    25428: 4483,   # Rishi Sunak
    25353: 4514,   # Keir Starmer
    10766: 1427,   # Andy Burnham (PM since July 2026)
}

PARLIAMENT_PHOTO_URL = "https://members-api.parliament.uk/api/Members/{}/Thumbnail"


# --- JSON parsing ---

def download_people_json():
    """Download ParlParse people.json and return the parsed JSON object."""
    logger.info("Downloading ParlParse people.json...")
    try:
        r = api_get(PARLPARSE_PEOPLE_URL, timeout=120)
        data = r.json()
        logger.info("Downloaded people.json: %d persons, %d memberships",
                    len(data.get("persons", [])), len(data.get("memberships", [])))
        return data
    except Exception as e:
        logger.error("Failed to download people.json: %s", e)
        return {"persons": [], "memberships": []}


def get_display_name(person):
    """Extract the best display name from a Popolo person object."""
    other_names = person.get("other_names", [])
    # Prefer "Main" note
    for n in other_names:
        if n.get("note") == "Main":
            given = n.get("given_name", "")
            family = n.get("family_name", "")
            if given or family:
                return f"{given} {family}".strip()
    # Fallback to first entry
    if other_names:
        given = other_names[0].get("given_name", "")
        family = other_names[0].get("family_name", "")
        return f"{given} {family}".strip()
    return "Unknown"


def get_alternate_names(person):
    """Extract alternate name spellings from a Popolo person object."""
    alt_names = []
    for n in person.get("other_names", []):
        if n.get("note") == "Alternate":
            given = n.get("given_name", "")
            family = n.get("family_name", "")
            name = f"{given} {family}".strip()
            if name:
                alt_names.append(name)
    return alt_names


def extract_twfy_person_id(person):
    """Extract the TWFY person ID from the person's id field.

    'uk.org.publicwhip/person/10001' -> 10001
    """
    pid = person.get("id", "")
    if "/person/" in pid:
        try:
            return int(pid.split("/person/")[-1])
        except ValueError:
            pass
    return 0


def extract_parliament_member_id(person):
    """Extract the Parliament member ID from identifiers.

    Looks for scheme 'datadotparl_id'. Returns 0 if not found
    (common for Lords pre-2010).
    """
    for ident in person.get("identifiers", []):
        if ident.get("scheme") == "datadotparl_id":
            try:
                return int(ident["identifier"])
            except (ValueError, TypeError):
                pass
    return 0


def is_membership_active_after(membership, year):
    """Check if a membership was active during or after the given year."""
    start = membership.get("start_date", "9999")
    end = membership.get("end_date", "9999")
    if not start:
        return False
    # Active if start <= end_of_range AND (end >= year OR still active)
    if start > "2026":
        return False
    if end == "9999-12-31" or end >= year:
        return True
    return False


def get_house_from_membership(membership):
    """Determine house from membership ID.

    'uk.org.publicwhip/member/123' -> 1 (Commons)
    'uk.org.publicwhip/lord/123'  -> 2 (Lords)
    """
    mid = membership.get("id", "")
    if "/member/" in mid:
        return 1
    if "/lord/" in mid:
        return 2
    return 0


def parse_people_json(data):
    """Parse people.json and return a list of historical_member row dicts.

    Filters to persons with at least one membership active from START_YEAR onwards.
    """
    persons = data.get("persons", [])
    memberships = data.get("memberships", [])

    # Build person_id -> memberships lookup
    person_memberships = {}
    for m in memberships:
        pid = m.get("person_id", "")
        person_memberships.setdefault(pid, []).append(m)

    rows = []
    matched = 0
    unmatched = 0

    for person in persons:
        pid = person.get("id", "")
        person_ms = person_memberships.get(pid, [])

        # Filter to memberships active from START_YEAR
        active_ms = [m for m in person_ms if is_membership_active_after(m, START_YEAR)]
        if not active_ms:
            continue

        twfy_id = extract_twfy_person_id(person)
        if twfy_id == 0:
            unmatched += 1
            continue

        parl_id = extract_parliament_member_id(person)
        display_name = get_display_name(person)
        alt_names = get_alternate_names(person)

        # Determine house: prefer Commons if they were ever an MP
        house = 0
        for m in active_ms:
            h = get_house_from_membership(m)
            if h == 1:
                house = 1
                break
            elif h == 2 and house == 0:
                house = 2

        # Get party from most recent membership with on_behalf_of_id
        party = None
        constituency = None
        latest_start = ""
        for m in active_ms:
            start = m.get("start_date", "")
            if start > latest_start:
                latest_start = start
                party = m.get("on_behalf_of_id")
                post_id = m.get("post_id", "")
                # Extract constituency from post_id: uk.org.publicwhip/cons/263
                if "/cons/" in post_id:
                    constituency = post_id.split("/cons/")[-1]

        # Get service dates (earliest start, latest end)
        start_date = min((m.get("start_date", "9999") for m in active_ms if m.get("start_date")), default="")
        end_dates = [m.get("end_date", "") for m in active_ms if m.get("end_date") and m.get("end_date") != "9999-12-31"]
        end_date = max(end_dates) if end_dates else None  # None = still in office

        rows.append({
            "twfyPersonId": twfy_id,
            "parliamentMemberId": parl_id if parl_id > 0 else None,
            "displayName": display_name,
            "alternateNames": json.dumps(alt_names) if alt_names else None,
            "party": party,
            "house": house,
            "startDate": start_date if start_date != "9999" else None,
            "endDate": end_date,
            "constituency": constituency,
            "isCurrent": 1 if end_date is None else 0,
        })
        matched += 1

    logger.info("Parsed %d historical members (%d matched, %d skipped — no TWFY ID)",
                len(rows), matched, unmatched)
    return rows


def download_pm_photos():
    """Download photos for Prime Ministers 2000-2026 from the Parliament API.

    Returns a dict mapping twfyPersonId → photo BLOB (bytes).
    """
    photos = {}
    for twfy_id, parl_id in PM_PHOTOS.items():
        url = PARLIAMENT_PHOTO_URL.format(parl_id)
        try:
            r = api_get(url, timeout=30, max_retries=2)
            if r.status_code == 200 and r.content:
                photos[twfy_id] = r.content
                logger.info("Downloaded PM photo: twfyPersonId=%d, parlId=%d, %d bytes",
                            twfy_id, parl_id, len(r.content))
            else:
                logger.warning("PM photo failed: twfyPersonId=%d, parlId=%d, status=%d",
                               twfy_id, parl_id, r.status_code)
        except Exception as e:
            logger.warning("PM photo error: twfyPersonId=%d, parlId=%d, %s",
                           twfy_id, parl_id, e)
    logger.info("Downloaded %d/%d PM photos", len(photos), len(PM_PHOTOS))
    return photos


# --- DB operations ---

def build_historical_members_table(conn):
    """Create the historical_members and historical_members_fts4 tables."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historical_members (
            twfyPersonId INTEGER PRIMARY KEY,
            parliamentMemberId INTEGER,
            displayName TEXT NOT NULL,
            alternateNames TEXT,
            party TEXT,
            house INTEGER NOT NULL,
            startDate TEXT,
            endDate TEXT,
            constituency TEXT,
            isCurrent INTEGER NOT NULL DEFAULT 0,
            photo BLOB,
            lastUpdated INTEGER
        )
    """)
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS historical_members_fts4
        USING fts4(displayName, alternateNames, content=`historical_members`)
    """)
    # Migrate: add photo column if missing (delta mode copies old schema)
    cursor.execute("PRAGMA table_info(historical_members)")
    columns = {row[1] for row in cursor.fetchall()}
    if "photo" not in columns:
        cursor.execute("ALTER TABLE historical_members ADD COLUMN photo BLOB")
        logger.info("Migrated historical_members: added photo column")
    conn.commit()


def insert_historical_members(conn, rows, timestamp_millis, pm_photos=None):
    """Batch insert historical_members rows using INSERT OR REPLACE.

    Also updates PM photos and Tony Blair's parliamentMemberId if pm_photos is provided.
    """
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO historical_members (
            twfyPersonId, parliamentMemberId, displayName, alternateNames,
            party, house, startDate, endDate, constituency, isCurrent, photo, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    # Fix Tony Blair's parliamentMemberId (not in ParlParse, hardcoded in PM_PHOTOS)
    for row in rows:
        if row["twfyPersonId"] in PM_PHOTOS:
            row["parliamentMemberId"] = PM_PHOTOS[row["twfyPersonId"]]

    tuples = [
        (
            row["twfyPersonId"],
            row.get("parliamentMemberId"),
            row["displayName"],
            row.get("alternateNames"),
            row.get("party"),
            row["house"],
            row.get("startDate"),
            row.get("endDate"),
            row.get("constituency"),
            row["isCurrent"],
            pm_photos.get(row["twfyPersonId"]) if pm_photos else None,
            timestamp_millis,
        )
        for row in rows
    ]

    for i in range(0, len(tuples), BATCH_SIZE):
        batch = tuples[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted historical_members: %d/%d",
                    min(i + BATCH_SIZE, len(tuples)), len(tuples))

    # Rebuild FTS index
    cursor.execute("INSERT INTO historical_members_fts4(historical_members_fts4) VALUES('rebuild')")
    conn.commit()


# --- Build modes ---

def build_seed(output_path, schema_path, checkpoint_db=None):
    """Seed mode: create fresh DB, download JSON, parse, insert."""
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and _file_exists(checkpoint_db):
        if _abspath(checkpoint_db) != _abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        build_historical_members_table(conn)
        logger.info("Resuming from checkpoint")
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        build_historical_members_table(conn)

    data = download_people_json()
    rows = parse_people_json(data)

    pm_photos = download_pm_photos()
    if rows:
        insert_historical_members(conn, rows, timestamp_millis, pm_photos)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path):
    """Delta mode: copy previous DB, download latest JSON, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)
    build_historical_members_table(conn)

    data = download_people_json()
    rows = parse_people_json(data)

    pm_photos = download_pm_photos()
    if rows:
        insert_historical_members(conn, rows, timestamp_millis, pm_photos)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Delta build complete: %s", output_path)


def _file_exists(path):
    import os
    return os.path.exists(path)


def _abspath(path):
    import os
    return os.path.abspath(path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye historical members per-API SQLite database (historical_members.db)."
    )
    parser.add_argument("--output", default="historical_members.db",
                        help="Output path for the SQLite DB file. Default: historical_members.db.")
    parser.add_argument("--schema", required=True,
                        help="Path to the Room exported schema JSON (bundled_schema.json).")
    parser.add_argument("--mode", choices=["seed", "delta"], default="seed",
                        help="Build mode: seed (full) or delta (incremental). Default: seed.")
    parser.add_argument("--previous-db",
                        help="Path to previous DB file (required for delta mode).")
    parser.add_argument("--checkpoint-db",
                        help="Path to a checkpoint DB to resume from (seed mode only).")

    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, checkpoint_db=args.checkpoint_db)
    elif args.mode == "delta":
        build_delta(args.output, args.previous_db, args.schema)


if __name__ == "__main__":
    main()
