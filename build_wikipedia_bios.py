#!/usr/bin/env python3
"""Build Wikipedia biography extracts for all UK MPs.

Queries the English Wikipedia API for each MP's intro extract,
which is a 1-3 paragraph summary of their life and career — much
richer than the MNIS synopsis ("X is the Y MP for Z, since date").

The extract is stored in the mp_synopsis table, replacing the thin
MNIS synopsis. If Wikipedia has no article for an MP, the original
MNIS synopsis is kept.

Usage:
    python build_wikipedia_bios.py --output wiki_bios.db --schema <schema.json> --mps-db mps.db --goveye-db goveye.db

The output DB has a single table:
    wiki_bios(mpId INTEGER PRIMARY KEY, bioText TEXT, wikipediaUrl TEXT, lastUpdated INTEGER)
"""
import argparse
import json
import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
BATCH_SIZE = 20  # Wikipedia API allows up to 20 titles per request
RATE_LIMIT_DELAY = 1.0  # seconds between batch requests

# Honorifics to strip from MP display names when constructing Wikipedia titles.
# Wikipedia article titles generally don't include these prefixes.
HONORIFICS = [
    "Rt Hon ", "Right Honourable ", "Ms ", "Mr ", "Mrs ", "Miss ",
    "Sir ", "Dame ", "Dr ", "Rev ", "Reverend ",
]


def strip_honorifics(name: str) -> str:
    """Strip honorific prefixes from an MP display name."""
    result = name.strip()
    for honorific in HONORIFICS:
        if result.startswith(honorific):
            result = result[len(honorific):]
    return result.strip()


