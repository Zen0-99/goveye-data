#!/usr/bin/env python3
"""Per-API build script for GOV.UK Publications.

Fetches government publications from GOV.UK using a two-step process:
1. Search API (www.gov.uk/api/search.json) for discovery by organisation + date
2. Content API (www.gov.uk/api/content/{path}) for full details (body, image)

Per D-01: all document types captured (no filtering by document_type).
Per D-02: fetches the last 90 days of publications (hybrid storage).
Per D-03: body text is fetched for tag matching at build time but NOT stored
in the shipped entity tuple. Body text is stored in a separate temp table
(_publication_bodies) that merge_dbs.py copies into goveye.db for build_tags.py
to use, and is dropped before shipping.

Pitfall 6: GOV.UK body text is in Govspeak/HTML format. BeautifulSoup is used
to strip HTML before tag matching (direct regex on raw HTML matches tags/
attributes, not content).

Modes:
  seed  — create fresh DB, fetch last 90 days, insert
  delta — copy previous DB, fetch last 90 days, upsert

Usage:
  python build_gov_publications.py --output gov_publications.db --schema schemas/bundled_schema.json --mode seed
  python build_gov_publications.py --output gov_publications.db --schema schemas/bundled_schema.json --mode delta --previous-db prev_gov_publications.db
"""

import argparse
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

import schema as schema_module
from api_helper import api_get, API_DELAY, BATCH_SIZE, logger

# --- Constants ---

GOVUK_SEARCH = "https://www.gov.uk/api/search.json"
GOVUK_CONTENT = "https://www.gov.uk/api/content"
TABLE_NAMES = ["government_publications"]

# Temp table for build-time body text (D-03 — not shipped, dropped before publishing)
BODIES_TABLE = "_publication_bodies"


# --- GOV.UK Organisations API: department enumeration ---

GOVUK_ORGANISATIONS = "https://www.gov.uk/api/organisations"


def fetch_organisation_slugs():
    """Enumerate all current ministerial department slugs from the GOV.UK API.

    Paginates through the /api/organisations endpoint and filters for:
    - format == "Ministerial department"
    - superseding_organisations is empty (i.e. not superseded/historical)

    The old approach used aggregate_organisations=prefix on the Search API,
    but that parameter now returns 422.

    Returns:
        List of organisation slug strings (e.g. "hm-treasury", "home-office").
    """
    logger.info("Fetching organisation list from GOV.UK Organisations API")
    slugs = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        r = api_get(f"{GOVUK_ORGANISATIONS}?page={page}", timeout=60)
        data = r.json()
        total_pages = data.get("pages", 1)

        for org in data.get("results", []):
            if org.get("format") != "Ministerial department":
                continue
            # Skip superseded/historical departments
            if org.get("superseding_organisations"):
                continue
            slug = org.get("details", {}).get("slug", "")
            if slug:
                slugs.append(slug)

        page += 1
        if page <= total_pages:
            time.sleep(0.5)  # Be nice to the API

    logger.info("Found %d current ministerial departments", len(slugs))
    return slugs


# --- GOV.UK Search API: publication discovery ---

def fetch_publications_for_org(org_slug, start_date, end_date):
    """Fetch publication paths for an organisation within a date range.

    Uses the GOV.UK Search API with filter_organisations + filter_public_timestamp.
    Per D-01: no document_type filtering — all types captured.

    Args:
        org_slug: Organisation slug (e.g. "hm-treasury").
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.

    Returns:
        List of dicts with keys: link, title, description, public_timestamp,
        document_type, organisations.
    """
    all_results = []
    start = 0
    while True:
        params = {
            "filter_organisations": org_slug,
            "filter_public_timestamp": f"from:{start_date},to:{end_date}",
            "count": 100,
            "start": start,
        }
        logger.info("Fetching publications for %s: start=%d", org_slug, start)
        r = api_get(GOVUK_SEARCH, params=params, timeout=60)
        data = r.json()
        results = data.get("results", [])
        if not results:
            break
        for item in results:
            all_results.append({
                "link": item.get("link", ""),
                "title": item.get("title", ""),
                "description": item.get("description", ""),
                "public_timestamp": item.get("public_timestamp", ""),
                "document_type": item.get("document_type", ""),
                "organisations": item.get("organisations", []),
            })
        if len(results) < 100:
            break
        start += 100
        time.sleep(API_DELAY)
    logger.info("Fetched %d publications for %s", len(all_results), org_slug)
    return all_results


