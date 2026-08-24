#!/usr/bin/env python3
"""Generate seed-manifest.json with per-API dbHashes for change detection.

The seed manifest is what check_seed.py compares on the next Build Seed run
to determine if any per-API DB changed and the seed needs rebuilding.

Structure:
  {
    "version": 1,
    "generatedAt": "2026-08-22T...",
    "apiHashes": {
      "mps": "<sha256 from mps manifest>",
      "commons_votes": "<sha256 from commons manifest>",
      "lords_votes": "<sha256 from lords manifest>",
      "bills": "<sha256 from bills manifest>",
      "committees": "<sha256 from committees manifest>",
      "recess": "<sha256 from recess manifest>",
      "interests": "<sha256 from interests manifest>"
    }
  }

Usage:
  python generate_seed_manifest.py --output seed-manifest.json \
    --mps-manifest mps_manifest.json \
    --commons-votes-manifest commons_manifest.json \
    --lords-votes-manifest lords_manifest.json \
    --bills-manifest bills_manifest.json \
    --committees-manifest committees_manifest.json \
    --recess-manifest recess_manifest.json \
    --interests-manifest interests_manifest.json
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("generate_seed_manifest")

# Maps CLI argument names to the key used in apiHashes.
PER_API = [
    ("mps", "mps-manifest"),
    ("commons_votes", "commons-votes-manifest"),
    ("lords_votes", "lords-votes-manifest"),
    ("bills", "bills-manifest"),
    ("committees", "committees-manifest"),
    ("recess", "recess-manifest"),
    ("interests", "interests-manifest"),
    ("party_stats", "party-stats-manifest"),
    ("bio_data", "bio-data-manifest"),
    ("expenses", "expenses-manifest"),
    ("mp_links", "mp-links-manifest"),
    ("manifestos", "manifestos-manifest"),
    ("historical_members", "historical-members-manifest"),
    ("debates", "debates-manifest"),
    ("member_details", "member-details-manifest"),
    ("hansard", "hansard-manifest"),
    ("gov_publications", "gov-publications-manifest"),
    ("written_statements", "written-statements-manifest"),
    ("legislation", "legislation-manifest"),
]


def load_manifest(path):
    """Load a manifest JSON file, returning None if it doesn't exist."""
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Scripts whose code changes should trigger a seed rebuild even if no
# per-API data changed. check_seed.py compares these hashes against the
# stored values in seed-manifest.json.
TRACKED_SCRIPTS = ["build_precompute.py", "build_tags.py"]


def compute_file_hash(path):
    """Compute SHA-256 of a file's content (UTF-8, normalized line endings)."""
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def generate_seed_manifest(output_path, per_api_manifest_paths,
                           previous_seed_manifest_path=None):
    """Generate seed-manifest.json with per-API dbHashes.

    Args:
        output_path: Path to write seed-manifest.json.
        per_api_manifest_paths: Dict mapping API key to manifest path.
        previous_seed_manifest_path: Optional path to previous seed manifest
            for version increment.
    """
    # Determine version
    previous_version = None
    version = 1
    if previous_seed_manifest_path and os.path.exists(previous_seed_manifest_path):
        with open(previous_seed_manifest_path, "r", encoding="utf-8") as f:
            prev = json.load(f)
        previous_version = prev.get("version", 0)
        version = previous_version + 1

    # Extract dbHash from each per-API manifest
    api_hashes = {}
    for api_key, arg_name in PER_API:
        manifest_path = per_api_manifest_paths.get(api_key)
        manifest = load_manifest(manifest_path)
        if manifest is None:
            logger.warning("  %s manifest not found — storing null hash", api_key)
            api_hashes[api_key] = None
        else:
            api_hashes[api_key] = manifest.get("dbHash")
            logger.info("  %s: %s", api_key,
                        api_hashes[api_key][:12] if api_hashes[api_key] else "none")

    # Compute code hashes for tracked scripts (build_precompute.py, build_tags.py).
    # If these scripts change but no per-API data changed, check_seed.py will
    # detect the hash difference and force a full rebuild.
    code_hashes = {}
    for script in TRACKED_SCRIPTS:
        code_hashes[script] = compute_file_hash(script)
        logger.info("  code:%s: %s", script,
                    code_hashes[script][:12] if code_hashes[script] else "none")

    seed_manifest = {
        "version": version,
        "previousVersion": previous_version,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "apiHashes": api_hashes,
        "codeHashes": code_hashes,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(seed_manifest, f, indent=2, ensure_ascii=False)

    logger.info("Seed manifest written to %s (version %d)", output_path, version)


def main():
    parser = argparse.ArgumentParser(
        description="Generate seed-manifest.json with per-API dbHashes."
    )
    parser.add_argument("--output", required=True,
                        help="Path to write seed-manifest.json.")
    parser.add_argument("--mps-manifest", default=None)
    parser.add_argument("--commons-votes-manifest", default=None)
    parser.add_argument("--lords-votes-manifest", default=None)
    parser.add_argument("--bills-manifest", default=None)
    parser.add_argument("--committees-manifest", default=None)
    parser.add_argument("--recess-manifest", default=None)
    parser.add_argument("--interests-manifest", default=None)
    parser.add_argument("--party-stats-manifest", default=None)
    parser.add_argument("--bio-data-manifest", default=None)
    parser.add_argument("--expenses-manifest", default=None)
    parser.add_argument("--mp-links-manifest", default=None)
    parser.add_argument("--manifestos-manifest", default=None)
    parser.add_argument("--historical-members-manifest", default=None)
    parser.add_argument("--debates-manifest", default=None)
    parser.add_argument("--member-details-manifest", default=None)
    parser.add_argument("--hansard-manifest", default=None)
    parser.add_argument("--gov-publications-manifest", default=None)
    parser.add_argument("--written-statements-manifest", default=None)
    parser.add_argument("--legislation-manifest", default=None)
    parser.add_argument("--previous-seed-manifest", default=None,
                        help="Path to previous seed-manifest.json for version increment.")
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
        "bio_data": args.bio_data_manifest,
        "expenses": args.expenses_manifest,
        "mp_links": args.mp_links_manifest,
        "manifestos": args.manifestos_manifest,
        "historical_members": args.historical_members_manifest,
        "debates": args.debates_manifest,
        "member_details": args.member_details_manifest,
        "hansard": args.hansard_manifest,
        "gov_publications": args.gov_publications_manifest,
        "written_statements": args.written_statements_manifest,
        "legislation": args.legislation_manifest,
    }

    generate_seed_manifest(
        args.output, per_api_paths,
        previous_seed_manifest_path=args.previous_seed_manifest,
    )


if __name__ == "__main__":
    main()
