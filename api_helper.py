"""Shared API helper for GovEye build scripts.

Provides api_get() — a GET-with-retry helper extracted from build_db.py so
that all 5 per-API build scripts (build_mps, build_votes, build_bills,
build_committees, build_recess) can import it instead of duplicating the
retry logic (D-10).

Retries on requests.exceptions.ReadTimeout and
requests.exceptions.ConnectionError with exponential backoff (1s, 2s, 4s).
"""

import logging
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
            return r
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            wait = 2 ** attempt  # exponential backoff: 1s, 2s, 4s
            logger.warning(
                "API retry %d/%d for %s (waiting %ds): %s",
                attempt + 1, max_retries, url, wait, type(e).__name__,
            )
            time.sleep(wait)
    raise last_exc
