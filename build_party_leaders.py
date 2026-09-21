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
    15: (1427, "Prime Minister", "2026-07-20"),
    # Conservative (4) — Kemi Badenoch, Leader of the Opposition
    4: (4597, "Leader of the Opposition", "2024-11-02"),
    # Liberal Democrats (17) — Ed Davey
    17: (188, "Leader of the Liberal Democrats", "2019-12-22"),
    # SNP (29) — Pete Wishart (Westminster leader; Stephen Flynn not in mps
    # table; John Swinney is an MSP not an MP)
    29: (1440, "Leader of the Scottish National Party", "2024-07-10"),
    # DUP (7) — Gavin Robinson
    7: (4360, "Leader of the Democratic Unionist Party", "2024-02-03"),
    # Plaid Cymru (22) — Liz Saville Roberts (Westminster leader; Rhun ap
    # Iorwerth is the Senedd leader, not an MP)
    22: (4521, "Leader of Plaid Cymru", "2017-06-08"),
    # Green Party (44) — Zack Polanski is the current leader (since Sept 2025)
    # but is NOT an MP (London Assembly Member). The party_leaders table
    # references mps.id, so we use Adrian Ramsay (5320, co-leader and current
    # Green MP) as the fallback.
    44: (5320, "Leader of the Green Party", "2021-09-04"),
    # Reform UK (1036) — Nigel Farage
    1036: (5091, "Leader of Reform UK", "2024-06-11"),
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
            `leaderSinceDate` TEXT,
            PRIMARY KEY(`partyId`)
        );
        """
    )
    conn.commit()


# Real-world title variants that should map to a canonical LEADER_TITLES entry.
# MNIS uses "Leader of HM Official Opposition" — not "Leader of the Opposition".
LEADER_TITLE_ALIASES = {
    "leader of hm official opposition": "Leader of the Opposition",
    "leader of the official opposition": "Leader of the Opposition",
    "leader of the opposition": "Leader of the Opposition",
}

# Words that disqualify a title even if it starts with / equals a leader title.
# Guards against e.g. a sitting "Deputy Prime Minister" or "Acting Leader".
LEADER_DISQUALIFIERS = (
    "shadow", "deputy", "spokesperson", "whip", "secretary",
    "minister for", "minister of state", "parliamentary", "office of",
    "policy unit", "assistant", "junior", "acting", "interim",
)


def _matches_leader_title(post_title):
    """Match a CURRENT post title to a canonical leader title.

    Deliberately strict: the leader title must appear at the START of the post
    title (covers "Prime Minister and First Lord of the Treasury"), or the
    title must be a known alias. A loose substring contains-match is wrong —
    it matched "Chief Secretary to the Prime Minister" (Darren Jones),
    "…Head of the Prime Minister's Policy Unit" (Andrew Griffith) and
    "Spokesperson (Office of the Deputy Prime Minister)" (Ed Davey), putting a
    false 'Prime Minister' row on three parties.
    """
    if not post_title:
        return None
    lower = post_title.strip().lower()
    if lower in LEADER_TITLE_ALIASES:
        return LEADER_TITLE_ALIASES[lower]
    for leader_title in LEADER_TITLES:
        lt = leader_title.lower()
        if lower == lt or lower.startswith(lt + " ") or lower.startswith(lt + " ("):
            if not any(d in lower for d in LEADER_DISQUALIFIERS):
                return leader_title
    return None


# When two MPs of the same party both hold current leader-type posts, prefer
# the more senior title (PM > Opposition leader > party leader), then the
# most recent startDate.
_TITLE_PRIORITY = {"Prime Minister": 0, "Leader of the Opposition": 1}


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

    # Collect candidates first — INSERT OR REPLACE mid-scan let the LAST
    # matching MP win per party regardless of seniority. Rank instead.
    # party_id -> (priority, startDate, mpId, title)
    candidates = {}

    for mp_id, party_id, posts_json in rows:
        if not posts_json or not party_id:
            continue
        try:
            posts = json.loads(posts_json)
        except (json.JSONDecodeError, TypeError):
            continue

        for post in posts:
            # Only CURRENT posts count — a former post makes the holder a
            # former leader, not the leader. Historical substring matches
            # were the source of the 2026-09 corruption (three parties all
            # got a 'Prime Minister' row from ended junior-minister posts).
            if post.get("endDate"):
                continue
            matched = _matches_leader_title(post.get("title", ""))
            if matched:
                priority = _TITLE_PRIORITY.get(matched, 2)
                start = post.get("startDate") or ""
                cur = candidates.get(party_id)
                # Higher priority wins; on a tie, latest startDate wins.
                if cur is None or priority < cur[0] or (priority == cur[0] and start > cur[1]):
                    candidates[party_id] = (priority, start, mp_id, matched)
                break  # one leader post per MP is enough

    leaders_found = 0
    for party_id, (_, _, mp_id, matched) in candidates.items():
        cursor.execute(
            """INSERT OR REPLACE INTO party_leaders (partyId, memberId, title)
               VALUES (?, ?, ?)""",
            (party_id, mp_id, matched),
        )
        leaders_found += 1
        logger.info("Found leader: party %d → MP %d (%s)", party_id, mp_id, matched)

    # Populate leaderSinceDate from the winning post's startDate.
    for party_id, (_, start_date, mp_id, _) in candidates.items():
        if start_date:
            cursor.execute(
                "UPDATE party_leaders SET leaderSinceDate = ? WHERE partyId = ? AND leaderSinceDate IS NULL",
                (start_date, party_id),
            )

    found_parties = set(candidates)
    conn.commit()
    logger.info("Found %d leaders from bio_data (%d parties)", leaders_found, len(found_parties))

    # Apply hardcoded fallback for major parties without a leader
    fallback_used = 0
    for party_id, (member_id, title, since) in HARDCODED_LEADERS.items():
        if party_id not in found_parties:
            cursor.execute(
                """INSERT OR REPLACE INTO party_leaders
                   (partyId, memberId, title, leaderSinceDate)
                   VALUES (?, ?, ?, ?)""",
                (party_id, member_id, title, since),
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
