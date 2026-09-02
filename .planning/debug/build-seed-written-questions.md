---
slug: build-seed-written-questions
status: investigating
trigger: "Build Seed workflow fails because written_questions.db release doesn't exist. The written-questions workflow itself fails with Parliament API 500 errors on large paginated queries."
created: 2026-08-26
updated: 2026-08-26
---

# Debug Session: build-seed-written-questions

## Symptoms

1. **Expected behavior:** Build Seed workflow merges all 20 per-API DBs into goveye.db and publishes to seed-latest release.
2. **Actual behavior:** Build Seed fails at "Verify all 20 DBs present" step — written_questions.db is missing from per-api-cache and no release exists to download it from.
3. **Error messages:**
   - Build Seed: `ERROR: written_questions.db is missing — cannot build a complete seed`
   - Written Questions workflow: `requests.exceptions.HTTPError: 500 Server Error: Internal Server Error for url: https://questions-statements-api.parliament.uk/api/writtenquestions/questions?skip=5900&take=100`
   - Second run was cancelled (timeout or manual)
4. **Timeline:** written_questions workflow was added but never successfully completed. It runs weekly (Mon 7am UTC) and was manually triggered twice today — both failed.
5. **Reproduction:** Trigger `update-written-questions.yml` workflow — it fetches all written questions from Parliament API in pages of 100, fails at skip=5900 with 500 error.

## Prior fixes applied this session

1. **Schema sync:** Copied Room v26 schema to `goveye-data/schemas/bundled_schema.json` — fixed the original `bodyText` merge error. Committed and pushed.
2. **API retry fix:** Added 5xx HTTP error retry to `api_helper.py:api_get()` — previously only retried on ReadTimeout/ConnectionError. Committed and pushed.
3. **Local build attempt:** Started `build_written_questions.py --mode seed` locally — was still running after 20+ minutes, killed it.

## Current Focus

- hypothesis: The Parliament Written Questions API returns transient 500 errors on large paginated queries (skip=5900+). The retry fix may help but the seed build is also very slow (fetching thousands of questions). Need to investigate: (a) does the retry fix actually work, (b) is there a way to build written_questions.db locally and publish it manually to bootstrap the release, (c) should the workflow use checkpoint/resume like build_gov_publications.py does.
- next_action: Build written_questions.db locally with the retry fix applied, publish it manually to the written-questions-latest release, then re-trigger Build Seed.
- test: Run build_written_questions.py locally and verify it completes
- expecting: The retry fix allows the build to get past the 500 error at skip=5900

## Evidence

- timestamp: 2026-08-26T13:57 — Written Questions workflow failed with 500 Server Error at skip=5900
- timestamp: 2026-08-26T13:23 — Build Seed failed with "written_questions.db is missing"
- timestamp: 2026-08-26T14:22 — Local merge_dbs.py dry run succeeded with v26 schema (bodyText fix confirmed)
- timestamp: 2026-08-26T14:30 — api_helper.py updated to retry on 5xx errors, committed and pushed

## Eliminated

- hypothesis: The bodyText schema mismatch was the only Build Seed issue — eliminated, fixed by syncing to v26 schema. The remaining issue is written_questions.db not existing.
