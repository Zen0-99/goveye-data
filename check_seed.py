#!/usr/bin/env python3
"""Check if Build Seed needs to run by comparing per-API manifest hashes.

Downloads the 7 per-API manifest.json files from their GitHub Releases and
compares each dbHash against the last-known hashes stored in seed-manifest.json
on the seed-latest release. If all hashes match (no data changed since last
seed build), outputs needs_update=false. If any hash differs (or if this is
the first run with no seed-manifest.json), outputs needs_update=true.

Also outputs the list of changed API names so the workflow can selectively
re-download only the changed DBs.

Usage (in GitHub Actions):
    python check_seed.py --seed-manifest seed-manifest.json \
        --mps-manifest mps_manifest.json \
        --commons-votes-manifest commons_manifest.json \
        ... etc

Outputs to stdout for GitHub Actions:
    echo "needs_update=true" >> $GITHUB_OUTPUT
    echo "changed_apis=mps,bills" >> $GITHUB_OUTPUT
"""

import argparse
import json
import os
import sys

from api_helper import logger

# Maps API name to the manifest argument name
API_MANIFEST_ARGS = [
    ("mps", "mps_manifest"),
    ("commons_votes", "commons_votes_manifest"),
    ("lords_votes", "lords_votes_manifest"),
    ("bills", "bills_manifest"),
    ("committees", "committees_manifest"),
    ("recess", "recess_manifest"),
    ("interests", "interests_manifest"),
]


def load_manifest(path):
    """Load a manifest.json file, returning None if it doesn't exist."""
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def check_seed(seed_manifest_path, api_manifest_paths):
    """Compare per-API manifest dbHashes against the last-known seed manifest.

    Args:
        seed_manifest_path: Path to seed-manifest.json (may not exist on first run).
        api_manifest_paths: Dict mapping API name to manifest path.

    Returns:
        (needs_update: bool, changed_apis: list[str])
    """
    seed_manifest = load_manifest(seed_manifest_path)

    # First run — no seed manifest, need to build
    if seed_manifest is None:
        logger.info("No seed-manifest.json found — first run, needs update")
        return True, [name for name, _ in API_MANIFEST_ARGS]

    # Extract last-known hashes from seed manifest
    known_hashes = seed_manifest.get("perApiHashes", {})
    if not known_hashes:
        logger.info("seed-manifest.json has no perApiHashes — needs update")
        return True, [name for name, _ in API_MANIFEST_ARGS]

    changed_apis = []
    all_present = True

    for api_name, arg_name in API_MANIFEST_ARGS:
        manifest_path = api_manifest_paths.get(api_name)
        manifest = load_manifest(manifest_path)

        if manifest is None:
            logger.warning("Missing manifest for %s — will need to download", api_name)
            changed_apis.append(api_name)
            all_present = False
            continue

        current_hash = manifest.get("dbHash", "")
        known_hash = known_hashes.get(api_name, "")

        if current_hash != known_hash:
            logger.info(
                "%s: hash changed (known=%s, current=%s) — needs update",
                api_name, known_hash[:12], current_hash[:12],
            )
            changed_apis.append(api_name)
        else:
            logger.info("%s: hash unchanged — skip", api_name)

    needs_update = len(changed_apis) > 0
    return needs_update, changed_apis


def main():
    parser = argparse.ArgumentParser(
        description="Check if Build Seed needs to run by comparing per-API manifest hashes."
    )
    parser.add_argument(
        "--seed-manifest",
        help="Path to seed-manifest.json from the seed-latest release (may not exist on first run).",
    )
    for api_name, arg_name in API_MANIFEST_ARGS:
        parser.add_argument(
            f"--{arg_name.replace('_', '-')}",
            help=f"Path to {api_name} manifest.json.",
        )
    args = parser.parse_args()

    # Build the api_manifest_paths dict
    api_manifest_paths = {}
    for api_name, arg_name in API_MANIFEST_ARGS:
        api_manifest_paths[api_name] = getattr(args, arg_name)

    needs_update, changed_apis = check_seed(args.seed_manifest, api_manifest_paths)

    # Output for GitHub Actions
    print(f"needs_update={'true' if needs_update else 'false'}")
    print(f"changed_apis={','.join(changed_apis)}")

    if not needs_update:
        logger.info("All per-API hashes unchanged — no seed update needed")


if __name__ == "__main__":
    main()
