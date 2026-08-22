#!/usr/bin/env python3
"""Per-API build script for Hansard contribution counts.

Fetches per-MP question counts (written answers) from the Hansard API
and builds a per-API DB (hansard.db) with only the hansard_contributions
table — one row per MP with a synthetic itemId encoding the count.

The Hansard API (hansard-api.parliament.uk/search.json) returns
TotalWrittenAnswers per member, which represents the number of written
questions asked by that MP. This is used as questionCount in mp_stats.

Modes:
  seed  — create fresh DB, fetch counts for all 650 MPs
  delta — copy previous DB, re-fetch all counts, upsert

Usage:
  python build_hansard.py --output hansard.db --schema schemas/bundled_schema.json --mode seed --mps-db mps.db
  python build_hansard.py --output hansard.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_hansard.db --mps-db mps.db
"""

import argparse
import os
import shutil
import sqlite3
import time

import requests

import schema as schema_module
from api_helper import API_DELAY, BATCH_SIZE, logger

# --- Constants ---

HANSARD_API = "https://hansard-api.parliament.uk/search.json"
TABLE_NAMES = ["hansard_contributions"]


# --- Hansard API ---

def fetch_member_counts(member_id, timeout=30):
    """Fetch contribution counts for a single MP from the Hansard API.

    Returns (total_contributions, total_written_answers).
    """
    r = requests.get(HANSARD_API, params={
        "memberId": member_id,
        "itemsPerPage": 1,
    }, timeout=timeout)
    if r.status_code != 200:
        logger.warning("Hansard API returned %d for memberId=%d", r.status_code, member_id)
        return 0, 0
    data = r.json()
    total_contributions = data.get("TotalContributions", 0)
    total_written_answers = data.get("TotalWrittenAnswers", 0)
    return total_contributions, total_written_answers


def fetch_all_mps_from_db(mps_db_path):
    """Fetch all MP IDs from the mps.db file."""
    conn = sqlite3.connect(mps_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nameDisplayAs FROM mps WHERE house = 1")
    mps = cursor.fetchall()
    conn.close()
    return mps


def insert_counts(conn, counts, timestamp_millis):
    """Insert hansard_contributions rows — one summary row per MP.

    Each row has a synthetic itemId (memberId * 1000000) to avoid collisions.
    The question count is stored in debateSectionId (INTEGER field) so the
    precompute script can read it with a simple SELECT.
    contributionText stores "questions=N,contributions=M" for debugging.
    """
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO hansard_contributions
            (itemId, memberId, memberName, contributionText,
             sittingDate, house, debateSection, debateSectionId, lastUpdated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    rows = []
    for member_id, member_name, question_count, contribution_count in counts:
        # Synthetic itemId: memberId * 1000000
        # debateSectionId stores the question count (written answers) for fast querying
        # contributionText stores both counts for debugging
        rows.append((
            member_id * 1_000_000,
            member_id,
            member_name or "",
            f"questions={question_count},contributions={contribution_count}",
            "",  # sittingDate — not used for summary rows
            "Commons",
            "Summary",  # debateSection — marks this as a summary row
            question_count,  # debateSectionId — repurposed to store question count
            timestamp_millis,
        ))

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
    conn.commit()
    logger.info("Inserted %d hansard contribution summary rows", len(rows))


# --- Build modes ---

def build_seed(output_path, schema_path, mps_db, mp_limit=None, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch counts for all MPs."""
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        # Get already-processed MPs
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT memberId FROM hansard_contributions")
        skip_ids = {row[0] for row in cursor.fetchall()}
        logger.info("Resuming from checkpoint: %d MPs already processed", len(skip_ids))
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        skip_ids = set()

    mps = fetch_all_mps_from_db(mps_db)
    if mp_limit:
        mps = mps[:mp_limit]

    counts = []
    total_questions = 0
    for member_id, member_name in mps:
        if member_id in skip_ids:
            continue
        total_contributions, total_written_answers = fetch_member_counts(member_id)
        counts.append((member_id, member_name, total_written_answers, total_contributions))
        total_questions += total_written_answers
        if (len(counts) % 50) == 0:
            logger.info("Fetched %d/%d MPs (%d total questions so far)",
                        len(counts), len(mps), total_questions)
            # Insert in batches so checkpoint resume works
            insert_counts(conn, counts, timestamp_millis)
            counts = []
        time.sleep(0.3)  # Be gentle with the API

    if counts:
        insert_counts(conn, counts, timestamp_millis)

    logger.info("Hansard build complete: %d MPs, %d total questions", len(mps), total_questions)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mps_db, mp_limit=None):
    """Delta mode: copy previous DB, re-fetch all counts, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    mps = fetch_all_mps_from_db(mps_db)
    if mp_limit:
        mps = mps[:mp_limit]

    counts = []
    for member_id, member_name in mps:
        total_contributions, total_written_answers = fetch_member_counts(member_id)
        counts.append((member_id, member_name, total_written_answers, total_contributions))
        if (len(counts) % 50) == 0:
            logger.info("Fetched %d/%d MPs", len(counts), len(mps))
            insert_counts(conn, counts, timestamp_millis)
            counts = []
        time.sleep(0.3)

    if counts:
        insert_counts(conn, counts, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Hansard contribution counts per-API SQLite database (hansard.db)."
    )
    parser.add_argument(
        "--output", default="hansard.db",
        help="Output path for the SQLite DB file. Default: hansard.db.",
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
        help="Path to the mps.db file (to get MP IDs).",
    )
    parser.add_argument(
        "--mp-limit", type=int, default=None,
        help="Limit number of MPs fetched (for testing).",
    )
    parser.add_argument(
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, args.mps_db,
                   mp_limit=args.mp_limit, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema, args.mps_db,
                    mp_limit=args.mp_limit)


if __name__ == "__main__":
    main()
