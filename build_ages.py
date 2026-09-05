#!/usr/bin/env python3
"""Build script for MP birth dates from Wikidata.

Downloads ParlParse people.json (which has Wikidata Q-IDs for each MP),
then batch-queries the Wikidata SPARQL endpoint for dateOfBirth.

Produces ages.db with a single `mp_ages` table (mpId, dateOfBirth).
This is a separate DB so it doesn't need rebuilding when other data changes.

The merge step (merge_dbs.py) updates bio_data.dateOfBirth from mp_ages.

Usage:
  python build_ages.py --output ages.db --goveye-db goveye.db
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_ages")

PARLPARSE_PEOPLE_URL = "https://raw.githubusercontent.com/mysociety/parlparse/master/members/people.json"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIDATA_USER_AGENT = "GovEye/1.0 (https://goveye.app; contact@goveye.app)"


def download_people_json():
    """Download ParlParse people.json and return parsed JSON."""
    logger.info("Downloading ParlParse people.json...")
    try:
        req = urllib.request.Request(PARLPARSE_PEOPLE_URL)
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        logger.info("Downloaded people.json: %d persons", len(data.get("persons", [])))
        return data
    except Exception as e:
        logger.error("Failed to download people.json: %s", e)
        return {"persons": []}


def build_wikidata_lookup(data, goveye_db_path):
    """Build a mapping of parliament_member_id → wikidata_qid from ParlParse data.

    Only includes MPs that exist in the goveye.db mps table.
    """
    # Get the set of MP IDs we care about
    conn = sqlite3.connect(goveye_db_path)
    mp_ids = set(row[0] for row in conn.execute("SELECT id FROM mps").fetchall())
    conn.close()
    logger.info("Looking for Wikidata Q-IDs for %d MPs", len(mp_ids))

    lookup = {}
    for person in data.get("persons", []):
        # Find the datadotparl_id (parliament member ID)
        identifiers = person.get("identifiers", [])
        mp_id = None
        qid = None
        for ident in identifiers:
            scheme = ident.get("scheme", "")
            value = ident.get("identifier", "")
            if scheme == "datadotparl_id":
                try:
                    mp_id = int(value)
                except (ValueError, TypeError):
                    pass
            if scheme == "wikidata":
                qid = value
        if mp_id and qid and mp_id in mp_ids:
            lookup[mp_id] = qid

    logger.info("Matched %d MPs to Wikidata Q-IDs", len(lookup))
    return lookup


def query_wikidata_batch(qids):
    """Query Wikidata SPARQL for dateOfBirth of the given Q-IDs.

    Returns a dict of qid → dateOfBirth string (YYYY-MM-DD or None).
    """
    if not qids:
        return {}

    # SPARQL query — batch all Q-IDs in one query using VALUES
    values = " ".join(f"wd:{q}" for q in qids)
    query = f"""
SELECT ?item ?dob WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item wdt:P569 ?dob }}
}}
"""
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{WIKIDATA_SPARQL_URL}?{params}"

    logger.info("Querying Wikidata SPARQL for %d entities...", len(qids))
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": WIKIDATA_USER_AGENT,
            "Accept": "application/sparql-results+json",
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            results = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error("Wikidata SPARQL query failed: %s", e)
        return {}

    dob_map = {}
    bindings = results.get("results", {}).get("bindings", [])
    for binding in bindings:
        qid = binding.get("item", {}).get("value", "").split("/")[-1]
        dob = binding.get("dob", {}).get("value")
        if dob:
            # Wikidata dates may have precision (e.g. "+1953-11-26T00:00:00Z")
            # Take just the date part
            dob = dob[:10]
        dob_map[qid] = dob

    found = sum(1 for v in dob_map.values() if v)
    logger.info("Wikidata returned %d results, %d with dateOfBirth", len(dob_map), found)
    return dob_map


def create_ages_db(output_path):
    """Create the ages.db with mp_ages table."""
    conn = sqlite3.connect(output_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mp_ages (
            mpId INTEGER PRIMARY KEY,
            dateOfBirth TEXT
        )
    """)
    conn.commit()
    return conn


