#!/usr/bin/env python3
"""Per-API build script for MP member details (synopsis, contacts, experience, biography).

Fetches Synopsis, Contact, Experience, and Biography data for all current
Commons MPs from the Parliament Members API and builds a per-API DB
(member_details.db) with four tables: mp_synopsis, mp_contacts,
mp_experience, mp_career_events.

These are per-MP endpoints (one HTTP call per MP per endpoint), so this
script makes 4 × N calls (where N ≈ 650). With API_DELAY=0.2s, the full
seed takes ~9 minutes.

Modes:
  seed  — create fresh DB, fetch all data, insert
  delta — copy previous DB, fetch all data, upsert

Usage:
  python build_member_details.py --output member_details.db --schema schemas/bundled_schema.json --mode seed --mps-db mps.db
  python build_member_details.py --output member_details.db --schema schemas/bundled_schema.json --mode delta --previous-db prev.db --mps-db mps.db
"""

import argparse
import os
import shutil
import sqlite3
import time

import schema as schema_module
from api_helper import API_DELAY, BATCH_SIZE, api_get, logger

# --- Constants ---

MEMBERS_BASE = "https://members-api.parliament.uk/api/"
TABLE_NAMES = ["mp_synopsis", "mp_contacts", "mp_experience", "mp_career_events"]