# --- GOV.UK Content API: full publication details ---

def fetch_publication_details(path):
    """Fetch full content details for a GOV.UK publication by path.

    Args:
        path: The publication path (e.g. "/government/news/budget-2026").

    Returns:
        Dict with title, description, document_type, first_published_at,
        public_updated_at, details.body, details.image, links.organisations,
        links.primary_publishing_organisation, base_path.
    """
    r = api_get(f"{GOVUK_CONTENT}/{path}", timeout=60)
    return r.json()


# --- HTML stripping for tag matching (Pitfall 6) ---

def strip_html_for_tag_matching(html_text):
    """Strip HTML/Govspeak tags from body text for tag pattern matching.

    Pitfall 6: direct regex on raw HTML matches tags/attributes, not content.
    BeautifulSoup extracts plain text from the HTML body.

    Args:
        html_text: Raw HTML/Govspeak body text from the Content API.

    Returns:
        Plain text string with HTML tags removed.
    """
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    return soup.get_text()


# --- Mapping + insertion ---

def map_publication_to_entity(search_item, content_details, timestamp_millis, pub_id):
    """Map a publication to a government_publications row tuple.

    Per D-03: body text is NOT included in the entity tuple. It is stored
    separately in _publication_bodies for build-time tag matching.

    Matches GovernmentPublicationEntity fields:
    (id, title, summary, url, documentType, organisation, organisationSlug,
     firstPublishedAt, publicUpdatedAt, imageUrl, lastUpdated)

    Args:
        search_item: Dict from the Search API.
        content_details: Dict from the Content API (or None if fetch failed).
        timestamp_millis: Build timestamp.
        pub_id: Sequential integer ID assigned by the build script.
    """
    if content_details:
        details = content_details.get("details", {}) or {}
        links = content_details.get("links", {}) or {}
        primary_org = links.get("primary_publishing_organisation", [])
        org_name = ""
        org_slug = ""
        if primary_org:
            org_name = primary_org[0].get("title", "") if isinstance(primary_org, list) else primary_org.get("title", "")
            org_slug = primary_org[0].get("slug", "") if isinstance(primary_org, list) else primary_org.get("slug", "")
        if not org_slug:
            orgs = links.get("organisations", [])
            if orgs:
                first_org = orgs[0] if isinstance(orgs, list) else orgs
                org_name = first_org.get("title", "") if isinstance(first_org, dict) else ""
                org_slug = first_org.get("slug", "") if isinstance(first_org, dict) else ""

        title = content_details.get("title", "") or search_item.get("title", "")
        description = content_details.get("description", "") or search_item.get("description", "")
        document_type = content_details.get("document_type", "") or search_item.get("document_type", "")
        first_published = content_details.get("first_published_at", "") or search_item.get("public_timestamp", "")
        public_updated = content_details.get("public_updated_at", "") or search_item.get("public_timestamp", "")
        base_path = content_details.get("base_path", "") or search_item.get("link", "")
        image_url = details.get("image", None)
    else:
        # Fallback to search item data only
        title = search_item.get("title", "")
        description = search_item.get("description", "")
        document_type = search_item.get("document_type", "")
        first_published = search_item.get("public_timestamp", "")
        public_updated = search_item.get("public_timestamp", "")
        base_path = search_item.get("link", "")
        org_name = ""
        org_slug = ""
        if search_item.get("organisations"):
            orgs = search_item["organisations"]
            if isinstance(orgs, list) and orgs:
                org_name = orgs[0].get("title", "") if isinstance(orgs[0], dict) else str(orgs[0])
                org_slug = orgs[0].get("slug", "") if isinstance(orgs[0], dict) else ""
        image_url = None

    url = f"https://www.gov.uk{base_path}" if base_path else ""

    return (
        pub_id,
        title,
        description,
        url,
        document_type,
        org_name,
        org_slug,
        first_published,
        public_updated,
        image_url,
        timestamp_millis,
    )


