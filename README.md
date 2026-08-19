# goveye-data

Bundled database hosting for [GovEye](https://github.com/Zen0-99/GovEye) — a UK Parliament monitoring app.

This repo is self-contained: it contains the Python build script, GitHub Action workflow, Room schema JSON, and the `database-latest` release tag that hosts the built DB. It does **not** check out the GovEye code repo. When Room entities change in GovEye (rare — deliberate DB version bump), the new `schemas/N.json` is manually re-copied here.

## Purpose

Per decision D-06, the DB is hosted in a separate public repo so that:
- Release history is separated (app releases vs DB releases)
- DB hosting is independent of code repo visibility
- The build script, schema JSON, and Action all live together

The DB eliminates all runtime API load for directory and voting data. It's built once daily in CI and downloaded by the app on first launch. Past voting data is immutable and never re-fetched (D-03).

## Contents

```
goveye-data/
├── build_db.py          # Main build script: fetch APIs, build SQLite DB
├── schema.py            # Schema parser: parses Room exported 8.json
├── validate_schema.py   # Validates built DB against schema (fails CI on drift)
├── diff_db.py           # Generates JSON diff patch (new DB vs previous)
├── manifest.py          # Generates manifest.json (version, hashes, sizes)
├── requirements.txt     # requests>=2.31.0 (all other deps are stdlib)
├── schemas/
│   └── 8.json           # Room exported schema JSON (source of truth)
├── tests/
│   ├── test_build_db.py # Unit tests for schema parsing, DB creation, MP insertion
│   └── test_diff_db.py  # Unit tests for diff generation and manifest
└── .github/workflows/
    └── update-database.yml  # Daily GitHub Action (cron 3am UTC + manual)
```

## Running the build script locally

```bash
# Install dependencies (only requests is non-stdlib)
pip install -r requirements.txt

# Seed build — full historical fetch (all 650 MPs + all divisions + all votes)
python build_db.py --output goveye.db --schema schemas/8.json --mode seed

# Delta build — incremental fetch (only new divisions since last run)
python build_db.py --output goveye.db --schema schemas/8.json --mode delta --previous-db prev_goveye.db

# Validate the built DB against the schema
python validate_schema.py --db goveye.db --schema schemas/8.json

# Generate a diff patch
python diff_db.py --new goveye.db --previous prev_goveye.db --schema schemas/8.json --output patch.json

# Generate manifest
python manifest.py --db goveye.db --patch patch.json --output manifest.json --schema schemas/8.json

# Run tests
python -m unittest discover tests -v
```

### Testing flags

- `--mp-limit N`: Limit number of MPs fetched (for testing)
- `--divisions-limit N`: Limit number of divisions fetched per house (for testing)

## Data sources

Per D-08, only these APIs are used (no MNIS — deferred to Phase 11):

| API | Base URL | Usage |
|-----|----------|-------|
| Members API | `https://members-api.parliament.uk/api/` | All 650 current Commons MPs (House=1) |
| Commons Votes API | `https://commonsvotes-api.parliament.uk/data/` | All Commons divisions + votes (house=1) |
| Lords Votes API | `https://lordsvotes-api.parliament.uk/data/` | All Lords divisions + votes (house=2) |

**Note:** The Commons Votes API returns a bare JSON list (not an object with items). The Lords Votes API also returns a bare list and uses PascalCase paths (`Divisions/search`, `Divisions/{id}`) with camelCase field names.

## Daily Action

The GitHub Action (`.github/workflows/update-database.yml`) runs daily at 3am UTC:

1. Downloads the previous `goveye.db` and `manifest.json` from the `database-latest` release
2. Builds the DB (delta mode if previous exists, seed mode on first run)
3. Validates the schema against `schemas/8.json` (fails on mismatch — D-01)
4. Generates a JSON diff patch (`patch.json`)
5. Generates a manifest (`manifest.json`) with version, SHA-256 hashes, and file sizes
6. Publishes all three files to the `database-latest` release tag (overwrites previous)

The `database-latest` release tag always points to the latest DB. The app fetches `manifest.json` on startup to check for updates.

## Schema sync

When Room entities change in the GovEye app (rare — deliberate DB version bump):

1. Export the new schema JSON from Room (`exportSchema=true`)
2. Copy the new `N.json` to `goveye-data/schemas/`
3. Update `build_db.py` if new entities need data fetching
4. Update `validate_schema.py` if the identity hash changed
5. Trigger a seed build via `workflow_dispatch`

## manifest.json format

```json
{
  "version": 42,
  "previousVersion": 41,
  "schemaVersion": 8,
  "generatedAt": "2026-08-20T03:00:00+00:00",
  "dbHash": "sha256-hex-of-goveye.db",
  "dbSize": 167772160,
  "patchHash": "sha256-hex-of-patch.json",
  "patchSize": 24576
}
```

The app fetches this ~200 byte file on startup to check if an update is available (DATA-03).

## patch.json format

```json
{
  "patchVersion": 42,
  "previousVersion": 41,
  "generatedAt": "2026-08-20T03:00:00+00:00",
  "schemaVersion": 8,
  "changes": {
    "mps": {
      "upsert": [{"id": 172, "nameListAs": "Abbott, Ms Diane", ...}],
      "delete": []
    },
    "divisions": {
      "upsert": [{"id": 2412, "title": "...", ...}],
      "delete": []
    },
    "division_votes": {
      "upsert": [{"divisionId": 2412, "memberId": 172, ...}],
      "delete": []
    }
  }
}
```

The patch maps directly to Android Room DAO `@Upsert` and `@Query DELETE` operations — no raw SQL execution on Android (no injection risk).

## Decisions referenced

- **D-01:** Python build script (stdlib sqlite3 + requests, no JVM in CI)
- **D-02:** Bundle ALL historical voting data (~5,000+ divisions)
- **D-03:** Daily Action fetches only new divisions — past is immutable
- **D-04:** First-launch download from GitHub Releases
- **D-06:** Separate public repo for DB hosting
- **D-07:** Include Lords divisions alongside Commons
- **D-08:** MNIS deferred to Phase 11 — Members API + Votes API only
