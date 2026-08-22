#!/usr/bin/env python3
"""Build script for debate transcripts.

Fetches debate pages from TheyWorkForYou for each division that has a
twfyDebateUrl, parses the HTML to extract individual speeches (speaker
name, speech text, intervention flag), matches speaker names to Parliament
member IDs from the MPs DB, and stores everything in a debates.db with
only the debate_speeches table.

The twfyDebateUrl column is populated by build_commons_votes.py and
build_lords_votes.py (which scrape the TWFY division page to get the
debate GID link). This script reads those URLs from the per-API votes
DBs and fetches the debate pages.

Modes:
  seed  — create fresh DB, fetch all debate pages
  delta — copy previous DB, fetch only NEW debate pages (divisions not
          already in the DB)

Usage:
  python build_debates.py --output debates.db --schema schemas/bundled_schema.json --mode seed
  python build_debates.py --output debates.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_debates.db

  Required: --commons-db commons_votes.db --lords-db lords_votes.db --mps-db mps.db
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time

from bs4 import BeautifulSoup

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

TABLE_NAMES = ["debate_speeches"]


# --- Name matching ---

# Honorifics that the Parliament API includes but TWFY strips.
# Must handle multi-word titles (e.g. "Mrs Kemi" → "Kemi").
_HONORIFICS = {
    "mr", "mrs", "ms", "miss", "sir", "dame", "dr", "lord", "lady",
    "baroness", "earl", "viscount", "rt", "hon", "right", "rev",
    "reverend", "father", "fr",
}


def _strip_honorifics(name):
    """Strip leading honorifics from a name.

    "Ms Diane Abbott" → "Diane Abbott"
    "Sir Lindsay Hoyle" → "Lindsay Hoyle"
    "Rt Hon Dame Meg Hillier" → "Meg Hillier"
    """
    if not name:
        return ""
    words = name.split()
    while words and words[0].lower().rstrip(".") in _HONORIFICS:
        words.pop(0)
    return " ".join(words)


def build_name_lookup(mps_db_path):
    """Build a name → memberId lookup from the MPs DB.

    Matches on nameDisplayAs (e.g. "Liz Kendall") and nameListAs
    (e.g. "Kendall, Liz"). Returns a dict mapping lowercase name → memberId.

    Honorifics (Ms, Mrs, Sir, Dame, etc.) are stripped from both the DB
    names and the TWFY speaker names, since TWFY omits them but the
    Parliament API includes them.
    """
    if not os.path.exists(mps_db_path):
        logger.warning("MPs DB not found: %s — no name matching", mps_db_path)
        return {}

    conn = sqlite3.connect(mps_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nameDisplayAs, nameListAs FROM mps WHERE house = 1")
    lookup = {}
    for row in cursor.fetchall():
        mp_id, display_name, list_name = row
        if display_name:
            stripped = _strip_honorifics(display_name)
            lookup[stripped.lower().strip()] = mp_id
        if list_name:
            # Also try reversed format: "Abbott, Ms Diane" → "Diane Abbott"
            # Strip honorifics from the reversed name too
            parts = list_name.split(", ")
            if len(parts) == 2:
                reversed_name = f"{parts[1]} {parts[0]}"
                stripped = _strip_honorifics(reversed_name)
                lookup[stripped.lower().strip()] = mp_id
    conn.close()

    logger.info("Built name lookup: %d MPs", len(lookup))
    return lookup


def build_twfy_id_lookup(historical_members_db_path):
    """Build a twfy_person_id → parliament_member_id lookup from the historical_members DB.

    This is the primary matching mechanism — the debate scraper extracts
    twfy_person_id from TWFY HTML, and this lookup maps it directly to the
    Parliament member ID without name matching.

    Returns an empty dict if the DB is not found (falls back to name matching).
    """
    if not os.path.exists(historical_members_db_path):
        logger.warning("Historical members DB not found: %s — falling back to name matching only",
                       historical_members_db_path)
        return {}

    conn = sqlite3.connect(historical_members_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT twfyPersonId, parliamentMemberId FROM historical_members WHERE parliamentMemberId IS NOT NULL")
    lookup = {}
    for row in cursor.fetchall():
        twfy_id, parl_id = row
        lookup[twfy_id] = parl_id
    conn.close()

    logger.info("Built TWFY ID lookup: %d mappings", len(lookup))
    return lookup


def match_speaker(name, lookup):
    """Match a speaker name to a Parliament member ID.

    Returns 0 if no match found.
    """
    if not name:
        return 0
    key = name.lower().strip()
    return lookup.get(key, 0)


# --- Debate page parsing ---

def parse_debate_page(html, debate_gid, division_id, name_lookup, timestamp_millis, twfy_id_lookup=None):
    """Parse a TWFY debate page and return a list of debate_speeches rows.

    Each row is a tuple matching the debate_speeches table schema:
    (debateGid, speechGid, divisionId, speakerName, memberId, twfyPersonId,
     speakerPosition, speechText, speechOrder, isIntervention, lastUpdated)
    """
    soup = BeautifulSoup(html, "html.parser")
    speeches = soup.find_all("div", class_="debate-speech")

    rows = []
    order = 0
    for speech in speeches:
        gid = speech.get("id", "")

        # Speaker info
        speaker_el = speech.find("h2", class_="debate-speech__speaker")
        name = ""
        twfy_person_id = 0
        position = ""

        if speaker_el:
            name_el = speaker_el.find("strong", class_="debate-speech__speaker__name")
            if name_el:
                name = name_el.get_text(strip=True)

            pos_el = speaker_el.find("small", class_="debate-speech__speaker__position")
            if pos_el:
                position = pos_el.get_text(strip=True)

            link = speaker_el.find("a", href=True)
            if link:
                href = link["href"]
                mp_match = re.search(r"/mp/\?p=(\d+)", href)
                lord_match = re.search(r"/peer/\?p=(\d+)", href)
                if mp_match:
                    twfy_person_id = int(mp_match.group(1))
                elif lord_match:
                    twfy_person_id = int(lord_match.group(1))

        # Content
        content_el = speech.find("div", class_="debate-speech__content")
        content = ""
        if content_el:
            content = content_el.get_text(separator=" ", strip=True)

        # Skip empty speeches (procedural blocks with no speaker and no content)
        if not name and not content:
            continue

        # Check if intervention
        parent_div = speech.find("div", class_=re.compile("intervention"))
        is_intervention = 1 if parent_div else 0

        # Match speaker to Parliament member ID
        # Primary: TWFY person ID → Parliament member ID (direct lookup)
        # Fallback: name matching against current MPs
        if twfy_id_lookup and twfy_person_id > 0:
            member_id = twfy_id_lookup.get(twfy_person_id, 0)
            if member_id == 0:
                # TWFY ID not in historical members — try name matching
                member_id = match_speaker(name, name_lookup)
        else:
            member_id = match_speaker(name, name_lookup)

        rows.append((
            debate_gid,
            gid,
            division_id,
            name,
            member_id,
            twfy_person_id,
            position,
            content,
            order,
            is_intervention,
            timestamp_millis,
        ))
        order += 1

    return rows


# --- Debate URL extraction ---

def get_debate_urls_from_votes_db(db_path, house):
    """Read divisions from a votes DB and return a list of
    (divisionId, twfyDebateUrl) tuples, filtering by house.

    Only divisions with a non-null twfyDebateUrl are returned.
    Deduplicates by twfyDebateUrl — multiple divisions may share the
    same debate page.
    """
    if not os.path.exists(db_path):
        logger.warning("Votes DB not found: %s", db_path)
        return []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if twfyDebateUrl column exists
    cursor.execute("PRAGMA table_info(divisions)")
    columns = [col[1] for col in cursor.fetchall()]
    if "twfyDebateUrl" not in columns:
        logger.warning("twfyDebateUrl column not in %s — skipping", db_path)
        conn.close()
        return []

    cursor.execute(
        "SELECT id, twfyDebateUrl FROM divisions WHERE house = ? AND twfyDebateUrl IS NOT NULL",
        (house,),
    )
    results = cursor.fetchall()
    conn.close()

    return [(row[0], row[1]) for row in results]


def extract_debate_gid(twfy_url):
    """Extract the debate GID from a TWFY debate URL.

    URL format: https://www.theyworkforyou.com/debates/?gid=2025-07-01b.159.0#g246.0
    or:         https://www.theyworkforyou.com/lords/?gid=2026-07-22b.1188.0#g1202.0

    Returns: (debate_gid, full_url_without_fragment)
    """
    # Extract gid parameter
    match = re.search(r"[?&]gid=([^&#]+)", twfy_url)
    if not match:
        return None, twfy_url
    return match.group(1), twfy_url.split("#")[0]


# --- Build modes ---

def build_seed(output_path, schema_path, commons_db, lords_db, mps_db,
               divisions_limit=None, checkpoint_db=None, historical_members_db=None,
               max_debates=None):
    """Seed mode: fetch all debate pages and build a fresh debates.db.

    If max_debates is set, processes at most that many new debates before
    saving the checkpoint DB and returning. Returns True if more debates
    remain to be processed (caller should exit with code 2 so the CI chain
    can spawn the next batch).
    """
    timestamp_millis = int(time.time() * 1000)

    # Build name lookup for MP matching
    name_lookup = build_name_lookup(mps_db)

    # Build TWFY ID lookup for direct speaker matching
    twfy_id_lookup = build_twfy_id_lookup(historical_members_db) if historical_members_db else {}

    # Collect all debate URLs from both votes DBs
    commons_urls = get_debate_urls_from_votes_db(commons_db, 1)
    lords_urls = get_debate_urls_from_votes_db(lords_db, 2)

    logger.info("Commons debate URLs: %d", len(commons_urls))
    logger.info("Lords debate URLs: %d", len(lords_urls))

    # Deduplicate by debate GID — multiple divisions may share the same debate
    all_divisions = commons_urls + lords_urls
    seen_gids = {}
    unique_debates = []
    for division_id, url in all_divisions:
        gid, clean_url = extract_debate_gid(url)
        if gid and gid not in seen_gids:
            seen_gids[gid] = (division_id, clean_url)
            unique_debates.append((gid, division_id, clean_url))

    logger.info("Unique debates: %d (from %d divisions)",
                len(unique_debates), len(all_divisions))

    if divisions_limit:
        unique_debates = unique_debates[:divisions_limit]
        logger.info("Limited to %d debates", len(unique_debates))

    # If checkpoint DB exists, skip debates already processed
    existing_gids = set()
    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT debateGid FROM debate_speeches")
        existing_gids = {row[0] for row in cursor.fetchall()}
        logger.info("Checkpoint: %d debates already processed", len(existing_gids))
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )

    cursor = conn.cursor()

    # Map each division to its debate GID (for the divisionId column)
    # We store speeches per-division, so if multiple divisions share a
    # debate, we fetch the page once and insert speeches for each division.
    division_to_debate = {}
    for division_id, url in all_divisions:
        gid, clean_url = extract_debate_gid(url)
        if gid:
            division_to_debate.setdefault(gid, []).append((division_id, clean_url))

    processed = 0
    more_work = False
    for gid, primary_division_id, clean_url in unique_debates:
        if gid in existing_gids:
            continue

        # Get all divisions that share this debate
        divisions_for_debate = division_to_debate.get(gid, [(primary_division_id, clean_url)])

        # Fetch the debate page
        try:
            r = api_get(clean_url, timeout=60, max_retries=2)
        except Exception as e:
            logger.warning("Failed to fetch debate %s: %s", clean_url, e)
            continue

        # Parse and insert speeches for each division sharing this debate
        for division_id, _ in divisions_for_debate:
            rows = parse_debate_page(
                r.text, gid, division_id, name_lookup, timestamp_millis, twfy_id_lookup,
            )
            if rows:
                for i in range(0, len(rows), BATCH_SIZE):
                    batch = rows[i:i + BATCH_SIZE]
                    cursor.executemany(
                        """INSERT OR REPLACE INTO debate_speeches
                           (debateGid, speechGid, divisionId, speakerName,
                            memberId, twfyPersonId, speakerPosition,
                            speechText, speechOrder, isIntervention,
                            lastUpdated)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        batch,
                    )
                conn.commit()

        processed += 1
        matched = sum(1 for r in rows if r[4] > 0) if rows else 0
        logger.info(
            "Debate %d/%d: gid=%s, %d speeches (%d matched) — %d divisions",
            processed, len(unique_debates), gid, len(rows), matched,
            len(divisions_for_debate),
        )

        time.sleep(API_DELAY)

        # Stop after processing max_debates new debates — checkpoint is
        # already saved (committed per-debate above), so the CI chain can
        # resume from this DB in the next batch job.
        if max_debates and processed >= max_debates:
            more_work = True
            break

    conn.commit()
    logger.info("VACUUMing database...")
    conn.execute("VACUUM")
    conn.close()

    if more_work:
        logger.info("Reached max-debates limit (%d processed). Checkpoint saved to %s",
                    processed, output_path)
    else:
        logger.info("Seed build complete: %s (%d debates processed)", output_path, processed)

    return more_work


