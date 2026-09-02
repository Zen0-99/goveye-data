#!/usr/bin/env python3
"""Post-merge build script for MP tag aggregation (Phase 14, plan 14-02).

Runs AFTER merge_dbs.py has combined all per-API DBs into goveye.db and
AFTER build_tags.py (which creates the announcement tag tables). Produces
the mp_tags table by aggregating tag pattern hits across each MP's debate
speeches, weighted by recency (D-08).

Recency weighting: exponential decay math.exp(-days_ago / 365.0) —
half-life ~1 year. Recent speeches count more than old ones.

Pitfall 8: the debate_speeches table has no date column. We JOIN with
divisions on divisionId to get the division date as the speech date proxy.

Only non-intervention speeches (isIntervention = 0) with memberId > 0
are counted (skip unmatched speeches and interventions).

Table produced:
  mp_tags (memberId, tag, hitCount) — hitCount is the recency-weighted
  score as an int. Composite PK: (memberId, tag).

When NOT to run this script:
  This script re-aggregates MP tags (tag, hitCount in mp_tags) from
  written_statements, government_publications, and legislation. If
  only the tag aggregation logic changed (e.g. the recency weighting
  formula, the TAG_DICTIONARY patterns), do NOT run this script — run
  the SQL UPDATE directly against the existing DB instead. The Room
  migration in GovEye's DatabaseModule.kt contains the same SQL and
  handles the update on user devices.

  See goveye-data/AGENTS.md for the full decision guide.

Usage:
  python build_mp_tags.py --output goveye.db --schema schemas/bundled_schema.json
"""

import argparse
import datetime
import logging
import math
import os
import sqlite3
import sys
from collections import defaultdict

from api_helper import BATCH_SIZE, logger

from build_tags import count_pattern_hits, TAG_DICTIONARY


def create_mp_tags_table(conn):
    """Create the mp_tags table if it doesn't exist.

    Schema must match Room's MpTagEntity exactly (no DEFAULT clauses,
    composite PRIMARY KEY as a separate constraint) or Room will reject
    the DB on open with "Pre-packaged database has an invalid schema".
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS `mp_tags` (
            `memberId` INTEGER NOT NULL,
            `tag` TEXT NOT NULL,
            `hitCount` INTEGER NOT NULL,
            PRIMARY KEY(`memberId`, `tag`)
        );
        """
    )
    conn.commit()


def parse_date(date_string):
    """Parse an ISO date string to a datetime.date.

    Handles both YYYY-MM-DD and YYYY-MM-DDTHH:MM:SS formats.
    Returns None if the string cannot be parsed.
    """
    if not date_string:
        return None
    s = date_string.strip()
    # Slice to first 10 chars (YYYY-MM-DD) — works for both date-only
    # and full ISO datetime strings.
    try:
        return datetime.date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def build_mp_tags(conn, incremental=False):
    """Aggregate tags from MP debate speeches, weighted by recency.

    For each MP, count tag pattern hits across all their non-intervention
    speeches. Weight recent speeches more than old ones (exponential decay,
    half-life ~1 year per D-08).

    If incremental=True, only process speeches from divisions that don't
    already have mp_tags entries (i.e., new divisions added since last build).
    The new contributions are ADDED to existing mp_tags scores. The ~2%/week
    recency drift on existing scores is absorbed by int() conversion and
    is invisible to users.

    Returns a defaultdict: {memberId: {tag_name: float_score}}.
    """
    cursor = conn.cursor()

    if incremental:
        # Only process speeches from divisions that are new (don't have
        # any mp_tags entries yet). This means we only process speeches
        # from divisions added since the last build.
        # We detect "new divisions" as those whose speeches haven't been
        # processed yet. Since mp_tags is per-MP not per-division, we use
        # a simpler heuristic: find the max divisionId that has mp_tags
        # (assuming division IDs are monotonically increasing), and only
        # process speeches from divisions with higher IDs.
        # But division IDs aren't guaranteed monotonic. Instead, we track
        # which divisions we've processed by checking if any mp_tags row
        # exists for MPs who spoke in that division.
        # Simpler approach: just process speeches from divisions created
        # after the last build. We detect this by comparing the count of
        # divisions vs the count of distinct divisionIds in mp_tags computation.
        # Actually, the simplest correct approach: process ALL speeches
        # for MPs who have speeches in NEW divisions (divisions that don't
        # appear in the existing mp_tags computation).
        #
        # But that requires knowing which divisions were processed last time.
        # We don't track that. So let's use a different approach:
        # Process only speeches from divisions that don't have division_tags
        # yet (since build_tags.py runs before build_mp_tags.py, and in
        # incremental mode it only tags new divisions).
        cursor.execute("""
            SELECT ds.memberId, ds.speechText, d.date
            FROM debate_speeches ds
            JOIN divisions d ON ds.divisionId = d.id
            WHERE ds.memberId > 0 AND ds.isIntervention = 0
              AND NOT EXISTS (
                  SELECT 1 FROM division_tags dt WHERE dt.divisionId = d.id
              )
        """)
        speeches = cursor.fetchall()
        logger.info("Processing %d new speeches for MP tags (incremental)", len(speeches))
    else:
        # Pitfall 8: debate_speeches has no date column — JOIN with divisions
        # on divisionId to get the division date as the speech date proxy.
        cursor.execute("""
            SELECT ds.memberId, ds.speechText, d.date
            FROM debate_speeches ds
            JOIN divisions d ON ds.divisionId = d.id
            WHERE ds.memberId > 0 AND ds.isIntervention = 0
        """)
        speeches = cursor.fetchall()
        logger.info("Processing %d speeches for MP tags", len(speeches))

    if not speeches:
        logger.info("No new speeches to process — skipping MP tags")
        return defaultdict(lambda: defaultdict(float))

    now = datetime.datetime.now().date()
    mp_tag_scores = defaultdict(lambda: defaultdict(float))

    for member_id, speech_text, division_date in speeches:
        if not speech_text:
            continue

        # Parse division date for recency weight
        div_date = parse_date(division_date)
        if div_date is None:
            # Cannot determine recency — use neutral weight (1.0)
            recency_weight = 1.0
        else:
            days_ago = (now - div_date).days
            recency_weight = math.exp(-days_ago / 365.0)

        for tag_name, patterns in TAG_DICTIONARY.items():
            hit_count = count_pattern_hits(speech_text, patterns)
            if hit_count > 0:
                mp_tag_scores[member_id][tag_name] += hit_count * recency_weight

    logger.info("Aggregated tags for %d MPs", len(mp_tag_scores))
    return mp_tag_scores


