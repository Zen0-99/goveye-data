#!/usr/bin/env python3
"""Generate seed-manifest.json tracking per-API dbHashes.

Reads the 7 per-API manifest.json files and produces a seed-manifest.json
that records which per-API dbHashes were used to build the current seed.
Build Seed's check phase compares these hashes against future per-API
manifests to decide if a re-merge is needed.

Usage:
    python generate_seed_manifest.py --output seed-manifest.json \
        --mps-manifest mps_manifest.json \
        --commons-votes-manifest commons_manifest.json \
        ... etc
"""

import argparse
import json
import os
from datetime import datetime, timezone

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


def generate_seed_manifest(api_manifest_paths):
    """Build a seed-manifest dict from the 7 per-API manifests.

    Args:
        api_manifest_paths: Dict mapping API name to manifest path.

    Returns:
        Dict with perApiHashes, generatedAt, and per-API versions.
    """
    per_api_hashes = {}
    per_api_versions = {}

    for api_name, arg_name in API_MANIFEST_ARGS:
        manifest_path = api_manifest_paths.get(api_name)
        manifest = load_manifest(manifest_path)

        if manifest is None:
            logger.warning("Missing manifest for %s — hash will be null", api_name)
            per_api_hashes[api_name] = None
            per_api_versions[api_name] = None
        else:
            per_api_hashes[api_name] = manifest.get("dbHash")
            per_api_versions[api_name] = manifest.get("version")

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "perApiHashes": per_api_hashes,
        "perApiVersions": per_api_versions,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate seed-manifest.json tracking per-API dbHashes."
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to write seed-manifest.json.",
    )
    for api_name, arg_name in API_MANIFEST_ARGS:
        parser.add_argument(
            f"--{arg_name.replace('_', '-')}",
            help=f"Path to {api_name} manifest.json.",
        )
    args = parser.parse_args()

    api_manifest_paths = {}
    for api_name, arg_name in API_MANIFEST_ARGS:
        api_manifest_paths[api_name] = getattr(args, arg_name)

    seed_manifest = generate_seed_manifest(api_manifest_paths)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(seed_manifest, f, indent=2)

    logger.info("Wrote seed-manifest.json to %s", args.output)


if __name__ == "__main__":
    main()
