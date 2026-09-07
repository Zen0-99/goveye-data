#!/usr/bin/env python3
"""Backfill bodyText for publications that have null/empty bodyText.

Fetches body text from the GOV.UK Content API for each publication
that has null or empty bodyText, and updates the DB in place.

Designed to run on GitHub Actions. Uses concurrent requests for speed.
Processes in batches of 500 with a checkpoint after each batch.

Usage:
    python backfill_bodytext.py --db gov_publications.db --batch-size 500 --max-rows 0

    --max-rows 0 means process all. Set to a number for testing.
"""
import argparse
import sqlite3
import time
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://www.gov.uk/api/content"
TIMEOUT = 30
MAX_WORKERS = 20  # concurrent API calls

# URL path segments that indicate index/announcement pages without body content.
# Skipping these saves ~40% of API calls (they return empty body anyway).
SKIP_URL_PATTERNS = [
    "/government/collections/",
    "/government/statistics/announcements/",
    "/government/publications?",  # query-string publications (rare)
]


def strip_html_formatted(html_text):
    """Convert HTML to formatted plain text with paragraph/bullet preservation."""
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6"]):
        tag.insert_before("\n\n")
        tag.insert_after("\n\n")
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text()
    lines = text.split("\n")
    cleaned = []
    blank_count = 0
    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                cleaned.append("")
        else:
            blank_count = 0
            cleaned.append(line.strip())
    return "\n".join(cleaned).strip()


def fetch_body_text(url):
    """Fetch body text from GOV.UK Content API for a publication URL."""
    try:
        path = urlparse(url).path
        api_url = API_BASE + path
        resp = requests.get(api_url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        details = data.get("details", {})
        body_html = details.get("body", "")
        if not body_html:
            # Try govspeak as fallback
            body_html = details.get("govspeak", "")
        if not body_html:
            return None
        formatted = strip_html_formatted(body_html)
        return formatted if formatted else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Backfill bodyText for publications")
    parser.add_argument("--db", required=True, help="Path to gov_publications.db")
    parser.add_argument("--batch-size", type=int, default=500, help="Rows per batch")
    parser.add_argument("--max-rows", type=int, default=0, help="Max rows to process (0 = all)")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N rows (for resuming)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    c = conn.cursor()

    # Count publications with null/empty bodyText
    c.execute("SELECT COUNT(*) FROM government_publications WHERE bodyText IS NULL OR bodyText = ''")
    total_missing = c.fetchone()[0]
    logger.info("Publications with missing bodyText: %d", total_missing)

    if total_missing == 0:
        logger.info("Nothing to backfill — all publications have bodyText")
        return

    # Fetch all publications with missing bodyText, ordered by id
    c.execute(
        "SELECT id, url FROM government_publications WHERE bodyText IS NULL OR bodyText = '' ORDER BY id"
    )
    all_rows = c.fetchall()

    # Pre-filter: skip URLs that are known to not have body content
    filtered_rows = []
    skipped_by_pattern = 0
    for pub_id, url in all_rows:
        if url and any(pattern in url for pattern in SKIP_URL_PATTERNS):
            skipped_by_pattern += 1
            continue
        filtered_rows.append((pub_id, url))
    all_rows = filtered_rows
    if skipped_by_pattern > 0:
        logger.info("Pre-filtered %d URLs (collections/announcements)", skipped_by_pattern)

    if args.offset > 0:
        all_rows = all_rows[args.offset:]
        logger.info("Skipping first %d rows (offset)", args.offset)

    if args.max_rows > 0:
        all_rows = all_rows[: args.max_rows]

    logger.info("Processing %d publications with %d concurrent workers", len(all_rows), MAX_WORKERS)

    updated = 0
    skipped = 0
    errors = 0
    batch_start = time.time()

    for i in range(0, len(all_rows), args.batch_size):
        batch = all_rows[i : i + args.batch_size]
        batch_num = i // args.batch_size + 1
        total_batches = (len(all_rows) + args.batch_size - 1) // args.batch_size

        # Fetch bodyText concurrently
        results = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(fetch_body_text, url): pub_id
                for pub_id, url in batch
                if url
            }
            for future in as_completed(futures):
                pub_id = futures[future]
                try:
                    body = future.result()
                    if body:
                        results[pub_id] = body
                except Exception:
                    pass

        # Update DB
        for pub_id, body in results.items():
            c.execute(
                "UPDATE government_publications SET bodyText = ? WHERE id = ?",
                (body, pub_id),
            )
            updated += 1

        skipped += len(batch) - len(results)
        errors += sum(1 for _, url in batch if not url)

        conn.commit()

        elapsed = time.time() - batch_start
        rate = (i + len(batch)) / elapsed if elapsed > 0 else 0
        remaining = (len(all_rows) - i - len(batch)) / rate if rate > 0 else 0
        logger.info(
            "Batch %d/%d: updated=%d skipped=%d errors=%d | total updated=%d | "
            "%.0f rows/s | ETA %.0fs",
            batch_num,
            total_batches,
            len(results),
            len(batch) - len(results),
            sum(1 for _, url in batch if not url),
            updated,
            rate,
            remaining,
        )

    conn.commit()
    conn.close()

    logger.info(
        "Backfill complete: %d updated, %d skipped (no body on API), %d errors (no URL)",
        updated,
        skipped,
        errors,
    )


if __name__ == "__main__":
    main()