def search_wikidata_by_name(name):
    """Search Wikidata API by name and return the Q-ID of the best UK politician match."""
    params = urllib.parse.urlencode({
        "action": "wbsearchentities",
        "search": name,
        "language": "en",
        "format": "json",
        "type": "item",
        "limit": "5",
    })
    url = f"https://www.wikidata.org/w/api.php?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": WIKIDATA_USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for r in data.get("search", []):
            desc = r.get("description", "").lower()
            label = r.get("label", "").lower()
            if any(kw in desc for kw in ["british politician", "uk politician",
                                          "member of parliament", "mp for",
                                          "british mp", "politician from the united kingdom",
                                          "politician from the uk", "british politician from"]):
                return r["id"]
            if label == name.lower() and "politician" in desc:
                return r["id"]
    except Exception:
        pass
    return None


def get_wikidata_dob(qid):
    """Fetch date of birth (P569) for a single Wikidata entity."""
    params = urllib.parse.urlencode({
        "action": "wbgetclaims",
        "entity": qid,
        "property": "P569",
        "format": "json",
    })
    url = f"https://www.wikidata.org/w/api.php?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": WIKIDATA_USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        claims = data.get("claims", {}).get("P569", [])
        if claims:
            val = claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
            time_str = val.get("time", "")
            if time_str:
                return time_str[1:11]  # Remove + prefix, take YYYY-MM-DD
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Build MP birth dates from Wikidata")
    parser.add_argument("--output", required=True, help="Path to ages.db output")
    parser.add_argument("--goveye-db", required=True, help="Path to goveye.db for MP ID lookup")
    args = parser.parse_args()

    if not os.path.exists(args.goveye_db):
        print(f"ERROR: {args.goveye_db} does not exist")
        sys.exit(1)

    # 1. Download ParlParse people.json
    data = download_people_json()

    # 2. Build wikidata lookup (mp_id → qid)
    qid_lookup = build_wikidata_lookup(data, args.goveye_db)
    if not qid_lookup:
        logger.warning("No Wikidata Q-IDs found in ParlParse — will search by name")

    # 3. Query Wikidata for dateOfBirth (batch) for MPs with Q-IDs
    mp_ages = {}
    if qid_lookup:
        all_qids = list(qid_lookup.values())
        dob_map = query_wikidata_batch(all_qids)
        for mp_id, qid in qid_lookup.items():
            dob = dob_map.get(qid)
            if dob:
                mp_ages[mp_id] = dob
        logger.info("Got birth dates for %d / %d MPs via ParlParse Q-IDs", len(mp_ages), len(qid_lookup))

    # 4. For MPs without Q-IDs in ParlParse (e.g. 2024 cohort), search Wikidata
    #    API by name and fetch DOB directly. This is the fallback for new MPs
    #    whose Wikidata entries haven't been linked in ParlParse yet.
    conn = sqlite3.connect(args.goveye_db)
    all_mp_ids = set(row[0] for row in conn.execute("SELECT id FROM mps").fetchall())
    conn.close()
    missing_ids = all_mp_ids - set(mp_ages.keys())
    if missing_ids:
        logger.info("Searching Wikidata API by name for %d MPs without Q-IDs...", len(missing_ids))
        # Get MP names from goveye.db
        conn = sqlite3.connect(args.goveye_db)
        mp_names = {}
        for row in conn.execute("SELECT id, nameDisplayAs FROM mps"):
            if row[0] in missing_ids:
                mp_names[row[0]] = row[1]
        conn.close()

        import re as _re
        search_found = 0
        for mp_id, name in mp_names.items():
            clean_name = _re.sub(r'^(Sir|Dame|Dr|Mr|Mrs|Ms)\s+', '', name).strip()
            qid = search_wikidata_by_name(clean_name)
            if qid:
                dob = get_wikidata_dob(qid)
                if dob:
                    mp_ages[mp_id] = dob
                    search_found += 1
            time.sleep(0.5)  # Rate limit
        logger.info("Found %d more DOBs via Wikidata name search", search_found)

    logger.info("Total birth dates: %d / %d MPs", len(mp_ages), len(all_mp_ids))

    # 5. Write to ages.db
    conn = create_ages_db(args.output)
    rows = [(mp_id, dob) for mp_id, dob in mp_ages.items()]
    conn.executemany("INSERT OR REPLACE INTO mp_ages (mpId, dateOfBirth) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()

    logger.info("Wrote %d rows to %s", len(rows), args.output)


if __name__ == "__main__":
    main()
