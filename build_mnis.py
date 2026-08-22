#!/usr/bin/env python3
"""Per-API build script for MNIS biographical data (Phase 11, plan 11-01).

Fetches MNIS (Members' Names Information System) biographical data for all
current Commons MPs from the Parliament UK Members Data Platform and builds
a per-API DB (bio_data.db) with only the bio_data table + the full schema's
Room identity hash.

MNIS provides: maiden speech dates, government/opposition posts, honours,
date/place of birth, committee memberships with chair flags. Posts and
honours are stored as JSON arrays for flexible timeline rendering on the
MP profile (D-02: merge into existing career timeline).

Modes:
  seed  — create fresh DB, fetch all MNIS data, insert
  delta — copy previous DB, fetch all MNIS data, upsert

Usage:
  python build_mnis.py --output bio_data.db --schema schemas/bundled_schema.json --mode seed --mps-db mps.db
  python build_mnis.py --output bio_data.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_bio_data.db --mps-db mps.db
"""

import argparse
import json
import os
import shutil
import sqlite3
import time
import xml.etree.ElementTree as ET

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

MNIS_BASE = "https://data.parliament.uk/membersdataplatform/services/mnis/members/query/"
# API limits output params to 4 per request; FullBiog includes MaidenSpeeches data
MNIS_OUTPUT_PARAMS = "FullBiog|Committees|GovernmentPosts|Honours"
MNIS_BATCH_SIZE = 30  # MPs per API call (API has URL length limit ~285 chars)

TABLE_NAMES = ["bio_data"]


# --- MP ID fetching ---

def fetch_mp_ids_from_db(mps_db_path):
    """Read all MP IDs from the mps.db file.

    Returns a list of MP ID integers.
    """
    conn = sqlite3.connect(mps_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM mps ORDER BY id")
    mp_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    logger.info("Read %d MP IDs from %s", len(mp_ids), mps_db_path)
    return mp_ids


# --- MNIS API ---

def fetch_mnis_batch(mp_ids):
    """Fetch MNIS XML for a batch of up to 40 MP IDs.

    The MNIS API accepts comma-separated IDs in the URL:
      id=4101,4102,4103/FullBiog|Committees|GovernmentPosts|Honours

    FullBiog includes MaidenSpeeches data. The API limits output params
    to 4 per request and batch size to ~40 IDs.

    Returns a list of parsed member dicts.
    """
    ids_str = ",".join(str(mid) for mid in mp_ids)
    url = f"{MNIS_BASE}id={ids_str}/{MNIS_OUTPUT_PARAMS}"

    logger.info("Fetching MNIS data for %d MPs", len(mp_ids))
    r = api_get(url, timeout=90)
    xml_text = r.text

    members = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.error("Failed to parse MNIS XML: %s", e)
        return members

    for member_elem in root.findall(".//Member"):
        member_data = parse_mnis_member(member_elem)
        if member_data:
            members.append(member_data)

    logger.info("Parsed %d member records from MNIS XML", len(members))
    return members


def _text(elem, tag):
    """Safely extract text from a child element, returning None if missing."""
    child = elem.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return None


def _normalize_date(date_str):
    """Normalize a date string to ISO YYYY-MM-DD format.

    MNIS dates are typically already ISO, but some may be in UK format.
    """
    if not date_str:
        return None
    date_str = date_str.strip()
    # Already ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    if len(date_str) >= 10 and date_str[4] == "-":
        return date_str[:10]
    # UK format DD/MM/YYYY -> YYYY-MM-DD
    parts = date_str.split("/")
    if len(parts) == 3:
        dd, mm, yyyy = parts
        return f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}"
    return date_str


def parse_mnis_member(member_elem):
    """Parse a single MNIS Member XML element into a dict.

    Extracts: Member_Id, DateOfBirth, town/country of birth, maiden speech date,
    government posts, opposition posts, honours, committees.
    Posts and honours are stored as JSON arrays.
    """
    # Member_Id is an attribute, not a child element
    mp_id_str = member_elem.get("Member_Id")
    if not mp_id_str:
        return None

    try:
        mp_id = int(mp_id_str)
    except ValueError:
        return None

    # Date of birth — child element <DateOfBirth>
    dob = _normalize_date(_text(member_elem, "DateOfBirth"))

    # Place of birth — nested in <BasicDetails>
    town_of_birth = None
    country_of_birth = None
    basic_elem = member_elem.find("BasicDetails")
    if basic_elem is not None:
        town_of_birth = _text(basic_elem, "TownOfBirth")
        country_of_birth = _text(basic_elem, "CountryOfBirth")

    # Maiden speech date
    maiden_speech_date = None
    maiden_elem = member_elem.find("MaidenSpeeches")
    if maiden_elem is not None:
        speech = maiden_elem.find("MaidenSpeech")
        if speech is not None:
            maiden_speech_date = _normalize_date(_text(speech, "SpeechDate"))

    # Government posts
    gov_posts = []
    gov_elem = member_elem.find("GovernmentPosts")
    if gov_elem is not None:
        for post in gov_elem.findall("GovernmentPost"):
            gov_posts.append({
                "type": "Government Post",
                "title": _text(post, "Title"),
                "department": _text(post, "Department"),
                "startDate": _normalize_date(_text(post, "StartDate")),
                "endDate": _normalize_date(_text(post, "EndDate")),
            })

    # Opposition posts
    opp_posts = []
    opp_elem = member_elem.find("OppositionPosts")
    if opp_elem is not None:
        for post in opp_elem.findall("OppositionPost"):
            opp_posts.append({
                "type": "Opposition Post",
                "title": _text(post, "Title"),
                "department": _text(post, "Department"),
                "startDate": _normalize_date(_text(post, "StartDate")),
                "endDate": _normalize_date(_text(post, "EndDate")),
            })

    # Honours
    honours = []
    honours_elem = member_elem.find("Honours")
    if honours_elem is not None:
        for honour in honours_elem.findall("Honour"):
            honours.append({
                "title": _text(honour, "Title"),
                "date": _normalize_date(_text(honour, "Date")),
            })

    # Committees
    committees = []
    comm_elem = member_elem.find("Committees")
    if comm_elem is not None:
        for comm in comm_elem.findall("Committee"):
            committees.append({
                "name": _text(comm, "Name"),
                "startDate": _normalize_date(_text(comm, "StartDate")),
                "endDate": _normalize_date(_text(comm, "EndDate")),
                "isChair": _text(comm, "Chair") == "True",
            })

    return {
        "mpId": mp_id,
        "maidenSpeechDate": maiden_speech_date,
        "dateOfBirth": dob,
        "townOfBirth": town_of_birth,
        "countryOfBirth": country_of_birth,
        "honoursJson": json.dumps(honours) if honours else None,
        "postsJson": json.dumps(gov_posts + opp_posts) if (gov_posts or opp_posts) else None,
        "committeesJson": json.dumps(committees) if committees else None,
    }


