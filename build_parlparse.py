#!/usr/bin/env python3
"""Per-API build script for ParlParse social links (Phase 11, plan 11-03).

Downloads ParlParse people.json (Popolo format), extracts social media links
and Wikipedia URLs for all MPs, matches to Parliament member IDs, and builds
a per-API DB (mp_links.db) with only the mp_links table.

ParlParse people.json URL: https://parser.theyworkforyou.com/people.json

Modes:
  seed  — create fresh DB, download JSON, parse, insert
  delta — copy previous DB, download latest JSON, upsert

Usage:
  python build_parlparse.py --output mp_links.db --schema schemas/bundled_schema.json --mode seed --mps-db mps.db
  python build_parlparse.py --output mp_links.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_mp_links.db --mps-db mps.db
"""

import argparse
import json
import os
import shutil
import sqlite3
import time

import requests

import schema as schema_module
from api_helper import BATCH_SIZE, api_get, logger

# --- Constants ---

PARLPARSE_PEOPLE_URL = "https://raw.githubusercontent.com/mysociety/parlparse/master/members/people.json"
TABLE_NAMES = ["mp_links"]

# --- Honorifics stripping (reused from build_debates.py) ---

_HONORIFICS = {
    "mr", "mrs", "ms", "miss", "dr", "sir", "dame", "lord", "lady",
    "baroness", "earl", "viscount", "rt", "hon", "right", "rev",
    "reverend", "father", "fr",
}


def _strip_honorifics(name):
    """Strip leading honorifics from a name."""
    if not name:
        return ""
    words = name.strip().split()
    while words and words[0].lower().rstrip(".") in _HONORIFICS:
        words.pop(0)
    return " ".join(words)


def build_name_lookup(mps_db_path):
    """Build a name -> memberId lookup from the MPs DB."""
    conn = sqlite3.connect(mps_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nameDisplayAs, nameListAs FROM mps WHERE house = 1")
    lookup = {}
    for row in cursor.fetchall():
        mp_id, display_name, list_name = row
        if display_name:
            stripped = _strip_honorifics(display_name)
            lookup[stripped.lower().strip()] = mp_id
        if list_name:
            parts = list_name.split(", ")
            if len(parts) == 2:
                reversed_name = f"{parts[1]} {parts[0]}"
                stripped = _strip_honorifics(reversed_name)
                lookup[stripped.lower().strip()] = mp_id
    conn.close()
    logger.info("Built name lookup: %d MPs", len(lookup))
    return lookup


def match_person(name, lookup):
    """Match a ParlParse person name to a Parliament member ID. Returns 0 if no match."""
    if not name:
        return 0
    stripped = _strip_honorifics(name).lower().strip()
    if stripped in lookup:
        return lookup[stripped]
    # Try without middle initials
    words = stripped.split()
    if len(words) > 2:
        filtered = [w for w in words if len(w) > 1]
        if len(filtered) >= 2:
            reduced = " ".join(filtered)
            if reduced in lookup:
                return lookup[reduced]
    return 0


# --- JSON parsing ---

def download_people_json():
    """Download ParlParse people.json and return the parsed JSON object."""
    logger.info("Downloading ParlParse people.json...")
    try:
        r = api_get(PARLPARSE_PEOPLE_URL, timeout=120)
        data = r.json()
        persons = data.get("persons", data) if isinstance(data, dict) else data
        logger.info("Downloaded people.json: %d persons", len(persons) if isinstance(persons, list) else 0)
        return persons
    except Exception as e:
        logger.error("Failed to download people.json: %s", e)
        return []


def parse_person(person, name_lookup):
    """Extract social links from a Popolo person object.

    Returns a dict with mpId and link fields, or None if no match.
    """
    name = person.get("name", "")
    if not name:
        return None

    mp_id = match_person(name, name_lookup)
    if mp_id == 0:
        return None

    # Extract from identifiers (scheme-based)
    identifiers = person.get("identifiers", [])
    links = person.get("links", [])

    twitter_handle = None
    for ident in identifiers:
        if ident.get("scheme") == "twitter":
            twitter_handle = ident.get("identifier")
            break

    # Extract from links (note-based)
    facebook_url = None
    instagram_url = None
    linkedin_url = None
    wikipedia_url = None
    personal_website_url = None

    for link in links:
        note = (link.get("note") or "").lower()
        url = link.get("url", "")
        if not url:
            continue
        if note == "twitter" and not twitter_handle:
            # Extract handle from URL
            twitter_handle = url.rstrip("/").split("/")[-1]
        elif note == "facebook":
            facebook_url = url
        elif note == "instagram":
            instagram_url = url
        elif note == "linkedin":
            linkedin_url = url
        elif note == "wikipedia":
            wikipedia_url = url
        elif note in ("personal", "website", "homepage"):
            personal_website_url = url

    # Skip if no links at all
    if not any([twitter_handle, facebook_url, instagram_url,
                linkedin_url, wikipedia_url, personal_website_url]):
        return None

    return {
        "mpId": mp_id,
        "twitterHandle": twitter_handle,
        "facebookUrl": facebook_url,
        "instagramUrl": instagram_url,
        "linkedinUrl": linkedin_url,
        "wikipediaUrl": wikipedia_url,
        "personalWebsiteUrl": personal_website_url,
    }


def parse_people_json(persons, name_lookup):
    """Parse all persons and return a list of link row dicts."""
    rows = []
    matched = 0
    unmatched = 0

    for person in persons:
        result = parse_person(person, name_lookup)
        if result:
            rows.append(result)
            matched += 1
        else:
            unmatched += 1

    logger.info("Parsed %d link rows (%d matched, %d unmatched/no links)",
                len(rows), matched, unmatched)
    return rows


# --- DB operations ---

def build_mp_links_table(conn):
    """Create the mp_links table if it doesn't exist."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mp_links (
            mpId INTEGER PRIMARY KEY,
            twitterHandle TEXT,
            facebookUrl TEXT,
            instagramUrl TEXT,
            linkedinUrl TEXT,
            wikipediaUrl TEXT,
            personalWebsiteUrl TEXT,
            lastUpdated INTEGER
        )
    """)
    conn.commit()


