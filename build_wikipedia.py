#!/usr/bin/env python3
"""Unified Wikipedia/Wikidata enrichment for GovEye.

One script resolves each MP to a Wikidata item once (via P10428, the
parliament.uk member ID — exact, no name guessing) and applies every
Wikipedia/Wikidata enrichment to the owning per-API DB:

    --bio-db PATH       fill bio_data.dateOfBirth where NULL
                        (Wikidata P569 first, intro-extract regex fallback)
    --synopsis-db PATH  replace mp_synopsis.synopsisText with the Wikipedia
                        intro extract (richer than the MNIS synopsis)
    --links-db PATH     fill/update mp_links.wikipediaUrl from the enwiki
                        sitelink
    --career-db PATH    upsert mp_career_events rows (education P69,
                        occupation P106) with source='wikipedia'

All flags may be combined in one invocation — the Wikidata lookups and
Wikipedia extract fetches are shared. Enrichment lands in per-API DBs so
weekly patches carry it and merge_dbs.py picks it up for the seed.

Replaces: build_ages.py (P569 now fetched directly), build_wikipedia_bios.py
(--dob-only / --inplace modes), build_wikipedia_career.py (entire script).

Usage:
    python build_wikipedia.py --mps-db mps.db --bio-db bio_data.db
    python build_wikipedia.py --mps-db mps.db --synopsis-db member_details.db --career-db member_details.db
    python build_wikipedia.py --mps-db mps.db --links-db mp_links.db
"""

import argparse
import json
import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.request

from api_helper import BATCH_SIZE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_wikipedia")

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "GovEye/1.0 (https://goveye.app; contact@goveye.app)"
SPARQL_DELAY = 0.5
SPARQL_TIMEOUT = 120
QID_LOOKUP_BATCH = 200
STATEMENT_BATCH = 50  # keep small — Wikidata times out on big statement queries
WIKIPEDIA_BATCH = 20  # Wikipedia API allows up to 20 titles per request
RATE_LIMIT_DELAY = 1.0

# mp_career_events.id is AUTOINCREMENT and the table is shared with
# parliament-source rows (built by build_member_details.py). Partition the id
# space: parliament rows below this base, wikipedia rows at/above it.
WIKIPEDIA_ID_BASE = 1_000_000_000

# Honorifics to strip when falling back to name-based title guessing for MPs
# without a Wikidata sitelink.
HONORIFICS = [
    "Rt Hon ", "Right Honourable ", "Ms ", "Mr ", "Mrs ", "Miss ",
    "Sir ", "Dame ", "Dr ", "Rev ", "Reverend ",
]


def strip_honorifics(name: str) -> str:
    result = name.strip()
    for honorific in HONORIFICS:
        if result.startswith(honorific):
            result = result[len(honorific):]
    return result.strip()


def fetch_mp_ids_from_db(mps_db_path):
    """Read (id, nameDisplayAs) for all MPs from mps.db."""
    conn = sqlite3.connect(mps_db_path)
    mp_data = conn.execute("SELECT id, nameDisplayAs FROM mps ORDER BY id").fetchall()
    conn.close()
    logger.info("Read %d MPs from %s", len(mp_data), mps_db_path)
    return mp_data


# --- Wikidata SPARQL ---

