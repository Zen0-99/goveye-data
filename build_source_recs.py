#!/usr/bin/env python3
"""Post-merge build script for source recommendations (Phase 14, plan 14-02).

Runs AFTER merge_dbs.py and build_tags.py (which creates publication_tags).
Produces the source_recommendations table using a hybrid tag→department
mapping (D-06):

1. Hardcoded base mapping (TAG_TO_DEPARTMENTS): each of the 26 tags maps to
   a list of (organisationSlug, organisationName) tuples for departments
   that are relevant to that topic. These entries get isRecommended=1.

2. Data-driven refinement: query publication_tags JOINed with
   government_publications to get hit counts per (tag, organisationSlug).
   Departments with high hit counts for a tag get isRecommended=1 even if
   not in the hardcoded mapping.

Table produced:
  source_recommendations (tag, organisationSlug, organisationName, hitCount,
  isRecommended) — composite PK: (tag, organisationSlug).

Usage:
  python build_source_recs.py --output goveye.db --schema schemas/bundled_schema.json
"""

import argparse
import logging
import os
import sqlite3
import sys
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("build_source_recs")

# --- Hardcoded tag → department base mapping (D-06) ---
# Each tag maps to a list of (organisationSlug, organisationName) tuples.
# These are the departments most relevant to each topic. GOV.UK organisation
# slugs are used (matching the organisationSlug column in government_publications).
TAG_TO_DEPARTMENTS = {
    "Universal Credit": [
        ("department-for-work-pensions", "Department for Work & Pensions"),
    ],
    "PIP & Disability Benefits": [
        ("department-for-work-pensions", "Department for Work & Pensions"),
    ],
    "Disability": [
        ("department-for-work-pensions", "Department for Work & Pensions"),
        ("government-equalities-office", "Government Equalities Office"),
    ],
    "Welfare & Social Security": [
        ("department-for-work-pensions", "Department for Work & Pensions"),
        ("treasury", "Treasury"),
    ],
    "Immigration & Asylum": [
        ("home-office", "Home Office"),
    ],
    "Budget & Fiscal": [
        ("treasury", "Treasury"),
    ],
    "Taxation": [
        ("hm-revenue-customs", "HM Revenue & Customs"),
        ("treasury", "Treasury"),
    ],
    "NHS": [
        ("department-of-health-and-social-care", "Department of Health & Social Care"),
    ],
    "Social Care": [
        ("department-of-health-and-social-care", "Department of Health & Social Care"),
    ],
    "Mental Health": [
        ("department-of-health-and-social-care", "Department of Health & Social Care"),
    ],
    "Education": [
        ("department-for-education", "Department for Education"),
    ],
    "Children & Families": [
        ("department-for-education", "Department for Education"),
        ("department-for-work-pensions", "Department for Work & Pensions"),
    ],
    "Climate & Environment": [
        ("department-for-energy-security-and-net-zero", "Department for Energy Security & Net Zero"),
        ("department-for-environment-food-rural-affairs", "Department for Environment, Food & Rural Affairs"),
    ],
    "Justice & Crime": [
        ("ministry-of-justice", "Ministry of Justice"),
        ("home-office", "Home Office"),
    ],
    "Human Rights": [
        ("ministry-of-justice", "Ministry of Justice"),
        ("foreign-commonwealth-development-office", "Foreign, Commonwealth & Development Office"),
    ],
    "Defence": [
        ("ministry-of-defence", "Ministry of Defence"),
    ],
    "Housing": [
        ("ministry-of-housing-communities-and-local-government", "Ministry of Housing, Communities & Local Government"),
    ],
    "Transport": [
        ("department-for-transport", "Department for Transport"),
    ],
    "Brexit & EU": [
        ("cabinet-office", "Cabinet Office"),
        ("foreign-commonwealth-development-office", "Foreign, Commonwealth & Development Office"),
    ],
    "Foreign Policy": [
        ("foreign-commonwealth-development-office", "Foreign, Commonwealth & Development Office"),
    ],
    "Employment & Workers": [
        ("department-for-business-and-trade", "Department for Business & Trade"),
        ("department-for-work-pensions", "Department for Work & Pensions"),
    ],
    "Business & Enterprise": [
        ("department-for-business-and-trade", "Department for Business & Trade"),
    ],
    "Energy": [
        ("department-for-energy-security-and-net-zero", "Department for Energy Security & Net Zero"),
    ],
    "Constitutional & Devolution": [
        ("cabinet-office", "Cabinet Office"),
        ("ministry-of-housing-communities-and-local-government", "Ministry of Housing, Communities & Local Government"),
    ],
    "Technology & Digital": [
        ("department-for-science-innovation-and-technology", "Department for Science, Innovation & Technology"),
        ("cabinet-office", "Cabinet Office"),
    ],
    "Agriculture & Farming": [
        ("department-for-environment-food-rural-affairs", "Department for Environment, Food & Rural Affairs"),
    ],
}

# Threshold for data-driven recommendations: a department must have at least
# this many total hit counts for a tag to be recommended even if not in the
# hardcoded mapping.
DATA_DRIVEN_THRESHOLD = 3


