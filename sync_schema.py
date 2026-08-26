#!/usr/bin/env python3
"""Sync bundled_schema.json from the GovEye app repo.

Fetches the latest Room-exported schema JSON from the GovEye GitHub repo
(the source of truth) and overwrites schemas/bundled_schema.json. Falls
back to the committed version if the fetch fails (network error, rate
limit, repo restructured).

Usage:
    python sync_schema.py [--schema-dir schemas] [--repo Zen0-99/GovEye]

In CI, run this BEFORE any build script that references bundled_schema.json.
Locally, run it after pulling GovEye changes that modify Room entities.
"""

import argparse
import json
import logging
import os
import sys
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Room schema directory in GovEye — derived from the @Database class FQN:
#   com.goveye.app.data.local.BundledDatabase
# Room exports to: <schemaLocation>/com.goveye.app.data.local.BundledDatabase/<version>.json
SCHEMA_SUBDIR = "core/data/schemas/com.goveye.app.data.local.BundledDatabase"

# GitHub API endpoint for listing directory contents
GITHUB_CONTENTS_API = "https://api.github.com/repos/{repo}/contents/{path}"

# Raw file download URL pattern
GITHUB_RAW_URL = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"


def fetch_latest_schema_version(repo: str) -> int | None:
    """Query GitHub API for the highest-numbered schema JSON in the repo.

    Returns the version number (e.g. 26), or None if the fetch fails.
    """
    url = GITHUB_CONTENTS_API.format(repo=repo, path=SCHEMA_SUBDIR)
    req = urllib.request.Request(url, headers={"User-Agent": "goveye-data-sync"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            files = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to list schema directory from %s: %s", url, e)
        return None

    versions = []
    for f in files:
        name = f.get("name", "")
        if name.endswith(".json"):
            try:
                versions.append(int(name.removesuffix(".json")))
            except ValueError:
                continue
    if not versions:
        logger.warning("No schema JSON files found in %s", SCHEMA_SUBDIR)
        return None
    return max(versions)


def download_schema(repo: str, version: int, branch: str = "master") -> str | None:
    """Download a specific schema version's JSON content.

    Returns the JSON string, or None if the fetch fails.
    """
    path = f"{SCHEMA_SUBDIR}/{version}.json"
    url = GITHUB_RAW_URL.format(repo=repo, branch=branch, path=path)
    req = urllib.request.Request(url, headers={"User-Agent": "goveye-data-sync"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        logger.warning("Failed to download schema from %s: %s", url, e)
        return None


def sync_schema(schema_dir: str, repo: str, branch: str = "master") -> bool:
    """Sync the latest schema from GovEye to schemas/bundled_schema.json.

    Returns True if the schema was updated (or is already current),
    False if the fetch failed and we fell back to the committed version.
    """
    dest_path = os.path.join(schema_dir, "bundled_schema.json")

    # 1. Find the latest schema version in GovEye
    latest_version = fetch_latest_schema_version(repo)
    if latest_version is None:
        logger.warning(
            "Could not fetch schema version from GovEye — "
            "falling back to committed %s", dest_path
        )
        if os.path.exists(dest_path):
            logger.info("Using committed schema as fallback")
            return False
        else:
            logger.error("No committed schema fallback exists — aborting")
            sys.exit(1)

    logger.info("Latest GovEye schema version: v%d", latest_version)

    # 2. Download the schema JSON
    schema_json = download_schema(repo, latest_version, branch)
    if schema_json is None:
        # Try main branch if master failed
        if branch == "master":
            logger.info("Master branch failed — trying main")
            schema_json = download_schema(repo, latest_version, branch="main")
    if schema_json is None:
        logger.warning(
            "Could not download schema v%d — falling back to committed %s",
            latest_version, dest_path
        )
        if os.path.exists(dest_path):
            logger.info("Using committed schema as fallback")
            return False
        else:
            logger.error("No committed schema fallback exists — aborting")
            sys.exit(1)

    # 3. Check if the committed version is already current
    new_data = json.loads(schema_json)
    if os.path.exists(dest_path):
        with open(dest_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
        if old_data == new_data:
            logger.info("Schema already current (v%d) — no update needed", latest_version)
            return True

    # 4. Write the new schema
    os.makedirs(schema_dir, exist_ok=True)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(schema_json)
    logger.info("Synced schema v%d → %s", latest_version, dest_path)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Sync bundled_schema.json from the GovEye app repo."
    )
    parser.add_argument(
        "--schema-dir", default="schemas",
        help="Directory for bundled_schema.json (default: schemas)",
    )
    parser.add_argument(
        "--repo", default="Zen0-99/GovEye",
        help="GovEye GitHub repo (owner/name, default: Zen0-99/GovEye)",
    )
    parser.add_argument(
        "--branch", default="master",
        help="Branch to fetch from (default: master, falls back to main)",
    )
    args = parser.parse_args()

    success = sync_schema(args.schema_dir, args.repo, args.branch)
    if success:
        print("Schema sync complete")
    else:
        print("Schema sync fell back to committed version — check network/repo")


if __name__ == "__main__":
    main()
