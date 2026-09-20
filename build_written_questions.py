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
from datetime import datetime, timedelta

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

    Pitfall 4: the bulk API truncates text at ~258 chars. When
    len(questionText) >= 255, we fetch the full text from
    GET {QUESTIONS_API}/{id}.

    Also fetches the full answerText if the bulk answerText is truncated
    (len >= 255). The individual endpoint returns the complete answerText.

    Includes retry with backoff for 429 (Too Many Requests) responses.

    Args:
        question_id: The Parliament question ID.

    Returns:
        Tuple of (question_text, answer_text) — full text strings,
        or ("", "") if the fetch fails.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            r = api_get(f"{QUESTIONS_API}/{question_id}", timeout=30)
            data = r.json()
            val = data.get("value", data)
            return val.get("questionText", ""), val.get("answerText", "")
        except Exception as e:
            if "429" in str(e) and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                logger.warning("429 rate limit for question %s, retrying in %ds (attempt %d/%d)",
                               question_id, wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            logger.warning("Failed to fetch full text for question %s: %s", question_id, e)
            return "", ""
    return "", ""


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
        "house": 1 if val.get("house") == "Commons" else 2,
        "heading": val.get("heading") or "",
        "dateForAnswer": val.get("dateForAnswer") or "",
        "dateAnswered": val.get("dateAnswered") or "",
        "answerText": val.get("answerText") or "",
        "answeringMemberId": val.get("answeringMemberId") or 0,
        "isWithdrawn": val.get("isWithdrawn", False),
        "answerIsHolding": val.get("answerIsHolding", False),
        "answerIsCorrection": val.get("answerIsCorrection", False),
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


def fetch_questions_since(since_date, mp_ids):
    """Delta fetch: only questions tabled OR answered since `since_date`.

    The full corpus is ~400k rows and per-MP pagination takes ~25 minutes.
    A weekly delta only needs recent activity: tabledWhenFrom catches new
    questions, answeredWhenFrom catches late answers to older questions
    (answers can arrive weeks after tabling). Both are shallow queries —
    no deep-pagination 500s. Results are deduped by id and filtered to
    known Commons MPs.
    """
    queries = {"tabledWhenFrom": since_date, "answeredWhenFrom": since_date}
    seen = {}
    for param, value in queries.items():
        skip = 0
        while True:
            params = {"skip": skip, "take": API_BATCH_SIZE, param: value}
            try:
                r = api_get(QUESTIONS_API, params=params, timeout=60)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code >= 500:
                    logger.error("Skipping %s page at skip=%d (persistent 5xx)", param, skip)
                    break
                raise
            results = r.json().get("results", [])
            if not results:
                break
            for item in results:
                q = _parse_question(item.get("value", {}))
                if q.get("askingMemberId") in mp_ids:
                    seen[q["id"]] = q
            if len(results) < API_BATCH_SIZE:
                break
            skip += API_BATCH_SIZE
        logger.info("%s=%s: %d questions kept so far", param, value, len(seen))
    return list(seen.values())


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
     answeringBodyName, questionText, house, lastUpdated,
     heading, dateForAnswer, dateAnswered, answerText,
     answeringMemberId, isWithdrawn, answerIsHolding, answerIsCorrection)
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
        q.get("heading") or "",
        q.get("dateForAnswer") or "",
        q.get("dateAnswered") or "",
        q.get("answerText") or "",
        q.get("answeringMemberId") or 0,
        1 if q.get("isWithdrawn") else 0,
        1 if q.get("answerIsHolding") else 0,
        1 if q.get("answerIsCorrection") else 0,
    )


