#!/usr/bin/env python3
"""Build postcode and council mapping data from postcodes.io API.

Downloads:
  1. All UK outcodes (~11,000) from postcodes.io
  2. For each outcode, gets parliamentary constituency + admin district
  3. Downloads council details from planning.data.gov.uk (379 authorities)
  4. Builds a constituency-to-council mapping using the outcode data

Outputs:
  - postcodes.db: postcode_outcodes table (outcode, constituency, admin_district)
  - councils.db: councils + constituency_council_cross_ref tables

Usage:
  python build_postcodes_councils.py --output-postcodes postcodes.db --output-councils councils.db
"""

import argparse
import json
import os
import sqlite3
import time
import urllib.request

POSTCODES_IO_BASE = "https://api.postcodes.io"
PLANNING_DATA_URL = "https://www.planning.data.gov.uk/entity.json?dataset=local-authority&limit=400"

# Rate limiting: postcodes.io allows 600 requests/5min = 2 req/sec
# but bulk outcode lookup is more efficient
API_DELAY = 0.6  # seconds between requests


def fetch_json(url, timeout=30):
    """Fetch JSON from a URL with error handling."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'GovEye/1.0 (https://goveye.app)',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_all_outcodes():
    """Fetch all UK outcodes from postcodes.io.
    
    Returns list of outcode dicts with keys: outcode, longitude, latitude.
    Note: The /outcodes endpoint returns all outcodes but without constituency info.
    We need to query each outcode individually for constituency + admin district.
    """
    print("Fetching outcode list from postcodes.io...")
    # The /outcodes endpoint returns all outcodes
    outcodes = []
    page = 0
    while True:
        url = f"{POSTCODES_IO_BASE}/outcodes?limit=100&page={page}"
        try:
            data = fetch_json(url)
            results = data.get('result', [])
            if not results:
                break
            for r in results:
                outcodes.append(r['outcode'])
            print(f"  Page {page}: {len(results)} outcodes (total: {len(outcodes)})")
            page += 1
            time.sleep(API_DELAY)
            if page > 200:  # Safety limit
                break
        except Exception as e:
            print(f"  Error on page {page}: {e}")
            break
    return outcodes


def fetch_outcode_details(outcode):
    """Fetch details for a single outcode from postcodes.io.
    
    Returns dict with parliamentary_constituency (list), admin_district (list),
    and codes.
    """
    url = f"{POSTCODES_IO_BASE}/outcodes/{urllib.parse.quote(outcode)}"
    try:
        data = fetch_json(url, timeout=15)
        if data.get('status') == 200 and data.get('result'):
            r = data['result']
            return {
                'outcode': outcode,
                'constituencies': r.get('parliamentary_constituency', []),
                'admin_districts': r.get('admin_district', []),
                'admin_county': r.get('admin_county', []),
                'region': r.get('region', ''),
                'country': r.get('country', ''),
                'longitude': r.get('longitude'),
                'latitude': r.get('latitude'),
            }
    except Exception as e:
        print(f"  Error fetching {outcode}: {e}")
    return None


def fetch_councils():
    """Fetch council details from planning.data.gov.uk."""
    print("Fetching councils from planning.data.gov.uk...")
    data = fetch_json(PLANNING_DATA_URL, timeout=30)
    entities = data.get('entities', [])
    councils = []
    for e in entities:
        if e.get('end-date'):  # Skip defunct councils
            continue
        councils.append({
            'reference': e.get('reference', ''),
            'name': e.get('name', ''),
            'website': e.get('website', ''),
            'region': e.get('region', ''),
            'local_authority_type': e.get('local-authority-type', ''),
            'statistical_geography': e.get('statistical-geography', ''),
            'wikidata': e.get('wikidata', ''),
            'twitter': e.get('twitter', ''),
        })
    print(f"  Got {len(councils)} active councils")
    return councils


def build_postcodes_db(outcodes_data, output_path, schema_path):
    """Build the postcodes database with outcode-to-constituency mapping."""
    import schema as schema_module
    
    # Create DB with schema
    if os.path.exists(output_path):
        os.remove(output_path)
    
    conn = sqlite3.connect(output_path)
    cur = conn.cursor()
    
    # Create postcode_outcodes table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS postcode_outcodes (
            outcode TEXT NOT NULL,
            constituencyName TEXT,
            adminDistrict TEXT,
            adminCounty TEXT,
            region TEXT,
            country TEXT,
            longitude REAL,
            latitude REAL,
            PRIMARY KEY(outcode)
        )
    """)
    
    # Insert outcode data
    for oc_data in outcodes_data:
        if oc_data is None:
            continue
        # Each outcode can have multiple constituencies/districts
        # Store as comma-separated for now
        constituencies = oc_data.get('constituencies', [])
        districts = oc_data.get('admin_districts', [])
        
        cur.execute(
            "INSERT OR REPLACE INTO postcode_outcodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                oc_data['outcode'],
                '|'.join(constituencies) if constituencies else None,
                '|'.join(districts) if districts else None,
                '|'.join(oc_data.get('admin_county', [])) if oc_data.get('admin_county') else None,
                oc_data.get('region', ''),
                oc_data.get('country', ''),
                oc_data.get('longitude'),
                oc_data.get('latitude'),
            )
        )
    
    conn.commit()
    count = cur.execute("SELECT COUNT(*) FROM postcode_outcodes").fetchone()[0]
    print(f"  Inserted {count} outcodes into postcodes.db")
    conn.close()


