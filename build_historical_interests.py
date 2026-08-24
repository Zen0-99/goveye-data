#!/usr/bin/env python3
"""Build historical interests from mySociety all_time_register dataset.

Imports financial interests data (2000-2026) for:
  - All 626 current MPs that have historical data in the mySociety dataset
  - All former Prime Ministers (2000-2026): Blair, Brown, Cameron, May,
    Johnson, Truss, Sunak, plus current PM Starmer

The script:
  1. Downloads (or uses cached) the mySociety all_time_register CSV
  2. Matches member names to GovEye MP IDs (current MPs) or creates
     placeholder MP records for former PMs not in the GovEye DB
  3. Extracts monetary amounts from free-text using a pattern hierarchy
  4. Maps mySociety categories to GovEye category numbers and buckets
  5. Inserts into the interests table with a high ID offset (1,000,000+)
     to distinguish historical entries from live API entries

Usage:
  python build_historical_interests.py --output interests_historical.db
  python build_historical_interests.py --output interests_historical.db --goveye-db goveye.db
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime

import schema as schema_module

# --- Constants ---

CSV_URL = (
    "https://pages.mysociety.org/parl_register_interests/"
    "data/all_time_register/latest/register_of_interests.csv"
)
CSV_PATH = "all_time_register.csv"

# Former PMs to include even if not current MPs
# (name_variants, display_name) — variants are names that appear in the CSV
FORMER_PMS = [
    (["Tony Blair"], "Tony Blair"),
    (["Gordon Brown"], "Gordon Brown"),
    (["David Cameron"], "David Cameron"),
    (["Theresa May"], "Theresa May"),
    (["Boris Johnson"], "Boris Johnson"),
    (["Elizabeth Truss", "Liz Truss"], "Liz Truss"),
    (["Rishi Sunak"], "Rishi Sunak"),
    # Keir Starmer is current PM and already a current MP, but listed for completeness
    (["Keir Starmer"], "Keir Starmer"),
]

# ID offset for historical entries (distinguishes from live API entries)
HISTORICAL_ID_OFFSET = 1_000_000

# Placeholder MP IDs for former PMs not in GovEye DB
# Use negative IDs to clearly distinguish from real Parliament MNIS IDs
FORMER_PM_ID_OFFSET = -10_000

# --- Category mapping ---

# mySociety category_name -> (categoryNumber, categoryName, bucket)
# Based on GovEye's BUCKET_MAPPING and the Parliament API category structure
CATEGORY_MAPPING = {
    # Employment/Earnings (category 1)
    "Employment and earnings": ("1", "Employment and earnings", "Employment/Earnings"),
    "Employment and earnings - Ad hoc payments": ("1.1", "Employment and earnings - Ad hoc payments", "Employment/Earnings"),
    "Employment and earnings - Ongoing paid employment": ("1.2", "Employment and earnings - Ongoing paid employment", "Employment/Earnings"),
    "Remunerated employment, office, profession etc": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated employment, office, profession etc.": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated employment, office, profession etc..": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated employment, office, profession etc...": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated employment, office, profession, etc": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated employment, office, profession, etc.": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated employment, office, profession, etc..": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated employment, office, profession, etc...": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated employment, office, profession et": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated Directorships": ("1", "Employment and earnings", "Employment/Earnings"),
    "Remunerated directorships": ("1", "Employment and earnings", "Employment/Earnings"),
    "Directorships": ("1", "Employment and earnings", "Employment/Earnings"),

    # Financial Support (category 2)
    "Donations and other support (including loans) for activities as an MP": ("2", "Donations and other support (including loans) for activities as an MP", "Financial Support"),
    "Sponsorship or financial or material support": ("2", "Donations and other support (including loans) for activities as an MP", "Financial Support"),
    "Sponsorships": ("2", "Donations and other support (including loans) for activities as an MP", "Financial Support"),
    "(a) Support linked to an MP but received by a local party organisation or indirectly via a central party organisation": ("2", "Donations and other support (including loans) for activities as an MP", "Financial Support"),
    "(b) Any other support not included in Category 2(a)": ("2", "Donations and other support (including loans) for activities as an MP", "Financial Support"),
    "Loans and other controlled transactions": ("2", "Donations and other support (including loans) for activities as an MP", "Financial Support"),

    # Gifts (category 3, 4, 5)
    "Gifts, benefits and hospitality (UK)": ("3", "Gifts, benefits and hospitality from UK sources", "Gifts"),
    "Gifts, benefits and hospitality (UK) ": ("3", "Gifts, benefits and hospitality from UK sources", "Gifts"),
    "Gifts, benefits and hospitality from UK sources": ("3", "Gifts, benefits and hospitality from UK sources", "Gifts"),
    "Visits outside the UK": ("4", "Visits outside the UK", "Gifts"),
    "Overseas visits": ("4", "Visits outside the UK", "Gifts"),
    "Oversea visits": ("4", "Visits outside the UK", "Gifts"),
    "Overseas visit": ("4", "Visits outside the UK", "Gifts"),
    "Overseas visits ": ("4", "Visits outside the UK", "Gifts"),
    "Visits outside the UK": ("4", "Visits outside the UK", "Gifts"),
    "Gifts and benefits from sources outside the UK": ("5", "Gifts and benefits from sources outside the UK", "Gifts"),
    "Overseas benefits and gifts": ("5", "Gifts and benefits from sources outside the UK", "Gifts"),

    # Land/Property (category 6)
    "Land and Property": ("6", "Land and property (within or outside the UK)", "Land/Property"),
    "Land and property (within or outside the UK)": ("6", "Land and property (within or outside the UK)", "Land/Property"),
    "Land and property portfolio with a value over \xa3100,000 and where indicated, the portfolio provides a rental income of over \xa310,000 a year": ("6", "Land and property (within or outside the UK)", "Land/Property"),
    "Land and property portfolio: (i) value over \xa3100,000 and/or (ii) giving rental income of over \xa310,000 a year": ("6", "Land and property (within or outside the UK)", "Land/Property"),
    "Land and property: (i) value over \xa3100,000 (ii) rental income of over \xa310,000 a year": ("6", "Land and property (within or outside the UK)", "Land/Property"),

    # Shareholdings (category 7)
    "Shareholdings": ("7", "Shareholdings", "Shareholdings"),
    "Registrable shareholdings": ("7", "Shareholdings", "Shareholdings"),
    "Registerable Shareholdings": ("7", "Shareholdings", "Shareholdings"),
    "(i) Shareholdings: over 15% of issued share capital": ("7", "Shareholdings", "Shareholdings"),
    "(ii) Other shareholdings, valued at more than \xa370,000": ("7", "Shareholdings", "Shareholdings"),

    # Miscellaneous (category 8)
    "Miscellaneous": ("8", "Miscellaneous", "Other"),
    "Miscellaneous ": ("8", "Miscellaneous", "Other"),
    "Miscellaneous and unremunerated interests": ("8", "Miscellaneous", "Other"),

    # Family (category 9, 10)
    "Family members employed": ("9", "Family members employed", "Other"),
    "Family members employed and paid from parliamentary expenses": ("9", "Family members employed", "Other"),
    "Family members engaged in lobbying the public sector on behalf of a third party or client": ("10", "Family members engaged in third-party lobbying", "Other"),
    "Family members engaged in third-party lobbying": ("10", "Family members engaged in third-party lobbying", "Other"),

    # Clients (legacy category, map to Employment)
    "Clients": ("1", "Employment and earnings", "Employment/Earnings"),
}

# Fallback bucket mapping by category name keywords
def fallback_category(cat_name):
    """Map unknown category names to a category number and bucket."""
    cl = cat_name.lower().strip()
    if "employment" in cl or "director" in cl or "client" in cl:
        return ("1", "Employment and earnings", "Employment/Earnings")
    if "donation" in cl or "support" in cl or "sponsor" in cl or "loan" in cl:
        return ("2", "Donations and other support (including loans) for activities as an MP", "Financial Support")
    if "gift" in cl or "hospitality" in cl:
        if "outside the uk" in cl or "overseas" in cl:
            return ("5", "Gifts and benefits from sources outside the UK", "Gifts")
        return ("3", "Gifts, benefits and hospitality from UK sources", "Gifts")
    if "overseas" in cl or "visit" in cl:
        return ("4", "Visits outside the UK", "Gifts")
    if "share" in cl:
        return ("7", "Shareholdings", "Shareholdings")
    if "land" in cl or "property" in cl:
        return ("6", "Land and property (within or outside the UK)", "Land/Property")
    if "misc" in cl:
        return ("8", "Miscellaneous", "Other")
    if "family" in cl:
        if "lobby" in cl:
            return ("10", "Family members engaged in third-party lobbying", "Other")
        return ("9", "Family members employed", "Other")
    # Default to Miscellaneous
    return ("8", "Miscellaneous", "Other")


def map_category(cat_name):
    """Map a mySociety category name to (categoryNumber, categoryName, bucket)."""
    # Try exact match first
    if cat_name in CATEGORY_MAPPING:
        return CATEGORY_MAPPING[cat_name]
    # Try stripping whitespace
    stripped = cat_name.strip()
    if stripped in CATEGORY_MAPPING:
        return CATEGORY_MAPPING[stripped]
    # Fallback to keyword matching
    return fallback_category(cat_name)


# --- Amount extraction ---

POUND = r'(?:\xa3|£)\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)'
RANGE = re.compile(rf'{POUND}\s*[-\u2013]\s*(?:\xa3|£)?\s?(\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?)')
UP_TO = re.compile(rf'[Uu]p to\s+{POUND}')
OVER = re.compile(rf'[Oo]ver\s+{POUND}')
EXACT = re.compile(POUND)
TOTAL_VALUE = re.compile(rf'[Tt]otal\s+value\s*(?:\xa3|£)?\s?(\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?)')
VALUE_LABEL = re.compile(rf'[Vv]alue\s*(?:of)?\s*(?:\xa3|£)?\s?(\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?)')
STRUCTURED_VALUE = re.compile(r'^Value:\s*(\d+(?:\.\d+)?)\s*$', re.MULTILINE)


def extract_amount_pence(text):
    """Extract monetary amount from free text. Returns (pence, currency) or (None, None)."""
    if not text:
        return (None, None)

    # Priority 1: Structured "Value: X" field (new 2024+ format)
    m = STRUCTURED_VALUE.search(text)
    if m:
        return (int(round(float(m.group(1)) * 100)), "GBP")

    # Priority 2: Total value
    m = TOTAL_VALUE.search(text)
    if m:
        return (int(round(float(m.group(1).replace(",", "")) * 100)), "GBP")

    # Priority 3: Range -> upper bound
    m = RANGE.search(text)
    if m:
        return (int(round(float(m.group(2).replace(",", "")) * 100)), "GBP")

    # Priority 4: "Up to £X"
    m = UP_TO.search(text)
    if m:
        return (int(round(float(m.group(1).replace(",", "")) * 100)), "GBP")

    # Priority 5: "Over £X"
    m = OVER.search(text)
    if m:
        return (int(round(float(m.group(1).replace(",", "")) * 100)), "GBP")

    # Priority 6: "value £X"
    m = VALUE_LABEL.search(text)
    if m:
        return (int(round(float(m.group(1).replace(",", "")) * 100)), "GBP")

    # Priority 7: Any £ amount
    m = EXACT.search(text)
    if m:
        return (int(round(float(m.group(1).replace(",", "")) * 100)), "GBP")

    return (None, None)


# --- Date extraction ---

REGISTERED_DATE = re.compile(r'\(Registered\s+(\d{1,2}\s+\w+\s+\d{4})\)', re.IGNORECASE)
DATE_ISO = re.compile(r'(\d{4}-\d{2}-\d{2})')

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_date_text(date_str):
    """Parse '23 December 2003' to '2003-12-23'. Returns ISO string or None."""
    if not date_str:
        return None
    date_str = date_str.strip()
    # Already ISO?
    if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return date_str
    # "DD Month YYYY"
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', date_str)
    if m:
        day = int(m.group(1))
        month = MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        if month:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def extract_date(text, earliest_decl, latest_decl):
    """Extract the best date for this interest. Returns ISO date string or None."""
    # Prefer latest_declaration from the dataset
    if latest_decl and latest_decl.strip():
        iso = parse_date_text(latest_decl.strip()[:10])
        if iso:
            return iso
    if earliest_decl and earliest_decl.strip():
        iso = parse_date_text(earliest_decl.strip()[:10])
        if iso:
            return iso
    # Fall back to "Registered DD Month YYYY" in text
    m = REGISTERED_DATE.search(text or "")
    if m:
        iso = parse_date_text(m.group(1))
        if iso:
            return iso
    # Fall back to ISO date in text
    m = DATE_ISO.search(text or "")
    if m:
        return m.group(1)
    return None


# --- Name matching ---

def normalize_name(name):
    """Normalize a name for matching."""
    if not name:
        return []
    variants = []
    clean = name
    for prefix in ["Ms ", "Mr ", "Mrs ", "Miss ", "Dr ", "Rt Hon ", "Hon ", "Sir ", "Dame "]:
        clean = clean.replace(prefix, "")
    clean = clean.strip()
    # "Abbott, Ms Diane" -> "Abbott Diane" -> "Diane Abbott"
    if ", " in clean:
        last, first = clean.split(", ", 1)
        clean = f"{first} {last}"
    clean = clean.lower().strip()
    variants.append(clean)
    # Also try without middle names
    parts = clean.split()
    if len(parts) >= 3:
        variants.append(f"{parts[0]} {parts[-1]}")
    return variants


def build_name_index(db_path):
    """Build name -> MP id index from GovEye's mps table."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, nameListAs, nameDisplayAs FROM mps")
    index = {}
    for mp_id, list_as, display_as in cur.fetchall():
        for name in [list_as, display_as]:
            for variant in normalize_name(name):
                if variant not in index:
                    index[variant] = mp_id
    conn.close()
    return index


