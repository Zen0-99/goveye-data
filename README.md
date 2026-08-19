# goveye-data

Bundled database hosting for [GovEye](https://github.com/Zen0-99/GovEye) — a UK Parliament monitoring app.

This repo is self-contained: it contains the Python build scripts, GitHub Action workflows, Room schema JSON, and the release tags that host the built DBs. It does **not** check out the GovEye code repo. When Room entities change in GovEye (rare — deliberate DB version bump), the new `schemas/N.json` is manually re-copied here.

## Purpose

Per decision D-06, the DB is hosted in a separate public repo so that:
- Release history is separated (app releases vs DB releases)
- DB hosting is independent of code repo visibility
- The build scripts, schema JSON, and Actions all live together

The DB eliminates all runtime API load for directory, voting, bills, committees, and recess data. It's built on staggered schedules in CI and downloaded by the app on first launch. Past voting data is immutable and never re-fetched (D-03).

## Multi-Database Architecture (D-10)

The single `build_db.py` has been refactored into **5 per-API build scripts** + **1 merge script**. Each build script creates its own per-API DB with only its tables, fetches its data source, and publishes to its own release tag. `merge_dbs.py` combines the 5 per-API DBs into one `goveye.db` for the seed release (D-10a).

### Why split?

- **Staggered builds** — only the data source that changed gets a new release
- **Smaller patches** — each patch only contains changes for one API's tables
- **Different update frequencies** — votes change hourly, MPs daily, committees weekly
- **Independent failure isolation** — a Lords Votes API timeout only affects `votes.db`, not the entire DB. Commons data is committed first in `build_votes.py` so a Lords failure still publishes Commons data (resilience fix)

### Build scripts

| Script | DB | Tables | API | Schedule | Release tag |
|--------|-----|--------|-----|----------|-------------|
| `build_mps.py` | mps.db | mps, mps_fts | Members API | daily 3am UTC | mps-latest |
| `build_votes.py` | votes.db | divisions, division_votes | Commons + Lords Votes API | 4x daily (every 6h, D-09) | votes-latest |
| `build_bills.py` | bills.db | bills, bill_stages | Bills API | 2x daily (every 12h) | bills-latest |
| `build_committees.py` | committees.db | committees, mp_committee_cross_ref | Committees API (per-MP) | weekly (Monday 4am) | committees-latest |
| `build_recess.py` | recess.db | recess_dates, recess_dates_meta | Egg Timer API (HTML) | monthly (1st, 5am) | recess-latest |
| `merge_dbs.py` | goveye.db | all 16 tables | (combines the 5 above) | manual (workflow_dispatch) | seed-latest |

### Shared modules

- `api_helper.py` — shared `api_get()` with 60s timeout, 3 retries, exponential backoff (extracted from build_db.py; all 5 build scripts import it)
- `schema.py` — schema parser + `create_database_with_tables()` which creates a per-API DB with only its tables + the full schema's Room identity hash
- `diff_db.py` — generates JSON diff patches; accepts `--tables` to only diff the tables relevant to a build script (D-10)
- `manifest.py` — generates manifest.json with version, SHA-256 hashes, and sizes
- `validate_schema.py` — validates a built DB against the Room schema (used on the merged goveye.db only)

### Resilience design in build_votes.py

`build_votes.py` fetches Commons divisions + votes FIRST, commits to the DB, THEN fetches Lords divisions + votes. If the Lords API fails (timeout), the build script catches the exception, logs a warning, and exits successfully with Commons-only data. This is the fix for the first build attempt crash where a Lords timeout took down the entire build.

## Contents

```
goveye-data/
├── api_helper.py          # Shared api_get() retry helper (D-10)
├── build_mps.py           # Per-API build: MPs → mps.db (daily)
├── build_votes.py         # Per-API build: Commons + Lords votes → votes.db (4x daily)
├── build_bills.py         # Per-API build: bills + stages → bills.db (2x daily)
├── build_committees.py    # Per-API build: committees → committees.db (weekly)
├── build_recess.py        # Per-API build: recess dates → recess.db (monthly)
├── merge_dbs.py           # Merges 5 per-API DBs → goveye.db (seed release, D-10a)
├── build_db.py            # Legacy single build script (kept for reference, no workflow references it)
├── schema.py              # Schema parser + create_database_with_tables()
├── validate_schema.py     # Validates built DB against schema (fails CI on drift)
├── diff_db.py             # Generates JSON diff patch (--tables filter, D-10)
├── manifest.py            # Generates manifest.json (version, hashes, sizes)
├── requirements.txt       # requests>=2.31.0 (all other deps are stdlib)
├── schemas/
│   └── 8.json             # Room exported schema JSON (source of truth)
├── tests/
│   ├── test_api_helper.py     # Unit tests for api_get() retry logic
│   ├── test_build_mps.py      # Unit tests for build_mps.py
│   ├── test_build_votes.py    # Unit tests for build_votes.py (incl. Lords resilience)
│   ├── test_build_bills.py    # Unit tests for build_bills.py
│   ├── test_build_committees.py # Unit tests for build_committees.py
│   ├── test_build_recess.py   # Unit tests for build_recess.py (HTML parsing)
│   ├── test_merge_dbs.py      # Unit tests for merge_dbs.py
│   ├── test_build_db.py       # Unit tests for legacy build_db.py
│   └── test_diff_db.py        # Unit tests for diff_db.py and manifest.py
└── .github/workflows/
    ├── update-mps.yml         # Daily MPs build → mps-latest
    ├── update-votes.yml       # 4x daily votes build → votes-latest
    ├── update-bills.yml       # 2x daily bills build → bills-latest
    ├── update-committees.yml  # Weekly committees build → committees-latest
    ├── update-recess.yml      # Monthly recess build → recess-latest
    └── build-seed.yml         # Manual seed build (all 5 + merge) → seed-latest
```

## Running the build scripts locally

```bash
# Install dependencies (only requests is non-stdlib)
pip install -r requirements.txt

# --- Per-API builds (each creates its own DB with only its tables) ---

# MPs (daily)
python build_mps.py --output mps.db --schema schemas/8.json --mode seed
python build_mps.py --output mps.db --schema schemas/8.json --mode delta --previous-db prev_mps.db

# Votes (4x daily) — Commons first, then Lords (resilient)
python build_votes.py --output votes.db --schema schemas/8.json --mode seed
python build_votes.py --output votes.db --schema schemas/8.json --mode delta --previous-db prev_votes.db

# Bills (2x daily)
python build_bills.py --output bills.db --schema schemas/8.json --mode seed
python build_bills.py --output bills.db --schema schemas/8.json --mode delta --previous-db prev_bills.db

# Committees (weekly — 650 per-MP API calls)
python build_committees.py --output committees.db --schema schemas/8.json --mode seed
python build_committees.py --output committees.db --schema schemas/8.json --mode delta --previous-db prev_committees.db

# Recess dates (monthly — Egg Timer HTML scraping)
python build_recess.py --output recess.db --schema schemas/8.json --mode seed
python build_recess.py --output recess.db --schema schemas/8.json --mode delta --previous-db prev_recess.db

# --- Merge (combines the 5 per-API DBs into goveye.db for the seed release) ---
python merge_dbs.py --output goveye.db --schema schemas/8.json \
    --mps-db mps.db --votes-db votes.db --bills-db bills.db \
    --committees-db committees.db --recess-db recess.db

# --- Validate the merged DB against the schema ---
python validate_schema.py --db goveye.db --schema schemas/8.json

# --- Generate a diff patch (per-API, with --tables filter) ---
python diff_db.py --new mps.db --previous prev_mps.db --schema schemas/8.json --output patch.json --tables mps,mps_fts

# --- Generate manifest ---
python manifest.py --db mps.db --patch patch.json --output manifest.json --schema schemas/8.json

# --- Run tests ---
python -m unittest discover tests -v
```

### Testing flags

- `--mp-limit N`: Limit number of MPs fetched (build_mps, build_committees)
- `--divisions-limit N`: Limit divisions fetched per house (build_votes)
- `--bill-limit N`: Limit number of bills fetched (build_bills)

## Data sources

Per D-08, only these APIs are used (no MNIS — deferred to Phase 11):

| API | Base URL | Used by | Usage |
|-----|----------|---------|-------|
| Members API | `https://members-api.parliament.uk/api/` | build_mps, build_committees | All 650 current Commons MPs (House=1) |
| Commons Votes API | `https://commonsvotes-api.parliament.uk/data/` | build_votes | All Commons divisions + votes (house=1) |
| Lords Votes API | `https://lordsvotes-api.parliament.uk/data/` | build_votes | All Lords divisions + votes (house=2) |
| Bills API | `https://bills-api.parliament.uk/api/v1/` | build_bills | All bills + bill stages |
| Committees API | `https://committees-api.parliament.uk/api/` | build_committees | Per-MP committee memberships (650 calls) |
| Egg Timer API | `https://api.parliament.uk/egg-timer/` | build_recess | Recess dates (HTML scraping) |

**Note:** MP photos are NOT bundled — `thumbnailUrl` is stored as URL text only (DATA-04). The app loads photos on-demand via Coil.

## GitHub Action workflows

### Per-API workflows (5)

Each per-API workflow runs on its own schedule + `workflow_dispatch`:

1. Downloads the previous per-API DB + manifest from its release tag
2. Builds the DB (delta mode if previous exists, seed mode on first run)
3. Generates a diff patch with `--tables` filter (only that API's tables)
4. Generates a manifest with version increment
5. Publishes the DB + patch + manifest to its release tag

Per-API DBs are intermediate artifacts — `validate_schema.py` is NOT run on them (it checks all 16 tables, but per-API DBs only have 2). Schema validation happens on the merged `goveye.db` in `build-seed.yml`.

### Seed workflow (build-seed.yml)

Triggered manually (`workflow_dispatch` only — the seed is built on demand):

1. Runs all 5 per-API build scripts in seed mode
2. Runs `merge_dbs.py` to combine them into `goveye.db` (all 16 tables + correct identity hash)
3. Runs `validate_schema.py` on the merged DB (PASSES — all 16 tables present)
4. Publishes `goveye.db` to the `seed-latest` release tag

This is the first-launch download source for the Android app (D-04).

## Schema sync

When Room entities change in the GovEye app (rare — deliberate DB version bump):

1. Export the new schema JSON from Room (`exportSchema=true`)
2. Copy the new `N.json` to `goveye-data/schemas/`
3. Update the build scripts if new entities need data fetching
4. Update `validate_schema.py` if the identity hash changed
5. Trigger a seed build via `build-seed.yml` `workflow_dispatch`

## manifest.json format

```json
{
  "version": 42,
  "previousVersion": 41,
  "schemaVersion": 8,
  "generatedAt": "2026-08-20T03:00:00+00:00",
  "dbHash": "sha256-hex-of-db",
  "dbSize": 16777216,
  "patchHash": "sha256-hex-of-patch.json",
  "patchSize": 24576
}
```

The app fetches this ~200 byte file on startup to check if an update is available (DATA-03). Each release tag (mps-latest, votes-latest, etc.) has its own manifest with its own version sequence.

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
    }
  }
}
```

The patch maps directly to Android Room DAO `@Upsert` and `@Query DELETE` operations — no raw SQL execution on Android (no injection risk). Per-API patches only contain changes for that API's tables (e.g. the mps-latest patch only has `mps` changes).

## Decisions referenced

- **D-01:** Python build scripts (stdlib sqlite3 + requests, no JVM in CI)
- **D-02:** Bundle ALL historical voting data (~5,000+ divisions)
- **D-03:** Daily Action fetches only new divisions — past is immutable
- **D-04:** First-launch download from GitHub Releases (seed-latest tag)
- **D-06:** Separate public repo for DB hosting
- **D-07:** Include Lords divisions alongside Commons
- **D-08:** MNIS deferred to Phase 11 — Members/Votes/Bills/Committees/Egg Timer APIs only
- **D-09:** Votes updated 4x daily (every 6h) — DB-patch notifications with 6h latency
- **D-10:** 5 separate per-API build scripts, each with own manifest + patch stream + schedule + release tag
- **D-10a:** Android hybrid 2-database architecture — merge_dbs.py produces the seed goveye.db; per-API patches all apply to the single goveye.db
- **D-11:** Bundle bills + committees + recess dates; keep hansard + profile detail as live API
