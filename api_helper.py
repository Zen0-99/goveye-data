"""Shared API helper for GovEye build scripts.

Provides api_get() — a GET-with-retry helper extracted from build_db.py so
that all 5 per-API build scripts (build_mps, build_votes, build_bills,
build_committees, build_recess) can import it instead of duplicating the
retry logic (D-10).

Retries on requests.exceptions.ReadTimeout and
requests.exceptions.ConnectionError with exponential backoff (1s, 2s, 4s).
"""

import logging
import re
import time

import requests

# --- Constants ---

API_DELAY = 0.2  # seconds between API calls for rate limiting (A3)
API_TIMEOUT = 60  # seconds — Lords API is slow, 30s wasn't enough
API_MAX_RETRIES = 3  # retry on timeout/connection error
BATCH_SIZE = 1000  # rows per transaction for batch inserts (Pitfall 7)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("goveye_build")


def api_get(url, params=None, timeout=60, max_retries=3):
    """GET with retry logic. Retries on timeout and connection errors.

    Args:
        url: The URL to GET.
        params: Optional query params dict.
        timeout: Per-request timeout in seconds (default 60).
        max_retries: Max attempts (default 3).

    Returns:
        The requests.Response object (with raise_for_status already called).

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            # Force UTF-8 — the Parliament APIs return JSON with £ symbols
            # but don't always set charset in Content-Type. Without this,
            # requests defaults to ISO-8859-1 (HTTP/1.1 spec), corrupting
            # multi-byte UTF-8 characters like £ (U+00A3) into replacement chars.
            r.encoding = "utf-8"
            return r
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            wait = 2 ** attempt  # exponential backoff: 1s, 2s, 4s
            logger.warning(
                "API retry %d/%d for %s (waiting %ds): %s",
                attempt + 1, max_retries, url, wait, type(e).__name__,
            )
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            # Retry on 5xx server errors (transient — Parliament API sometimes
            # returns 500 on large paginated queries). Don't retry 4xx (client
            # errors are permanent — bad URL, auth, etc.).
            if e.response.status_code < 500:
                raise
            last_exc = e
            wait = 2 ** attempt
            logger.warning(
                "API retry %d/%d for %s (waiting %ds): HTTP %d",
                attempt + 1, max_retries, url, wait, e.response.status_code,
            )
            time.sleep(wait)
            last_exc = e
            wait = 2 ** attempt  # exponential backoff: 1s, 2s, 4s
            logger.warning(
                "API retry %d/%d for %s (waiting %ds): %s",
                attempt + 1, max_retries, url, wait, type(e).__name__,
            )
            time.sleep(wait)
    raise last_exc


# Regex to extract the TWFY debate link from a division page.
# Matches: href="/debates/?gid=2025-07-01b.159.0#g246.0"
# or:      href="/lords/?gid=2026-07-22b.1188.0#g1202.0"
_TWFY_DEBATE_RE = re.compile(
    r'href="(/(?:debates|lords)/\?[^"]*gid=[^"#]+)(#[^"]*)?"'
)


def fetch_twfy_debate_url(date, number, house):
    """Fetch the TWFY division page and extract the debate GID link.

    The Parliament Votes APIs don't return any debate reference (GID).
    TheyWorkForYou's division page contains a "Read the debate" link with
    the Hansard GID. We scrape it at build time and store the full TWFY
    debate URL so the app can link directly to the debate (with speeches,
    not just the vote tally).

    Args:
        date: Division date in ISO format (YYYY-MM-DD).
        number: Division number (from the API).
        house: 1 for Commons, 2 for Lords.

    Returns:
        Full TWFY debate URL (e.g.
        "https://www.theyworkforyou.com/debates/?gid=2025-07-01b.159.0#g246.0")
        or None if the page couldn't be fetched or no debate link was found.
    """
    if number is None:
        return None

    house_name = "lords" if house == 2 else "commons"
    twfy_division_url = (
        f"https://www.theyworkforyou.com/divisions/pw-{date}-{number}-{house_name}"
    )

    try:
        r = api_get(twfy_division_url, timeout=30, max_retries=2)
        matches = _TWFY_DEBATE_RE.findall(r.text)
        if matches:
            path, fragment = matches[0]
            return f"https://www.theyworkforyou.com{path}{fragment or ''}"
        logger.warning("No debate GID found on TWFY page: %s", twfy_division_url)
        return None
    except Exception as e:
        logger.warning("Failed to fetch TWFY debate URL for %s: %s", twfy_division_url, e)
        return None
