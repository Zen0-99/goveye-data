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


def build_mp_tags(conn):
    """Aggregate tags from MP debate speeches, weighted by recency.

    For each MP, count tag pattern hits across all their non-intervention
    speeches. Weight recent speeches more than old ones (exponential decay,
    half-life ~1 year per D-08).

    Returns a defaultdict: {memberId: {tag_name: float_score}}.
    """
    cursor = conn.cursor()

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


def populate_mp_tags(conn, mp_tag_scores):
    """Convert float scores to int and insert into mp_tags table in batches."""
    cursor = conn.cursor()

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
    args = parser.parse_args()

    if not os.path.exists(args.output):
        print(f"ERROR: {args.output} does not exist — run merge_dbs.py first.")
        sys.exit(1)

    conn = sqlite3.connect(args.output)
    try:
        logger.info("Creating mp_tags table...")
        create_mp_tags_table(conn)

        # Clear existing mp_tags before repopulating
        conn.execute("DELETE FROM mp_tags")
        conn.commit()

        logger.info("Building MP tags from debate speeches...")
        mp_tag_scores = build_mp_tags(conn)

        logger.info("Populating mp_tags table...")
        populate_mp_tags(conn, mp_tag_scores)

        # Verify
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM mp_tags")
        count = cursor.fetchone()[0]
        logger.info("Done: mp_tags has %d rows", count)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
