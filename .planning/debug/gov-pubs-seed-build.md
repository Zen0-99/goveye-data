---
slug: gov-pubs-seed-build
status: resolved
trigger: "Gov Publications seed build keeps failing on CI with different errors — _publication_bodies schema mismatch, imageUrl dict binding error, 58-min timeout. User wants to build locally instead of CI cat-and-mouse."
created: 2026-08-24
updated: 2026-08-24
---

# Debug: Gov Publications Seed Build

## Symptoms
- **Expected:** `build_gov_publications.py --mode seed` produces a valid `gov_publications.db` with 90 days of publications from all GOV.UK departments
- **Actual:** CI seed build fails with various errors:
  1. `table _publication_bodies has no column named id` (fixed — wrong column name in duplicate CREATE TABLE)
  2. `Error binding parameter 10: type 'dict' is not supported` (fixed — imageUrl is a dict from Content API)
  3. Cancelled at 58-min timeout (seed build too slow for CI free tier)
- **Timeline:** Started failing when Phase 14 added gov_publications as a new per-API DB. No previous release exists, so workflow can't use delta mode.
- **Reproduction:** Run `python build_gov_publications.py --output gov_publications.db --schema schemas/bundled_schema.json --mode seed` — takes 30+ min, may hit data binding errors

## Current Focus
- **hypothesis:** Building locally will let us iterate on data issues fast, then publish the working DB directly to the `gov-publications-latest` release (same approach as Interests bootstrap)
- **next_action:** Run build_gov_publications.py locally in seed mode, capture any errors, fix, repeat until clean DB produced

## Evidence
- (timestamp: 2026-08-24) CI run 32720845424 failed: `_publication_bodies has no column named id` — checkpoint DB from earlier failed run had wrong schema
- (timestamp: 2026-08-24) CI run 32721575752 failed: `Error binding parameter 10: type 'dict' is not supported` — imageUrl from Content API is a dict, not string
- (timestamp: 2026-08-24) CI run 32729475274 cancelled at 58-min timeout — seed build fetching from ~52 departments + Content API detail fetches

## Eliminated
- (hypothesis: gov.uk API endpoint was wrong) — Fixed earlier, `/api/organisations` works, publications are being fetched successfully

## Resolution
- root_cause: Three separate issues: (1) duplicate CREATE TABLE for _publication_bodies used wrong column name, (2) GOV.UK Content API returns image as dict not string, (3) seed build takes ~50 min fetching 5766 publications from 25 departments with 0.2s delay per Content API fetch — exceeds CI iteration speed
- fix: (1) removed duplicate CREATE TABLE, added schema migration to drop wrong-schema table, (2) extract url from dict, (3) built locally and published directly to gov-publications-latest release — future runs use delta mode
- verification: Local build completed successfully — 5766 publications, 0 errors, DB published to GitHub release
- files_changed: build_gov_publications.py, .github/workflows/update-gov-publications.yml