def match_member(member_name, name_index):
    """Match a mySociety member_name to a GovEye MP id."""
    if not member_name:
        return None
    clean = member_name.strip().lower()
    # Direct match
    if clean in name_index:
        return name_index[clean]
    # Try without titles
    for prefix in ["ms ", "mr ", "mrs ", "miss ", "dr ", "rt hon ", "hon ", "sir ", "dame "]:
        if clean.startswith(prefix):
            clean2 = clean[len(prefix):]
            if clean2 in name_index:
                return name_index[clean2]
    # Try first + last name only
    parts = clean.split()
    if len(parts) >= 3:
        fl = f"{parts[0]} {parts[-1]}"
        if fl in name_index:
            return name_index[fl]
    return None


# --- Former PM handling ---

def build_former_pm_index():
    """Build name -> former PM id index for PMs not in GovEye DB."""
    index = {}
    pm_ids = {}
    for i, (variants, display_name) in enumerate(FORMER_PMS):
        pm_id = FORMER_PM_ID_OFFSET - i
        pm_ids[pm_id] = display_name
        for v in variants:
            index[v.lower().strip()] = pm_id
    return index, pm_ids


def match_former_pm(member_name, former_pm_index):
    """Match a member name to a former PM id."""
    if not member_name:
        return None
    clean = member_name.strip().lower()
    return former_pm_index.get(clean)