def insert_publications(conn, publications, bodies, timestamp_millis):
    """Insert publications into government_publications + _publication_bodies.

    Args:
        conn: SQLite connection.
        publications: List of entity tuples (without body).
        bodies: Dict mapping pub_id -> stripped body text.
        timestamp_millis: Build timestamp.
    """
    cursor = conn.cursor()

    # Create temp bodies table if not exists (D-03 — build-time only, not in Room schema)
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {BODIES_TABLE} (
            id INTEGER PRIMARY KEY,
            body TEXT
        )
    """)

    insert_sql = """
        INSERT OR REPLACE INTO government_publications (
            id, title, summary, url, documentType, organisation, organisationSlug,
            firstPublishedAt, publicUpdatedAt, imageUrl, lastUpdated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    for i in range(0, len(publications), BATCH_SIZE):
        batch = publications[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        logger.info("Inserted publications: %d/%d", min(i + BATCH_SIZE, len(publications)), len(publications))

    # Insert bodies into temp table
    body_rows = [(pub_id, body) for pub_id, body in bodies.items() if body]
    for i in range(0, len(body_rows), BATCH_SIZE):
        batch = body_rows[i:i + BATCH_SIZE]
        cursor.executemany(
            f"INSERT OR REPLACE INTO {BODIES_TABLE} (id, body) VALUES (?, ?)",
            batch,
        )
    conn.commit()
    logger.info("Inserted %d publication bodies (temp table, D-03)", len(body_rows))


# --- Build modes ---

def build_seed(output_path, schema_path, days=90, checkpoint_db=None):
    """Seed mode: create fresh DB, fetch last 90 days of publications, insert.

    Per D-03: body text is fetched and stored in _publication_bodies temp table
    for build_tags.py (14-02) to use. The shipped DB must NOT contain body text.

    Checkpoint/resume: inserts per-organisation and commits after each org,
    so a timeout mid-fetch preserves already-processed data. On resume,
    skips organisations that already have publications in the DB.
    """
    timestamp_millis = int(time.time() * 1000)

    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(id) FROM government_publications")
        row = cursor.fetchone()
        next_id = (row[0] or 0) + 1 if row else 1
        logger.info("Resuming from checkpoint: next publication ID = %d", next_id)
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES,
        )
        next_id = 1

    # _publication_bodies temp table is created by insert_publications (D-03)

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    org_slugs = fetch_organisation_slugs()

    # On resume, find which organisations already have publications.
    # The organisationSlug column stores the primary org slug per publication.
    cursor.execute("SELECT COUNT(*) FROM government_publications")
    existing_count = cursor.fetchone()[0]

    # Build a set of already-processed org slugs from the checkpoint
    processed_orgs = set()
    if existing_count > 0:
        cursor.execute("SELECT DISTINCT organisationSlug FROM government_publications WHERE organisationSlug != ''")
        for (slug,) in cursor.fetchall():
            if slug:
                processed_orgs.add(slug)
        logger.info("Checkpoint has %d publications from %d organisations — will skip those",
                    existing_count, len(processed_orgs))

    total_inserted = 0

    for org_slug in org_slugs:
        if org_slug in processed_orgs:
            logger.info("Skipping %s (already in checkpoint)", org_slug)
            continue

        search_results = fetch_publications_for_org(org_slug, start_date, end_date)
        org_publications = []
        org_bodies = {}

        for item in search_results:
            path = item.get("link", "")
            if not path:
                continue
            # Fetch full details via Content API
            try:
                content_details = fetch_publication_details(path)
            except Exception as e:
                logger.warning("Failed to fetch content for %s: %s", path, e)
                content_details = None
                time.sleep(API_DELAY)
                continue

            # Strip HTML from body for tag matching (Pitfall 6, D-03)
            details = (content_details or {}).get("details", {}) or {}
            raw_body = details.get("body", "")
            stripped_body = strip_html_for_tag_matching(raw_body)

            entity = map_publication_to_entity(item, content_details, timestamp_millis, next_id)
            org_publications.append(entity)
            if stripped_body:
                org_bodies[next_id] = stripped_body
            next_id += 1
            time.sleep(API_DELAY)

        # Insert and commit per-organisation so checkpoint preserves progress
        if org_publications:
            insert_publications(conn, org_publications, org_bodies, timestamp_millis)
            conn.commit()
            total_inserted += len(org_publications)
            logger.info("Inserted %d publications for %s (total: %d)",
                        len(org_publications), org_slug, total_inserted)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Seed build complete: %s (%d new publications, %d total)",
                output_path, total_inserted, existing_count + total_inserted)