def build_delta(output_path, previous_db, schema_path, commons_db, lords_db, mps_db,
                divisions_limit=None, historical_members_db=None):
    """Delta mode: fetch only NEW debate pages not in the previous DB."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    conn = sqlite3.connect(output_path)
    cursor = conn.cursor()

    # Get existing debate GIDs
    cursor.execute("SELECT DISTINCT debateGid FROM debate_speeches")
    existing_gids = {row[0] for row in cursor.fetchall()}
    logger.info("Delta: %d debates already in DB", len(existing_gids))

    # Build name lookup
    name_lookup = build_name_lookup(mps_db)

    # Build TWFY ID lookup for direct speaker matching
    twfy_id_lookup = build_twfy_id_lookup(historical_members_db) if historical_members_db else {}

    # Collect all debate URLs
    commons_urls = get_debate_urls_from_votes_db(commons_db, 1)
    lords_urls = get_debate_urls_from_votes_db(lords_db, 2)
    all_divisions = commons_urls + lords_urls

    # Find new debates
    division_to_debate = {}
    new_debates = []
    for division_id, url in all_divisions:
        gid, clean_url = extract_debate_gid(url)
        if gid:
            division_to_debate.setdefault(gid, []).append((division_id, clean_url))
            if gid not in existing_gids:
                new_debates.append((gid, division_id, clean_url))

    # Deduplicate
    seen = set()
    unique_new = []
    for gid, div_id, url in new_debates:
        if gid not in seen:
            seen.add(gid)
            unique_new.append((gid, div_id, url))

    logger.info("New debates to fetch: %d", len(unique_new))

    if divisions_limit:
        unique_new = unique_new[:divisions_limit]

    processed = 0
    for gid, primary_division_id, clean_url in unique_new:
        divisions_for_debate = division_to_debate.get(gid, [(primary_division_id, clean_url)])

        try:
            r = api_get(clean_url, timeout=60, max_retries=2)
        except Exception as e:
            logger.warning("Failed to fetch debate %s: %s", clean_url, e)
            continue

        for division_id, _ in divisions_for_debate:
            rows = parse_debate_page(
                r.text, gid, division_id, name_lookup, timestamp_millis, twfy_id_lookup,
            )
            if rows:
                for i in range(0, len(rows), BATCH_SIZE):
                    batch = rows[i:i + BATCH_SIZE]
                    cursor.executemany(
                        """INSERT OR REPLACE INTO debate_speeches
                           (debateGid, speechGid, divisionId, speakerName,
                            memberId, twfyPersonId, speakerPosition,
                            speechText, speechOrder, isIntervention,
                            lastUpdated)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        batch,
                    )
                conn.commit()

        processed += 1
        matched = sum(1 for r in rows if r[4] > 0) if rows else 0
        logger.info(
            "Delta %d/%d: gid=%s, %d speeches (%d matched)",
            processed, len(unique_new), gid, len(rows), matched,
        )

        time.sleep(API_DELAY)

    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    logger.info("Delta build complete: %s (%d new debates)", output_path, processed)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye debate transcripts DB (debates.db)."
    )
    parser.add_argument(
        "--output", default="debates.db",
        help="Output path for the SQLite DB file.",
    )
    parser.add_argument(
        "--schema", required=True,
        help="Path to the Room exported schema JSON (bundled_schema.json).",
    )
    parser.add_argument(
        "--mode", choices=["seed", "delta"], default="seed",
        help="Build mode: seed (full) or delta (incremental).",
    )
    parser.add_argument(
        "--previous-db",
        help="Path to previous DB file (required for delta mode).",
    )
    parser.add_argument(
        "--commons-db", required=True,
        help="Path to commons_votes.db (for reading twfyDebateUrl).",
    )
    parser.add_argument(
        "--lords-db", required=True,
        help="Path to lords_votes.db (for reading twfyDebateUrl).",
    )
    parser.add_argument(
        "--mps-db", required=True,
        help="Path to mps.db (for name matching).",
    )
    parser.add_argument(
        "--divisions-limit", type=int, default=None,
        help="Limit number of debates fetched (for testing).",
    )
    parser.add_argument(
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only).",
    )
    parser.add_argument(
        "--max-debates", type=int, default=None,
        help="Maximum number of new debates to process per run (seed mode only). "
             "When the limit is reached, the checkpoint DB is saved and the script "
             "exits with code 2 to signal that more work remains.",
    )
    parser.add_argument(
        "--historical-members-db",
        help="Path to historical_members.db (for TWFY person ID → Parliament member ID matching).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        more_work = build_seed(
            args.output, args.schema,
            commons_db=args.commons_db,
            lords_db=args.lords_db,
            mps_db=args.mps_db,
            divisions_limit=args.divisions_limit,
            checkpoint_db=args.checkpoint_db,
            historical_members_db=args.historical_members_db,
            max_debates=args.max_debates,
        )
        if more_work:
            sys.exit(2)
    else:
        build_delta(
            args.output, args.previous_db, args.schema,
            commons_db=args.commons_db,
            lords_db=args.lords_db,
            mps_db=args.mps_db,
            divisions_limit=args.divisions_limit,
            historical_members_db=args.historical_members_db,
        )


if __name__ == "__main__":
    main()