# --- DB operations ---

def build_bio_data_table(conn):
    """Create the bio_data table if it doesn't exist."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bio_data (
            mpId INTEGER PRIMARY KEY,
            maidenSpeechDate TEXT,
            dateOfBirth TEXT,
            townOfBirth TEXT,
            countryOfBirth TEXT,
            honoursJson TEXT,
            postsJson TEXT,
            committeesJson TEXT,
            lastUpdated INTEGER
        )
    """)
    conn.commit()


def insert_bio_data(conn, rows, timestamp_millis):
    """Batch insert bio_data rows using INSERT OR REPLACE."""
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO bio_data (
            mpId, maidenSpeechDate, dateOfBirth, townOfBirth, countryOfBirth,
            honoursJson, postsJson, committeesJson, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    tuples = [
        (
            row["mpId"],
            row.get("maidenSpeechDate"),
            row.get("dateOfBirth"),
            row.get("townOfBirth"),
            row.get("countryOfBirth"),
            row.get("honoursJson"),
            row.get("postsJson"),
            row.get("committeesJson"),
            timestamp_millis,
        )
        for row in rows
    ]

    for i in range(0, len(tuples), BATCH_SIZE):
        batch = tuples[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted bio_data: %d/%d", min(i + BATCH_SIZE, len(tuples)), len(tuples))


def get_processed_mp_ids(conn):
    """Get the set of MP IDs that already have bio_data in the checkpoint DB."""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DISTINCT mpId FROM bio_data")
        return {row[0] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        return set()


# --- Build modes ---

def fetch_all_mnis_data(mp_ids, skip_ids=None):
    """Fetch MNIS data for all MP IDs in batches of 50.

    If skip_ids is provided, those MPs are skipped (already in checkpoint).
    """
    if skip_ids is None:
        skip_ids = set()

    all_data = []
    to_fetch = [mid for mid in mp_ids if mid not in skip_ids]
    skipped = len(mp_ids) - len(to_fetch)

    logger.info(
        "Fetching MNIS data for %d MPs (%d skipped from checkpoint)",
        len(to_fetch), skipped,
    )

    for i in range(0, len(to_fetch), MNIS_BATCH_SIZE):
        batch = to_fetch[i:i + MNIS_BATCH_SIZE]
        batch_num = i // MNIS_BATCH_SIZE + 1
        total_batches = (len(to_fetch) + MNIS_BATCH_SIZE - 1) // MNIS_BATCH_SIZE
        logger.info("MNIS batch %d/%d (%d MPs)", batch_num, total_batches, len(batch))

        try:
            members = fetch_mnis_batch(batch)
            all_data.extend(members)
        except Exception as e:
            logger.error("Failed to fetch batch %d: %s", batch_num, e)
            # Continue on partial failure — same pattern as build_interests.py

        time.sleep(API_DELAY)

    logger.info("Fetched MNIS data for %d MPs total", len(all_data))
    return all_data


def build_seed(output_path, schema_path, mps_db, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch all MNIS data, insert.

    If checkpoint_db exists and has data, skip MPs already in the bio_data table.
    """
    timestamp_millis = int(time.time() * 1000)
    skip_ids = set()

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        build_bio_data_table(conn)
        skip_ids = get_processed_mp_ids(conn)
        logger.info("Resuming from checkpoint: %d MPs already processed", len(skip_ids))
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        build_bio_data_table(conn)

    mp_ids = fetch_mp_ids_from_db(mps_db)
    bio_data = fetch_all_mnis_data(mp_ids, skip_ids=skip_ids)
    if bio_data:
        insert_bio_data(conn, bio_data, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mps_db):
    """Delta mode: copy previous DB, fetch all MNIS data, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)
    build_bio_data_table(conn)

    mp_ids = fetch_mp_ids_from_db(mps_db)
    bio_data = fetch_all_mnis_data(mp_ids)
    if bio_data:
        insert_bio_data(conn, bio_data, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye MNIS bio_data per-API SQLite database (bio_data.db)."
    )
    parser.add_argument(
        "--output", default="bio_data.db",
        help="Output path for the SQLite DB file. Default: bio_data.db.",
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
        "--mps-db", required=True,
        help="Path to the mps.db file to read MP IDs for fetching.",
    )
    parser.add_argument(
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only). Skips MPs already in the bio_data table.",
    )

    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, args.mps_db, checkpoint_db=args.checkpoint_db)
    elif args.mode == "delta":
        build_delta(args.output, args.previous_db, args.schema, args.mps_db)


if __name__ == "__main__":
    main()
