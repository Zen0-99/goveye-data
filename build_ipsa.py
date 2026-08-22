#!/usr/bin/env python3
"""Per-API build script for IPSA expenses (Phase 11, plan 11-02).

Downloads IPSA individual business costs CSV from theipsa.org.uk API,
parses and buckets into high-level categories (D-09), matches MPs via
Parliamentary ID (with name matching fallback), and builds a per-API
DB (expenses.db) with only the expenses table + the full schema's
Room identity hash.

IPSA publishes claims every 2 months. Categories are bucketed:
Staffing, Office, Travel, Other (D-09).

Modes:
  seed  — create fresh DB, download CSV, parse, insert
  delta — copy previous DB, download latest CSV, upsert

Usage:
  python build_ipsa.py --output expenses.db --schema schemas/bundled_schema.json --mode seed --mps-db mps.db
  python build_ipsa.py --output expenses.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_expenses.db --mps-db mps.db
"""

import argparse
import csv
import io
import os
import re
import shutil
import sqlite3
import time

import requests

import schema as schema_module
from api_helper import api_get, BATCH_SIZE, logger

# --- Constants ---

IPSA_API_BASE = "https://www.theipsa.org.uk/api/download"
# Download the two most recent financial years to get the latest data
IPSA_YEARS = ["25_26", "24_25"]
TABLE_NAMES = ["expenses"]

# Bucket mapping (D-09): raw IPSA categories → high-level buckets
IPSA_BUCKET_MAPPING = {
    "Staffing": "Staffing",
    "Office Costs": "Office",
    "Accommodation/Travel": "Travel",
    "Stationery": "Office",
    "Communications": "Office",
    "Equipment": "Office",
    "Staffing Travel": "Travel",
    "Family Travel": "Travel",
    "Travel": "Travel",
    "Accommodation": "Travel",
    "Food": "Travel",
    "Utilities": "Office",
    "Publications": "Office",
    "IT Equipment": "Office",
    "Office Equipment": "Office",
    "Constituency Office": "Office",
    "Westminster Office": "Office",
    "Salary": "Staffing",
    "Payroll": "Staffing",
    "Staff": "Staffing",
}


def get_bucket(category):
    """Map an IPSA category to a high-level bucket label.

    Falls back to partial matching (case-insensitive substring), then "Other".
    """
    if not category:
        return "Other"

    # Direct lookup
    if category in IPSA_BUCKET_MAPPING:
        return IPSA_BUCKET_MAPPING[category]

    # Case-insensitive lookup
    for key, bucket in IPSA_BUCKET_MAPPING.items():
        if category.lower() == key.lower():
            return bucket

    # Partial matching: check if any key is a substring of the category
    cat_lower = category.lower()
    for key, bucket in IPSA_BUCKET_MAPPING.items():
        if key.lower() in cat_lower:
            return bucket

    return "Other"


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
    """Build a name → memberId lookup from the MPs DB.

    Matches on nameDisplayAs and nameListAs with honorific stripping.
    Returns a dict mapping lowercase name → memberId.
    """
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


def match_mp_name(name, lookup):
    """Match an IPSA MP name to a Parliament member ID.

    Returns 0 if no match found.
    """
    if not name:
        return 0

    stripped = _strip_honorifics(name).lower().strip()
    if stripped in lookup:
        return lookup[stripped]

    # Try without middle initials (e.g. "John P Smith" → "John Smith")
    words = stripped.split()
    if len(words) > 2:
        # Remove single-letter tokens (likely middle initials)
        filtered = [w for w in words if len(w) > 1]
        if len(filtered) >= 2:
            reduced = " ".join(filtered)
            if reduced in lookup:
                return lookup[reduced]

    return 0


# --- IPSA CSV download ---