# --- Main build ---

def download_csv(path):
    """Download the mySociety CSV if not already cached."""
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        print(f"Using cached CSV: {path} ({os.path.getsize(path)} bytes)")
        return
    print(f"Downloading CSV from {CSV_URL}...")
    import urllib.request
    urllib.request.urlretrieve(CSV_URL, path)
    print(f"Downloaded: {path} ({os.path.getsize(path)} bytes)")


def build_historical(output_path, goveye_db, csv_path, schema_path="schemas/bundled_schema.json"):
    """Build the historical interests database."""
    # Download CSV if needed
    download_csv(csv_path)

    # Build name indexes
    print("Building name index from GovEye DB...")
    name_index = build_name_index(goveye_db)
    print(f"  Current MP name index: {len(name_index)} entries")

    former_pm_index, former_pm_ids = build_former_pm_index()
    print(f"  Former PM index: {len(former_pm_index)} entries")

    # Determine which MPs to include
    # 1. All current MPs that match (626)
    # 2. All former PMs (by name)
    # A current MP who is also a PM (e.g. Starmer) uses their real MP ID

    # Stats
    stats = {
        "total_csv_rows": 0,
        "matched_current_mp": 0,
        "matched_former_pm": 0,
        "unmatched_skipped": 0,
        "inserted": 0,
        "with_amount": 0,
        "without_amount": 0,
    }

    # Prepare output DB — copy schema from goveye_db
    print(f"Creating output DB: {output_path}")
    if os.path.exists(output_path):
        os.remove(output_path)

    # Copy the goveye_db to get the mps table and schema, then clear interests
    import shutil
    shutil.copy2(goveye_db, output_path)
    conn = sqlite3.connect(output_path)
    cur = conn.cursor()

    # Ensure the interests table exists (mps.db only has the mps table)
    schema_module.ensure_schema(conn, schema_path, ["interests"])

    # Clear existing interests (we'll insert only historical ones)
    cur.execute("DELETE FROM interests")
    conn.commit()

    timestamp_millis = int(time.time() * 1000)

    # Insert placeholder MP records for former PMs not in the DB
    for pm_id, display_name in former_pm_ids.items():
        # Check if this PM is already a current MP (e.g. Starmer)
        cur.execute("SELECT id FROM mps WHERE id = ?", (pm_id,))
        if not cur.fetchone():
            # Insert placeholder with all NOT NULL columns satisfied
            cur.execute(
                "INSERT OR IGNORE INTO mps "
                "(id, nameListAs, nameDisplayAs, partyId, partyName, partyAbbreviation, "
                "partyBackgroundColour, partyForegroundColour, constituencyId, constituencyName, "
                "house, isActive, lastUpdated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pm_id, display_name, display_name, 0, "Former PM", "Former PM",
                 "#666666", "#FFFFFF", 0, "Former PM", 1, 0, timestamp_millis),
            )
    conn.commit()

    # Process CSV
    print("Processing CSV...")
    batch = []
    batch_size = 1000
    historical_id = HISTORICAL_ID_OFFSET

    # Track which former PMs we found data for
    found_former_pms = set()
    # Track matched current MP IDs
    matched_current_mp_ids = set()

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total_csv_rows"] += 1
            member_name = row.get("member_name", "").strip()
            ft = row.get("free_text", "").strip()

            # Skip empty entries
            if not ft or ft in ("Nil.", " Nil.", " Nil", "Nil"):
                continue

            # Try matching to current MP first
            mp_id = match_member(member_name, name_index)
            member_type = "current_mp"

            # If not a current MP, try former PM
            if mp_id is None:
                pm_id = match_former_pm(member_name, former_pm_index)
                if pm_id is not None:
                    mp_id = pm_id
                    member_type = "former_pm"
                    found_former_pms.add(pm_id)
                else:
                    stats["unmatched_skipped"] += 1
                    continue

            if member_type == "current_mp":
                stats["matched_current_mp"] += 1
                matched_current_mp_ids.add(mp_id)
            else:
                stats["matched_former_pm"] += 1

            # Map category
            cat_name = row.get("category_name", "").strip()
            cat_number, cat_name_mapped, bucket = map_category(cat_name)

            # Extract amount
            pence, currency = extract_amount_pence(ft)
            if pence is not None:
                stats["with_amount"] += 1
            else:
                stats["without_amount"] += 1

            # Extract date
            date = extract_date(ft, row.get("earliest_declaration"), row.get("latest_declaration"))
            registration_date = date
            published_date = date

            # Build the entry
            # Use the free_text as the summary, truncated to 2000 chars
            summary = ft[:2000]

            # Build fieldsJson — store the original mySociety columns as JSON
            import json
            fields = {
                "source": "mysociety_all_time_register",
                "public_whip_id": row.get("public_whip_id", ""),
                "earliest_declaration": row.get("earliest_declaration", ""),
                "latest_declaration": row.get("latest_declaration", ""),
                "extracted_orgs": row.get("extracted_orgs", ""),
                "extracted_sum": row.get("extracted_sum", ""),
            }
            fields_json = json.dumps(fields)

            # Use a per-member, per-date hash for the ID to avoid duplicates
            # ID = offset + sequential counter
            historical_id += 1

            batch.append((
                historical_id,
                mp_id,
                summary,
                0,  # categoryId (not used by GovEye, uses categoryNumber)
                cat_number,
                cat_name_mapped,
                registration_date,
                published_date,
                0,  # rectified
                fields_json,
                timestamp_millis,
                pence,
                currency,
                bucket,
            ))

            if len(batch) >= batch_size:
                cur.executemany(
                    "INSERT OR REPLACE INTO interests "
                    "(id, memberId, summary, categoryId, categoryNumber, categoryName, "
                    "registrationDate, publishedDate, rectified, fieldsJson, lastUpdated, "
                    "parsedAmountPence, currencyCode, bucket) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
                conn.commit()
                stats["inserted"] += len(batch)
                batch = []
                print(f"  Inserted {stats['inserted']} rows...")

    # Insert remaining batch
    if batch:
        cur.executemany(
            "INSERT OR REPLACE INTO interests "
            "(id, memberId, summary, categoryId, categoryNumber, categoryName, "
            "registrationDate, publishedDate, rectified, fieldsJson, lastUpdated, "
            "parsedAmountPence, currencyCode, bucket) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        conn.commit()
        stats["inserted"] += len(batch)

    # Report
    print("\n" + "=" * 60)
    print("HISTORICAL INTERESTS BUILD COMPLETE")
    print("=" * 60)
    print(f"Total CSV rows:          {stats['total_csv_rows']}")
    print(f"Matched current MPs:     {stats['matched_current_mp']} ({len(matched_current_mp_ids)} unique)")
    print(f"Matched former PMs:      {stats['matched_former_pm']} ({len(found_former_pms)} unique)")
    print(f"Unmatched (skipped):     {stats['unmatched_skipped']}")
    print(f"Inserted:                {stats['inserted']}")
    print(f"  With amount:           {stats['with_amount']} ({100*stats['with_amount']/max(stats['inserted'],1):.1f}%)")
    print(f"  Without amount:        {stats['without_amount']}")

    # Per-bucket stats
    cur.execute("SELECT bucket, COUNT(*), SUM(CASE WHEN parsedAmountPence IS NOT NULL THEN 1 ELSE 0 END) FROM interests GROUP BY bucket ORDER BY COUNT(*) DESC")
    print("\nPer-bucket:")
    for bucket, total, with_amt in cur.fetchall():
        print(f"  {bucket}: {total} rows ({with_amt} with amount, {100*with_amt/max(total,1):.1f}%)")

    # Per-year stats
    cur.execute("SELECT substr(registrationDate,1,4) as year, COUNT(*) FROM interests WHERE year IS NOT NULL GROUP BY year ORDER BY year")
    print("\nPer-year:")
    for year, count in cur.fetchall():
        print(f"  {year}: {count}")

    # Former PMs found
    print(f"\nFormer PMs with data: {len(found_former_pms)}/{len(former_pm_ids)}")
    for pm_id in sorted(found_former_pms):
        cur.execute("SELECT COUNT(*) FROM interests WHERE memberId = ?", (pm_id,))
        count = cur.fetchone()[0]
        print(f"  {former_pm_ids[pm_id]} (id={pm_id}): {count} rows")

    conn.close()
    print(f"\nOutput: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Build historical interests from mySociety data")
    parser.add_argument("--output", default="interests_historical.db", help="Output DB path")
    parser.add_argument("--goveye-db", default="goveye.db", help="GovEye DB path (for MP names)")
    parser.add_argument("--csv", default=CSV_PATH, help="Path to mySociety CSV (cached)")
    parser.add_argument("--schema", default="schemas/bundled_schema.json", help="Path to Room schema JSON")
    args = parser.parse_args()

    build_historical(args.output, args.goveye_db, args.csv, args.schema)


if __name__ == "__main__":
    main()