def insert_mp_links(conn, rows, timestamp_millis):
    """Batch insert mp_links rows using INSERT OR REPLACE."""
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO mp_links (
            mpId, twitterHandle, facebookUrl, instagramUrl,
            linkedinUrl, wikipediaUrl, personalWebsiteUrl, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    tuples = [
        (
            row["mpId"],
            row.get("twitterHandle"),
            row.get("facebookUrl"),
            row.get("instagramUrl"),
            row.get("linkedinUrl"),
            row.get("wikipediaUrl"),
            row.get("personalWebsiteUrl"),
            timestamp_millis,
        )
        for row in rows
    ]

    for i in range(0, len(tuples), BATCH_SIZE):
        batch = tuples[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted mp_links: %d/%d", min(i + BATCH_SIZE, len(tuples)), len(tuples))


# --- Build modes ---

def build_seed(output_path, schema_path, mps_db, checkpoint_db=None):
    """Seed mode: create fresh DB, download JSON, parse, insert."""
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        build_mp_links_table(conn)
        logger.info("Resuming from checkpoint")
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        build_mp_links_table(conn)

    name_lookup = build_name_lookup(mps_db)
    persons = download_people_json()
    rows = parse_people_json(persons, name_lookup)

    if rows:
        insert_mp_links(conn, rows, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mps_db):
    """Delta mode: copy previous DB, download latest JSON, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)
    build_mp_links_table(conn)

    name_lookup = build_name_lookup(mps_db)
    persons = download_people_json()
    rows = parse_people_json(persons, name_lookup)

    if rows:
        insert_mp_links(conn, rows, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye ParlParse social links per-API SQLite database (mp_links.db)."
    )
    parser.add_argument("--output", default="mp_links.db",
                        help="Output path for the SQLite DB file. Default: mp_links.db.")
    parser.add_argument("--schema", required=True,
                        help="Path to the Room exported schema JSON (bundled_schema.json).")
    parser.add_argument("--mode", choices=["seed", "delta"], default="seed",
                        help="Build mode: seed (full) or delta (incremental). Default: seed.")
    parser.add_argument("--previous-db",
                        help="Path to previous DB file (required for delta mode).")
    parser.add_argument("--mps-db", required=True,
                        help="Path to the mps.db file for MP name matching.")
    parser.add_argument("--checkpoint-db",
                        help="Path to a checkpoint DB to resume from (seed mode only).")

    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, args.mps_db, checkpoint_db=args.checkpoint_db)
    elif args.mode == "delta":
        build_delta(args.output, args.previous_db, args.schema, args.mps_db)


if __name__ == "__main__":
    main()
