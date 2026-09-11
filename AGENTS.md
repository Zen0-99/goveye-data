# goveye-data — Agent & Contributor Guide

## Critical: When NOT to run a build script

Before running any `build_*.py` script, ask: **Is the source data changing, or just the transformation?**

- **Source data change** (new votes published, MP interests amended, bill stages updated) → run the build script in delta mode. It fetches fresh data from the Parliament API and upserts.
- **Transformation logic change** (re-mapping a derived column like `bucket`, changing a parser, updating a tag algorithm) → **do NOT run the build script**. Run the SQL directly against the existing DB. The build script will re-fetch all 650 MPs from the API for no reason.

### Derived columns by script

| Script | Derived columns | Source columns |
|--------|----------------|----------------|
| `build_interests.py` | `bucket`, `parsedAmountPence`, `currencyCode` | `categoryNumber`, `fieldsJson`, `summary` |
| `build_precompute.py` | `activityScore`, `rebellionRate`, `voteParticipationRate`, all `*Percentile` columns | `division_votes`, `debate_speeches`, `committees` |
| `build_tags.py` | `tag`, `hitCount` (in `division_tags`, `bill_tags`, etc.) | `divisions`, `bills`, `debate_speeches` |
| `build_ipsa.py` | `bucket` | `category`, `type` |
| `build_mp_tags.py` | `tag`, `hitCount` (in `mp_tags`) | `written_statements`, `government_publications`, `legislation` |
| `build_bills.py` | `currentStageDescription`, `currentStageAbbreviation` | `bill_stages` |

