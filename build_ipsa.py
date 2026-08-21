#!/usr/bin/env python3
"""Per-API build script for IPSA expenses (Phase 11, plan 11-02).

Downloads IPSA expense claims CSV from parliamentary-standards.org.uk,
parses and buckets into high-level categories (D-09), matches MP names
to Parliament member IDs, and builds a per-API DB (expenses.db) with
only the expenses table + the full schema's Room identity hash.

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

IPSA_DATA_URL = "https://parliamentary-standards.org.uk/DataDownloads.aspx"
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
    """Download the latest IPSA expense claims CSV.

    The IPSA website uses ASP.NET postbacks. We simulate the postback
    to trigger the CSV download for the latest publication cycle.

    Returns the CSV content as a string, or None if download fails.
    """
    logger.info("Fetching IPSA DataDownloads page for postback parameters...")

    try:
        # Step 1: GET the page to extract __VIEWSTATE and __EVENTVALIDATION
        r = api_get(IPSA_DATA_URL, timeout=60)
        html = r.text

        # Extract hidden fields
        viewstate = _extract_hidden_field(html, "__VIEWSTATE")
        viewstategenerator = _extract_hidden_field(html, "__VIEWSTATEGENERATOR")
        eventvalidation = _extract_hidden_field(html, "__EVENTVALIDATION")

        if not viewstate:
            logger.error("Failed to extract __VIEWSTATE from IPSA page")
            return None

        # Step 2: POST to trigger the "Latest data" download
        logger.info("Triggering IPSA CSV download via postback...")
        post_data = {
            "__EVENTTARGET": "ctl00$cphMainContentsArea$lnkBtnLatest",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": viewstategenerator or "",
            "__EVENTVALIDATION": eventvalidation or "",
        }

        r = requests.post(
            IPSA_DATA_URL,
            data=post_data,
            timeout=120,
            headers={"Referer": IPSA_DATA_URL},
        )
        r.raise_for_status()
        r.encoding = "utf-8"

        # Check if the response is CSV (starts with a header row)
        content_type = r.headers.get("Content-Type", "")
        if "text/csv" in content_type or "application/csv" in content_type:
            logger.info("Downloaded IPSA CSV (%d bytes)", len(r.text))
            return r.text

        # Some servers return CSV as application/octet-stream or text/plain
        if r.text.strip().startswith('"') or "," in r.text.split("\n")[0]:
            logger.info("Downloaded IPSA CSV (%d bytes, content-type: %s)", len(r.text), content_type)
            return r.text

        logger.error("IPSA download did not return CSV (content-type: %s)", content_type)
        logger.debug("First 500 chars: %s", r.text[:500])
        return None

    except Exception as e:
        logger.error("Failed to download IPSA CSV: %s", e)
        return None


def _extract_hidden_field(html, field_name):
    """Extract a hidden input field value from HTML."""
    pattern = f'name="{field_name}"\\s+id="{field_name}"\\s+value="([^"]*)"'
    match = re.search(pattern, html)
    if match:
        return match.group(1)
    # Try alternate attribute order
    pattern2 = f'id="{field_name}"\\s+name="{field_name}"\\s+value="([^"]*)"'
    match2 = re.search(pattern2, html)
    if match2:
        return match2.group(1)
    return None


# --- CSV parsing ---

def parse_ipsa_csv(csv_text, name_lookup):
    """Parse IPSA CSV text and return a list of expense row dicts.

    Each row: {mpId, category, bucket, amountPence, claimDate, status}
    """
    if not csv_text:
        return []

    rows = []
    reader = csv.DictReader(io.StringIO(csv_text))

    # Log the header for debugging
    headers = reader.fieldnames or []
    logger.info("IPSA CSV headers: %s", headers)

    # IPSA CSV columns (may vary — try common names):
    # - MP name: "MP's Name" or "Name"
    # - Category: "Category" or "Category Type"
    # - Amount: "Amount Claimed" or "Amount" or "Amount Paid"
    # - Date: "Claim Date" or "Date" or "Payment Date"
    # - Status: "Status" or "Claim Status"

    name_col = _find_column(headers, ["MP's Name", "MP Name", "Name", "Member"])
    category_col = _find_column(headers, ["Category", "Category Type", "Expense Type", "Type"])
    amount_col = _find_column(headers, ["Amount Claimed", "Amount", "Amount Paid", "Total", "Value"])
    date_col = _find_column(headers, ["Claim Date", "Date", "Payment Date", "Date of Claim"])
    status_col = _find_column(headers, ["Status", "Claim Status", "Approval Status"])

    if not name_col or not category_col or not amount_col:
        logger.error(
            "Could not find required columns in IPSA CSV. "
            "Name: %s, Category: %s, Amount: %s",
            name_col, category_col, amount_col,
        )
        return []

    matched = 0
    unmatched = 0

    for row in reader:
        name = row.get(name_col, "").strip()
        category = row.get(category_col, "").strip()
        amount_str = row.get(amount_col, "0").strip()
        claim_date = row.get(date_col, "").strip() if date_col else ""
        status = row.get(status_col, "").strip() if status_col else ""

        # Parse amount to pence
        amount_pence = _parse_amount(amount_str)
        if amount_pence is None:
            continue

        # Match MP name to Parliament member ID
        mp_id = match_mp_name(name, name_lookup)
        if mp_id == 0:
            unmatched += 1
            if unmatched <= 10:  # Log first 10 unmatched for debugging
                logger.warning("Could not match IPSA name: '%s'", name)
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
