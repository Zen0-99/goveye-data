#!/usr/bin/env python3
"""Per-API build script for Party Manifestos (Phase 11, plan 11-05).

Downloads plain-text manifestos for 7 major UK parties, stores in
party_manifestos table with FTS4 virtual table for full-text search.

Manifestos change rarely (only at general elections), so the workflow
runs monthly and skips publish if unchanged.

Modes:
  seed  — create fresh DB, download all manifestos, insert
  delta — copy previous DB, compare hashes, upsert if changed

Usage:
  python build_manifestos.py --output manifestos.db --schema schemas/bundled_schema.json --mode seed
"""

import argparse
import hashlib
import os
import shutil
import sqlite3
import time

import requests

import schema as schema_module
from api_helper import BATCH_SIZE, api_get, logger

# --- Constants ---

TABLE_NAMES = ["party_manifestos", "party_manifestos_fts4"]

# Party ID → manifesto info (partyId from mps.db)
# For 2024 manifestos, we bundle the text directly or download from known sources.
# In production, these would be downloaded from Lancaster Wmatrix or party websites.
# For the build script, we support both URL download and local file bundling.
PARTY_MANIFESTOS = {
    4:    {"abbrev": "Con", "name": "Conservative", "filename": "Conservatives.txt"},
    15:   {"abbrev": "Lab", "name": "Labour", "filename": "Labour.txt"},
    17:   {"abbrev": "LD", "name": "Liberal Democrat", "filename": "LiberalDemocrats.txt"},
    44:   {"abbrev": "Green", "name": "Green", "filename": "Green.txt"},
    22:   {"abbrev": "PC", "name": "Plaid Cymru", "filename": "PlaidCymru.txt"},
    29:   {"abbrev": "SNP", "name": "SNP", "filename": "SNP.txt"},
    1036: {"abbrev": "RUK", "name": "Reform UK", "filename": "ReformUK.txt"},
}

MANIFESTO_YEAR = 2024

# Lancaster Wmatrix hosts edited plain-text versions of UK election manifestos.
# These are manually edited from PDFs (headers/footers removed, pseudo-XML tags).
MANIFESTO_BASE_URL = "https://ucrel.lancs.ac.uk/wmatrix/ukmanifestos2024/text/"


def compute_word_count(text):
    """Compute word count by splitting on whitespace."""
    return len(text.split())


def download_manifesto(party_id, party_info):
    """Download a manifesto text file.

    Tries the base URL first, then falls back to local files in ./manifestos/ directory.
    Returns (text, source) or (None, None) if download fails.
    """
    filename = party_info["filename"]

    # Try URL download
    url = f"{MANIFESTO_BASE_URL}{filename}"
    try:
        r = api_get(url, timeout=60)
        text = r.text
        if text and len(text) > 100:
            logger.info("Downloaded manifesto for %s (%d words)", party_info["name"], compute_word_count(text))
            return text, url
    except Exception as e:
        logger.warning("Failed to download %s from URL: %s", filename, e)

    # Try local file
    local_path = os.path.join("manifestos", filename)
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            if text and len(text) > 100:
                logger.info("Loaded manifesto for %s from local file (%d words)",
                           party_info["name"], compute_word_count(text))
                return text, f"local:{local_path}"
        except Exception as e:
            logger.warning("Failed to load local manifesto %s: %s", local_path, e)

    logger.warning("No manifesto available for %s (partyId=%d)", party_info["name"], party_id)
    return None, None


# --- DB operations ---

def build_manifestos_tables(conn):
    """Create the party_manifestos table and FTS4 virtual table."""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS party_manifestos (
            partyId INTEGER PRIMARY KEY,
            manifestoText TEXT NOT NULL,
            manifestoYear INTEGER NOT NULL,
            wordCount INTEGER NOT NULL,
            source TEXT
        )
    """)

    # FTS4 virtual table with external content
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS party_manifestos_fts4
        USING fts4(manifestoText, content='party_manifestos')
    """)

    conn.commit()


def insert_manifesto(conn, party_id, text, year, source):
    """Insert a manifesto into party_manifestos and populate FTS4."""
    cursor = conn.cursor()
    word_count = compute_word_count(text)

    # Insert into main table
    cursor.execute("""
        INSERT OR REPLACE INTO party_manifestos (partyId, manifestoText, manifestoYear, wordCount, source)
        VALUES (?, ?, ?, ?, ?)
    """, (party_id, text, year, word_count, source))

    # Populate FTS4 (external content table — insert rowid reference)
    cursor.execute("DELETE FROM party_manifestos_fts4 WHERE rowid = ?", (party_id,))
    cursor.execute("INSERT INTO party_manifestos_fts4 (rowid, manifestoText) VALUES (?, ?)",
                   (party_id, text))

    conn.commit()
    logger.info("Inserted manifesto: partyId=%d, words=%d, source=%s", party_id, word_count, source)


def get_manifesto_hash(conn, party_id):
    """Get SHA-256 hash of existing manifesto text for comparison."""
    cursor = conn.cursor()
    cursor.execute("SELECT manifestoText FROM party_manifestos WHERE partyId = ?", (party_id,))
    row = cursor.fetchone()
    if row and row[0]:
        return hashlib.sha256(row[0].encode("utf-8")).hexdigest()
    return None


# --- Build modes ---

def build_seed(output_path, schema_path):
    """Seed mode: create fresh DB, download all manifestos, insert."""
    if os.path.exists(output_path):
        os.remove(output_path)

    conn = schema_module.create_database_with_tables(output_path, schema_path, TABLE_NAMES)
    build_manifestos_tables(conn)

    for party_id, party_info in PARTY_MANIFESTOS.items():
        text, source = download_manifesto(party_id, party_info)
        if text:
            insert_manifesto(conn, party_id, text, MANIFESTO_YEAR, source)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path):
    """Delta mode: copy previous DB, compare hashes, upsert if changed."""
    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)
    build_manifestos_tables(conn)

    changes = 0
    for party_id, party_info in PARTY_MANIFESTOS.items():
        text, source = download_manifesto(party_id, party_info)
        if not text:
            continue

        new_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        old_hash = get_manifesto_hash(conn, party_id)

        if new_hash != old_hash:
            insert_manifesto(conn, party_id, text, MANIFESTO_YEAR, source)
            changes += 1
            logger.info("Manifesto changed for %s", party_info["name"])
        else:
            logger.info("Manifesto unchanged for %s, skipping", party_info["name"])

    if changes > 0:
        logger.info("VACUUMing database to minimize file size...")
        conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s (%d changes)", output_path, changes)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Party Manifestos per-API SQLite database (manifestos.db)."
    )
    parser.add_argument("--output", default="manifestos.db",
                        help="Output path for the SQLite DB file. Default: manifestos.db.")
    parser.add_argument("--schema", required=True,
                        help="Path to the Room exported schema JSON (bundled_schema.json).")
    parser.add_argument("--mode", choices=["seed", "delta"], default="seed",
                        help="Build mode: seed (full) or delta (incremental). Default: seed.")
    parser.add_argument("--previous-db",
                        help="Path to previous DB file (required for delta mode).")

    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema)
    elif args.mode == "delta":
        build_delta(args.output, args.previous_db, args.schema)


if __name__ == "__main__":
    main()
