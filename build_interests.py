#!/usr/bin/env python3
"""Per-API build script for interests (Phase 9).

Fetches all financial interests for all current Commons MPs from the
Interests API and builds a per-API DB (interests.db) with only the
interests table + the full schema's Room identity hash.

Includes a two-tier monetary parser (D-01) that extracts amounts from
fieldsJson: Tier 1 checks structured field typeInfo.currencyCode +
field.value; Tier 2 falls back to regex on free text. Parsed amounts
stored as integer pence. Never fabricates — returns None for unparseable.

Modes:
  seed  — create fresh DB, fetch all interests, insert
  delta — copy previous DB, fetch all interests, upsert (interests can
          be amended/rectified — INSERT OR REPLACE)

Usage:
  python build_interests.py --output interests.db --schema schemas/bundled_schema.json --mode seed
  python build_interests.py --output interests.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_interests.db
"""

import argparse
import json
import re
import shutil
import sqlite3
import time

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

INTERESTS_BASE = "https://interests-api.parliament.uk/api/v1/"
MEMBERS_BASE = "https://members-api.parliament.uk/api/"
PAGE_SIZE_INTERESTS = 20
PAGE_SIZE_MEMBERS = 20

TABLE_NAMES = ["interests"]

# --- Bucket mapping (D-04, D-05) ---

# Maps API category number (string) to high-level bucket label.
# Based on TheyWorkForYou's confirmed 10-category structure.
BUCKET_MAPPING = {
    "1": "Employment/Earnings",
    "1.1": "Employment/Earnings",
    "1.2": "Employment/Earnings",
    "2": "Financial Support",
    "3": "Gifts",
    "4": "Gifts",
    "5": "Gifts",
    "6": "Land/Property",
    "7": "Shareholdings",
    "8": "Other",
    "9": "Other",
    "10": "Other",
}


def get_bucket(category_number):
    """Map an API category number to a high-level bucket label.

    Falls back to the parent category (e.g. '1.1' -> '1' -> 'Employment/Earnings').
    Returns None if no mapping found.
    """
    if category_number in BUCKET_MAPPING:
        return BUCKET_MAPPING[category_number]
    # Try parent category (e.g. "1.1" -> "1")
    parent = category_number.split(".")[0]
    return BUCKET_MAPPING.get(parent)


# --- Monetary parser (D-01, D-02, D-03) ---

# Core: £ followed by number with optional thousands separators
POUND_PATTERN = re.compile(
    r'£\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*'
    r'(million|billion|thousand|m|bn|k)?',
    re.IGNORECASE,
)

# Range: £X to £Y, £X–£Y, £X-£Y -> take the higher value
RANGE_PATTERN = re.compile(
    r'£\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:to|[-–])\s*'
    r'£?\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)',
    re.IGNORECASE,
)

# Multipliers for suffixes
MULTIPLIERS = {
    "million": 1_000_000,
    "m": 1_000_000,
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "thousand": 1_000,
    "k": 1_000,
}


def _apply_multiplier(amount, suffix):
    """Apply a multiplier suffix (million, bn, k, etc.) to an amount."""
    if suffix:
        suffix_lower = suffix.lower()
        if suffix_lower in MULTIPLIERS:
            return amount * MULTIPLIERS[suffix_lower]
    return amount


def _regex_extract(text):
    """Extract a monetary amount from free text using regex.

    Returns (pence, currency_code) or (None, None).
    Currency defaults to "GBP" if a £ amount is found.
    """
    if not text or not isinstance(text, str):
        return (None, None)

    # Try range pattern first — take the higher value (conservative)
    range_match = RANGE_PATTERN.search(text)
    if range_match:
        val1 = float(range_match.group(1).replace(",", ""))
        val2 = float(range_match.group(2).replace(",", ""))
        amount = max(val1, val2)
        return (int(round(amount * 100)), "GBP")

    # Check for negative (only for single amounts, not ranges)
    is_negative = bool(re.search(r'(?:^|\s)-\s*£', text))

    # Try single amount pattern
    pound_match = POUND_PATTERN.search(text)
    if pound_match:
        amount_str = pound_match.group(1).replace(",", "")
        amount = float(amount_str)
        suffix = pound_match.group(2)
        amount = _apply_multiplier(amount, suffix)
        if is_negative:
            amount = -amount
        return (int(round(amount * 100)), "GBP")

    return (None, None)


