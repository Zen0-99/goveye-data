#!/usr/bin/env python3
"""Per-API build script for ParlParse social links (Phase 11, plan 11-03).

Downloads ParlParse people.json (Popolo format) and separate social-media
XML files, extracts social media links and website URLs for all MPs,
matches to Parliament member IDs via datadotparl_id, and builds a per-API
DB (mp_links.db) with only the mp_links table.

ParlParse data URLs:
  people.json:           https://raw.githubusercontent.com/mysociety/parlparse/master/members/people.json
  social-media-commons:  https://raw.githubusercontent.com/mysociety/parlparse/master/members/social-media-commons.xml
  websites:              https://raw.githubusercontent.com/mysociety/parlparse/master/members/websites.xml

Modes:
  seed  — create fresh DB, download data, parse, insert
  delta — copy previous DB, download latest data, upsert

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
import xml.etree.ElementTree as ET

import requests

import schema as schema_module
from api_helper import BATCH_SIZE, api_get, logger

# --- Constants ---

PARLPARSE_BASE = "https://raw.githubusercontent.com/mysociety/parlparse/master/members/"
PARLPARSE_PEOPLE_URL = PARLPARSE_BASE + "people.json"
PARLPARSE_SOCIAL_URL = PARLPARSE_BASE + "social-media-commons.xml"
PARLPARSE_WEBSITES_URL = PARLPARSE_BASE + "websites.xml"
TABLE_NAMES = ["mp_links"]


# --- Person ID mapping ---

def download_people_json():
    """Download ParlParse people.json and build a mapping from
    ParlParse person ID → datadotparl_id (Parliament MP ID).

    Returns a dict: {parlparse_id: datadotparl_id}
    """
    logger.info("Downloading ParlParse people.json...")
    try:
        r = api_get(PARLPARSE_PEOPLE_URL, timeout=120)
        data = r.json()
        persons = data.get("persons", data) if isinstance(data, dict) else data
        logger.info("Downloaded people.json: %d persons", len(persons) if isinstance(persons, list) else 0)

        id_map = {}
        for person in persons:
            parlparse_id = person.get("id", "")
            if not parlparse_id:
                continue
            for ident in person.get("identifiers", []):
                if ident.get("scheme") == "datadotparl_id":
                    try:
                        id_map[parlparse_id] = int(ident.get("identifier"))
                    except (ValueError, TypeError):
                        pass
                    break

        logger.info("Mapped %d ParlParse IDs to datadotparl IDs", len(id_map))
        return id_map

    except Exception as e:
        logger.error("Failed to download people.json: %s", e)
        return {}


# --- Social media XML parsing ---

def download_social_media():
    """Download social-media-commons.xml and parse social media links.

    Returns a dict: {parlparse_id: {twitter, facebook, bluesky, ...}}
    """
    logger.info("Downloading social-media-commons.xml...")
    try:
        r = api_get(PARLPARSE_SOCIAL_URL, timeout=60)
        root = ET.fromstring(r.text)

        social = {}
        for info in root.findall("personinfo"):
            pid = info.get("id", "")
            if not pid:
                continue
            social[pid] = {
                "twitterHandle": info.get("twitter_username") or None,
                "facebookUrl": info.get("facebook_page") or None,
                "blueskyHandle": info.get("bluesky_handle") or None,
            }

        logger.info("Parsed %d social media entries", len(social))
        return social

    except Exception as e:
        logger.error("Failed to download social-media-commons.xml: %s", e)
        return {}


def download_websites():
    """Download websites.xml and parse personal website URLs.

    Returns a dict: {parlparse_id: website_url}
    """
    logger.info("Downloading websites.xml...")
    try:
        r = api_get(PARLPARSE_WEBSITES_URL, timeout=60)
        root = ET.fromstring(r.text)

        websites = {}
        for info in root.findall("personinfo"):
            pid = info.get("id", "")
            if not pid:
                continue
            url = info.get("mp_website", "").strip()
            if url:
                websites[pid] = url

        logger.info("Parsed %d website entries", len(websites))
        return websites

    except Exception as e:
        logger.error("Failed to download websites.xml: %s", e)
        return {}


# --- Merge and build rows ---

def build_link_rows(id_map, social, websites):
    """Merge social media and website data, mapping to Parliament MP IDs.

    Returns a list of link row dicts.
    """
    # Collect all ParlParse IDs that have any data
    all_pids = set(social.keys()) | set(websites.keys())

    rows = []
    matched = 0
    unmatched = 0

    for pid in all_pids:
        mp_id = id_map.get(pid)
        if not mp_id:
            unmatched += 1
            continue

        soc = social.get(pid, {})
        website = websites.get(pid)

        twitter = soc.get("twitterHandle")
        facebook = soc.get("facebookUrl")
        bluesky = soc.get("blueskyHandle")
        # Note: Bluesky handle is parsed but not stored — the mp_links table
        # has no bluesky column. Skip entries with only Bluesky data.

        if not any([twitter, facebook, website]):
            continue

        rows.append({
            "mpId": mp_id,
            "twitterHandle": twitter,
            "facebookUrl": facebook,
            "instagramUrl": None,
            "linkedinUrl": None,
            "wikipediaUrl": None,
            "personalWebsiteUrl": website,
        })
        matched += 1

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
    """Seed mode: create fresh DB, download data, parse, insert."""
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

    id_map = download_people_json()
    social = download_social_media()
    websites = download_websites()
    rows = build_link_rows(id_map, social, websites)

    if rows:
        insert_mp_links(conn, rows, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mps_db):
    """Delta mode: copy previous DB, download latest data, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)
    build_mp_links_table(conn)

    id_map = download_people_json()
    social = download_social_media()
    websites = download_websites()
    rows = build_link_rows(id_map, social, websites)

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
