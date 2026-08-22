#!/usr/bin/env python3
"""Per-API build script for Party Stats (Phase 11, plan 11-06).

Fetches party descriptions, infobox data (founded year, leader), and
2024 general election results (seats won, vote share) from Wikipedia,
stores in party_stats table.

Usage:
  python build_party_stats.py --output party_stats.db --schema schemas/bundled_schema.json --mode seed --mps-db mps.db
"""

import argparse
import os
import re
import shutil
import sqlite3
import time

import requests
from bs4 import BeautifulSoup

import schema as schema_module
from api_helper import BATCH_SIZE, api_get, logger

TABLE_NAMES = ["party_stats"]

WIKI_HEADERS = {
    "User-Agent": "GovEyeApp/1.0 (https://github.com/Zen0-99/goveye-data)"
}

# Major parties to fetch stats for (partyId from mps.db)
MAJOR_PARTIES = {
    4:    {"name": "Conservative Party", "wiki": "Conservative_Party_(UK)"},
    15:   {"name": "Labour Party", "wiki": "Labour_Party_(UK)"},
    17:   {"name": "Liberal Democrats", "wiki": "Liberal_Democrats_(UK)"},
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

# Map Wikipedia election-table party names to MNIS party IDs.
# The 2024 election results table uses slightly different names.
ELECTION_NAME_TO_PARTY_ID = {
    "Labour Party": 15,
    "Conservative Party": 4,
    "Liberal Democrats": 17,
    "Reform UK": 1036,
    "Green Party": 44,
    "Scottish National Party": 29,
    "Plaid Cymru": 22,
    "Democratic Unionist Party": 7,
    "Sinn Féin": 30,
    "Social Democratic and Labour Party": 31,
    "Alliance Party": 1,
    "Ulster Unionist Party": 38,
    "Traditional Unionist Voice": 158,
    "Conservative and Unionist Party": 4,
    "Labour": 15,
    "Conservative": 4,
    "Reform": 1036,
    "Green": 44,
    "SNP": 29,
}


def fetch_wikipedia_description(wiki_title):
    """Fetch the first 1-2 sentences of a Wikipedia article via the REST API."""
    if not wiki_title:
        return None
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{wiki_title}"
    try:
        r = requests.get(url, timeout=30, headers=WIKI_HEADERS)
        if r.status_code == 200:
            data = r.json()
            extract = data.get("extract", "")
            if extract and len(extract) > 50:
                return extract[:500]
        return None
    except Exception as e:
        logger.warning("Failed to fetch Wikipedia description for %s: %s", wiki_title, e)
        return None


def fetch_infobox_data(wiki_title):
    """Fetch founded year and leader name from a Wikipedia article infobox.

    Returns:
        (founded_year, leader_name) tuple of strings or None values.
    """
    if not wiki_title:
        return None, None
    url = f"https://en.wikipedia.org/w/api.php?action=parse&page={wiki_title}&prop=text&format=json&redirects=1"
    try:
        r = requests.get(url, timeout=30, headers=WIKI_HEADERS)
        if r.status_code != 200:
            return None, None
        data = r.json()
        html = data.get("parse", {}).get("text", {}).get("*", "")
        if not html:
            return None, None

        soup = BeautifulSoup(html, "html.parser")
        infobox = soup.find("table", class_="infobox")
        if not infobox:
            return None, None

        founded_year = None
        leader_name = None

        for row in infobox.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue
            label = th.get_text(strip=True).lower()
            value = td.get_text(strip=True, separator=" ")

            # "Leader" or "President" (Sinn Féin uses President)
            if label in ("leader", "president") and not leader_name:
                leader_name = re.split(r"\s*\[", value)[0].strip()
            elif "founded" in label and not founded_year:
                year_match = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", value)
                if year_match:
                    founded_year = year_match.group(1)

        return founded_year, leader_name
    except Exception as e:
        logger.warning("Failed to fetch infobox for %s: %s", wiki_title, e)
        return None, None


def fetch_2024_election_results():
    """Fetch 2024 UK general election results from Wikipedia.

    Parses the results table to extract seats won and vote share per party.

    Returns:
        Dict mapping MNIS party ID to {"seats": int, "vote_share": float (0-1)}.
    """
    results = {}
    url = "https://en.wikipedia.org/w/api.php?action=parse&page=2024_United_Kingdom_general_election&prop=text&format=json"
    try:
        r = requests.get(url, timeout=60, headers=WIKI_HEADERS)
        if r.status_code != 200:
            logger.warning("Failed to fetch 2024 election page: HTTP %d", r.status_code)
            return results
        data = r.json()
        html = data.get("parse", {}).get("text", {}).get("*", "")
        if not html:
            return results

        soup = BeautifulSoup(html, "html.parser")

        # Find the results table with "MPs" and "Aggregate votes" and "Of total"
        # There are multiple wikitables — we want the one with vote share percentages,
        # NOT the candidates table (which has "Candidates" column instead).
        results_table = None
        for table in soup.find_all("table", class_="wikitable"):
            headers = [th.get_text(strip=True, separator=" ") for th in table.find_all("th")]
            header_text = " ".join(headers)
            # The results table has "MPs", "Aggregate votes", and "Of total" (vote %)
            # but NOT "Candidates" (that's a different table)
            if "MPs" in header_text and "Aggregate votes" in header_text and "Candidates" not in header_text:
                results_table = table
                break

        if not results_table:
            logger.warning("Could not find 2024 election results table")
            return results

        for row in results_table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 5:
                continue
            cell_texts = [c.get_text(strip=True, separator=" ") for c in cells]

            # Skip header rows
            party_name = cell_texts[1] if len(cell_texts) > 1 else ""
            if not party_name or party_name in ("Leader", "Total", "Party", "Affiliate"):
                continue

            # Clean party name — remove footnote markers
            party_name_clean = re.sub(r"\s*\[.*?\]", "", party_name).strip()

            # Find MNIS party ID
            party_id = ELECTION_NAME_TO_PARTY_ID.get(party_name_clean)
            if not party_id:
                for wiki_name, pid in ELECTION_NAME_TO_PARTY_ID.items():
                    if wiki_name.lower() in party_name_clean.lower() or party_name_clean.lower() in wiki_name.lower():
                        party_id = pid
                        break
            if not party_id:
                continue

            # Parse seats (col 3) — strip footnote markers like [ ab ]
            seats_raw = re.sub(r"\s*\[.*?\]", "", cell_texts[3]).replace(",", "").strip()
            try:
                seats = int(seats_raw)
            except ValueError:
                seats = None

            # Parse vote share — find the last cell with a % sign
            vote_share = None
            for ct in reversed(cell_texts):
                ct_clean = re.sub(r"\s*\[.*?\]", "", ct).strip()
                if "%" in ct_clean:
                    pct_match = re.search(r"([\d.]+)%", ct_clean)
                    if pct_match:
                        vote_share = float(pct_match.group(1)) / 100.0
                    break

            if seats is not None or vote_share is not None:
                results[party_id] = {"seats": seats, "vote_share": vote_share}
                logger.info("Election result: %s (ID=%d) -> seats=%s, vote_share=%s",
                            party_name_clean, party_id, seats, vote_share)

        logger.info("Fetched election results for %d parties", len(results))
        return results

    except Exception as e:
        logger.warning("Failed to fetch 2024 election results: %s", e)
        return results


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

    # Fetch 2024 election results once (single API call)
    election_results = fetch_2024_election_results()

    for party_id, party_name in active_parties:
        party_info = MAJOR_PARTIES.get(party_id, {})
        wiki_title = party_info.get("wiki")

        description = fetch_wikipedia_description(wiki_title)
        founded_year, leader_name = fetch_infobox_data(wiki_title)

        # Election stats from the 2024 results table
        election = election_results.get(party_id, {})
        vote_share = election.get("vote_share")
        seats = election.get("seats")

        insert_party_stats(
            conn, party_id, description,
            founded_year=founded_year,
            leader_name=leader_name,
            vote_share=vote_share,
            seats=seats,
            election_year=2024,
            timestamp=timestamp_millis
        )
        logger.info("Inserted stats for partyId=%d (%s): founded=%s, leader=%s, seats=%s, vote=%s",
                    party_id, party_name, founded_year, leader_name, seats, vote_share)

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
    election_results = fetch_2024_election_results()

    for party_id, party_name in active_parties:
        party_info = MAJOR_PARTIES.get(party_id, {})
        wiki_title = party_info.get("wiki")
        description = fetch_wikipedia_description(wiki_title)
        founded_year, leader_name = fetch_infobox_data(wiki_title)

        election = election_results.get(party_id, {})
        vote_share = election.get("vote_share")
        seats = election.get("seats")

        insert_party_stats(
            conn, party_id, description,
            founded_year=founded_year, leader_name=leader_name,
            vote_share=vote_share, seats=seats,
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