def parse_amount(fields_json):
    """Two-tier monetary parser (D-01).

    Deserializes fields_json (a JSON string of List<InterestFieldDto>) and
    extracts a monetary amount.

    Tier 1 — Structured fields:
        Check field.typeInfo.currencyCode + field.value for structured data.

    Tier 2 — Regex on free text:
        Fall back to regex on field value strings.

    Returns:
        (parsed_amount_pence, currency_code) or (None, None).
        Amount is integer pence (e.g. £5,000 -> 500000).
        Never fabricates — returns None for unparseable fields.
    """
    if not fields_json:
        return (None, None)

    try:
        fields = json.loads(fields_json)
    except (json.JSONDecodeError, TypeError):
        return (None, None)

    if not isinstance(fields, list):
        return (None, None)

    for field in fields:
        if not isinstance(field, dict):
            continue

        type_info = field.get("typeInfo") or {}
        currency_code = type_info.get("currencyCode")
        value = field.get("value")
        field_name = (field.get("name") or "").lower()

        # Tier 1: Structured field with currency code
        if currency_code and value is not None:
            if isinstance(value, (int, float)):
                pence = int(round(float(value) * 100))
                return (pence, currency_code)
            elif isinstance(value, str):
                # Value is a string — try regex on it
                pence, _ = _regex_extract(value)
                if pence is not None:
                    return (pence, currency_code)

        # Tier 2: Regex on string values for fields that look monetary
        if isinstance(value, str) and value:
            # Check if field name suggests monetary content
            monetary_hints = ("amount", "value", "rate", "payment", "sum",
                              "fee", "salary", "income", "donation", "gift")
            if any(hint in field_name for hint in monetary_hints):
                pence, found_currency = _regex_extract(value)
                if pence is not None:
                    currency = currency_code or found_currency
                    return (pence, currency)

        # Also try regex on any string value (catch-all for free text)
        if isinstance(value, str) and "£" in value:
            pence, found_currency = _regex_extract(value)
            if pence is not None:
                currency = currency_code or found_currency
                return (pence, currency)

    return (None, None)


# --- Members API (need MP IDs to query interests per MP) ---

def fetch_all_mp_ids(mp_limit=None):
    """Fetch all current Commons MP IDs from the Members API.

    Returns a list of MP ID integers.
    """
    mp_ids = []
    skip = 0

    while True:
        params = {
            "House": 1,
            "IsCurrentMember": "true",
            "itemsPerPage": PAGE_SIZE_MEMBERS,
            "skip": skip,
        }
        logger.info("Fetching MPs for interests: skip=%d", skip)
        r = api_get(
            f"{MEMBERS_BASE}Members/Search",
            params=params,
            timeout=60,
        )
        data = r.json()
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            mp_id = item.get("value", item).get("id")
            if mp_id:
                mp_ids.append(mp_id)

        if mp_limit is not None and len(mp_ids) >= mp_limit:
            mp_ids = mp_ids[:mp_limit]
            break

        if len(items) < PAGE_SIZE_MEMBERS:
            break

        skip += PAGE_SIZE_MEMBERS
        time.sleep(API_DELAY)

    logger.info("Fetched %d MP IDs", len(mp_ids))
    return mp_ids


# --- Interests API ---

def fetch_interests_for_mp(member_id):
    """Fetch all interests for a single MP from the Interests API.

    The API returns {"items": [...], "totalResults": N}. Paginated 20/page.
    """
    interests = []
    skip = 0

    while True:
        params = {
            "MemberId": member_id,
            "Take": PAGE_SIZE_INTERESTS,
            "Skip": skip,
            "SortOrder": "PublishingDateDescending",
        }
        logger.info("Fetching interests for MP %d: skip=%d", member_id, skip)
        r = api_get(
            f"{INTERESTS_BASE}Interests",
            params=params,
            timeout=60,
        )
        data = r.json()
        items = data.get("items", [])
        if not items:
            break

        interests.extend(items)

        if len(items) < PAGE_SIZE_INTERESTS:
            break

        skip += PAGE_SIZE_INTERESTS
        time.sleep(API_DELAY)

    logger.info("Fetched %d interests for MP %d", len(interests), member_id)
    return interests


