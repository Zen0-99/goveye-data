#!/usr/bin/env python3
"""Build script for division and bill tags.

Tags are derived by pattern matching on debate speech text + division/bill
titles. Each tag has a list of case-insensitive patterns (including
abbreviations and synonyms). If a tag's patterns match >= THRESHOLD times
in the combined text for a division, the tag is attached.

For bills, tags are aggregated from all divisions related to the bill
(matched by bill title appearing in division title), plus the bill title
itself is checked against all tag patterns.

Tables produced:
  division_tags (divisionId, tag, hitCount)
  bill_tags (billId, tag, hitCount)

This script runs on the MERGED goveye.db (after merge_dbs.py, before or
after build_precompute.py). It needs: divisions, debate_speeches, bills.

Usage:
  python build_tags.py --output goveye.db --schema schemas/bundled_schema.json
"""

import argparse
import re
import sqlite3
import time
from collections import defaultdict

from api_helper import BATCH_SIZE, logger

# --- Tag dictionary ---
# Each tag maps to a list of case-insensitive patterns (plain text or regex).
# Patterns are matched against the combined text of all speeches + division title.
# A tag is attached if the total hit count across all patterns >= THRESHOLD.

TAG_DICTIONARY = {
    # --- Welfare & Benefits ---
    "Universal Credit": [
        r"universal credit",
        r"\bUC\b(?!\s*(?:statement|report))",  # UC but not "UC statement"
    ],
    "PIP & Disability Benefits": [
        r"\bPIP\b",
        r"personal independence payment",
        r"disability living allowance",
        r"\bDLA\b",
        r"attendance allowance",
        r"employment and support allowance",
        r"\bESA\b",
        r"carer'?s? allowance",
    ],
    "Disability": [
        r"disability",
        r"disabled",
        r"accessibility",
        r"reasonable adjustment",
        r"Equality Act 2010",
        r"disabled person",
        r"disabled people",
    ],
    "Welfare & Social Security": [
        r"\bwelfare\b",
        r"social security",
        r"benefit cap",
        r"bedroom tax",
        r"spare room subsidy",
        r"sanction(?:s)?\b.*benefit",
        r"universal credit.*sanction",
        r"benefit claim",
    ],

    # --- Immigration ---
    "Immigration & Asylum": [
        r"immigration",
        r"asylum",
        r"refugee",
        r"\bmigrant\b",
        r"border security",
        r"Rwanda",
        r"illegal migration",
        r"small boats",
        r"detention centre",
        r"immigration removal",
        r"Nationality and Borders",
    ],

    # --- Economy ---
    "Budget & Fiscal": [
        r"\bbudget\b",
        r"fiscal",
        r"\btreasury\b",
        r"public spending",
        r"spending review",
        r"autumn statement",
        r"spring statement",
        r"budget resolution",
    ],
    "Taxation": [
        r"\btax\b",
        r"\bVAT\b",
        r"income tax",
        r"national insurance",
        r"corporation tax",
        r"capital gains",
        r"stamp duty",
        r"fuel duty",
        r"alcohol duty",
        r"tobacco duty",
        r"windfall tax",
        r"energy profits levy",
    ],

    # --- Health ---
    "NHS": [
        r"\bNHS\b",
        r"national health service",
        r"health service",
        r"\bhospital\b",
        r"\bGP\b",
        r"general practice",
        r"ambulance",
        r"A&E",
        r"accident and emergency",
        r"waiting list",
        r"NHS trust",
        r"integrated care",
    ],
    "Social Care": [
        r"social care",
        r"care home",
        r"\bcare\b.*home",
        r"\bcarer\b",
        r"adult social care",
        r"children'?s? social care",
        r"domiciliary care",
        r"residential care",
    ],
    "Mental Health": [
        r"mental health",
        r"psychiatric",
        r"psychological",
        r"\bCAMHS\b",
        r"mental illness",
        r"mental health act",
    ],

    # --- Education ---
    "Education": [
        r"\beducation\b",
        r"\bschool\b",
        r"\bteacher\b",
        r"\bpupil\b",
        r"\bstudent\b",
        r"\buniversity\b",
        r"tuition fee",
        r"\bacademy\b",
        r"free school",
        r"OFSTED",
        r"curriculum",
        r"GCSE",
        r"\bA-level\b",
    ],
    "Children & Families": [
        r"\bchildren\b",
        r"\bchild\b",
        r"childcare",
        r"\bnursery\b",
        r"early years",
        r"parental leave",
        r"maternity leave",
        r"paternity leave",
        r"child protection",
        r"safeguarding",
    ],

    # --- Environment ---
    "Climate & Environment": [
        r"climate",
        r"net zero",
        r"\bcarbon\b",
        r"emission",
        r"\benvironment\b",
        r"biodiversity",
        r"pollution",
        r"fossil fuel",
        r"renewable energy",
        r"climate change",
        r"global warming",
        r"greenhouse gas",
        r"deforestation",
    ],

    # --- Justice ---
    "Justice & Crime": [
        r"\bjustice\b",
        r"\bprison\b",
        r"sentencing",
        r"\bcrime\b",
        r"criminal",
        r"\bpolice\b",
        r"policing",
        r"\bcourt\b",
        r"magistrate",
        r"probation",
        r"youth offending",
        r"criminal justice",
    ],
    "Human Rights": [
        r"human rights",
        r"European Convention",
        r"\bECHR\b",
        r"civil liberty",
        r"freedom of",
        r"human rights act",
    ],

    # --- Defence ---
    "Defence": [
        r"\bdefence\b",
        r"armed forces",
        r"\bmilitary\b",
        r"\bNATO\b",
        r"\bveteran\b",
        r"ministry of defence",
        r"\bMOD\b",
        r"defence spending",
    ],

    # --- Housing ---
    "Housing": [
        r"\bhousing\b",
        r"homeless",
        r"\btenant\b",
        r"renter",
        r"landlord",
        r"planning permission",
        r"affordable housing",
        r"social housing",
        r"leasehold",
        r"rent control",
        r"section 21",
        r"no-fault eviction",
    ],

    # --- Transport ---
    "Transport": [
        r"\btransport\b",
        r"\brail\b",
        r"railway",
        r"\btrain\b",
        r"\bbus\b",
        r"\broad\b",
        r"highway",
        r"aviation",
        r"airport",
        r"\bHS2\b",
        r"active travel",
        r"cycling",
    ],

    # --- Brexit & EU ---
    "Brexit & EU": [
        r"Brexit",
        r"European Union",
        r"\bEU\b(?!\s*(?:law|directive|regulation))",
        r"withdrawal agreement",
        r"retained EU law",
        r"single market",
        r"customs union",
        r"European Communities",
    ],

    # --- Foreign Policy ---
    "Foreign Policy": [
        r"foreign policy",
        r"Israel",
        r"Gaza",
        r"Palestine",
        r"Ukraine",
        r"Russia",
        r"\bChina\b",
        r"Taiwan",
        r"\bIran\b",
        r"foreign office",
        r"international development",
        r"aid budget",
        r"foreign aid",
    ],

    # --- Work & Business ---
    "Employment & Workers": [
        r"employment",
        r"\bworker\b",
        r"trade union",
        r"minimum wage",
        r"living wage",
        r"zero hours",
        r"gig economy",
        r"\bstrike\b",
        r"industrial action",
        r"picket",
        r"right to strike",
        r"strikes.*bill",
    ],
    "Business & Enterprise": [
        r"\bbusiness\b",
        r"small business",
        r"\bSME\b",
        r"enterprise",
        r"startup",
        r"sole trader",
        r"self-employed",
    ],

    # --- Energy ---
    "Energy": [
        r"\benergy\b",
        r"\boil\b",
        r"\bgas\b(?!\s*(?:station|works|boiler))",
        r"nuclear",
        r"wind(?:farm| turbine)?",
        r"solar",
        r"fuel poverty",
        r"energy price",
        r"\bOFGEM\b",
        r"national grid",
        r"energy security",
    ],

    # --- Constitutional ---
    "Constitutional & Devolution": [
        r"constitution",
        r"devolution",
        r"Scottish independence",
        r"indyref",
        r"House of Lords reform",
        r"proportional representation",
        r"voting system",
        r"electoral reform",
        r"devolved parliament",
        r"Scottish Parliament",
        r"Senedd",
        r"Northern Ireland Assembly",
    ],

    # --- Technology ---
    "Technology & Digital": [
        r"\bAI\b",
        r"artificial intelligence",
        r"digital",
        r"online safety",
        r"social media",
        r"data protection",
        r"\bGDPR\b",
        r"broadband",
        r"internet",
        r"cyber",
        r"encryption",
    ],

    # --- Agriculture ---
    "Agriculture & Farming": [
        r"\bfarming\b",
        r"\bagriculture\b",
        r"\bfarmer\b",
        r"agricultural",
        r"rural",
        r"livestock",
        r"\bcrop\b",
        r"food security",
        r"common agricultural",
    ],
}