def fetch_wikipedia_extracts(titles: list[str]) -> dict[str, dict]:
    """Fetch intro extracts for a batch of Wikipedia titles.

    Returns a dict mapping the original input title to:
        { 'extract': str, 'url': str, 'resolved_title': str }
    """
    if not titles:
        return {}

    titles_str = "|".join(titles)
    params = urllib.parse.urlencode({
        "action": "query",
        "titles": titles_str,
        "prop": "extracts|info",
        "exintro": "1",
        "explaintext": "1",
        "inprop": "url",
        "format": "json",
        "redirects": "1",
    })
    url = f"{WIKIPEDIA_API}?{params}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "GovEye/1.0 (https://goveye.app; contact@goveye.app)",
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.error("Wikipedia API error: %s", e)
        return {}

    # Build redirect map: normalized input title -> resolved title
    redirects = {}
    for r in data.get("query", {}).get("redirects", []):
        redirects[r["from"]] = r["to"]

    # Build normalized title map
    normalized = {}
    for n in data.get("query", {}).get("normalized", []):
        normalized[n["from"]] = n["to"]

    result = {}
    pages = data.get("query", {}).get("pages", {})
    # Build a map from resolved title -> page data
    title_to_page = {}
    for pid, page in pages.items():
        title_to_page[page.get("title", "")] = page

    for original_title in titles:
        # Follow normalization + redirect chain
        resolved = normalized.get(original_title, original_title)
        resolved = redirects.get(resolved, resolved)

        page = title_to_page.get(resolved)
        if page is None or "missing" in page:
            result[original_title] = None
            continue

        extract = page.get("extract", "")
        page_url = page.get("fullurl", "")
        result[original_title] = {
            "extract": extract,
            "url": page_url,
            "resolved_title": resolved,
        }

    return result


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _extract_dob(text: str) -> str | None:
    """Extract a birth date from a Wikipedia intro extract.

    Handles "born 27 September 1953" and "born September 27, 1953".
    Returns YYYY-MM-DD or None. Rejects implausible years (MPs must be
    adults, so born before ~2008 and after ~1930).
    """
    m = re.search(r"born(?:\s+on)?\s+(\d{1,2})\s+(\w+)\s+(\d{4})", text, re.IGNORECASE)
    if not m:
        m = re.search(r"born(?:\s+on)?\s+(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text, re.IGNORECASE)
        if not m:
            return None
        month_name, day, year = m.group(1), m.group(2), m.group(3)
    else:
        day, month_name, year = m.group(1), m.group(2), m.group(3)
    month = _MONTHS.get(month_name.lower())
    if not month:
        return None
    year_i = int(year)
    if year_i < 1930 or year_i > 2008:
        return None
    return f"{year_i:04d}-{month:02d}-{int(day):02d}"


def build_wiki_bios(output_db: str, mps_db: str, goveye_db: str, inplace: bool = False,
                    dob_only: bool = False):
    """Build the wiki_bios database from Wikipedia extracts.

    If inplace=True, merges the Wikipedia bios directly into goveye.db,
    updating mp_synopsis.synopsisText and mp_links.wikipediaUrl.

    If dob_only=True, only fills bio_data.dateOfBirth for MPs that lack one
    (used by update-bio-data.yml to enrich bio_data.db after build_mnis.py).
    In that mode --goveye-db is the target DB (any DB containing a bio_data
    table) and only MPs with NULL dateOfBirth are fetched.
    """
    # Read MP IDs and names from mps.db
    mps_conn = sqlite3.connect(mps_db)
    mps_conn.row_factory = sqlite3.Row
    mp_rows = mps_conn.execute(
        "SELECT id, nameDisplayAs, nameListAs FROM mps ORDER BY id"
    ).fetchall()
    mps_conn.close()
    logger.info("Read %d MPs from %s", len(mp_rows), mps_db)

    # Read existing MNIS synopses from goveye.db (to keep them as fallback)
    goveye_conn = sqlite3.connect(goveye_db)
    goveye_conn.row_factory = sqlite3.Row
    if dob_only:
        existing_synopses = {}
    else:
        existing_synopses = {
            row["mpId"]: row["synopsisText"]
            for row in goveye_conn.execute("SELECT mpId, synopsisText FROM mp_synopsis").fetchall()
        }

    if dob_only:
        # Only fetch Wikipedia for MPs still missing a DOB — MNIS-derived
        # DOBs (2+ PreferredNames) always win and never get re-fetched.
        missing_dob_ids = {
            row["mpId"]
            for row in goveye_conn.execute(
                "SELECT mpId FROM bio_data WHERE dateOfBirth IS NULL OR dateOfBirth = ''"
            ).fetchall()
        }
        mp_rows = [mp for mp in mp_rows if mp["id"] in missing_dob_ids]
        logger.info("DOB-only mode: %d MPs still missing dateOfBirth", len(mp_rows))
    goveye_conn.close()
    if not dob_only:
        logger.info("Read %d existing synopses from %s", len(existing_synopses), goveye_db)

    if dob_only:
        # Write DOBs directly into the target DB (bio_data.db or goveye.db).
        out_conn = sqlite3.connect(goveye_db)
        logger.info("DOB-only mode — writing to bio_data in %s", goveye_db)
    elif inplace:
        # Write directly into goveye.db
        out_conn = sqlite3.connect(goveye_db)
        logger.info("Inplace mode — merging Wikipedia bios directly into %s", goveye_db)
    else:
        # Create output DB
        out_conn = sqlite3.connect(output_db)
        out_conn.execute("""
            CREATE TABLE IF NOT EXISTS wiki_bios (
                mpId INTEGER PRIMARY KEY,
                bioText TEXT,
                wikipediaUrl TEXT,
                lastUpdated INTEGER NOT NULL
            )
        """)
        out_conn.commit()

    timestamp = int(time.time() * 1000)
    fetched = 0
    skipped = 0

    # Process in batches
    for i in range(0, len(mp_rows), BATCH_SIZE):
        batch = mp_rows[i:i + BATCH_SIZE]
        # Build Wikipedia titles from display names, stripping honorifics
        title_to_mp = {}
        for mp in batch:
            wiki_title = strip_honorifics(mp["nameDisplayAs"])
            title_to_mp[wiki_title] = mp["id"]

        titles = list(title_to_mp.keys())
        logger.info("Fetching batch %d-%d (%d titles)", i, min(i + BATCH_SIZE, len(mp_rows)), len(titles))

        results = fetch_wikipedia_extracts(titles)

        for title, mp_id in title_to_mp.items():
            result = results.get(title)
            if result is None or not result["extract"]:
                skipped += 1
                logger.debug("  No Wikipedia article for MP %d (%s)", mp_id, title)
                continue

            # Clean the extract — remove any remaining HTML tags
            bio_text = re.sub(r"<[^>]+>", "", result["extract"]).strip()

            # Skip very short extracts (likely disambiguation pages)
            if len(bio_text) < 100:
                skipped += 1
                logger.debug("  Short extract for MP %d (%s): %d chars", mp_id, title, len(bio_text))
                continue

            # Skip Wikipedia disambiguation pages — these contain "may refer to"
            # and list unrelated people/things instead of a real biography.
            # This catches disambiguation pages that are longer than 100 chars
            # (e.g. "Jack Abbott may refer to Jack Henry Abbott, American
            # criminal; Jack Abbott, American college football coach; ...")
            bio_lower = bio_text.lower()
            if "may refer to" in bio_lower or "usually refers to" in bio_lower or "can refer to" in bio_lower:
                skipped += 1
                logger.debug("  Disambiguation page for MP %d (%s): %s", mp_id, title, bio_text[:80])
                continue

            if dob_only:
                # Only fill DOB — don't touch synopsis/links.
                dob = _extract_dob(bio_text)
                if dob:
                    out_conn.execute(
                        "UPDATE bio_data SET dateOfBirth = ? WHERE mpId = ? AND (dateOfBirth IS NULL OR dateOfBirth = '')",
                        (dob, mp_id),
                    )
            elif inplace:
                # Update mp_synopsis with the richer Wikipedia extract
                out_conn.execute(
                    "UPDATE mp_synopsis SET synopsisText = ? WHERE mpId = ?",
                    (bio_text, mp_id),
                )
                # Update mp_links with the Wikipedia URL
                out_conn.execute(
                    "UPDATE mp_links SET wikipediaUrl = ? WHERE mpId = ?",
                    (result["url"], mp_id),
                )
                # Fill dateOfBirth where MNIS has none — the intro extract
                # almost always contains "born 27 September 1953". Only fills
                # NULLs so an MNIS-derived DOB (2+ PreferredNames) always wins.
                dob = _extract_dob(bio_text)
                if dob:
                    out_conn.execute(
                        "UPDATE bio_data SET dateOfBirth = ? WHERE mpId = ? AND (dateOfBirth IS NULL OR dateOfBirth = '')",
                        (dob, mp_id),
                    )
            else:
                out_conn.execute(
                    "INSERT OR REPLACE INTO wiki_bios (mpId, bioText, wikipediaUrl, lastUpdated) VALUES (?, ?, ?, ?)",
                    (mp_id, bio_text, result["url"], timestamp),
                )
            fetched += 1

        out_conn.commit()
        time.sleep(RATE_LIMIT_DELAY)

    out_conn.close()
    logger.info("Done: fetched %d Wikipedia bios, skipped %d MPs (no article or too short)", fetched, skipped)


def main():
    parser = argparse.ArgumentParser(
        description="Build Wikipedia biography extracts for UK MPs."
    )
    parser.add_argument("--output", default="wiki_bios.db", help="Output SQLite DB path (ignored if --inplace or --dob-only)")
    parser.add_argument("--mps-db", required=True, help="Path to mps.db")
    parser.add_argument("--goveye-db", required=True, help="Path to goveye.db (for existing synopses, or target if --inplace/--dob-only)")
    parser.add_argument("--inplace", action="store_true", help="Merge Wikipedia bios directly into goveye.db")
    parser.add_argument("--dob-only", action="store_true",
                        help="Only fill bio_data.dateOfBirth for MPs missing one (target: --goveye-db). Skips synopsis/links writes.")
    args = parser.parse_args()

    build_wiki_bios(args.output, args.mps_db, args.goveye_db, inplace=args.inplace,
                    dob_only=args.dob_only)


if __name__ == "__main__":
    main()