def build_councils_db(councils, outcodes_data, output_path):
    """Build the councils database with council details and constituency mapping."""
    if os.path.exists(output_path):
        os.remove(output_path)
    
    conn = sqlite3.connect(output_path)
    cur = conn.cursor()
    
    # Create councils table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS councils (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT NOT NULL,
            name TEXT NOT NULL,
            website TEXT,
            region TEXT,
            localAuthorityType TEXT,
            statisticalGeography TEXT,
            wikidata TEXT,
            twitter TEXT,
            contactEmail TEXT,
            contactPhone TEXT,
            lastUpdated INTEGER NOT NULL
        )
    """)
    
    # Create constituency_council_cross_ref table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS constituency_council_cross_ref (
            constituencyName TEXT NOT NULL,
            councilId INTEGER NOT NULL,
            overlapPercentage REAL,
            lastUpdated INTEGER NOT NULL,
            PRIMARY KEY(constituencyName, councilId)
        )
    """)
    
    timestamp = int(time.time() * 1000)
    
    # Insert councils
    council_id_map = {}  # name -> id
    for c in councils:
        cur.execute(
            "INSERT INTO councils (reference, name, website, region, localAuthorityType, "
            "statisticalGeography, wikidata, twitter, contactEmail, contactPhone, lastUpdated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c['reference'], c['name'], c['website'], c['region'],
             c['local_authority_type'], c['statistical_geography'],
             c['wikidata'], c.get('twitter', ''), None, None, timestamp)
        )
        council_id_map[c['name'].lower()] = cur.lastrowid
    
    print(f"  Inserted {len(councils)} councils")
    
    # Build constituency-to-council mapping from outcode data
    # Each outcode maps to constituencies AND admin districts
    # We use this to infer which constituencies overlap which councils
    mapping_count = 0
    con_to_councils = {}  # constituency name -> set of council names
    
    for oc_data in outcodes_data:
        if oc_data is None:
            continue
        constituencies = oc_data.get('constituencies', [])
        districts = oc_data.get('admin_districts', [])
        
        for con in constituencies:
            if con not in con_to_councils:
                con_to_councils[con] = set()
            for district in districts:
                con_to_councils[con].add(district)
    
    # Insert cross-ref entries
    for con_name, council_names in con_to_councils.items():
        for council_name in council_names:
            council_id = council_id_map.get(council_name.lower())
            if council_id:
                cur.execute(
                    "INSERT OR REPLACE INTO constituency_council_cross_ref VALUES (?, ?, ?, ?)",
                    (con_name, council_id, None, timestamp)
                )
                mapping_count += 1
    
    print(f"  Inserted {mapping_count} constituency-council mappings")
    print(f"  Mapped {len(con_to_councils)} constituencies to councils")
    
    conn.commit()
    conn.close()


def main():
    import urllib.parse
    
    parser = argparse.ArgumentParser(description="Build postcode and council data")
    parser.add_argument("--output-postcodes", default="postcodes.db", help="Output postcodes DB path")
    parser.add_argument("--output-councils", default="councils.db", help="Output councils DB path")
    parser.add_argument("--cache-outcodes", default="outcodes_cache.json", help="Cache file for outcode data")
    args = parser.parse_args()
    
    # Check if we have cached outcode data
    if os.path.exists(args.cache_outcodes):
        print(f"Loading cached outcode data from {args.cache_outcodes}")
        with open(args.cache_outcodes, 'r') as f:
            outcodes_data = json.load(f)
        print(f"  Loaded {len(outcodes_data)} outcodes")
    else:
        # Fetch all outcodes
        outcodes = fetch_all_outcodes()
        print(f"Total outcodes to fetch: {len(outcodes)}")
        
        # Fetch details for each outcode
        outcodes_data = []
        for i, oc in enumerate(outcodes):
            if i % 100 == 0:
                print(f"  Fetching outcode {i+1}/{len(outcodes)}: {oc}")
            details = fetch_outcode_details(oc)
            outcodes_data.append(details)
            time.sleep(API_DELAY)
        
        # Cache the data
        with open(args.cache_outcodes, 'w') as f:
            json.dump(outcodes_data, f)
        print(f"  Cached {len(outcodes_data)} outcodes to {args.cache_outcodes}")
    
    # Fetch councils
    councils = fetch_councils()
    
    # Build postcodes DB
    print(f"\nBuilding postcodes DB: {args.output_postcodes}")
    build_postcodes_db(outcodes_data, args.output_postcodes, None)
    
    # Build councils DB
    print(f"\nBuilding councils DB: {args.output_councils}")
    build_councils_db(councils, outcodes_data, args.output_councils)
    
    print("\nDone!")


if __name__ == "__main__":
    main()
