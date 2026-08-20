#!/usr/bin/env python3
"""Check if Parliament is in recess (Commons, houseId=1).

Used by GitHub Actions workflows to skip unnecessary builds during
parliamentary recess. Outputs `skip=true` or `skip=false` to stdout.

Fails open: on ANY error (API down, parse failure, network error),
outputs `skip=false` (run the build) — never blocks a build due to
a check failure.

Usage:
    python check_recess.py

GitHub Actions:
    - name: Check if parliament is in recess
      id: recess_check
      run: python check_recess.py
    # Then: if: steps.recess_check.outputs.skip != 'true'
"""

import sys
from datetime import datetime, timezone

from build_recess import fetch_recess_html, parse_recess_dates


def is_in_recess_today():
    """Check if today (UTC) falls within any Commons recess date range.

    Returns True if in recess, False otherwise.
    Raises on API/parse errors (caller handles fail-open).
    """
    html = fetch_recess_html(1)  # houseId=1 = Commons
    recess_dates = parse_recess_dates(html)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for _description, start_date, end_date in recess_dates:
        if start_date <= today <= end_date:
            return True
    return False


def main():
    try:
        in_recess = is_in_recess_today()
    except Exception as e:
        # Fail open — never block a build due to a check failure
        print(f"Recess check error (failing open): {e}", file=sys.stderr)
        print("skip=false")
        return

    print(f"skip={'true' if in_recess else 'false'}")


if __name__ == "__main__":
    main()