# Minimum pattern matches for a tag to be attached to a division.
# 2 means the topic must be mentioned at least twice in the combined text.
DIVISION_TAG_THRESHOLD = 2

# For bills, threshold is lower because the bill title is a strong signal.
BILL_TAG_THRESHOLD = 1

TABLE_NAMES = ["division_tags", "bill_tags", "tag_metadata"]

# --- Tag descriptions (precomputed, stored in tag_metadata table) ---
TAG_DESCRIPTIONS = {
    "Universal Credit": "Debates and votes on Universal Credit, the UK's means-tested benefit for working-age people.",
    "PIP & Disability Benefits": "Personal Independence Payments, Disability Living Allowance, Employment and Support Allowance, and Carer's Allowance.",
    "Disability": "Disability rights, accessibility, reasonable adjustments, and the Equality Act 2010.",
    "Welfare & Social Security": "Welfare policy, benefit caps, sanctions, and the social security system.",
    "Immigration & Asylum": "Immigration policy, asylum claims, refugee rights, border security, and detention.",
    "Budget & Fiscal": "Government budgets, fiscal policy, public spending, and Treasury statements.",
    "Taxation": "Income tax, corporation tax, VAT, National Insurance, and duty changes.",
    "NHS": "National Health Service funding, hospitals, GP services, waiting lists, and healthcare policy.",
    "Social Care": "Adult and children's social care, care homes, and carer support.",
    "Mental Health": "Mental health services, psychiatric care, and mental health legislation.",
    "Education": "Schools, teachers, curriculum, universities, tuition fees, and education policy.",
    "Children & Families": "Childcare, child protection, parental leave, and family policy.",
    "Climate & Environment": "Climate change, net zero, carbon emissions, biodiversity, and environmental protection.",
    "Justice & Crime": "Criminal justice, policing, courts, prisons, sentencing, and probation.",
    "Human Rights": "Human rights legislation, civil liberties, and the European Convention on Human Rights.",
    "Defence": "Armed forces, military spending, NATO, veterans, and defence policy.",
    "Housing": "Housing policy, homelessness, tenants' rights, planning, and affordable housing.",
    "Transport": "Rail, roads, buses, aviation, HS2, and transport infrastructure.",
    "Brexit & EU": "Brexit, the EU withdrawal, retained EU law, and UK-EU relations.",
    "Foreign Policy": "Foreign affairs, international development, aid, and global conflicts.",
    "Employment & Workers": "Workers' rights, trade unions, minimum wage, strikes, and employment law.",
    "Business & Enterprise": "Small businesses, SMEs, enterprise policy, and self-employment.",
    "Energy": "Energy policy, oil and gas, nuclear, renewables, fuel poverty, and energy security.",
    "Constitutional & Devolution": "Constitutional reform, devolution, Scottish independence, and electoral systems.",
    "Technology & Digital": "AI, digital policy, online safety, data protection, and cybersecurity.",
    "Agriculture & Farming": "Farming, agriculture, rural affairs, livestock, and food security.",
}


