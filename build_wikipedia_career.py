#!/usr/bin/env python3
"""Per-API build script for MP pre-parliament career data from Wikidata.

Fetches education (P69) and occupation (P106) data for UK MPs from the
Wikidata SPARQL endpoint and stores it in a per-API DB (wikipedia_career.db)
with a single table: mp_career_events.

Approach:
  1. Read MP IDs from mps.db (same as other build scripts).
  2. Batch-query Wikidata SPARQL to find the Wikidata item (QID) that has
     a parliament.uk member ID (P10428) matching each MP's Parliament API ID.
  3. For each batch of QIDs, fetch P69 (education) and P106 (occupation)
     statements with qualifiers (start time P580, end time P582, and
     academic major P812 on P69).
  4. Map these to mp_career_events rows with source='wikipedia'.

Modes:
  seed  — create fresh DB, fetch all data, insert
  delta — copy previous DB, fetch all data, upsert

Usage:
  python build_wikipedia_career.py --output wikipedia_career.db --schema schemas/bundled_schema.json --mode seed --mps-db mps.db
  python build_wikipedia_career.py --output wikipedia_career.db --schema schemas/bundled_schema.json --mode delta --previous-db prev.db --mps-db mps.db
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

from api_helper import BATCH_SIZE, logger

# --- Constants ---

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIDATA_USER_AGENT = "GovEye/1.0 (https://goveye.app; contact@goveye.app)"
SPARQL_DELAY = 0.5  # seconds between SPARQL queries for rate limiting
QID_LOOKUP_BATCH = 200  # MP IDs per P10428 lookup query
STATEMENT_BATCH = 50  # QIDs per education/occupation query (keep small to avoid timeout)
SPARQL_TIMEOUT = 120  # seconds — Wikidata SPARQL can be slow

CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS mp_career_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mpId INTEGER NOT NULL,
        category TEXT,
        name TEXT,
        house INTEGER,
        startDate TEXT,
        endDate TEXT,
        additionalInfo TEXT,
        additionalInfoLink TEXT,
        constituencyName TEXT,
        constituencyId INTEGER,
        source TEXT,
        lastUpdated INTEGER NOT NULL
    )
"""


# --- MP ID fetching ---