def insert_questions(conn, questions, timestamp_millis):
    """Insert questions into the written_questions table using batch executemany.

    UPSERT with a preserve guard: the bulk API returns text stubs
    (questionText truncated at 255 chars, answerText at 258). When the
    incoming value looks like a stub and the stored text is longer
    (previously-fetched full text), the stored text wins — otherwise every
    delta run would clobber full text with stubs and re-fetch the entire
    corpus.
    """
    cursor = conn.cursor()
    insert_sql = """
        INSERT INTO written_questions (
            id, memberId, uin, dateTabled, answeringBodyId,
            answeringBodyName, questionText, house, lastUpdated,
            heading, dateForAnswer, dateAnswered, answerText,
            answeringMemberId, isWithdrawn, answerIsHolding, answerIsCorrection
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            memberId = excluded.memberId,
            uin = excluded.uin,
            dateTabled = excluded.dateTabled,
            answeringBodyId = excluded.answeringBodyId,
            answeringBodyName = excluded.answeringBodyName,
            house = excluded.house,
            lastUpdated = excluded.lastUpdated,
            heading = excluded.heading,
            dateForAnswer = excluded.dateForAnswer,
            dateAnswered = excluded.dateAnswered,
            answeringMemberId = excluded.answeringMemberId,
            isWithdrawn = excluded.isWithdrawn,
            answerIsHolding = excluded.answerIsHolding,
            answerIsCorrection = excluded.answerIsCorrection,
            questionText = CASE
                WHEN length(excluded.questionText) >= 255
                     AND length(written_questions.questionText) > length(excluded.questionText)
                     AND substr(written_questions.questionText, 1, 255) = substr(excluded.questionText, 1, 255)
                THEN written_questions.questionText
                ELSE excluded.questionText
            END,
            answerText = CASE
                WHEN length(excluded.answerText) >= 258
                     AND length(written_questions.answerText) > length(excluded.answerText)
                     AND substr(written_questions.answerText, 1, 258) = substr(excluded.answerText, 1, 258)
                THEN written_questions.answerText
                ELSE excluded.answerText
            END
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


def build_seed(output_path, schema_path, mps_db, mp_limit=None,
               checkpoint_db=None, max_full_text=None, workers=10):
    """Seed mode: create fresh DB, fetch all questions, filter to MPs, insert.

    If checkpoint_db exists and has data, upserts on top of it (INSERT OR
    REPLACE handles dedup).

    If max_full_text is set, fetches full text for at most that many truncated
    questions, then exits with sys.exit(2) to signal that more work remains.
    The caller (CI chain) should resume from the saved checkpoint DB.

    On checkpoint resume, skips the API fetch phase entirely — questions are
    already in the DB with truncated text. Only continues full-text fetching
    for questions that still have truncated text.

    Returns True if more full-text work remains (caller should exit 2).
    """
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        existing = get_processed_question_ids(conn)
        logger.info("Resuming from checkpoint: %d questions already in DB", len(existing))
        # Check if we need to fetch from API or just continue full-text
        row_count = conn.execute("SELECT COUNT(*) FROM written_questions").fetchone()[0]
        if row_count > 0:
            # Questions already fetched — skip API phase, go straight to
            # full-text fetching for remaining truncated questions
            logger.info("Checkpoint has %d questions — skipping API fetch, "
                        "continuing full-text fetching", row_count)
            return _fetch_full_text_batch(conn, max_full_text, workers)
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

    # Full-text fetching (possibly batched)
    more_work = _fetch_full_text_batch(conn, max_full_text, workers)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s", output_path)
    return more_work


def _fetch_full_text_batch(conn, max_full_text=None, workers=10):
    """Fetch full text for truncated questions, optionally batched.

    If max_full_text is set, processes at most that many truncated questions
    and returns True if more remain. If None, processes all (legacy behavior).

    On checkpoint resume, queries the DB for questions that still have
    truncated text (len >= 255) and fetches full text for them.
    """
    # Find truncated questions from the DB (works for both fresh insert and
    # checkpoint resume). The bulk API truncates questionText at exactly 255
    # chars and answerText at exactly 258 (verified against the published
    # DB: 107,704 rows at 255, 292,537 at 258, nothing in between). Using
    # >= 255 would also match already-fetched full text — every batch would
    # re-fetch completed rows and never converge.
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM written_questions "
        "WHERE length(questionText) = 255 OR length(answerText) = 258 "
        "ORDER BY id"
    )
    truncated_ids = [row[0] for row in cursor.fetchall()]
    logger.info("Fetching full text for %d truncated questions (parallel, %d workers)",
                len(truncated_ids), workers)

    if not truncated_ids:
        return False

    # Apply batch limit
    if max_full_text is not None and max_full_text > 0:
        batch = truncated_ids[:max_full_text]
        more_after_batch = len(truncated_ids) > max_full_text
        logger.info("Batch limit: processing %d/%d truncated questions",
                    len(batch), len(truncated_ids))
    else:
        batch = truncated_ids
        more_after_batch = False

    update_q_sql = "UPDATE written_questions SET questionText = ? WHERE id = ?"
    update_a_sql = "UPDATE written_questions SET answerText = ? WHERE id = ?"
    done_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_id = {
            executor.submit(fetch_full_question_text, qid): qid
            for qid in batch
        }
        for future in as_completed(future_to_id):
            qid = future_to_id[future]
            try:
                full_qtext, full_atext = future.result()
                if full_qtext:
                    conn.execute(update_q_sql, (full_qtext, qid))
                if full_atext:
                    conn.execute(update_a_sql, (full_atext, qid))
                conn.commit()
            except Exception as e:
                logger.warning("Failed to fetch full text for question %s: %s", qid, e)
            done_count += 1
            if done_count % 100 == 0:
                logger.info("Full text progress: %d/%d (%.0f%%)",
                            done_count, len(batch),
                            100.0 * done_count / len(batch))

    logger.info("Full text fetching complete: %d/%d done", done_count, len(batch))

    if more_after_batch:
        logger.info("Batch limit reached — %d truncated questions remain. "
                    "Exit code 2 will signal CI to spawn next batch.",
                    len(truncated_ids) - max_full_text)
        return True

    return False


def build_delta(output_path, previous_db, schema_path, mps_db, mp_limit=None,
                max_full_text=None, workers=10):
    """Delta mode: copy previous DB, re-fetch all questions, filter, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    # Ensure schema is up-to-date (previous DB may be from an older schema version)
    schema_module.ensure_schema(conn, schema_path, TABLE_NAMES)
    logger.info("Schema ensured for delta build")

    # Manual migration: add answer/response columns if the schema JSON
    # hasn't been synced yet (same columns as MIGRATION_32_33 in the app).
    wq_cols = [r[1] for r in conn.execute("PRAGMA table_info(written_questions)").fetchall()]
    wq_new_cols = [
        ("heading", "TEXT NOT NULL DEFAULT ''"),
        ("dateForAnswer", "TEXT NOT NULL DEFAULT ''"),
        ("dateAnswered", "TEXT NOT NULL DEFAULT ''"),
        ("answerText", "TEXT NOT NULL DEFAULT ''"),
        ("answeringMemberId", "INTEGER NOT NULL DEFAULT 0"),
        ("isWithdrawn", "INTEGER NOT NULL DEFAULT 0"),
        ("answerIsHolding", "INTEGER NOT NULL DEFAULT 0"),
        ("answerIsCorrection", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_def in wq_new_cols:
        if col_name not in wq_cols:
            conn.execute(f"ALTER TABLE written_questions ADD COLUMN {col_name} {col_def}")
            logger.info("Delta migration: added %s column to written_questions", col_name)
    conn.commit()

    # Load MP IDs for filtering
    mp_ids = fetch_all_mps_from_db(mps_db)
    logger.info("Loaded %d MP IDs from %s for filtering", len(mp_ids), mps_db)

    # Delta only needs recent activity — a full per-MP sweep of ~400k rows
    # takes ~25 min and blows the workflow timeout before full-text fetching
    # even starts. 30-day lookback covers new questions + late answers.
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    filtered = fetch_questions_since(since, mp_ids)
    logger.info("Fetched %d questions tabled/answered since %s", len(filtered), since)

    if mp_limit:
        filtered = filtered[:mp_limit]

    # Save all questions immediately (with truncated text) to prevent data loss
    if filtered:
        insert_questions(conn, filtered, timestamp_millis)
        logger.info("Saved %d questions to DB (truncated text pending for some)", len(filtered))

    # Pitfall 4: fetch full text for truncated questions/answers (parallelised)
    # and update the DB incrementally as each full text arrives.
    #
    # A question needs fetching when the fresh bulk text is a stub
    # (questionText >= 255, answerText >= 258 truncation boundaries) AND the
    # stored row is not already longer (i.e. not already full-text). The
    # previous check ("stored len < 255 means done") was inverted — fetched
    # full text is LONGER than the stub, so every fetched question was
    # re-fetched every run and the corpus (~340k) never fit the timeout.
    # Load stored lengths BEFORE insert_questions runs — the upsert's
    # preserve guard keeps stored full text, so post-insert lengths are
    # equivalent, but pre-insert is the unambiguous "what we had" state.
    stored_lens = {
        row[0]: (row[1] or 0, row[2] or 0)
        for row in conn.execute(
            "SELECT id, length(questionText), length(answerText) "
            "FROM written_questions"
        ).fetchall()
    }

    truncated = []
    for q in filtered:
        sq, sa = stored_lens.get(q["id"], (0, 0))
        q_stub = len(q.get("questionText", "")) >= 255
        a_stub = len(q.get("answerText", "")) >= 255
        q_needs = q_stub and sq <= len(q["questionText"])
        a_needs = a_stub and sa <= len(q["answerText"])
        if q_needs or a_needs:
            truncated.append(q)

    total_truncated = len(truncated)
    if max_full_text is not None and max_full_text > 0:
        truncated = truncated[:max_full_text]
    logger.info(
        "Fetching full text for %d truncated questions (parallel, %d workers; "
        "%d total remaining)", len(truncated), workers, total_truncated,
    )

    update_q_sql = "UPDATE written_questions SET questionText = ? WHERE id = ?"
    update_a_sql = "UPDATE written_questions SET answerText = ? WHERE id = ?"
    done_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_q = {
            executor.submit(fetch_full_question_text, q["id"]): q
            for q in truncated
        }
        for future in as_completed(future_to_q):
            q = future_to_q[future]
            try:
                full_qtext, full_atext = future.result()
                if full_qtext:
                    q["questionText"] = full_qtext
                    conn.execute(update_q_sql, (full_qtext, q["id"]))
                if full_atext:
                    q["answerText"] = full_atext
                    conn.execute(update_a_sql, (full_atext, q["id"]))
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
    parser.add_argument(
        "--workers", type=int, default=10,
        help="Parallel workers for full-text fetching. Default: 10.",
    )
    parser.add_argument(
        "--max-full-text", type=int, default=None,
        help="Maximum number of truncated questions to fetch full text for per run. "
             "In seed mode, reaching the limit exits with code 2 for the CI batch "
             "chain; in delta mode the run just publishes with partial progress.",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        import sys
        more_work = build_seed(args.output, args.schema, args.mps_db,
                               mp_limit=args.mp_limit, checkpoint_db=args.checkpoint_db,
                               max_full_text=args.max_full_text, workers=args.workers)
        if more_work:
            logger.info("More full-text work remains — exiting with code 2 for CI chain")
            sys.exit(2)
    else:
        build_delta(args.output, args.previous_db, args.schema, args.mps_db,
                    mp_limit=args.mp_limit, max_full_text=args.max_full_text, workers=args.workers)


if __name__ == "__main__":
    main()