def count_pattern_hits(text, patterns):
    """Count total hits across all patterns in the text (case-insensitive)."""
    if not text:
        return 0
    total = 0
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        total += len(matches)
    return total


def build_division_tags(conn):
    """Build division_tags by pattern matching on speeches + titles."""
    cursor = conn.cursor()

    # Get all divisions
    cursor.execute("SELECT id, title FROM divisions")
    divisions = cursor.fetchall()
    logger.info("Processing %d divisions for tags", len(divisions))

    # Get all debate speeches grouped by divisionId
    cursor.execute("SELECT divisionId, speechText FROM debate_speeches")
    speeches_by_division = defaultdict(list)
    for division_id, speech_text in cursor.fetchall():
        speeches_by_division[division_id].append(speech_text or "")

    # Build tags
    tag_rows = []
    divisions_with_tags = 0

    for division_id, title in divisions:
        # Combine title + all speeches into one text
        speeches = speeches_by_division.get(division_id, [])
        combined_text = (title or "") + " " + " ".join(speeches)

        for tag_name, patterns in TAG_DICTIONARY.items():
            hit_count = count_pattern_hits(combined_text, patterns)
            if hit_count >= DIVISION_TAG_THRESHOLD:
                tag_rows.append((division_id, tag_name, hit_count))

        if any(r[0] == division_id for r in tag_rows[-100:]):  # rough check
            divisions_with_tags += 1

    # More accurate count
    divisions_with_tags = len(set(r[0] for r in tag_rows))

    # Insert
    insert_sql = """INSERT OR REPLACE INTO division_tags (divisionId, tag, hitCount)
                    VALUES (?, ?, ?)"""
    for i in range(0, len(tag_rows), BATCH_SIZE):
        batch = tag_rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
    conn.commit()

    logger.info("Division tags: %d tags across %d divisions (%d divisions have no tags)",
                len(tag_rows), divisions_with_tags, len(divisions) - divisions_with_tags)
    return tag_rows


