#!/usr/bin/env python3
"""Check if the seed DB needs rebuilding by comparing per-API manifest hashes.

Reads the seed-manifest.json (which stores the dbHash from each per-API
manifest at the time the seed was last built) and compares them against
the current per-API manifests. If any hash changed (or the seed manifest
doesn't exist yet), the seed needs rebuilding.

Outputs GitHub Actions step outputs via stdout (>> $GITHUB_OUTPUT):
  needs_update=true|false
  changed_apis=mps,commons_votes,lords_votes,...

Usage:
  python check_seed.py \
    --seed-manifest seed-manifest.json \
    --mps-manifest mps_manifest.json \
    --commons-votes-manifest commons_manifest.json \
    --lords-votes-manifest lords_manifest.json \
    --bills-manifest bills_manifest.json \
    --committees-manifest committees_manifest.json \
    --recess-manifest recess_manifest.json \
    --interests-manifest interests_manifest.json
"""

import argparse
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("check_seed")

# Maps CLI argument names to the key used in seed-manifest.json
# and the API name used in changed_apis output + build-seed.yml case statements.
PER_API = [
    ("mps", "mps-manifest", "mps"),
    ("commons_votes", "commons-votes-manifest", "commons_votes"),
    ("lords_votes", "lords-votes-manifest", "lords_votes"),
    ("bills", "bills-manifest", "bills"),
    ("committees", "committees-manifest", "committees"),
    ("recess", "recess-manifest", "recess"),
    ("interests", "interests-manifest", "interests"),
    ("party_stats", "party-stats-manifest", "party_stats"),
]


def load_manifest(path):
    """Load a manifest JSON file, returning None if it doesn't exist."""
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def check_seed(seed_manifest_path, per_api_manifest_paths):
    """Compare per-API manifest hashes against the seed manifest.

    Args:
        seed_manifest_path: Path to seed-manifest.json (may not exist on first run).
        per_api_manifest_paths: Dict mapping API key to manifest path.

    Returns:
        Tuple of (needs_update: bool, changed_apis: list[str]).
    """
    seed_manifest = load_manifest(seed_manifest_path)

    if seed_manifest is None:
        logger.info("No seed-manifest.json found — first run, seed needs building")
        # All APIs are "changed" on first run
        all_apis = [api_name for _, _, api_name in PER_API]
        return True, all_apis

    # The seed manifest stores per-API hashes under "apiHashes" key
    stored_hashes = seed_manifest.get("apiHashes", {})

    changed_apis = []
    for api_key, arg_name, api_name in PER_API:
        manifest_path = per_api_manifest_paths.get(api_key)
        current_manifest = load_manifest(manifest_path)

        if current_manifest is None:
            logger.warning("  %s manifest not found — skipping", api_name)
            continue

        current_hash = current_manifest.get("dbHash")
        stored_hash = stored_hashes.get(api_key)

        if stored_hash is None:
            logger.info("  %s: no stored hash — changed", api_name)
            changed_apis.append(api_name)
        elif current_hash != stored_hash:
            logger.info("  %s: hash changed (%s → %s)", api_name,
                        stored_hash[:12] if stored_hash else "none",
                        current_hash[:12] if current_hash else "none")
            changed_apis.append(api_name)
        else:
            logger.info("  %s: unchanged", api_name)

    needs_update = len(changed_apis) > 0
    return needs_update, changed_apis


def main():
    parser = argparse.ArgumentParser(
        description="Check if seed DB needs rebuilding by comparing per-API manifest hashes."
    )
    parser.add_argument("--seed-manifest", required=True,
                        help="Path to seed-manifest.json (may not exist on first run).")
    parser.add_argument("--mps-manifest", default=None)
    parser.add_argument("--commons-votes-manifest", default=None)
    parser.add_argument("--lords-votes-manifest", default=None)
    parser.add_argument("--bills-manifest", default=None)
    parser.add_argument("--committees-manifest", default=None)
    parser.add_argument("--recess-manifest", default=None)
    parser.add_argument("--interests-manifest", default=None)
    parser.add_argument("--party-stats-manifest", default=None)
    args = parser.parse_args()

    per_api_paths = {
        "mps": args.mps_manifest,
        "commons_votes": args.commons_votes_manifest,
        "lords_votes": args.lords_votes_manifest,
        "bills": args.bills_manifest,
        "committees": args.committees_manifest,
        "recess": args.recess_manifest,
        "interests": args.interests_manifest,
        "party_stats": args.party_stats_manifest,
    }

    needs_update, changed_apis = check_seed(args.seed_manifest, per_api_paths)

    # Output GitHub Actions step outputs
    print(f"needs_update={'true' if needs_update else 'false'}")
    print(f"changed_apis={','.join(changed_apis)}")

    if needs_update:
        logger.info("Seed needs update — changed APIs: %s", ", ".join(changed_apis))
    else:
        logger.info("All per-API hashes unchanged — no seed update needed")


if __name__ == "__main__":
    main()