When a derived column's logic changes:
1. Write the SQL `UPDATE ... SET col = CASE...END` (this is the same SQL as the Room migration in GovEye's `DatabaseModule.kt`)
2. Run it directly: `python -c "import sqlite3; c=sqlite3.connect('interests.db'); c.execute('...'); c.commit(); c.close()"`
3. The Room migration handles the same change on user devices — no DB re-download needed

## Pipeline overview

### Build scripts → per-API DBs → merge → seed DB

Each `build_*.py` script fetches from one Parliament API and writes a per-API SQLite DB (e.g. `interests.db`, `mps.db`). Per-API DBs are independent — running one does not affect another. They only interact at merge time.

`merge_dbs.py` combines all per-API DBs into `goveye.db` (the seed DB bundled in the APK / downloaded on first launch).

### Per-API DBs and their APIs

| Script | DB file | Tables | API |
|--------|---------|--------|-----|
| `build_mps.py` | mps.db | mps | Members API |
| `build_commons_votes.py` | commons_votes.db | divisions, division_votes | Commons Votes API |
| `build_lords_votes.py` | lords_votes.db | divisions, division_votes | Lords Votes API |
| `build_bills.py` | bills.db | bills, bill_stages | Bills API |
| `build_committees.py` | committees.db | committees, mp_committee_cross_ref | Committees API |
| `build_recess.py` | recess.db | recess_dates, recess_dates_meta | Egg Timer API |
| `build_interests.py` | interests.db | interests | Interests API |
| `build_ipsa.py` | expenses.db | expenses | IPSA CSV |
| `build_mnis.py` | mps.db (enrichment) | mps (additional columns) | MNIS API |
| `build_hansard.py` | hansard.db | hansard_contributions | Hansard API |
| `build_debates.py` | debates.db | debate_speeches | Hansard API |
| `build_member_details.py` | member_details.db | mp_synopsis, mp_contacts, mp_experience | Members API |
| `build_party_stats.py` | party_stats.db | party_stats | Computed from divisions |
| `build_bio_data.py` | bio_data.db | bio_data | Members API |
| `build_manifestos.py` | manifestos.db | party_manifestos | TheyWorkForYou |
| `build_mp_links.py` | mp_links.db | mp_links | Members API |
| `build_councils.py` | councils.db | councils | GovRegister API |
| `build_postcodes_councils.py` | (enrichment) | postcodes → council mapping | ONS Postcode Directory |
| `build_gov_publications.py` | gov_publications.db | government_publications, _publication_bodies | GOV.UK Content API |
| `build_written_statements.py` | written_statements.db | written_statements | Written Statements API |
| `build_legislation.py` | legislation.db | legislation | Legislation API |
| `build_historical_members.py` | historical_members.db | historical_members | Members API (historical) |
| `build_historical_interests.py` | historical_interests.db | interests (historical) | Interests API (historical) |
| `build_party_leaders.py` | (enrichment) | party_leaders | Members API |
| `build_source_recs.py` | (enrichment) | source_recommendations | Computed from tags |
| `build_precompute.py` | precompute.db | mp_stats | Computed from divisions/speeches/committees |
| `build_tags.py` | tags.db | division_tags, bill_tags, etc. | Computed from divisions/bills/debates |
| `build_mp_tags.py` | mp_tags.db | mp_tags | Computed from statements/publications/legislation |

### Post-build scripts (no API — computed from existing DBs)

These scripts derive data from other per-API DBs. They do NOT fetch from external APIs:
- `build_precompute.py` — MP statistics (rebellion rate, participation, activity score, percentiles)
- `build_tags.py` — Tag extraction from division/bill/debate text
- `build_mp_tags.py` — MP-level tag aggregation from statements/publications/legislation
- `build_party_stats.py` — Party-level voting statistics
- `build_source_recs.py` — Source recommendation ranking from tag metadata

### Merge and post-merge scripts

- `merge_dbs.py` — Combines all per-API DBs into `goveye.db`
- `merge_interests.py` — Merges live interests DB with historical interests DB
- `build_db.py` — Legacy single build script (kept for reference, no workflow references it)

## Build modes

All `build_*.py` scripts support two modes:

- **seed** — Create fresh DB, fetch all data from API, insert. Slow (full fetch). Used for first build or when schema changes.
- **delta** — Copy previous DB, fetch all data from API, upsert (INSERT OR REPLACE). Still fetches everything from the API — the "delta" is in the DB output (only changed rows produce a diff patch), not in the fetch.

**Delta mode does NOT skip API calls.** It re-fetches all data and upserts. The only scripts with true API-skip logic are `build_precompute.py` and `build_tags.py` (via `--changed-apis` flag).

## Enrichment data must have an owning stream

Any data that lives in `goveye.db` must be produced by a `build_*.py` script
that owns a per-API DB — otherwise it can't reach devices via patches and
will be silently lost on the next seed rebuild.

- **Good**: Wikipedia DOBs live in `bio_data.db` via `build_mnis.py` +
  `build_wikipedia_bios.py --dob-only` in `update-bio-data.yml`. Future
  bio-data patches carry them.
- **Bad**: writing enrichment only into the merged `goveye.db` during
  build-seed — the data never lands in a per-API DB, so no patch carries it
  and devices that update incrementally never see it.

When adding a new data layer (new table, new enrichment):
1. Give it a `build_*.py` that writes a per-API DB (or enriches an existing one).
2. Give it an `update-*.yml` workflow that produces a diff patch.
3. Add the table's real PK to `diff_db.py` `TABLE_PRIMARY_KEYS` — missing
   entries silently collapse the patch to one upsert.
4. Add the table to `merge_dbs.py` `PER_API_TABLES`.
5. Verify `validate_schema.py` passes — it now checks full Room TableInfo
   parity (columns, affinities, PKs, indices, FKs, FTS triggers), which is
   exactly what Room validates on device after migrations.

## Schema sync

**Automated:** `sync_schema.py` fetches the latest Room schema JSON from the GovEye
GitHub repo and overwrites `schemas/bundled_schema.json` before every CI build.
The committed `bundled_schema.json` is a fallback (used if the fetch fails) and
for local dev. Run `python sync_schema.py` manually after pulling GovEye changes
that modify Room entities.

**Drift gate:** `sync_schema.py` also fetches `BundledDatabase.kt` and compares
its `version = N` against the latest schema JSON version. If the app's DB
version is ahead (schema export not pushed), the workflow fails — a seed built
at a stale schema opens via Room migrations on device, and any migration gap
crashes the app. If you hit this: export the Room schema in GovEye
(`N.json`), push, and re-run.

When Room entities change in the GovEye app:
1. Export the new schema JSON from Room (`exportSchema=true`) → `core/data/schemas/com.goveye.app.data.local.BundledDatabase/N.json`
2. Bump `version` in `BundledDatabase.kt` and add a migration in `DatabaseModule.kt`
3. Push GovEye — goveye-data CI will auto-sync the schema on the next workflow run
   (the drift gate fails the build if the schema JSON wasn't pushed)
4. Update build scripts if new entities need data fetching
5. Trigger a seed build

## Running tests

```bash
python -m unittest discover tests -v
```

## Common pitfalls

- **Don't run build scripts for derived column changes** — see "When NOT to run" above
- **Per-API DBs only have their own tables** — `validate_schema.py` checks all tables and will fail on a per-API DB. It's only run on the merged `goveye.db`.
- **FTS tables auto-populate via triggers** — don't insert into FTS tables directly in build scripts
- **WAL files on CI** — if a build script leaves an uncheckpointed WAL, `ATTACH DATABASE` will fail with "database is locked". Run `PRAGMA wal_checkpoint(TRUNCATE)` before ATTACH, or read into memory instead (see `merge_interests.py`).