def build_bill_tags(conn, division_tag_rows):
    """Build bill_tags from bill titles + aggregated division tags."""
    cursor = conn.cursor()

    # Get all bills (use shortTitle + longTitle for matching)
    cursor.execute("SELECT id, shortTitle, longTitle FROM bills")
    bills = cursor.fetchall()
    logger.info("Processing %d bills for tags", len(bills))

    # Build a map: divisionId → set of tags
    division_tags_map = defaultdict(set)
    for division_id, tag_name, _ in division_tag_rows:
        division_tags_map[division_id].add(tag_name)

    # Get division titles to match bills to divisions
    cursor.execute("SELECT id, title FROM divisions")
    divisions = cursor.fetchall()

    tag_rows = []
    bills_with_tags = 0

    for bill_id, short_title, long_title in bills:
        bill_title = (short_title or "") + " " + (long_title or "")
        if not bill_title.strip():
            continue

        # 1. Check bill title against all tag patterns
        bill_tag_counts = defaultdict(int)
        for tag_name, patterns in TAG_DICTIONARY.items():
            hit_count = count_pattern_hits(bill_title, patterns)
            if hit_count >= BILL_TAG_THRESHOLD:
                bill_tag_counts[tag_name] += hit_count

        # 2. Find divisions whose titles contain the bill name (or vice versa)
        # This is fuzzy — bill titles and division titles often share keywords
        bill_title_lower = bill_title.lower()
        for division_id, division_title in divisions:
            if not division_title:
                continue
            division_title_lower = division_title.lower()
            # Check if bill title appears in division title or vice versa
            # Use a simple substring check with the first few words of the bill title
            bill_words = bill_title_lower.replace(" bill", "").replace(" act", "").strip()
            if bill_words and len(bill_words) > 5:
                if bill_words in division_title_lower or division_title_lower[:50] in bill_title_lower:
                    # Aggregate tags from this division
                    for tag_name in division_tags_map.get(division_id, set()):
                        bill_tag_counts[tag_name] += 1

        # Insert bill tags
        for tag_name, hit_count in bill_tag_counts.items():
            tag_rows.append((bill_id, tag_name, hit_count))

        if bill_tag_counts:
            bills_with_tags += 1

    # Insert
    insert_sql = """INSERT OR REPLACE INTO bill_tags (billId, tag, hitCount)
                    VALUES (?, ?, ?)"""
    for i in range(0, len(tag_rows), BATCH_SIZE):
        batch = tag_rows[i:i + BATCH_SIZE]
        cursor.executemany(insert_sql, batch)
    conn.commit()

    logger.info("Bill tags: %d tags across %d bills (%d bills have no tags)",
                len(tag_rows), bills_with_tags, len(bills) - bills_with_tags)
    return tag_rows


def build_tag_metadata(conn, division_tag_rows, bill_tag_rows):
    """Build tag_metadata table with description + counts per tag."""
    cursor = conn.cursor()

    # Count divisions and bills per tag
    div_counts = defaultdict(int)
    for division_id, tag_name, _ in division_tag_rows:
        div_counts[tag_name] += 1

    bill_counts = defaultdict(int)
    for bill_id, tag_name, _ in bill_tag_rows:
        bill_counts[tag_name] += 1

    # Build metadata rows for all tags in the dictionary
    rows = []
    for tag_name in TAG_DICTIONARY:
        description = TAG_DESCRIPTIONS.get(tag_name, "")
        div_count = div_counts.get(tag_name, 0)
        bill_count = bill_counts.get(tag_name, 0)
        rows.append((tag_name, description, div_count, bill_count))

    # Insert
    insert_sql = """INSERT OR REPLACE INTO tag_metadata (tag, description, divisionCount, billCount)
                    VALUES (?, ?, ?, ?)"""
    cursor.executemany(insert_sql, rows)
    conn.commit()

    logger.info("Tag metadata: %d tags (%d with divisions, %d with bills)",
                len(rows),
                sum(1 for r in rows if r[2] > 0),
                sum(1 for r in rows if r[3] > 0))
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Build division and bill tags from debate speech text + titles."
    )
    parser.add_argument("--output", required=True,
                        help="Path to the merged goveye.db (modified in-place).")
    parser.add_argument("--schema", required=True,
                        help="Path to the Room exported schema JSON.")
    args = parser.parse_args()

    start = time.time()

    conn = sqlite3.connect(args.output)
    cursor = conn.cursor()

    # Clear existing tags
    cursor.execute("DELETE FROM division_tags")
    cursor.execute("DELETE FROM bill_tags")
    cursor.execute("DELETE FROM tag_metadata")
    conn.commit()

    # Build division tags
    division_tag_rows = build_division_tags(conn)

    # Build bill tags (using division tags as input)
    bill_tag_rows = build_bill_tags(conn, division_tag_rows)

    # Build tag metadata (descriptions + counts)
    build_tag_metadata(conn, division_tag_rows, bill_tag_rows)

    conn.close()
    elapsed = time.time() - start
    logger.info("Tag build complete in %.1fs", elapsed)


if __name__ == "__main__":
    main()
