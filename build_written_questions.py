#!/usr/bin/env python3
"""Per-API build script for Parliament Written Questions.

Fetches individual written questions (with full question text) from the
Parliament Written Questions API
(questions-statements-api.parliament.uk/api/writtenquestions/questions)
and builds a per-API DB (written_questions.db) with only the
written_questions table + the full schema's Room identity hash.

This replaces the old approach where build_hansard.py only stored per-MP
question *counts*. Now we store the actual question text, answering body,
date tabled, and UIN for each question — enabling the MP activity feed
(Phase 15) to display question content.

Pitfall 4: the bulk API truncates text fields at 255 characters. For any
question where len(questionText) == 255, we fetch the full text from the
individual endpoint GET {QUESTIONS_API}/{id}.

Questions are filtered to only those whose askingMemberId is in the
mps.db MP set (Commons MPs only, house=1).

Modes:
  seed  — create fresh DB, fetch all questions, insert
  delta — copy previous DB, re-fetch all questions, upsert

Usage:
  python build_written_questions.py --output written_questions.db --schema schemas/bundled_schema.json --mode seed --mps-db mps.db
  python build_written_questions.py --output written_questions.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_written_questions.db --mps-db mps.db
"""

import argparse
import os
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import schema as schema_module
from api_helper import BATCH_SIZE, logger, api_get

# --- Constants ---

QUESTIONS_API = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
TABLE_NAMES = ["written_questions"]
API_BATCH_SIZE = 500  # questions per API page (skip/take pagination)


# --- Written Questions API ---

def fetch_full_question_text(question_id):
    """Fetch the full text of a single question from the individual endpoint.

    Pitfall 4: the bulk API truncates text at 255 chars. When
    len(questionText) == 255, we fetch the full text from
    GET {QUESTIONS_API}/{id}.

    Args:
        question_id: The Parliament question ID.

    Returns:
        The full text string, or empty string if the fetch fails.
    """
    try:
        r = api_get(f"{QUESTIONS_API}/{question_id}", timeout=30)
        data = r.json()
        val = data.get("value", data)
        return val.get("text", "")
    except Exception as e:
        logger.warning("Failed to fetch full text for question %s: %s", question_id, e)
        return ""


def _parse_question(val):
    """Parse a single question dict from the API response."""
    return {
        "id": val.get("id"),
        "askingMemberId": val.get("askingMemberId"),
        "uin": val.get("uin", ""),
        "dateTabled": val.get("dateTabled", ""),
        "answeringBodyId": val.get("answeringBodyId"),
        "answeringBodyName": val.get("answeringBodyName", ""),
        "questionText": val.get("questionText", ""),
        "house": val.get("house", 1),
    }


def fetch_questions_for_mp(mp_id):
    """Fetch all written questions for a single MP using askingMemberId filter.

    This avoids the deep-pagination 500 errors that occur when fetching all
    questions with large skip values. Each MP typically has a few hundred
    questions, so skip values stay small.

    Returns a list of question dicts.
    """
    questions = []
    skip = 0
    while True:
        params = {"skip": skip, "take": API_BATCH_SIZE, "askingMemberId": mp_id}
        try:
            r = api_get(QUESTIONS_API, params=params, timeout=60)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code >= 500:
                logger.warning("Skipping page for MP %d at skip=%d (persistent 5xx)",
                               mp_id, skip)
                break
            raise
        data = r.json()
        results = data.get("results", [])
        if not results:
            break
        for item in results:
            val = item.get("value", {})
            questions.append(_parse_question(val))
        if len(results) < API_BATCH_SIZE:
            break
        skip += API_BATCH_SIZE
    return questions