# Fallback CREATE TABLE for mp_career_events — the schema JSON does not yet
# include this table, so create_database_with_tables() won't create it.
# Run this after DB creation to ensure the table exists.
MP_CAREER_EVENTS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS mp_career_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mpId INTEGER NOT NULL,
    category TEXT NOT NULL,
    name TEXT,
    house INTEGER,
    startDate TEXT,
    endDate TEXT,
    additionalInfo TEXT,
    additionalInfoLink TEXT,
    constituencyName TEXT,
    constituencyId INTEGER,
    source TEXT NOT NULL DEFAULT 'parliament',
    lastUpdated INTEGER NOT NULL
)
"""

MP_CAREER_EVENTS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS index_mp_career_events_mpId ON mp_career_events(mpId)"
)


def ensure_mp_career_events_table(conn):
    """Create the mp_career_events table if it doesn't exist.

    The schema JSON does not yet include this table, so
    create_database_with_tables() skips it. This fallback creates it
    (and its index) idempotently after the DB is opened.
    """
    cursor = conn.cursor()
    cursor.execute(MP_CAREER_EVENTS_CREATE_SQL)
    cursor.execute(MP_CAREER_EVENTS_INDEX_SQL)
    conn.commit()


# --- MP ID fetching ---

def fetch_mp_ids_from_db(mps_db_path):
    """Read all MP IDs from the mps.db file."""
    conn = sqlite3.connect(mps_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM mps ORDER BY id")
    mp_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    logger.info("Read %d MP IDs from %s", len(mp_ids), mps_db_path)
    return mp_ids


# --- API fetching ---

def fetch_synopsis(mp_id):
    """Fetch synopsis (biography text) for a single MP.

    Returns the synopsis string or None.
    """
    try:
        r = api_get(f"{MEMBERS_BASE}Members/{mp_id}/Synopsis", timeout=30)
        data = r.json()
        return data.get("value")
    except Exception as e:
        logger.warning("Synopsis fetch failed for MP %d: %s", mp_id, e)
        return None


def fetch_contacts(mp_id):
    """Fetch contact entries for a single MP.

    Returns a list of contact dicts (ContactDto format).
    """
    try:
        r = api_get(f"{MEMBERS_BASE}Members/{mp_id}/Contact", timeout=30)
        data = r.json()
        return data.get("value", [])
    except Exception as e:
        logger.warning("Contact fetch failed for MP %d: %s", mp_id, e)
        return []


def fetch_experience(mp_id):
    """Fetch career experience entries for a single MP.

    Returns a list of experience dicts (BiographyExperienceDto format).
    """
    try:
        r = api_get(f"{MEMBERS_BASE}Members/{mp_id}/Experience", timeout=30)
        data = r.json()
        return data.get("value", [])
    except Exception as e:
        logger.warning("Experience fetch failed for MP %d: %s", mp_id, e)
        return []


def fetch_biography(mp_id):
    """Fetch the full biography for a single MP.

    Returns the "value" dict from the Biography endpoint, or None on failure.
    The value contains: representations, electionsContested, houseMemberships,
    governmentPosts, oppositionPosts, otherPosts, partyAffiliations,
    committeeMemberships.
    """
    try:
        r = api_get(f"{MEMBERS_BASE}Members/{mp_id}/Biography", timeout=30)
        data = r.json()
        return data.get("value")
    except Exception as e:
        logger.warning("Biography fetch failed for MP %d: %s", mp_id, e)
        return None


# --- Entity mapping ---

def map_synopsis(mp_id, synopsis_text, timestamp_millis):
    """Map to mp_synopsis row tuple."""
    return (mp_id, synopsis_text, timestamp_millis)


def map_contact(mp_id, contact_dto, timestamp_millis):
    """Map a ContactDto dict to an mp_contacts row tuple."""
    type_id = contact_dto.get("typeId") or 0
    return (
        mp_id,
        type_id,
        contact_dto.get("type"),
        1 if contact_dto.get("isPreferred") else 0 if contact_dto.get("isPreferred") is not None else None,
        1 if contact_dto.get("isWebAddress") else 0 if contact_dto.get("isWebAddress") is not None else None,
        contact_dto.get("line1"),
        contact_dto.get("line2"),
        contact_dto.get("line3"),
        contact_dto.get("line4"),
        contact_dto.get("line5"),
        contact_dto.get("postcode"),
        contact_dto.get("phone"),
        contact_dto.get("email"),
        contact_dto.get("website"),
        contact_dto.get("openingHours"),
        timestamp_millis,
    )


def map_experience(mp_id, exp_dto, timestamp_millis):
    """Map a BiographyExperienceDto dict to an mp_experience row tuple."""
    return (
        exp_dto.get("id") or 0,
        mp_id,
        exp_dto.get("type"),
        exp_dto.get("typeId"),
        exp_dto.get("title"),
        exp_dto.get("organisation"),
        exp_dto.get("startMonth"),
        exp_dto.get("startYear"),
        exp_dto.get("endMonth"),
        exp_dto.get("endYear"),
        timestamp_millis,
    )


# Maps each Biography "value" list key to the career-event category label
# stored in the mp_career_events.category column.
BIOGRAPHY_CATEGORY_MAP = [
    ("representations", "representation"),
    ("governmentPosts", "government_post"),
    ("oppositionPosts", "opposition_post"),
    ("otherPosts", "other_post"),
    ("partyAffiliations", "party_affiliation"),
    ("committeeMemberships", "committee"),
    ("houseMemberships", "house_membership"),
]


def map_biography_events(mp_id, bio_data, timestamp_millis):
    """Convert a Biography endpoint "value" dict into mp_career_events rows.

    Each category list (representations, governmentPosts, oppositionPosts,
    otherPosts, partyAffiliations, committeeMemberships, houseMemberships)
    becomes rows. For representations, the entry's name/id are also stored
    in constituencyName/constituencyId.

    Returns a list of row tuples matching the insert_career_events column
    order:
        (mpId, category, name, house, startDate, endDate, additionalInfo,
         additionalInfoLink, constituencyName, constituencyId, source,
         lastUpdated)
    """
    rows = []
    if not bio_data:
        return rows

    for list_key, category in BIOGRAPHY_CATEGORY_MAP:
        entries = bio_data.get(list_key) or []
        for entry in entries:
            is_representation = category == "representation"
            rows.append((
                mp_id,
                category,
                entry.get("name"),
                entry.get("house"),
                entry.get("startDate"),
                entry.get("endDate"),
                entry.get("additionalInfo"),
                entry.get("additionalInfoLink"),
                entry.get("name") if is_representation else None,
                entry.get("id") if is_representation else None,
                "parliament",
                timestamp_millis,
            ))
    return rows


# --- Insertion ---

def insert_synopsis(conn, rows):
    """Insert synopsis rows."""
    cursor = conn.cursor()
    sql = """
        INSERT OR REPLACE INTO mp_synopsis (mpId, synopsisText, lastUpdated)
        VALUES (?, ?, ?)
    """
    for i in range(0, len(rows), BATCH_SIZE):
        cursor.executemany(sql, rows[i:i + BATCH_SIZE])
        conn.commit()


def insert_contacts(conn, rows):
    """Insert contact rows."""
    cursor = conn.cursor()
    sql = """
        INSERT OR REPLACE INTO mp_contacts (
            mpId, typeId, type, isPreferred, isWebAddress,
            line1, line2, line3, line4, line5, postcode,
            phone, email, website, openingHours, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for i in range(0, len(rows), BATCH_SIZE):
        cursor.executemany(sql, rows[i:i + BATCH_SIZE])
        conn.commit()


def insert_experience(conn, rows):
    """Insert experience rows."""
    cursor = conn.cursor()
    sql = """
        INSERT OR REPLACE INTO mp_experience (
            id, mpId, type, typeId, title, organisation,
            startMonth, startYear, endMonth, endYear, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for i in range(0, len(rows), BATCH_SIZE):
        cursor.executemany(sql, rows[i:i + BATCH_SIZE])
        conn.commit()


def insert_career_events(conn, rows):
    """Insert mp_career_events rows.

    Existing rows for each MP (source='parliament') are deleted before
    inserting so updates replace cleanly. Rows are grouped by mpId for the
    delete to avoid one DELETE per row.
    """
    if not rows:
        return
    cursor = conn.cursor()
    # Delete existing parliament-sourced career events for the MPs in this
    # batch before inserting fresh rows.
    mp_ids = {row[0] for row in rows}
    for mp_id in mp_ids:
        cursor.execute(
            "DELETE FROM mp_career_events WHERE mpId = ? AND source = 'parliament'",
            (mp_id,),
        )
    sql = """
        INSERT INTO mp_career_events (
            mpId, category, name, house, startDate, endDate,
            additionalInfo, additionalInfoLink, constituencyName,
            constituencyId, source, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for i in range(0, len(rows), BATCH_SIZE):
        cursor.executemany(sql, rows[i:i + BATCH_SIZE])
        conn.commit()


# --- Build ---

def build_seed(output_path, schema_path, mps_db, mp_limit=None, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch all data, insert."""
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        ensure_mp_career_events_table(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT mpId FROM mp_synopsis")
        processed = {row[0] for row in cursor.fetchall()}
        logger.info("Resuming from checkpoint: %d MPs already in DB", len(processed))
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        ensure_mp_career_events_table(conn)
        processed = set()

    mp_ids = fetch_mp_ids_from_db(mps_db)
    if mp_limit:
        mp_ids = mp_ids[:mp_limit]

    synopsis_rows = []
    contact_rows = []
    experience_rows = []
    career_event_rows = []

    for i, mp_id in enumerate(mp_ids):
        if mp_id in processed:
            continue

        # Fetch all four endpoints for this MP
        synopsis = fetch_synopsis(mp_id)
        if synopsis:
            synopsis_rows.append(map_synopsis(mp_id, synopsis, timestamp_millis))

        contacts = fetch_contacts(mp_id)
        for c in contacts:
            contact_rows.append(map_contact(mp_id, c, timestamp_millis))

        experience = fetch_experience(mp_id)
        for e in experience:
            experience_rows.append(map_experience(mp_id, e, timestamp_millis))

        bio = fetch_biography(mp_id)
        if bio:
            career_event_rows.extend(map_biography_events(mp_id, bio, timestamp_millis))

        # Batch insert every 50 MPs to avoid holding everything in memory
        if (i + 1) % 50 == 0:
            insert_synopsis(conn, synopsis_rows)
            insert_contacts(conn, contact_rows)
            insert_experience(conn, experience_rows)
            insert_career_events(conn, career_event_rows)
            synopsis_rows = []
            contact_rows = []
            experience_rows = []
            career_event_rows = []
            logger.info("Processed %d/%d MPs", i + 1, len(mp_ids))

        time.sleep(API_DELAY)

    # Insert remaining
    if synopsis_rows:
        insert_synopsis(conn, synopsis_rows)
    if contact_rows:
        insert_contacts(conn, contact_rows)
    if experience_rows:
        insert_experience(conn, experience_rows)
    if career_event_rows:
        insert_career_events(conn, career_event_rows)

    logger.info("VACUUMing database...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mps_db, mp_limit=None):
    """Delta mode: copy previous DB, fetch all data, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    conn = sqlite3.connect(output_path)
    ensure_mp_career_events_table(conn)

    mp_ids = fetch_mp_ids_from_db(mps_db)
    if mp_limit:
        mp_ids = mp_ids[:mp_limit]

    synopsis_rows = []
    contact_rows = []
    experience_rows = []
    career_event_rows = []

    for i, mp_id in enumerate(mp_ids):
        synopsis = fetch_synopsis(mp_id)
        if synopsis:
            synopsis_rows.append(map_synopsis(mp_id, synopsis, timestamp_millis))

        contacts = fetch_contacts(mp_id)
        for c in contacts:
            contact_rows.append(map_contact(mp_id, c, timestamp_millis))

        experience = fetch_experience(mp_id)
        for e in experience:
            experience_rows.append(map_experience(mp_id, e, timestamp_millis))

        bio = fetch_biography(mp_id)
        if bio:
            career_event_rows.extend(map_biography_events(mp_id, bio, timestamp_millis))

        if (i + 1) % 50 == 0:
            insert_synopsis(conn, synopsis_rows)
            insert_contacts(conn, contact_rows)
            insert_experience(conn, experience_rows)
            insert_career_events(conn, career_event_rows)
            synopsis_rows = []
            contact_rows = []
            experience_rows = []
            career_event_rows = []
            logger.info("Processed %d/%d MPs", i + 1, len(mp_ids))

        time.sleep(API_DELAY)

    if synopsis_rows:
        insert_synopsis(conn, synopsis_rows)
    if contact_rows:
        insert_contacts(conn, contact_rows)
    if experience_rows:
        insert_experience(conn, experience_rows)
    if career_event_rows:
        insert_career_events(conn, career_event_rows)

    logger.info("VACUUMing database...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye member details per-API SQLite database (member_details.db)."
    )
    parser.add_argument("--output", default="member_details.db",
                        help="Output path for the SQLite DB file.")
    parser.add_argument("--schema", required=True,
                        help="Path to the Room exported schema JSON.")
    parser.add_argument("--mode", choices=["seed", "delta"], default="seed",
                        help="Build mode: seed (full) or delta (incremental).")
    parser.add_argument("--previous-db",
                        help="Path to previous DB file (required for delta mode).")
    parser.add_argument("--mps-db", required=True,
                        help="Path to mps.db (for MP ID list).")
    parser.add_argument("--mp-limit", type=int, default=None,
                        help="Limit number of MPs fetched (for testing).")
    parser.add_argument("--checkpoint-db",
                        help="Path to a checkpoint DB to resume from (seed mode only).")
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, args.mps_db,
                   mp_limit=args.mp_limit, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema, args.mps_db,
                    mp_limit=args.mp_limit)


if __name__ == "__main__":
    main()