def create_source_recs_table(conn):
    """Create the source_recommendations table if it doesn't exist.

    Schema must match Room's SourceRecommendationEntity exactly.
    isRecommended is stored as INTEGER (0/1) per SQLite convention.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS `source_recommendations` (
            `tag` TEXT NOT NULL,
            `organisationSlug` TEXT NOT NULL,
            `organisationName` TEXT NOT NULL,
            `hitCount` INTEGER NOT NULL,
            `isRecommended` INTEGER NOT NULL,
            PRIMARY KEY(`tag`, `organisationSlug`)
        );
        """
    )
    conn.commit()


def build_source_recs(conn):
    """Build source_recommendations using hybrid tag→department mapping (D-06).

    1. Insert hardcoded base entries with isRecommended=1.
    2. Query publication_tags × government_publications for data-driven hit
       counts per (tag, organisationSlug).
    3. Merge: update hit counts for existing entries; add new data-driven
       entries with isRecommended=1 if hit count >= DATA_DRIVEN_THRESHOLD.
    """
    cursor = conn.cursor()

    # Step 1: Insert hardcoded base entries
    base_rows = []
    for tag_name, departments in TAG_TO_DEPARTMENTS.items():
        for org_slug, org_name in departments:
            base_rows.append((tag_name, org_slug, org_name, 0, 1))  # isRecommended=1

    insert_sql = """INSERT OR REPLACE INTO source_recommendations
                    (tag, organisationSlug, organisationName, hitCount, isRecommended)
                    VALUES (?, ?, ?, ?, ?)"""
    cursor.executemany(insert_sql, base_rows)
    conn.commit()
    logger.info("Inserted %d hardcoded base recommendations", len(base_rows))

    # Step 2: Query data-driven hit counts from publication_tags
    # publication_tags (publicationId, tag, hitCount) JOIN government_publications
    # (id, ..., organisationSlug, organisation) to get org per tag.
    data_driven = defaultdict(lambda: {"hitCount": 0, "orgName": ""})
    try:
        cursor.execute("""
            SELECT pt.tag, gp.organisationSlug, gp.organisation, SUM(pt.hitCount)
            FROM publication_tags pt
            JOIN government_publications gp ON pt.publicationId = gp.id
            GROUP BY pt.tag, gp.organisationSlug
        """)
        for tag, org_slug, org_name, total_hits in cursor.fetchall():
            if not org_slug:
                continue
            key = (tag, org_slug)
            data_driven[key]["hitCount"] = total_hits or 0
            data_driven[key]["orgName"] = org_name or org_slug
    except sqlite3.OperationalError as e:
        logger.warning("Could not query publication_tags: %s — using hardcoded only", e)

    logger.info("Found %d data-driven (tag, org) combinations", len(data_driven))

    # Step 3: Merge data-driven with hardcoded
    # Build a set of hardcoded (tag, org_slug) pairs for quick lookup
    hardcoded_pairs = set()
    for tag_name, departments in TAG_TO_DEPARTMENTS.items():
        for org_slug, _ in departments:
            hardcoded_pairs.add((tag_name, org_slug))

    merged_rows = []
    data_driven_recommended = 0

    for (tag, org_slug), info in data_driven.items():
        hit_count = info["hitCount"]
        org_name = info["orgName"]
        is_recommended = 1 if (tag, org_slug) in hardcoded_pairs or hit_count >= DATA_DRIVEN_THRESHOLD else 0

        if (tag, org_slug) not in hardcoded_pairs and hit_count >= DATA_DRIVEN_THRESHOLD:
            data_driven_recommended += 1

        merged_rows.append((tag, org_slug, org_name, hit_count, is_recommended))

    # Update/insert merged rows (data-driven hit counts override hardcoded 0s)
    if merged_rows:
        cursor.executemany(insert_sql, merged_rows)
        conn.commit()
    logger.info("Merged %d data-driven rows (%d newly recommended)", len(merged_rows), data_driven_recommended)

    # Verify
    cursor.execute("SELECT COUNT(*) FROM source_recommendations")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM source_recommendations WHERE isRecommended = 1")
    recommended = cursor.fetchone()[0]
    logger.info("Done: source_recommendations has %d rows (%d recommended)", total, recommended)
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Post-merge build: produces source_recommendations table (hybrid tag→department mapping, D-06)."
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to the merged goveye.db (modified in-place).",
    )
    parser.add_argument(
        "--schema", required=False,
        help="Path to the Room exported schema JSON (for reference, not used directly).",
    )
    args = parser.parse_args()

    if not os.path.exists(args.output):
        print(f"ERROR: {args.output} does not exist — run merge_dbs.py first.")
        sys.exit(1)

    conn = sqlite3.connect(args.output)
    try:
        logger.info("Creating source_recommendations table...")
        create_source_recs_table(conn)

        # Clear existing source_recommendations before repopulating
        conn.execute("DELETE FROM source_recommendations")
        conn.commit()

        logger.info("Building source recommendations...")
        build_source_recs(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