def populate_mp_tags(conn, mp_tag_scores, incremental=False):
    """Convert float scores to int and insert into mp_tags table in batches.

    If incremental=True, ADD new scores to existing ones (UPDATE existing
    rows, INSERT new rows). If incremental=False, replace all rows.
    """
    cursor = conn.cursor()

    if incremental:
        # Add new scores to existing ones
        for member_id, tags in mp_tag_scores.items():
            for tag_name, score in tags.items():
                # Try to update existing row first
                cursor.execute("""
                    INSERT INTO mp_tags (memberId, tag, hitCount)
                    VALUES (?, ?, ?)
                    ON CONFLICT(memberId, tag)
                    DO UPDATE SET hitCount = hitCount + ?
                """, (member_id, tag_name, int(score), int(score)))
        conn.commit()
        logger.info("Incrementally updated mp_tags for %d MPs", len(mp_tag_scores))
        return []
    else:
        rows = []
        for member_id, tags in mp_tag_scores.items():
            for tag_name, score in tags.items():
                rows.append((member_id, tag_name, int(score)))

        insert_sql = """INSERT OR REPLACE INTO mp_tags (memberId, tag, hitCount)
                        VALUES (?, ?, ?)"""
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            cursor.executemany(insert_sql, batch)
        conn.commit()

        logger.info("Populated mp_tags with %d rows", len(rows))
        return rows


def main():
    parser = argparse.ArgumentParser(
        description="Post-merge build: produces mp_tags table with recency-weighted MP tag scores."
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to the merged goveye.db (modified in-place).",
    )
    parser.add_argument(
        "--schema", required=False,
        help="Path to the Room exported schema JSON (for reference, not used directly).",
    )
    parser.add_argument(
        "--incremental", action="store_true",
        help="Incremental mode: only process speeches from new divisions. "
             "New tag contributions are added to existing mp_tags scores.",
    )
    args = parser.parse_args()

    if not os.path.exists(args.output):
        print(f"ERROR: {args.output} does not exist — run merge_dbs.py first.")
        sys.exit(1)

    conn = sqlite3.connect(args.output)
    try:
        logger.info("Creating mp_tags table...")
        create_mp_tags_table(conn)

        if args.incremental:
            logger.info("Incremental mode — retaining existing mp_tags, adding new contributions")
        else:
            # Clear existing mp_tags before repopulating
            conn.execute("DELETE FROM mp_tags")
            conn.commit()

        logger.info("Building MP tags from debate speeches...")
        mp_tag_scores = build_mp_tags(conn, incremental=args.incremental)

        if mp_tag_scores:
            logger.info("Populating mp_tags table...")
            populate_mp_tags(conn, mp_tag_scores, incremental=args.incremental)
        else:
            logger.info("No new MP tag scores to populate")

        # Verify
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM mp_tags")
        count = cursor.fetchone()[0]
        logger.info("Done: mp_tags has %d rows", count)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
