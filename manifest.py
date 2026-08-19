#!/usr/bin/env python3
"""Generate manifest.json with version, SHA-256 hashes, and file sizes.

The manifest is what the Android app fetches on startup to check for
updates (DATA-03). It's ~200 bytes and contains:
  - version (int): incremented from previous manifest
  - previousVersion (int or null)
  - schemaVersion (8)
  - generatedAt (ISO timestamp)
  - dbHash (SHA-256 hex of goveye.db)
  - dbSize (bytes)
  - patchHash (SHA-256 hex of patch.json)
  - patchSize (bytes)

Usage:
  python manifest.py --db goveye.db --patch patch.json --output manifest.json
  python manifest.py --db goveye.db --patch patch.json --output manifest.json --previous-manifest prev_manifest.json
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import schema as schema_module


def compute_sha256(file_path):
    """Compute SHA-256 hash of a file.

    Args:
        file_path: Path to the file to hash.

    Returns:
        Hex digest string of the SHA-256 hash.
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read in 64KB chunks to handle large files
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def generate_manifest(db_path, patch_path, output_path, schema_path,
                      previous_manifest_path=None):
    """Generate manifest.json with version, hashes, and sizes.

    Args:
        db_path: Path to the goveye.db file.
        patch_path: Path to the patch.json file.
        output_path: Path to write manifest.json.
        schema_path: Path to the Room schema JSON (for schemaVersion).
        previous_manifest_path: Optional path to previous manifest for version increment.
    """
    schema = schema_module.load_schema(schema_path)
    schema_version = schema_module.get_version(schema)

    # Determine version numbers
    previous_version = None
    if previous_manifest_path and os.path.exists(previous_manifest_path):
        with open(previous_manifest_path, "r", encoding="utf-8") as f:
            prev_manifest = json.load(f)
        previous_version = prev_manifest.get("version", 0)
        version = previous_version + 1
    else:
        version = 1

    # Compute hashes and sizes
    db_hash = compute_sha256(db_path)
    db_size = os.path.getsize(db_path)

    patch_hash = compute_sha256(patch_path)
    patch_size = os.path.getsize(patch_path)

    manifest = {
        "version": version,
        "previousVersion": previous_version,
        "schemaVersion": schema_version,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dbHash": db_hash,
        "dbSize": db_size,
        "patchHash": patch_hash,
        "patchSize": patch_size,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Manifest written to {output_path}")
    print(f"  version: {version} (previous: {previous_version})")
    print(f"  dbHash: {db_hash[:16]}...")
    print(f"  dbSize: {db_size:,} bytes")
    print(f"  patchHash: {patch_hash[:16]}...")
    print(f"  patchSize: {patch_size:,} bytes")


def main():
    parser = argparse.ArgumentParser(
        description="Generate manifest.json with version, hashes, and sizes."
    )
    parser.add_argument(
        "--db", required=True,
        help="Path to the goveye.db file.",
    )
    parser.add_argument(
        "--patch", required=True,
        help="Path to the patch.json file.",
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to write manifest.json.",
    )
    parser.add_argument(
        "--schema", required=True,
        help="Path to the Room schema JSON (8.json).",
    )
    parser.add_argument(
        "--previous-manifest", default=None,
        help="Path to previous manifest.json for version increment.",
    )
    args = parser.parse_args()

    generate_manifest(
        args.db, args.patch, args.output, args.schema,
        previous_manifest_path=args.previous_manifest,
    )


if __name__ == "__main__":
    main()