def fetch_all_interests(mp_limit=None):
    """Fetch all interests for all current Commons MPs.

    1. Fetch all MP IDs.
    2. For each MP, fetch all their interests.
    3. Return the combined list.
    """
    mp_ids = fetch_all_mp_ids(mp_limit=mp_limit)
    all_interests = []

    for i, mp_id in enumerate(mp_ids, 1):
        logger.info("Fetching interests for MP %d/%d (id=%d)", i, len(mp_ids), mp_id)
        interests = fetch_interests_for_mp(mp_id)
        all_interests.extend(interests)
        time.sleep(API_DELAY)

    logger.info("Fetched %d total interests across %d MPs", len(all_interests), len(mp_ids))
    return all_interests


# --- Mapping ---

def map_interest_to_entity(interest_dto, timestamp_millis):
    """Map an InterestDto to an interests table row tuple.

    Includes the 3 new columns: parsedAmountPence, currencyCode, bucket.
    """
    category = interest_dto.get("category") or {}
    category_number = category.get("number") or ""
    fields = interest_dto.get("fields") or []
    fields_json = json.dumps(fields)
    parsed_pence, currency_code = parse_amount(fields_json)
    bucket = get_bucket(category_number)

    member = interest_dto.get("member") or {}

    return (
        interest_dto.get("id") or 0,           # id
        member.get("id", 0) or 0,              # memberId
        interest_dto.get("summary") or "",      # summary
        category.get("id") or 0,                # categoryId
        category_number,                         # categoryNumber
        category.get("name") or "",             # categoryName
        interest_dto.get("registrationDate"),    # registrationDate
        interest_dto.get("publishedDate"),       # publishedDate
        1 if interest_dto.get("rectified", False) else 0,  # rectified
        fields_json,                             # fieldsJson
        timestamp_millis,                        # lastUpdated
        parsed_pence,                            # parsedAmountPence
        currency_code,                           # currencyCode
        bucket,                                  # bucket
    )


def insert_interests(conn, interests, timestamp_millis):
    """Insert interests into the DB using batch executemany.

    Uses INSERT OR REPLACE so this works for both seed and delta modes.
    """
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO interests (
            id, memberId, summary, categoryId, categoryNumber, categoryName,
            registrationDate, publishedDate, rectified, fieldsJson, lastUpdated,
            parsedAmountPence, currencyCode, bucket
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = [map_interest_to_entity(interest, timestamp_millis) for interest in interests]

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted interests: %d/%d", min(i + BATCH_SIZE, len(rows)), len(rows))


# --- Build modes ---

def build_seed(output_path, schema_path, mp_limit=None):
    """Seed mode: create fresh DB, fetch all interests, insert."""
    timestamp_millis = int(time.time() * 1000)

    conn = schema_module.create_database_with_tables(
        output_path, schema_path, TABLE_NAMES,
    )

    interests = fetch_all_interests(mp_limit=mp_limit)
    if interests:
        insert_interests(conn, interests, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mp_limit=None):
    """Delta mode: copy previous DB, fetch all interests, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    interests = fetch_all_interests(mp_limit=mp_limit)
    if interests:
        insert_interests(conn, interests, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye interests per-API SQLite database (interests.db)."
    )
    parser.add_argument(
        "--output", default="interests.db",
        help="Output path for the SQLite DB file. Default: interests.db.",
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
        "--mp-limit", type=int, default=None,
        help="Limit number of MPs fetched (for testing).",
    )

    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, mp_limit=args.mp_limit)
    elif args.mode == "delta":
        build_delta(args.output, args.previous_db, args.schema, mp_limit=args.mp_limit)


if __name__ == "__main__":
    main()
