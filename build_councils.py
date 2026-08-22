#!/usr/bin/env python3
"""Build councils database from planning.data.gov.uk.

Downloads council details (name, website, region, type) for ~329 active
UK local authorities. The constituency-to-council mapping is done at
runtime by matching constituency names to council names.

Outputs:
  - councils.db: councils table only

Usage:
  python build_councils.py --output councils.db
"""

import argparse
import json
import os
import sqlite3
import time
import urllib.request

PLANNING_DATA_URL = "https://www.planning.data.gov.uk/entity.json?dataset=local-authority&limit=400"


def fetch_councils():
    """Fetch council details from planning.data.gov.uk."""
    print("Fetching councils from planning.data.gov.uk...")
    req = urllib.request.Request(PLANNING_DATA_URL, headers={
        'User-Agent': 'GovEye/1.0',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    
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


def build_councils_db(councils, output_path):
    """Build the councils database."""
    if os.path.exists(output_path):
        os.remove(output_path)
    
    conn = sqlite3.connect(output_path)
    cur = conn.cursor()
    
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
    
    timestamp = int(time.time() * 1000)
    
    for c in councils:
        cur.execute(
            "INSERT INTO councils (reference, name, website, region, localAuthorityType, "
            "statisticalGeography, wikidata, twitter, contactEmail, contactPhone, lastUpdated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c['reference'], c['name'], c['website'], c['region'],
             c['local_authority_type'], c['statistical_geography'],
             c['wikidata'], c.get('twitter', ''), None, None, timestamp)
        )
    
    conn.commit()
    
    count = cur.execute("SELECT COUNT(*) FROM councils").fetchone()[0]
    print(f"  Inserted {count} councils into {output_path}")
    
    # Show sample
    cur.execute("SELECT name, website, localAuthorityType FROM councils ORDER BY name LIMIT 10")
    print("  Sample councils:")
    for name, website, la_type in cur.fetchall():
        print(f"    {name} ({la_type}) - {website}")
    
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Build councils database")
    parser.add_argument("--output", default="councils.db", help="Output DB path")
    args = parser.parse_args()
    
    councils = fetch_councils()
    build_councils_db(councils, args.output)
    print("\nDone!")


if __name__ == "__main__":
    main()