def fetch_written_questions(mp_ids=None):
    """Fetch all written questions from the Parliament Written Questions API.

    If mp_ids is provided, fetches questions per-MP using the askingMemberId
    filter (avoids deep-pagination 500 errors). Uses a thread pool for
    parallelism.

    If mp_ids is None, falls back to unfiltered deep pagination (legacy,
    prone to 500 errors on large skips).

    Args:
        mp_ids: Optional set of MP IDs to fetch questions for.

    Returns:
        List of question dicts with keys: id, askingMemberId, uin,
        dateTabled, answeringBodyId, answeringBodyName, questionText, house.
    """
    if mp_ids is None:
        return _fetch_written_questions_legacy()

    all_questions = []
    mp_list = sorted(mp_ids)
    logger.info("Fetching written questions for %d MPs (parallel, take=%d)",
                len(mp_list), API_BATCH_SIZE)

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_mp = {
            executor.submit(fetch_questions_for_mp, mp_id): mp_id
            for mp_id in mp_list
        }
        for future in as_completed(future_to_mp):
            mp_id = future_to_mp[future]
            try:
                questions = future.result()
                all_questions.extend(questions)
                if questions:
                    logger.info("MP %d: %d questions (total so far: %d)",
                                mp_id, len(questions), len(all_questions))
            except Exception as e:
                logger.error("Failed to fetch questions for MP %d: %s", mp_id, e)

    logger.info("Fetched %d written questions total (from %d MPs)",
                len(all_questions), len(mp_list))
    return all_questions


def _fetch_written_questions_legacy():
    """Legacy fetch: unfiltered deep pagination (prone to 500 errors)."""
    all_questions = []
    skip = 0
    skipped_pages = 0
    logger.info("Fetching written questions from API (paginated, take=%d)", API_BATCH_SIZE)
    while True:
        params = {"skip": skip, "take": API_BATCH_SIZE}
        try:
            r = api_get(QUESTIONS_API, params=params, timeout=60)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code >= 500:
                logger.error("Skipping page at skip=%d (persistent 5xx)", skip)
                skipped_pages += 1
                skip += API_BATCH_SIZE
                continue
            raise
        data = r.json()
        results = data.get("results", [])
        if not results:
            break
        for item in results:
            val = item.get("value", {})
            all_questions.append(_parse_question(val))
        logger.info("Fetched %d questions (total so far: %d)", len(results), len(all_questions))
        if len(results) < API_BATCH_SIZE:
            break
        skip += API_BATCH_SIZE
    if skipped_pages:
        logger.warning("Skipped %d pages due to persistent 5xx errors (~%d questions lost)",
                       skipped_pages, skipped_pages * API_BATCH_SIZE)
    logger.info("Fetched %d written questions total", len(all_questions))
    return all_questions