def build_delta(output_path, previous_db, schema_path, days=90):
    """Delta mode: copy previous DB, fetch last 90 days, upsert."""
    timestamp_millis = int(time.time() * 1000)

    shutil.copy2(previous_db, output_path)
    logger.info("Copied previous DB to %s", output_path)

    conn = sqlite3.connect(output_path)

    # Ensure schema is up-to-date (previous DB may be from an older schema version)
    schema_module.ensure_schema(conn, schema_path, TABLE_NAMES)
    logger.info("Schema ensured for delta build")

    cursor = conn.cursor()
    cursor.execute("SELECT MAX(id) FROM government_publications")
    row = cursor.fetchone()
    next_id = (row[0] or 0) + 1 if row else 1

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    org_slugs = fetch_organisation_slugs()
    all_publications = []
    all_bodies = {}

    for org_slug in org_slugs:
        search_results = fetch_publications_for_org(org_slug, start_date, end_date)
        for item in search_results:
            path = item.get("link", "")
            if not path:
                continue
            try:
                content_details = fetch_publication_details(path)
            except Exception as e:
                logger.warning("Failed to fetch content for %s: %s", path, e)
                content_details = None
                time.sleep(API_DELAY)
                continue

            details = (content_details or {}).get("details", {}) or {}
            raw_body = details.get("body", "")
            stripped_body = strip_html_for_tag_matching(raw_body)

            entity = map_publication_to_entity(item, content_details, timestamp_millis, next_id)
            all_publications.append(entity)
            if stripped_body:
                all_bodies[next_id] = stripped_body
            next_id += 1
            time.sleep(API_DELAY)

    if all_publications:
        insert_publications(conn, all_publications, all_bodies, timestamp_millis)

    logger.info("VACUUMing database to minimize file size...")
    conn.execute("VACUUM")

    conn.close()
    logger.info("Delta build complete: %s (%d publications)", output_path, len(all_publications))


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Government Publications per-API SQLite database (gov_publications.db)."
    )
    parser.add_argument(
        "--output", default="gov_publications.db",
        help="Output path for the SQLite DB file. Default: gov_publications.db.",
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
        "--checkpoint-db",
        help="Path to a checkpoint DB to resume from (seed mode only). Upserts on top of existing data.",
    )
    parser.add_argument(
        "--days", type=int, default=90,
        help="Number of days of publications to fetch (default: 90 per D-02).",
    )
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")

    if args.mode == "seed":
        build_seed(args.output, args.schema, days=args.days, checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema, days=args.days)


if __name__ == "__main__":
    main()