def fetch_mp_ids_from_db(mps_db_path):
    """Read all MP IDs and names from the mps.db file.

    Returns a list of (id, name) tuples so we can log human-readable
    names alongside IDs for false-positive detection.
    """
    conn = sqlite3.connect(mps_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nameListAs FROM mps ORDER BY id")
    mp_data = [(row[0], row[1] if len(row) > 1 else "?") for row in cursor.fetchall()]
    conn.close()
    logger.info("Read %d MP IDs from %s", len(mp_data), mps_db_path)
    return mp_data


# --- Wikidata SPARQL ---

def run_sparql_query(query):
    """Execute a SPARQL query against the Wikidata endpoint.

    Returns the parsed JSON results dict, or None on failure.
    """
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{WIKIDATA_SPARQL_URL}?{params}"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": WIKIDATA_USER_AGENT,
            "Accept": "application/sparql-results+json",
        })
        with urllib.request.urlopen(req, timeout=SPARQL_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("Wikidata SPARQL query failed: %s", e)
        return None


def fetch_qids_for_mp_ids(mp_data):
    """Batch-query Wikidata for QIDs matching parliament.uk member IDs (P10428).

    Args:
        mp_data: List of (mp_id, mp_name) tuples.

    Returns a dict mapping mp_id (int) -> (wikidata_qid (str), mp_name (str)).
    MPs without a Wikidata item are simply absent from the dict.
    """
    qid_map = {}
    id_to_name = {mp_id: name for mp_id, name in mp_data}
    mp_ids = [mp_id for mp_id, _ in mp_data]
    total = len(mp_ids)

    for i in range(0, total, QID_LOOKUP_BATCH):
        batch = mp_ids[i:i + QID_LOOKUP_BATCH]
        values = " ".join(f'"{mid}"' for mid in batch)
        query = f"""
SELECT ?item ?parlId WHERE {{
  ?item wdt:P10428 ?parlId.
  VALUES ?parlId {{ {values} }}
}}
"""
        logger.info("Fetching Wikidata QIDs for MPs %d-%d/%d...",
                     i, min(i + QID_LOOKUP_BATCH, total), total)
        results = run_sparql_query(query)
        time.sleep(SPARQL_DELAY)

        if results is None:
            logger.warning("  QID lookup batch failed — skipping %d MPs", len(batch))
            continue

        bindings = results.get("results", {}).get("bindings", [])
        batch_matches = 0
        for binding in bindings:
            qid = binding.get("item", {}).get("value", "").split("/")[-1]
            parl_id_str = binding.get("parlId", {}).get("value", "")
            try:
                parl_id = int(parl_id_str)
            except (ValueError, TypeError):
                continue
            mp_name = id_to_name.get(parl_id, "?")
            qid_map[parl_id] = (qid, mp_name)
            batch_matches += 1

        logger.info("  Found %d QIDs in this batch (running total: %d)",
                     batch_matches, len(qid_map))

    matched = len(qid_map)
    unmatched = total - matched
    logger.info("Matched %d / %d MPs to Wikidata QIDs via P10428 (%d unmatched)",
                matched, total, unmatched)

    if unmatched > 0:
        unmatched_names = [id_to_name[mid] for mid in mp_ids if mid not in qid_map]
        logger.info("Unmatched MPs (first 20): %s",
                    ", ".join(unmatched_names[:20]))

    return qid_map


def fetch_career_statements(qids, qid_to_mp_name):
    """Fetch education (P69) and occupation (P106) statements for a batch of QIDs.

    Args:
        qids: List of Wikidata QID strings.
        qid_to_mp_name: Dict mapping QID -> MP name for logging.

    Returns a list of dicts, each with keys:
        qid, category, value_label, start_time, end_time, subject_label
    """
    if not qids:
        return []

    values = " ".join(f"wd:{q}" for q in qids)
    query = f"""
SELECT ?item ?prop ?value ?valueLabel ?startTime ?endTime ?subject ?subjectLabel WHERE {{
  VALUES ?item {{ {values} }}
  {{
    ?item p:P69 ?statement.
    ?statement ps:P69 ?value.
    BIND("education" AS ?prop)
    OPTIONAL {{ ?statement pq:P580 ?startTime. }}
    OPTIONAL {{ ?statement pq:P582 ?endTime. }}
    OPTIONAL {{ ?statement pq:P812 ?subject. }}
  }}
  UNION
  {{
    ?item p:P106 ?statement.
    ?statement ps:P106 ?value.
    BIND("occupation" AS ?prop)
    OPTIONAL {{ ?statement pq:P580 ?startTime. }}
    OPTIONAL {{ ?statement pq:P582 ?endTime. }}
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""
    results = run_sparql_query(query)
    time.sleep(SPARQL_DELAY)

    if results is None:
        logger.warning("  Career statements query failed — skipping %d QIDs", len(qids))
        return []

    statements = []
    bindings = results.get("results", {}).get("bindings", [])
    for binding in bindings:
        qid = binding.get("item", {}).get("value", "").split("/")[-1]
        category = binding.get("prop", {}).get("value")
        name = (binding.get("valueLabel", {}).get("value")
                or binding.get("value", {}).get("value", "").split("/")[-1])
        stmt = {
            "qid": qid,
            "category": category,
            "name": name,
            "start_time": binding.get("startTime", {}).get("value"),
            "end_time": binding.get("endTime", {}).get("value"),
            "subject": binding.get("subjectLabel", {}).get("value"),
        }
        statements.append(stmt)

        # Log each statement for false-positive detection
        mp_name = qid_to_mp_name.get(qid, "?")
        date_range = ""
        if stmt["start_time"] or stmt["end_time"]:
            s = format_wikidata_date(stmt["start_time"]) or "?"
            e = format_wikidata_date(stmt["end_time"]) or "present"
            date_range = f" [{s} → {e}]"
        subject = f" ({stmt['subject']})" if stmt["subject"] else ""
        logger.info("    %s: %s = %s%s%s", mp_name, category, name, subject, date_range)

    logger.info("  Retrieved %d career statements for %d QIDs", len(statements), len(qids))
    return statements


# --- Date formatting ---

def format_wikidata_date(date_str):
    """Extract YYYY-MM-DD from a Wikidata date string.

    Wikidata dates look like "+1953-11-26T00:00:00Z" (day precision),
    "+1953-00-00T00:00:00Z" (year precision), or may have BCE prefix.
    Returns the date part or None.
    """
    if not date_str:
        return None
    # Strip leading + if present
    cleaned = date_str.lstrip("+")
    # Take first 10 chars (YYYY-MM-DD)
    return cleaned[:10] if len(cleaned) >= 10 else cleaned


# --- Entity mapping ---

def map_career_event(mp_id, statement, timestamp_millis):
    """Map a Wikidata career statement dict to an mp_career_events row tuple."""
    return (
        mp_id,
        statement.get("category"),
        statement.get("name"),
        None,  # house
        format_wikidata_date(statement.get("start_time")),
        format_wikidata_date(statement.get("end_time")),
        statement.get("subject"),  # additionalInfo (degree subject for P69)
        None,  # additionalInfoLink
        None,  # constituencyName
        None,  # constituencyId
        "wikipedia",  # source
        timestamp_millis,
    )


# --- Insertion ---

def insert_career_events(conn, rows):
    """Insert career event rows, replacing existing wikipedia rows per MP first."""
    cursor = conn.cursor()

    # Delete existing wikipedia-source rows for each MP in the batch
    mp_ids_in_batch = {row[0] for row in rows}
    for mp_id in mp_ids_in_batch:
        cursor.execute(
            "DELETE FROM mp_career_events WHERE mpId = ? AND source = 'wikipedia'",
            (mp_id,),
        )

    sql = """
        INSERT OR REPLACE INTO mp_career_events (
            mpId, category, name, house, startDate, endDate,
            additionalInfo, additionalInfoLink, constituencyName, constituencyId,
            source, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for i in range(0, len(rows), BATCH_SIZE):
        cursor.executemany(sql, rows[i:i + BATCH_SIZE])
    conn.commit()


# --- DB setup ---

def create_fresh_db(output_path):
    """Create a fresh wikipedia_career.db with the mp_career_events table."""
    if os.path.exists(output_path):
        os.remove(output_path)
    conn = sqlite3.connect(output_path)
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()
    return conn


# --- Build ---

def fetch_all_career_data(mp_data, conn, timestamp_millis):
    """Fetch career data for all MPs and insert into the DB.

    Args:
        mp_data: List of (mp_id, mp_name) tuples.
        conn: SQLite connection.
        timestamp_millis: Timestamp for lastUpdated column.

    Returns (processed_count, skipped_count).
    """
    # 1. Batch-fetch QIDs for all MPs
    qid_map = fetch_qids_for_mp_ids(mp_data)
    if not qid_map:
        logger.warning("No Wikidata QIDs found for any MP — nothing to do")
        return 0, len(mp_data)

    # Build reverse maps: qid -> mp_id, qid -> mp_name
    mp_for_qid = {qid: mp_id for mp_id, (qid, _) in qid_map.items()}
    qid_to_mp_name = {qid: name for _, (qid, name) in qid_map.items()}

    # 2. Batch-fetch career statements for QIDs
    all_qids = list(qid_map.values())
    all_qids = [qid for qid, _ in all_qids]
    processed = 0
    skipped = 0
    pending_rows = []

    for i in range(0, len(all_qids), STATEMENT_BATCH):
        batch_qids = all_qids[i:i + STATEMENT_BATCH]
        logger.info("Fetching career data for QIDs %d-%d/%d...",
                     i, min(i + STATEMENT_BATCH, len(all_qids)), len(all_qids))

        statements = fetch_career_statements(batch_qids, qid_to_mp_name)

        for stmt in statements:
            mp_id = mp_for_qid.get(stmt["qid"])
            if mp_id is None:
                continue
            pending_rows.append(map_career_event(mp_id, stmt, timestamp_millis))

        # Batch insert every few QID batches to avoid holding too much in memory
        if len(pending_rows) >= BATCH_SIZE:
            insert_career_events(conn, pending_rows)
            processed += len(pending_rows)
            pending_rows = []

        logger.info("Processed %d/%d QIDs", min(i + STATEMENT_BATCH, len(all_qids)), len(all_qids))

    # Insert remaining
    if pending_rows:
        insert_career_events(conn, pending_rows)
        processed += len(pending_rows)

    skipped = len(mp_data) - len(qid_map)
    return processed, skipped


def build_seed(output_path, mps_db, mp_limit=None, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch all data, insert."""
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT mpId FROM mp_career_events WHERE source = 'wikipedia'")
        processed_mp_ids = {row[0] for row in cursor.fetchall()}
        logger.info("Resuming from checkpoint: %d MPs already in DB", len(processed_mp_ids))
    else:
        conn = create_fresh_db(output_path)
        processed_mp_ids = set()

    mp_data = fetch_mp_ids_from_db(mps_db)
    if mp_limit:
        mp_data = mp_data[:mp_limit]

    # Filter out already-processed MPs (checkpoint resume)
    remaining = [(mid, name) for mid, name in mp_data if mid not in processed_mp_ids]
    if len(remaining) < len(mp_data):
        logger.info("Skipping %d already-processed MPs", len(mp_data) - len(remaining))

    processed, skipped = fetch_all_career_data(remaining, conn, timestamp_millis)

    logger.info("VACUUMing database...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s (%d events, %d MPs without Wikidata)",
                output_path, processed, skipped)


def build_delta(output_path, previous_db, mps_db, mp_limit=None):
    """Delta mode: copy previous DB, fetch all data, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    conn = sqlite3.connect(output_path)
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()

    mp_data = fetch_mp_ids_from_db(mps_db)
    if mp_limit:
        mp_data = mp_data[:mp_limit]

    processed, skipped = fetch_all_career_data(mp_data, conn, timestamp_millis)

    logger.info("VACUUMing database...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Delta build complete: %s (%d events, %d MPs without Wikidata)",
                output_path, processed, skipped)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Wikipedia career per-API SQLite database (wikipedia_career.db)."
    )
    parser.add_argument("--output", default="wikipedia_career.db",
                        help="Output path for the SQLite DB file.")
    parser.add_argument("--schema", required=True,
                        help="Path to the Room exported schema JSON (for consistency with other build scripts).")
    parser.add_argument("--mode", choices=["seed", "delta"], default="seed",
                        help="Build mode: seed (full) or delta (incremental).")
    parser.add_argument("--previous-db",
                        help="Path to previous DB file (required for delta mode).")
    parser.add_argument("--mps-db", required=True,
                        help="Path to mps.db (for MP ID list).")
    parser.add_argument("--mp-limit", type=int, default=None,
                        help="Limit number of MPs fetched (for testing).")
    parser.add_argument("--checkpoint-db",
                        help="Path to a checkpoint DB to resume from (seed mode only).")
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.mps_db,
                   mp_limit=args.mp_limit, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.mps_db,
                    mp_limit=args.mp_limit)


if __name__ == "__main__":
    main()