def fetch_all_mps_from_db(mps_db_path):
    """Fetch all MP IDs from the mps.db file (Commons only, house=1).

    Returns a set of MP IDs for filtering questions.
    """
    conn = sqlite3.connect(mps_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM mps WHERE house = 1")
    mp_ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    return mp_ids


# --- Mapping + insertion ---

def map_question_to_entity(q, timestamp_millis):
    """Map a question dict to a written_questions row tuple.

    Matches WrittenQuestionEntity fields:
    (id, memberId, uin, dateTabled, answeringBodyId,
     answeringBodyName, questionText, house, lastUpdated)
    """
    return (
        q.get("id") or 0,
        q.get("askingMemberId") or 0,
        q.get("uin") or "",
        q.get("dateTabled") or "",
        q.get("answeringBodyId") or 0,
        q.get("answeringBodyName") or "",
        q.get("questionText") or "",
        q.get("house") or 1,
        timestamp_millis,
    )


def insert_questions(conn, questions, timestamp_millis):
    """Insert questions into the written_questions table using batch executemany.

    Uses INSERT OR REPLACE so this works for both seed and delta modes.
    """
    cursor = conn.cursor()
    insert_sql = """
        INSERT OR REPLACE INTO written_questions (
            id, memberId, uin, dateTabled, answeringBodyId,
            answeringBodyName, questionText, house, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = [map_question_to_entity(q, timestamp_millis) for q in questions]

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted questions: %d/%d", min(i + BATCH_SIZE, len(rows)), len(rows))


# --- Build modes ---

def get_processed_question_ids(conn):
    """Get the set of question IDs already in the checkpoint DB."""
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM written_questions")
    return {row[0] for row in cursor.fetchall()}


def build_seed(output_path, schema_path, mps_db, mp_limit=None, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch all questions, filter to MPs, insert.

    If checkpoint_db exists and has data, upserts on top of it (INSERT OR
    REPLACE handles dedup).
    """
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        existing = get_processed_question_ids(conn)
        logger.info("Resuming from checkpoint: %d questions already in DB", len(existing))
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )

    # Load MP IDs for filtering
    mp_ids = fetch_all_mps_from_db(mps_db)
    logger.info("Loaded %d MP IDs from %s for filtering", len(mp_ids), mps_db)

    # Fetch questions per-MP (avoids deep-pagination 500 errors)
    questions = fetch_written_questions(mp_ids=mp_ids)

    # Questions are already filtered to known MPs by the API
    filtered = questions
    logger.info("Fetched %d questions from known MPs", len(filtered))

    if mp_limit:
        filtered = filtered[:mp_limit]

    # Save all questions immediately (with truncated text) to prevent data loss
    if filtered:
        insert_questions(conn, filtered, timestamp_millis)
        logger.info("Saved %d questions to DB (truncated text pending for some)", len(filtered))

    # Pitfall 4: fetch full text for truncated questions (parallelised)
    # and update the DB incrementally as each full text arrives
    truncated = [q for q in filtered if len(q.get("questionText", "")) == 255]
    logger.info("Fetching full text for %d truncated questions (parallel, 10 workers)",
                len(truncated))

    update_sql = "UPDATE written_questions SET questionText = ? WHERE id = ?"
    done_count = 0
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_q = {
            executor.submit(fetch_full_question_text, q["id"]): q
            for q in truncated
        }
        for future in as_completed(future_to_q):
            q = future_to_q[future]
            try:
                full_text = future.result()
                if full_text:
                    q["questionText"] = full_text
                    conn.execute(update_sql, (full_text, q["id"]))
                    conn.commit()
            except Exception as e:
                logger.warning("Failed to fetch full text for question %s: %s", q.get("id"), e)
            done_count += 1
            if done_count % 100 == 0:
                logger.info("Full text progress: %d/%d (%.0f%%)",
                            done_count, len(truncated),
                            100.0 * done_count / len(truncated))

    logger.info("Full text fetching complete: %d/%d done", done_count, len(truncated))

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mps_db, mp_limit=None):
    """Delta mode: copy previous DB, re-fetch all questions, filter, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    # Ensure schema is up-to-date (previous DB may be from an older schema version)
    schema_module.ensure_schema(conn, schema_path, TABLE_NAMES)
    logger.info("Schema ensured for delta build")

    # Load MP IDs for filtering
    mp_ids = fetch_all_mps_from_db(mps_db)
    logger.info("Loaded %d MP IDs from %s for filtering", len(mp_ids), mps_db)

    # Fetch questions per-MP (avoids deep-pagination 500 errors)
    questions = fetch_written_questions(mp_ids=mp_ids)

    # Questions are already filtered to known MPs by the API
    filtered = questions
    logger.info("Fetched %d questions from known MPs", len(filtered))

    if mp_limit:
        filtered = filtered[:mp_limit]

    # Save all questions immediately (with truncated text) to prevent data loss
    if filtered:
        insert_questions(conn, filtered, timestamp_millis)
        logger.info("Saved %d questions to DB (truncated text pending for some)", len(filtered))

    # Pitfall 4: fetch full text for truncated questions (parallelised)
    # and update the DB incrementally as each full text arrives
    truncated = [q for q in filtered if len(q.get("questionText", "")) == 255]
    logger.info("Fetching full text for %d truncated questions (parallel, 10 workers)",
                len(truncated))

    update_sql = "UPDATE written_questions SET questionText = ? WHERE id = ?"
    done_count = 0
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_q = {
            executor.submit(fetch_full_question_text, q["id"]): q
            for q in truncated
        }
        for future in as_completed(future_to_q):
            q = future_to_q[future]
            try:
                full_text = future.result()
                if full_text:
                    q["questionText"] = full_text
                    conn.execute(update_sql, (full_text, q["id"]))
                    conn.commit()
            except Exception as e:
                logger.warning("Failed to fetch full text for question %s: %s", q.get("id"), e)
            done_count += 1
            if done_count % 100 == 0:
                logger.info("Full text progress: %d/%d (%.0f%%)",
                            done_count, len(truncated),
                            100.0 * done_count / len(truncated))

    logger.info("Full text fetching complete: %d/%d done", done_count, len(truncated))

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Written Questions per-API SQLite database (written_questions.db)."
    )
    parser.add_argument(
        "--output", default="written_questions.db",
        help="Output path for the SQLite DB file. Default: written_questions.db.",
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
        help="Path to the mps.db file (to get MP IDs for filtering).",
    )
    parser.add_argument(
        "--mp-limit", type=int, default=None,
        help="Limit number of questions inserted (for testing).",
    )
    parser.add_argument(
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only). Upserts on top of existing data.",
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