def download_ipsa_csv():
    """Download IPSA individual business costs CSV for the latest financial years.

    IPSA moved to a new API at theipsa.org.uk with direct CSV download:
      /api/download?type=individualBusinessCosts&year=25_26

    Returns the combined CSV content as a string, or None if download fails.
    """
    all_csv_parts = []

    for year in IPSA_YEARS:
        url = f"{IPSA_API_BASE}?type=individualBusinessCosts&year={year}"
        logger.info("Downloading IPSA CSV for year %s...", year)

        try:
            r = api_get(url, timeout=120)
            if r.text and len(r.text) > 100:
                text = r.text.lstrip('\ufeff')
                all_csv_parts.append((year, text))
                logger.info("Downloaded IPSA CSV for %s (%d bytes)", year, len(text))
            else:
                logger.warning("Empty response for IPSA year %s", year)
        except Exception as e:
            logger.error("Failed to download IPSA CSV for year %s: %s", year, e)

    if not all_csv_parts:
        return None

    if len(all_csv_parts) == 1:
        return all_csv_parts[0][1]

    # Combine CSVs: keep first header, skip subsequent headers
    combined_lines = []
    for i, (year, text) in enumerate(all_csv_parts):
        lines = text.split('\n')
        if i == 0:
            combined_lines.extend(lines)
        else:
            combined_lines.extend(lines[1:])
    return '\n'.join(combined_lines)


def _extract_hidden_field(html, field_name):
    """Extract a hidden input field value from HTML. (Legacy — no longer used.)"""
    pattern = f'name="{field_name}"\\s+id="{field_name}"\\s+value="([^"]*)"'
    match = re.search(pattern, html)
    if match:
        return match.group(1)
    pattern2 = f'id="{field_name}"\\s+name="{field_name}"\\s+value="([^"]*)"'
    match2 = re.search(pattern2, html)
    if match2:
        return match2.group(1)
    return None


# --- CSV parsing ---

def parse_ipsa_csv(csv_text, name_lookup):
    """Parse IPSA CSV text and return a list of expense row dicts.

    The new IPSA API CSV includes a 'Parliamentary ID' column that maps
    directly to Parliament member IDs, so name matching is only a fallback.

    Each row: {mpId, category, bucket, amountPence, claimDate, status}
    """
    if not csv_text:
        return []

    rows = []
    text = csv_text.lstrip('\ufeff')
    reader = csv.DictReader(io.StringIO(text))

    headers = reader.fieldnames or []
    logger.info("IPSA CSV headers: %s", headers)

    # New IPSA API columns:
    # Parliamentary ID, Year, Date, Claim Number, Name, Constituency,
    # Category, Cost Type, Short Description, Details, Journey Type,
    # From, To, Travel, Nights, Mileage, Amount Claimed, Amount Paid,
    # Amount Not Paid, Amount Repaid, Status, Reason If Not Paid,
    # Supply Month, Supply Period

    id_col = _find_column(headers, ["Parliamentary ID", "ParliamentaryID"])
    name_col = _find_column(headers, ["Name", "MP's Name", "MP Name", "Member"])
    category_col = _find_column(headers, ["Category", "Category Type", "Expense Type", "Type"])
    amount_col = _find_column(headers, ["Amount Claimed", "Amount", "Amount Paid", "Total", "Value"])
    date_col = _find_column(headers, ["Date", "Claim Date", "Payment Date", "Date of Claim"])
    status_col = _find_column(headers, ["Status", "Claim Status", "Approval Status"])

    if not category_col or not amount_col:
        logger.error(
            "Could not find required columns in IPSA CSV. "
            "Category: %s, Amount: %s",
            category_col, amount_col,
        )
        return []

    matched = 0
    unmatched = 0

    for row in reader:
        category = row.get(category_col, "").strip()
        amount_str = row.get(amount_col, "0").strip()
        claim_date = row.get(date_col, "").strip() if date_col else ""
        status = row.get(status_col, "").strip() if status_col else ""

        amount_pence = _parse_amount(amount_str)
        if amount_pence is None:
            continue

        # Match MP: prefer Parliamentary ID, fall back to name matching
        mp_id = 0
        if id_col:
            id_str = row.get(id_col, "").strip()
            if id_str:
                try:
                    mp_id = int(id_str)
                except ValueError:
                    pass

        if mp_id == 0 and name_col:
            name = row.get(name_col, "").strip()
            mp_id = match_mp_name(name, name_lookup)

        if mp_id == 0:
            unmatched += 1
            if unmatched <= 10:
                name = row.get(name_col, "").strip() if name_col else "?"
                logger.warning("Could not match IPSA row: name='%s', id='%s'",
                               name, row.get(id_col, "") if id_col else "")
            continue

        matched += 1
        bucket = get_bucket(category)

        rows.append({
            "mpId": mp_id,
            "category": category,
            "bucket": bucket,
            "amountPence": amount_pence,
            "claimDate": claim_date or None,
            "status": status or None,
        })

    logger.info(
        "Parsed %d expense rows (%d matched, %d unmatched)",
        len(rows), matched, unmatched,
    )
    return rows