def run_sparql_query(query):
    """Execute a SPARQL query with retry/backoff; returns parsed dict or None."""
    url = f"{WIKIDATA_SPARQL_URL}?{urllib.parse.urlencode({'query': query, 'format': 'json'})}"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/sparql-results+json",
            })
            with urllib.request.urlopen(req, timeout=SPARQL_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < 3:
                delay = 10 * (attempt + 1)
                logger.warning("Wikidata transient %d — retrying in %ds", e.code, delay)
                time.sleep(delay)
                continue
            logger.warning("Wikidata SPARQL query failed: %s", e)
            return None
        except Exception as e:
            if attempt < 3:
                delay = 10 * (attempt + 1)
                logger.warning("Wikidata SPARQL error (%s) — retrying in %ds", e, delay)
                time.sleep(delay)
                continue
            logger.warning("Wikidata SPARQL query failed: %s", e)
            return None
    return None


def resolve_qids(mp_data):
    """Map parliament member IDs to Wikidata QIDs via P10428.

    Returns {mp_id: qid}.
    """
    qid_map = {}
    mp_ids = [mp_id for mp_id, _ in mp_data]
    total = len(mp_ids)

    for i in range(0, total, QID_LOOKUP_BATCH):
        batch = mp_ids[i:i + QID_LOOKUP_BATCH]
        values = " ".join(f'"{mid}"' for mid in batch)
        results = run_sparql_query(f"""
SELECT ?item ?parlId WHERE {{
  ?item wdt:P10428 ?parlId.
  VALUES ?parlId {{ {values} }}
}}
""")
        time.sleep(SPARQL_DELAY)
        if results is None:
            logger.warning("  QID lookup batch failed — skipping %d MPs", len(batch))
            continue

        for binding in results.get("results", {}).get("bindings", []):
            qid = binding.get("item", {}).get("value", "").split("/")[-1]
            try:
                qid_map[int(binding.get("parlId", {}).get("value", ""))] = qid
            except (ValueError, TypeError):
                continue
        logger.info("  QID batch %d-%d: %d matched so far", i, min(i + QID_LOOKUP_BATCH, total), len(qid_map))

    logger.info("Matched %d/%d MPs to Wikidata QIDs via P10428", len(qid_map), total)
    return qid_map


def fetch_dobs_and_sitelinks(qids):
    """One SPARQL pass: P569 dateOfBirth + enwiki sitelink title per QID.

    Returns {qid: {"dob": "YYYY-MM-DD"|None, "title": str|None}}.
    """
    result = {qid: {"dob": None, "title": None} for qid in qids}
    for i in range(0, len(qids), QID_LOOKUP_BATCH):
        batch = qids[i:i + QID_LOOKUP_BATCH]
        values = " ".join(f"wd:{q}" for q in batch)
        results = run_sparql_query(f"""
SELECT ?item ?dob ?article WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item wdt:P569 ?dob. }}
  OPTIONAL {{
    ?article schema:about ?item;
             schema:isPartOf <https://en.wikipedia.org/>.
  }}
}}
""")
        time.sleep(SPARQL_DELAY)
        if results is None:
            logger.warning("  DOB/sitelink batch failed — skipping %d QIDs", len(batch))
            continue

        for binding in results.get("results", {}).get("bindings", []):
            qid = binding.get("item", {}).get("value", "").split("/")[-1]
            dob = binding.get("dob", {}).get("value")
            article = binding.get("article", {}).get("value")
            entry = result.setdefault(qid, {"dob": None, "title": None})
            if dob and len(dob) >= 10:
                entry["dob"] = dob.lstrip("+")[:10]
            if article:
                entry["title"] = urllib.parse.unquote(article.rsplit("/", 1)[-1]).replace("_", " ")
        logger.info("  DOB/sitelink batch %d-%d done", i, min(i + QID_LOOKUP_BATCH, len(qids)))

    found = sum(1 for v in result.values() if v["dob"])
    linked = sum(1 for v in result.values() if v["title"])
    logger.info("Wikidata: %d DOBs, %d enwiki sitelinks", found, linked)
    return result


def fetch_career_statements(qids):
    """Fetch education (P69) and occupation (P106) statements for QIDs.

    Returns list of {qid, category, name, start_time, end_time, subject}.
    """
    statements = []
    for i in range(0, len(qids), STATEMENT_BATCH):
        batch = qids[i:i + STATEMENT_BATCH]
        values = " ".join(f"wd:{q}" for q in batch)
        results = run_sparql_query(f"""
SELECT ?item ?prop ?value ?valueLabel ?startTime ?endTime ?subject ?subjectLabel WHERE {{
  VALUES ?item {{ {values} }}
  {{
    ?item p:P69 ?statement.
    ?statement ps:P69 ?value.
    BIND("education" AS ?prop)
    OPTIONAL {{ ?statement pq:P580 ?startTime. }}
    OPTIONAL {{ ?statement pq:P582 ?endTime. }}
    OPTIONAL {{ ?statement pq:P812 ?subject. }}
  }}
  UNION
  {{
    ?item p:P106 ?statement.
    ?statement ps:P106 ?value.
    BIND("occupation" AS ?prop)
    OPTIONAL {{ ?statement pq:P580 ?startTime. }}
    OPTIONAL {{ ?statement pq:P582 ?endTime. }}
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
""")
        time.sleep(SPARQL_DELAY)
        if results is None:
            logger.warning("  Career statements batch failed — skipping %d QIDs", len(batch))
            continue

        for binding in results.get("results", {}).get("bindings", []):
            statements.append({
                "qid": binding.get("item", {}).get("value", "").split("/")[-1],
                "category": binding.get("prop", {}).get("value"),
                "name": binding.get("valueLabel", {}).get("value")
                        or binding.get("value", {}).get("value", "").split("/")[-1],
                "start_time": binding.get("startTime", {}).get("value"),
                "end_time": binding.get("endTime", {}).get("value"),
                "subject": binding.get("subjectLabel", {}).get("value"),
            })
        logger.info("  Career batch %d-%d: %d statements so far",
                    i, min(i + STATEMENT_BATCH, len(qids)), len(statements))

    return statements


# --- Wikipedia extracts ---

def fetch_wikipedia_extracts(titles):
    """Fetch intro extracts for a batch of exact Wikipedia titles.

    Returns {input_title: {"extract": str, "url": str} | None}.
    """
    if not titles:
        return {}
    params = urllib.parse.urlencode({
        "action": "query",
        "titles": "|".join(titles),
        "prop": "extracts|info",
        "exintro": "1",
        "explaintext": "1",
        "inprop": "url",
        "format": "json",
        "redirects": "1",
    })
    req = urllib.request.Request(f"{WIKIPEDIA_API}?{params}", headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.error("Wikipedia API error: %s", e)
        return {}

    redirects = {r["from"]: r["to"] for r in data.get("query", {}).get("redirects", [])}
    normalized = {n["from"]: n["to"] for n in data.get("query", {}).get("normalized", [])}
    title_to_page = {p.get("title", ""): p for p in data.get("query", {}).get("pages", {}).values()}

    result = {}
    for original in titles:
        resolved = redirects.get(normalized.get(original, original), normalized.get(original, original))
        page = title_to_page.get(resolved)
        if page is None or "missing" in page:
            result[original] = None
            continue
        result[original] = {"extract": page.get("extract", ""), "url": page.get("fullurl", "")}
    return result


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def extract_dob_from_text(text: str):
    """Extract a birth date from a Wikipedia intro extract (fallback for P569)."""
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


def clean_extract(extract: str):
    """Strip HTML and reject disambiguation/too-short extracts."""
    text = re.sub(r"<[^>]+>", "", extract or "").strip()
    if len(text) < 100:
        return None
    low = text.lower()
    if "may refer to" in low or "usually refers to" in low or "can refer to" in low:
        return None
    return text


def fetch_extracts_for_members(mp_data, sitelinks):
    """Fetch intro extracts for all MPs, keyed by mp_id.

    Uses the resolved enwiki sitelink title when available, else falls back
    to the honorific-stripped display name.
    """
    extracts = {}
    title_for_mp = {}
    for mp_id, name in mp_data:
        title = sitelinks.get(mp_id) or strip_honorifics(name or "")
        if title:
            title_for_mp[mp_id] = title

    mp_list = list(title_for_mp.items())
    for i in range(0, len(mp_list), WIKIPEDIA_BATCH):
        batch = mp_list[i:i + WIKIPEDIA_BATCH]
        results = fetch_wikipedia_extracts([t for _, t in batch])
        for mp_id, title in batch:
            r = results.get(title)
            if r:
                extracts[mp_id] = r
        time.sleep(RATE_LIMIT_DELAY)
        if (i // WIKIPEDIA_BATCH) % 5 == 4:
            logger.info("  Extracts: %d/%d", min(i + WIKIPEDIA_BATCH, len(mp_list)), len(mp_list))

    logger.info("Fetched %d Wikipedia extracts", len(extracts))
    return extracts


# --- Wikidata date formatting ---

def format_wikidata_date(date_str):
    """Extract YYYY-MM-DD from a Wikidata date string, or None."""
    if not date_str:
        return None
    cleaned = date_str.lstrip("+")
    return cleaned[:10] if len(cleaned) >= 10 else cleaned


# --- Apply functions (each targets the owning per-API DB) ---

def apply_dobs(bio_db_path, mp_data, enrichment, extracts):
    """Fill bio_data.dateOfBirth where NULL — P569 first, extract regex fallback."""
    conn = sqlite3.connect(bio_db_path)
    missing = {
        row[0] for row in conn.execute(
            "SELECT mpId FROM bio_data WHERE dateOfBirth IS NULL OR dateOfBirth = ''"
        ).fetchall()
    }
    filled_p569 = filled_regex = 0
    for mp_id in missing:
        dob = enrichment.get(mp_id, {}).get("dob")
        if dob:
            conn.execute(
                "UPDATE bio_data SET dateOfBirth = ? WHERE mpId = ? AND (dateOfBirth IS NULL OR dateOfBirth = '')",
                (dob, mp_id),
            )
            filled_p569 += 1
            continue
        extract = extracts.get(mp_id, {}).get("extract") if extracts else None
        dob = extract_dob_from_text(extract) if extract else None
        if dob:
            conn.execute(
                "UPDATE bio_data SET dateOfBirth = ? WHERE mpId = ? AND (dateOfBirth IS NULL OR dateOfBirth = '')",
                (dob, mp_id),
            )
            filled_regex += 1
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM bio_data WHERE dateOfBirth IS NULL OR dateOfBirth = ''"
    ).fetchone()[0]
    conn.close()
    logger.info("bio_data DOBs: %d via P569, %d via extract regex, %d still missing",
                filled_p569, filled_regex, remaining)


def apply_synopses(details_db_path, extracts):
    """Replace mp_synopsis.synopsisText with the Wikipedia intro extract."""
    conn = sqlite3.connect(details_db_path)
    updated = 0
    for mp_id, r in extracts.items():
        text = clean_extract(r["extract"])
        if not text:
            continue
        conn.execute("UPDATE mp_synopsis SET synopsisText = ? WHERE mpId = ?", (text, mp_id))
        updated += 1
    conn.commit()
    conn.close()
    logger.info("mp_synopsis: %d Wikipedia extracts applied", updated)


def apply_links(links_db_path, sitelinks):
    """Fill/update mp_links.wikipediaUrl from enwiki sitelinks."""
    conn = sqlite3.connect(links_db_path)
    updated = 0
    for mp_id, title in sitelinks.items():
        url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
        cur = conn.execute(
            "UPDATE mp_links SET wikipediaUrl = ? WHERE mpId = ? AND (wikipediaUrl IS NULL OR wikipediaUrl != ?)",
            (url, mp_id, url),
        )
        updated += cur.rowcount
    conn.commit()
    conn.close()
    logger.info("mp_links: %d wikipediaUrls applied", updated)


def apply_career_events(career_db_path, statements, qid_map, timestamp_millis):
    """Upsert wikipedia-source mp_career_events rows (ids >= WIKIPEDIA_ID_BASE).

    Natural-key upsert: identical rows untouched, changed rows UPDATEd in
    place, vanished rows deleted — keeps ids stable so patches stay small.
    """
    mp_for_qid = {qid: mp_id for mp_id, qid in qid_map.items()}
    rows = []
    for stmt in statements:
        mp_id = mp_for_qid.get(stmt["qid"])
        if mp_id is None:
            continue
        rows.append((
            mp_id,
            stmt.get("category"),
            stmt.get("name"),
            None,  # house
            format_wikidata_date(stmt.get("start_time")),
            format_wikidata_date(stmt.get("end_time")),
            stmt.get("subject"),  # additionalInfo (degree subject for P69)
            None, None, None,   # additionalInfoLink, constituencyName, constituencyId
            "wikipedia",
            timestamp_millis,
        ))

    if not rows:
        logger.info("No wikipedia career statements to apply")
        return

    conn = sqlite3.connect(career_db_path)
    cursor = conn.cursor()
    mp_ids = {r[0] for r in rows}
    existing = {}
    for mp_id in mp_ids:
        for r in cursor.execute(
            "SELECT id, endDate, additionalInfo, category, name, startDate "
            "FROM mp_career_events WHERE mpId = ? AND source = 'wikipedia'",
            (mp_id,),
        ):
            existing.setdefault((mp_id, r[3], r[4], r[5]), []).append(r)

    next_id = cursor.execute(
        "SELECT COALESCE(MAX(id), ?) + 1 FROM mp_career_events WHERE id >= ?",
        (WIKIPEDIA_ID_BASE - 1, WIKIPEDIA_ID_BASE),
    ).fetchone()[0]

    seen_ids = set()
    updates = []
    inserts = []
    for (mp_id, category, name, house, start_date, end_date,
         add_info, add_link, const_name, const_id, source, ts) in rows:
        bucket = existing.get((mp_id, category, name, start_date))
        prev = bucket.pop(0) if bucket else None
        if prev is None:
            inserts.append((next_id, mp_id, category, name, house, start_date,
                            end_date, add_info, add_link, const_name, const_id,
                            source, ts))
            next_id += 1
            continue
        eid = prev[0]
        seen_ids.add(eid)
        if (end_date, add_info) != (prev[1], prev[2]):
            updates.append((end_date, add_info, ts, eid))

    all_ids = {r[0] for bucket in existing.values() for r in bucket}
    for eid in sorted(all_ids - seen_ids):
        cursor.execute("DELETE FROM mp_career_events WHERE id = ?", (eid,))
    cursor.executemany(
        "UPDATE mp_career_events SET endDate=?, additionalInfo=?, lastUpdated=? WHERE id=?",
        updates,
    )
    for i in range(0, len(inserts), BATCH_SIZE):
        cursor.executemany(
            "INSERT INTO mp_career_events (id, mpId, category, name, house, startDate, endDate,"
            " additionalInfo, additionalInfoLink, constituencyName, constituencyId, source, lastUpdated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            inserts[i:i + BATCH_SIZE],
        )
    conn.commit()
    conn.close()
    logger.info("mp_career_events: %d inserted, %d updated, %d deleted (wikipedia)",
                len(inserts), len(updates), len(all_ids - seen_ids))


def main():
    parser = argparse.ArgumentParser(
        description="Unified Wikipedia/Wikidata enrichment — applies to owning per-API DBs."
    )
    parser.add_argument("--mps-db", required=True, help="Path to mps.db (MP ID list)")
    parser.add_argument("--bio-db", help="Fill bio_data.dateOfBirth NULLs in this DB")
    parser.add_argument("--synopsis-db", help="Apply Wikipedia extracts to mp_synopsis in this DB")
    parser.add_argument("--links-db", help="Fill mp_links.wikipediaUrl in this DB")
    parser.add_argument("--career-db", help="Upsert wikipedia mp_career_events in this DB")
    parser.add_argument("--mp-limit", type=int, default=None, help="Limit MPs (testing)")
    args = parser.parse_args()

    if not any([args.bio_db, args.synopsis_db, args.links_db, args.career_db]):
        parser.error("At least one of --bio-db/--synopsis-db/--links-db/--career-db is required")

    timestamp_millis = int(time.time() * 1000)
    mp_data = fetch_mp_ids_from_db(args.mps_db)
    if args.mp_limit:
        mp_data = mp_data[:args.mp_limit]

    # Shared Wikidata resolution — one pass regardless of which enrichments run
    qid_map = resolve_qids(mp_data)
    qids = list(qid_map.values())

    enrichment = {}
    if args.bio_db or args.links_db or args.synopsis_db:
        dob_links = fetch_dobs_and_sitelinks(qids)
        enrichment = {mp_id: dob_links.get(qid, {}) for mp_id, qid in qid_map.items()}

    sitelinks = {mp_id: e["title"] for mp_id, e in enrichment.items() if e.get("title")}

    # Extracts needed for synopsis writes and DOB regex fallback
    extracts = {}
    if args.synopsis_db or args.bio_db:
        extracts = fetch_extracts_for_members(mp_data, sitelinks)

    if args.career_db:
        statements = fetch_career_statements(qids)
        apply_career_events(args.career_db, statements, qid_map, timestamp_millis)
    if args.bio_db:
        apply_dobs(args.bio_db, mp_data, enrichment, extracts)
    if args.synopsis_db:
        apply_synopses(args.synopsis_db, extracts)
    if args.links_db:
        apply_links(args.links_db, sitelinks)

    logger.info("Done")


if __name__ == "__main__":
    main()
