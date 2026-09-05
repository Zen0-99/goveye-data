#!/usr/bin/env python3
"""Post-merge build script for party leader identification (Phase 14, plan 14-02).

Runs AFTER merge_dbs.py has combined all per-API DBs into goveye.db.
Produces the party_leaders table by identifying leader-type titles from
MNIS bio_data postsJson (D-07).

The bio_data table stores posts as a JSON array in the postsJson column
(combining GovernmentPosts + OppositionPosts from MNIS). Each post has:
  {type, title, department, startDate, endDate}

We check if any post title matches a LEADER_TITLES entry (case-insensitive
contains match). If a match is found, we insert (partyId, memberId, title)
into party_leaders.

Fallback: if no leader post is found for a major party, we use
HARDCODED_LEADERS (partyId → (memberId, title)) to ensure the
party_leaders table is never empty for major parties (RESEARCH.md A6).

Table produced:
  party_leaders (partyId, memberId, title) — PK: partyId.

Usage:
  python build_party_leaders.py --output goveye.db --schema schemas/bundled_schema.json
"""

import argparse
import json
import logging
import os
import sqlite3
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("build_party_leaders")

# --- Leader titles to match in bio_data postsJson (D-07) ---
LEADER_TITLES = [
    "Prime Minister",
    "Leader of the Opposition",
    "Leader of the Labour Party",
    "Leader of the Conservative Party",
    "Leader of the Liberal Democrats",
    "Leader of the Scottish National Party",
    "Leader of the Democratic Unionist Party",
    "Leader of Plaid Cymru",
    "Leader of the Green Party",
    "Leader of Reform UK",
]

# --- Fallback: hardcoded current party leaders (RESEARCH.md A6) ---
# Used only when bio_data does not contain a leader-type post for a party.
# partyId → (memberId, title). Member IDs are Parliament API member IDs
# (must exist in the mps table). These should be verified/updated periodically.
# Last verified: 2026-09-03 against the mps table.
HARDCODED_LEADERS = {
    # Labour (15) — Andy Burnham, Prime Minister (since 2026-07-20)
    15: (1427, "Prime Minister"),
    # Conservative (4) — Kemi Badenoch, Leader of the Opposition
    4: (4597, "Leader of the Opposition"),
    # Liberal Democrats (17) — Ed Davey
    17: (188, "Leader of the Liberal Democrats"),
    # SNP (29) — Pete Wishart (Westminster leader; Stephen Flynn not in mps
    # table; John Swinney is an MSP not an MP)
    29: (1440, "Leader of the Scottish National Party"),
    # DUP (7) — Gavin Robinson
    7: (4360, "Leader of the Democratic Unionist Party"),
    # Plaid Cymru (22) — Liz Saville Roberts (Westminster leader; Rhun ap
    # Iorwerth is the Senedd leader, not an MP)
    22: (4521, "Leader of Plaid Cymru"),
    # Green Party (44) — Zack Polanski is the current leader (since Sept 2025)
    # but is NOT an MP (London Assembly Member). The party_leaders table
    # references mps.id, so we use Adrian Ramsay (5320, co-leader and current
    # Green MP) as the fallback.
    44: (5320, "Leader of the Green Party"),
    # Reform UK (1036) — Nigel Farage
    1036: (5091, "Leader of Reform UK"),
}


def create_party_leaders_table(conn):
    """Create the party_leaders table if it doesn't exist.

    Schema must match Room's PartyLeaderEntity exactly.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS `party_leaders` (
            `partyId` INTEGER NOT NULL,
            `memberId` INTEGER NOT NULL,
            `title` TEXT NOT NULL,
            PRIMARY KEY(`partyId`)
        );
        """
    )
    conn.commit()


def _matches_leader_title(post_title):
    """Check if a post title matches any LEADER_TITLES entry (case-insensitive contains)."""
    if not post_title:
        return None
    lower = post_title.lower()
    for leader_title in LEADER_TITLES:
        if leader_title.lower() in lower:
            return leader_title
    return None


def build_party_leaders(conn):
    """Identify party leaders from bio_data postsJson.

    Reads bio_data JOINed with mps (to get partyId). For each MP, parses
    postsJson and checks if any post title matches a LEADER_TITLES entry.
    If matched, inserts (partyId, memberId, title) into party_leaders.

    After scanning bio_data, applies HARDCODED_LEADERS fallback for any
    major party that has no leader in the table yet.
    """
    cursor = conn.cursor()

    # bio_data uses mpId (not memberId) and postsJson (not governmentPosts).
    # JOIN with mps to get partyId. mps.id = bio_data.mpId.
    try:
        cursor.execute("""
            SELECT bd.mpId, m.partyId, bd.postsJson
            FROM bio_data bd
            JOIN mps m ON bd.mpId = m.id
        """)
        rows = cursor.fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("Could not query bio_data/mps: %s — using hardcoded fallback only", e)
        rows = []

    logger.info("Scanning %d bio_data records for leader posts", len(rows))

    found_parties = set()
    leaders_found = 0

    for mp_id, party_id, posts_json in rows:
        if not posts_json or not party_id:
            continue
        try:
            posts = json.loads(posts_json)
        except (json.JSONDecodeError, TypeError):
            continue

        for post in posts:
            title = post.get("title", "")
            matched = _matches_leader_title(title)
            if matched:
                cursor.execute(
                    """INSERT OR REPLACE INTO party_leaders (partyId, memberId, title)
                       VALUES (?, ?, ?)""",
                    (party_id, mp_id, matched),
                )
                found_parties.add(party_id)
                leaders_found += 1
                logger.info("Found leader: party %d → MP %d (%s)", party_id, mp_id, matched)
                break  # one leader per party

    conn.commit()
    logger.info("Found %d leaders from bio_data (%d parties)", leaders_found, len(found_parties))

    # Apply hardcoded fallback for major parties without a leader
    fallback_used = 0
    for party_id, (member_id, title) in HARDCODED_LEADERS.items():
        if party_id not in found_parties:
            cursor.execute(
                """INSERT OR REPLACE INTO party_leaders (partyId, memberId, title)
                   VALUES (?, ?, ?)""",
                (party_id, member_id, title),
            )
            fallback_used += 1
            logger.info("Fallback leader: party %d → MP %d (%s)", party_id, member_id, title)

    conn.commit()
    logger.info("Used hardcoded fallback for %d parties", fallback_used)

    # Verify
    cursor.execute("SELECT COUNT(*) FROM party_leaders")
    total = cursor.fetchone()[0]
    logger.info("Done: party_leaders has %d rows", total)
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Post-merge build: produces party_leaders table from MNIS bio_data (D-07)."
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
        logger.info("Creating party_leaders table...")
        create_party_leaders_table(conn)

        # Clear existing party_leaders before repopulating
        conn.execute("DELETE FROM party_leaders")
        conn.commit()

        logger.info("Building party leaders from bio_data...")
        build_party_leaders(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
