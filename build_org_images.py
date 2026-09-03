#!/usr/bin/env python3
"""Post-build script: fetch organisation images from Wikipedia.

For each distinct organisation in government_publications that has no
imageUrl, fetches the organisation's logo/thumbnail from the Wikipedia
REST API and fills in the missing imageUrl.

This runs AFTER build_gov_publications.py and BEFORE merge_dbs.py.

Usage:
    python build_org_images.py --db gov_publications.db
    python build_org_images.py --db gov_publications.db --dry-run
"""

import argparse
import sqlite3
import time
import urllib.parse
import urllib.request
import json
import logging
import sys
import os

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WIKIPEDIA_REST = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_SEARCH = "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&format=json&srlimit=1"
API_DELAY = 1.0  # seconds between Wikipedia API calls (be nice)


def wiki_api_get(url, timeout=30):
    """Fetch JSON from a URL with a User-Agent header (Wikipedia requires it)."""
    req = urllib.request.Request(url, headers={"User-Agent": "GovEyeBot/1.0 (goveye.app)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_organisation_wikipedia_image(org_name, org_slug):
    """Fetch the Wikipedia thumbnail URL for a government organisation.

    Strategy:
    1. Try the REST API with the organisation name directly (most match)
    2. If that fails (404 or no thumbnail), try the search API
    3. If still no image, return None

    Args:
        org_name: Full organisation name (e.g. "Department of Health and Social Care")
        org_slug: GOV.UK slug (e.g. "department-of-health-and-social-care")

    Returns:
        Thumbnail URL string, or None if no image found.
    """
    # Strategy 1: Direct REST API lookup
    # Replace spaces with underscores for the URL
    title = org_name.replace(" ", "_")
    url = WIKIPEDIA_REST.format(title=urllib.parse.quote(title))

    try:
        data = wiki_api_get(url)
        thumbnail = data.get("thumbnail")
        if thumbnail and thumbnail.get("source"):
            return thumbnail["source"]
        # Page exists but no thumbnail — try originalimage
        original = data.get("originalimage")
        if original and original.get("source"):
            return original["source"]
    except Exception as e:
        logger.debug("Direct lookup failed for '%s': %s", org_name, e)

    # Strategy 2: Search API
    search_url = WIKIPEDIA_SEARCH.format(query=urllib.parse.quote(org_name))
    try:
        data = wiki_api_get(search_url)
        search_results = data.get("query", {}).get("search", [])
        if search_results:
            found_title = search_results[0]["title"]
            # Fetch the thumbnail for the found title
            rest_url = WIKIPEDIA_REST.format(title=urllib.parse.quote(found_title.replace(" ", "_")))
            try:
                page_data = wiki_api_get(rest_url)
                thumbnail = page_data.get("thumbnail")
                if thumbnail and thumbnail.get("source"):
                    logger.info("  Found via search: '%s' -> '%s'", org_name, found_title)
                    return thumbnail["source"]
            except Exception:
                pass
    except Exception as e:
        logger.debug("Search failed for '%s': %s", org_name, e)

    return None


def main():
    parser = argparse.ArgumentParser(description="Fetch organisation images from Wikipedia")
    parser.add_argument("--db", required=True, help="Path to gov_publications.db")
    parser.add_argument("--dry-run", action="store_true", help="Don't update the DB, just print what would change")
    parser.add_argument("--force", action="store_true", help="Re-fetch even for orgs that already have images")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    c = conn.cursor()

    # Get distinct organisations
    if args.force:
        orgs = c.execute(
            "SELECT DISTINCT organisation, organisationSlug FROM government_publications "
            "WHERE organisation IS NOT NULL AND organisation != '' "
            "ORDER BY organisation"
        ).fetchall()
    else:
        orgs = c.execute(
            "SELECT DISTINCT organisation, organisationSlug FROM government_publications "
            "WHERE organisation IS NOT NULL AND organisation != '' "
            "AND (imageUrl IS NULL OR imageUrl = '') "
            "ORDER BY organisation"
        ).fetchall()

    # Also find orgs with empty name but valid slug — look up the name
    # from other publications with the same slug
    empty_org_slugs = c.execute(
        "SELECT DISTINCT organisationSlug FROM government_publications "
        "WHERE (organisation IS NULL OR organisation = '') "
        "AND organisationSlug IS NOT NULL AND organisationSlug != '' "
        "AND (imageUrl IS NULL OR imageUrl = '')"
    ).fetchall()

    for (slug,) in empty_org_slugs:
        # Look up the org name from publications with the same slug
        name_row = c.execute(
            "SELECT organisation FROM government_publications "
            "WHERE organisationSlug = ? AND organisation IS NOT NULL AND organisation != '' "
            "LIMIT 1",
            (slug,)
        ).fetchone()
        if name_row:
            orgs.append((name_row[0], slug))
            logger.info("Found org name for empty-org slug '%s': '%s'", slug, name_row[0])
        else:
            # No publication with this slug has a name — try to derive from slug
            # e.g. "department-for-transport" -> "Department for Transport"
            derived = slug.replace("-", " ").title()
            orgs.append((derived, slug))
            logger.info("Derived org name from slug '%s': '%s'", slug, derived)

    logger.info("Found %d organisations to fetch images for", len(orgs))

    updated = 0
    skipped = 0

    for org_name, org_slug in orgs:
        logger.info("Fetching image for: %s (slug=%s)", org_name, org_slug)

        image_url = get_organisation_wikipedia_image(org_name, org_slug)

        if image_url:
            logger.info("  -> %s", image_url)
            if not args.dry_run:
                # Update all publications from this org that have no image.
                # Match by slug (more reliable than org name, which may be empty).
                c.execute(
                    "UPDATE government_publications SET imageUrl = ? "
                    "WHERE organisationSlug = ? AND (imageUrl IS NULL OR imageUrl = '')",
                    (image_url, org_slug)
                )
                updated += c.rowcount
            else:
                logger.info("  (dry-run, not updating)")
        else:
            logger.warning("  -> No image found")
            skipped += 1

        time.sleep(API_DELAY)

    if not args.dry_run:
        conn.commit()
        logger.info("Updated %d publication rows with organisation images", updated)
    logger.info("Skipped %d organisations (no Wikipedia image found)", skipped)

    conn.close()


if __name__ == "__main__":
    main()