def _find_column(headers, candidates):
    """Find the first matching column name from a list of candidates."""
    for candidate in candidates:
        for h in headers:
            if h.strip().lower() == candidate.lower():
                return h
    # Partial match
    for candidate in candidates:
        for h in headers:
            if candidate.lower() in h.strip().lower():
                return h
    return None


def _parse_amount(amount_str):
    """Parse a monetary amount string to integer pence.

    Handles: "£1,234.56", "1234.56", "1,234", "£1234"
    Returns None if unparseable.
    """
    if not amount_str:
        return None

    # Remove currency symbols, commas, spaces
    cleaned = re.sub(r'[£,\s]', '', amount_str)

    # Handle negative amounts (repayments)
    negative = cleaned.startswith('-')
    if negative:
        cleaned = cleaned[1:]

    try:
        amount = float(cleaned)
        pence = int(round(amount * 100))
        return -pence if negative else pence
    except ValueError:
        return None


# --- DB operations ---

def build_expenses_table(conn):
    """Create the expenses table if it doesn't exist."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mpId INTEGER,
            category TEXT,
            bucket TEXT,
            amountPence INTEGER,
            claimDate TEXT,
            status TEXT,
            lastUpdated INTEGER
        )
    """)
    conn.commit()


def insert_expenses(conn, rows, timestamp_millis):
    """Batch insert expense rows using INSERT OR REPLACE."""
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO expenses (
            id, mpId, category, bucket, amountPence, claimDate, status, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    tuples = [
        (
            None,  # autoGenerate
            row["mpId"],
            row["category"],
            row["bucket"],
            row["amountPence"],
            row.get("claimDate"),
            row.get("status"),
            timestamp_millis,
        )
        for row in rows
    ]

    for i in range(0, len(tuples), BATCH_SIZE):
        batch = tuples[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted expenses: %d/%d", min(i + BATCH_SIZE, len(tuples)), len(tuples))


# --- Build modes ---

def build_seed(output_path, schema_path, mps_db, checkpoint_db=None):
    """Seed mode: create fresh DB, download CSV, parse, insert."""
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        build_expenses_table(conn)
        logger.info("Resuming from checkpoint")
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        build_expenses_table(conn)

    name_lookup = build_name_lookup(mps_db)
    csv_text = download_ipsa_csv()
    expenses = parse_ipsa_csv(csv_text, name_lookup)

    if expenses:
        insert_expenses(conn, expenses, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mps_db):
    """Delta mode: copy previous DB, download latest CSV, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)
    build_expenses_table(conn)

    name_lookup = build_name_lookup(mps_db)
    csv_text = download_ipsa_csv()
    expenses = parse_ipsa_csv(csv_text, name_lookup)

    if expenses:
        # Clear old data and re-insert (expenses are republished in full each cycle)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM expenses")
        conn.commit()
        insert_expenses(conn, expenses, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye IPSA expenses per-API SQLite database (expenses.db)."
    )
    parser.add_argument(
        "--output", default="expenses.db",
        help="Output path for the SQLite DB file. Default: expenses.db.",
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
        help="Path to the mps.db file for MP name matching.",
    )
    parser.add_argument(
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only).",
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
