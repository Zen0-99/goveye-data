#!/usr/bin/env python3
"""Per-API build script for Party Stats (Phase 11, plan 11-06).

Fetches party descriptions from Wikipedia and election stats from
the Parliament API, stores in party_stats table.

Usage:
  python build_party_stats.py --output party_stats.db --schema schemas/bundled_schema.json --mode seed --mps-db mps.db
"""

import argparse
import os
import shutil
import sqlite3
import time

import requests
from bs4 import BeautifulSoup

import schema as schema_module
from api_helper import BATCH_SIZE, api_get, logger

TABLE_NAMES = ["party_stats"]

# Major parties to fetch stats for (partyId from mps.db)
MAJOR_PARTIES = {
    4:    {"name": "Conservative Party", "wiki": "Conservative_Party_(UK)"},
    15:   {"name": "Labour Party", "wiki": "Labour_Party_(UK)"},
    17:   {"name": "Liberal Democrats", "wiki": "Liberal_Democrats"},
    44:   {"name": "Green Party", "wiki": "Green_Party_of_England_and_Wales"},
    22:   {"name": "Plaid Cymru", "wiki": "Plaid_Cymru"},
    29:   {"name": "SNP", "wiki": "Scottish_National_Party"},
    1036: {"name": "Reform UK", "wiki": "Reform_UK"},
    7:    {"name": "DUP", "wiki": "Democratic_Unionist_Party"},
    30:   {"name": "Sinn Fein", "wiki": "Sinn_Fein"},
    31:   {"name": "SDLP", "wiki": "Social_Democratic_and_Labour_Party"},
    1:    {"name": "Alliance Party", "wiki": "Alliance_Party_of_Northern_Ireland"},
    38:   {"name": "UUP", "wiki": "Ulster_Unionist_Party"},
    158:  {"name": "TUV", "wiki": "Traditional_Unionist_Voice"},
    8:    {"name": "Independent", "wiki": None},
    47:   {"name": "Speaker", "wiki": "Speaker_of_the_House_of_Commons_(UK)"},
    1115: {"name": "Your Party", "wiki": None},
    1117: {"name": "Restore Britain", "wiki": None},
}


def fetch_wikipedia_description(wiki_title):
    """Fetch the first 1-2 sentences of a Wikipedia article via the REST API."""
    if not wiki_title:
        return None
    # Use the REST summary API — returns a clean extract without HTML parsing
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{wiki_title}"
    try:
        r = requests.get(url, timeout=30, headers={
            "User-Agent": "GovEyeApp/1.0 (https://github.com/Zen0-99/goveye-data)"
        })
        if r.status_code == 200:
            data = r.json()
            extract = data.get("extract", "")
            if extract and len(extract) > 50:
                return extract[:500]
        return None
    except Exception as e:
        logger.warning("Failed to fetch Wikipedia description for %s: %s", wiki_title, e)
        return None


def get_active_party_ids(mps_db_path):
    """Get all party IDs from the mps.db that have active MPs."""
    conn = sqlite3.connect(mps_db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT partyId, partyName
        FROM mps
        WHERE partyId IS NOT NULL AND partyId > 0
        ORDER BY partyId
    """)
    parties = cursor.fetchall()
    conn.close()
    return parties


def build_party_stats_table(conn):
    """Create the party_stats table."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS party_stats (
            partyId INTEGER PRIMARY KEY,
            description TEXT,
            foundedYear TEXT,
            leaderName TEXT,
            lastElectionVoteShare REAL,
            lastElectionSeats INTEGER,
            lastElectionYear INTEGER,
            lastUpdated INTEGER
        )
    """)
    conn.commit()


def insert_party_stats(conn, party_id, description, founded_year, leader_name,
                       vote_share, seats, election_year, timestamp):
    """Insert or replace a party stats row."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO party_stats
        (partyId, description, foundedYear, leaderName,
         lastElectionVoteShare, lastElectionSeats, lastElectionYear, lastUpdated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (party_id, description, founded_year, leader_name,
          vote_share, seats, election_year, timestamp))
    conn.commit()


def build_seed(output_path, schema_path, mps_db):
    """Seed mode: create fresh DB, fetch stats for all active parties."""
    timestamp_millis = int(time.time() * 1000)

    if os.path.exists(output_path):
        os.remove(output_path)

    conn = schema_module.create_database_with_tables(output_path, schema_path, TABLE_NAMES)
    build_party_stats_table(conn)

    active_parties = get_active_party_ids(mps_db)
    logger.info("Found %d active parties in mps.db", len(active_parties))

    for party_id, party_name in active_parties:
        party_info = MAJOR_PARTIES.get(party_id, {})
        wiki_title = party_info.get("wiki")

        description = fetch_wikipedia_description(wiki_title)

        # Election stats would come from Parliament API — using defaults for now
        insert_party_stats(
            conn, party_id, description,
            founded_year=None,
            leader_name=None,
            vote_share=None,
            seats=None,
            election_year=2024,
            timestamp=timestamp_millis
        )
        logger.info("Inserted stats for partyId=%d (%s)", party_id, party_name)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mps_db):
    """Delta mode: copy previous DB, re-fetch stats, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    conn = sqlite3.connect(output_path)
    build_party_stats_table(conn)

    active_parties = get_active_party_ids(mps_db)
    for party_id, party_name in active_parties:
        party_info = MAJOR_PARTIES.get(party_id, {})
        wiki_title = party_info.get("wiki")
        description = fetch_wikipedia_description(wiki_title)
        insert_party_stats(
            conn, party_id, description,
            founded_year=None, leader_name=None,
            vote_share=None, seats=None,
            election_year=2024, timestamp=timestamp_millis
        )

    conn.execute("VACUUM")
    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(description="Build GovEye Party Stats DB.")
    parser.add_argument("--output", default="party_stats.db")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--mode", choices=["seed", "delta"], default="seed")
    parser.add_argument("--previous-db")
    parser.add_argument("--mps-db", required=True)

    args = parser.parse_args()
    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, args.mps_db)
    else:
        build_delta(args.output, args.previous_db, args.schema, args.mps_db)


if __name__ == "__main__":
    main()
