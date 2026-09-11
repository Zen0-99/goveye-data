#!/usr/bin/env python3
"""Post-merge precomputation script (Phase 12).

Runs AFTER merge_dbs.py has combined all per-API DBs into goveye.db.
Produces two precomputed tables via SQL aggregation (no API calls):

  - mp_stats: one row per MP with questionCount, speechCount, committeeCount,
    voteParticipationRate, rebellionRate, rebellionCount, totalDivisionsVoted,
    activityScore, and 5 trait percentiles.
  - peer_averages: one row per house with avgQuestions, avgSpeeches,
    avgCommittees, avgParticipation, avgRebellion, mpCount.

This eliminates 5,500+ runtime DAO calls per profile open on Android.

Activity score weights (must match ActivityScoreCalculator.kt):
  votes 3.0, questions 2.5, speeches 2.5, finance 2.0 (total 10.0).
  Self-performance scoring — questions/speeches as per-month rates,
  finance as the rate-vs-average trait score (0-100) scaled to weight.
  Committees removed from the score; committeeCount is still stored for
  display and committeesPercentile is still computed.

When NOT to run this script:
  This script recomputes MP statistics (activityScore, rebellionRate,
  voteParticipationRate, all *Percentile columns) from division_votes,
  debate_speeches, committees, interests, expenses, bio_data and
  historical_members. If only a derived column's logic changed (e.g. the
  activityScore weights, the rebellionRate formula), do NOT run this
  script — run the SQL UPDATE directly against the existing DB instead.
  The Room migration in GovEye's DatabaseModule.kt contains the same SQL
  and handles the update on user devices.

  Note: this script has a --changed-apis flag that skips recomputation
  when source APIs haven't changed. But if the FORMULA changed, even
  --changed-apis won't help — run the SQL directly.

  See goveye-data/AGENTS.md for the full decision guide.

Usage:
  python build_precompute.py --output goveye.db --schema schemas/bundled_schema.json
"""

import argparse
import datetime
import json
import logging
import os
import sqlite3
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("build_precompute")

# ActivityScoreCalculator weights (must match ActivityScoreCalculator.kt)
# Score is 0.0-10.0: votes 3.0, questions 2.5, speeches 2.5, finance 2.0
# Self-performance scoring — no peer normalization for the score itself.
# (Finance is rate-vs-average — the same trait score shown on the radar —
#  scaled to the 2.0 weight, per ActivityScoreCalculator.kt.)
VOTE_WEIGHT = 3.0
QUESTIONS_WEIGHT = 2.5
SPEECHES_WEIGHT = 2.5
FINANCE_WEIGHT = 2.0

# Per-month rate that earns full marks for questions/speeches
FULL_MARKS_QUESTIONS_PER_MONTH = 2.0
FULL_MARKS_SPEECHES_PER_MONTH = 2.0

# Houses
COMMONS = 1
LORDS = 2

# Default tenure start when no bio_data or historical_members data is found.
# This preserves the previous behaviour's implicit 2016 cutoff.
DEFAULT_TENURE_START = "2016-01-01"


def get_tenure_start(conn, member_id):
    """Determine the MP's tenure start date for tenure-aware metric calculation.

    Source order matches StatsRepository (membershipStartDate → maidenSpeechDate
    → historical_members → default) so the precomputed participation rate
    matches the rate the app computes at runtime:
      1. mps.membershipStartDate — current unbroken tenure
      2. bio_data.maidenSpeechDate — when the MP started attending
      3. earliest historical_members.startDate
      4. DEFAULT_TENURE_START ("2016-01-01") if none is available

    Returns a date string in YYYY-MM-DD format (or DEFAULT_TENURE_START).
    """
    cursor = conn.cursor()

    # Primary: current unbroken membership start from mps
    cursor.execute(
        "SELECT membershipStartDate FROM mps WHERE id = ?",
        (member_id,),
    )
    row = cursor.fetchone()
    if row and row[0]:
        return row[0][:10]

    # Secondary: maiden speech date from bio_data
    cursor.execute(
        "SELECT maidenSpeechDate FROM bio_data WHERE mpId = ?",
        (member_id,),
    )
    row = cursor.fetchone()
    if row and row[0]:
        return row[0]

    # Fallback: earliest membership start from historical_members
    cursor.execute(
        """SELECT startDate FROM historical_members
           WHERE parliamentMemberId = ? AND startDate IS NOT NULL
           ORDER BY startDate ASC LIMIT 1""",
        (member_id,),
    )
    row = cursor.fetchone()
    if row and row[0]:
        return row[0]

    # Default: preserve previous behaviour's implicit 2016 cutoff
    return DEFAULT_TENURE_START


def get_months_since_tenure_start(conn, member_id):
    """Compute months from the MP's tenure start to now."""
    tenure_start = get_tenure_start(conn, member_id)
    try:
        start = datetime.date.fromisoformat(tenure_start[:10])
        now = datetime.date.today()
        months = (now.year - start.year) * 12 + (now.month - start.month)
        return max(1, months)
    except Exception:
        return 12  # fallback


def get_years_served(conn, member_id):
    """Years served as a float — matches StatsRepository.getYearsServed.

    Source order: mps.membershipStartDate (current unbroken tenure),
    then bio_data.maidenSpeechDate, then DEFAULT_TENURE_START.
    Returns at least 0.5 to avoid division-by-zero in rate calculations.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT membershipStartDate FROM mps WHERE id = ?",
        (member_id,),
    )
    row = cursor.fetchone()
    start_str = row[0][:10] if row and row[0] else None
    if not start_str:
        cursor.execute(
            "SELECT maidenSpeechDate FROM bio_data WHERE mpId = ?",
            (member_id,),
        )
        row = cursor.fetchone()
        start_str = row[0][:10] if row and row[0] else None
    if not start_str:
        start_str = DEFAULT_TENURE_START
    try:
        start = datetime.date.fromisoformat(start_str)
        days = (datetime.date.today() - start).days
        return max(0.5, days / 365.25)
    except Exception:
        return 1.0


def create_precompute_tables(conn):
    """Create mp_stats and peer_averages tables (additive — no raw tables touched).

    Schema must match Room's expected schema exactly (no DEFAULT clauses,
    PRIMARY KEY as a separate constraint) or Room will reject the DB on open
    with "Pre-packaged database has an invalid schema".
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS `mp_stats` (
            `memberId` INTEGER NOT NULL,
            `house` INTEGER NOT NULL,
            `questionCount` INTEGER NOT NULL,
            `speechCount` INTEGER NOT NULL,
            `committeeCount` INTEGER NOT NULL,
            `voteParticipationRate` REAL NOT NULL,
            `rebellionRate` REAL NOT NULL,
            `rebellionCount` INTEGER NOT NULL,
            `totalDivisionsVoted` INTEGER NOT NULL,
            `activityScore` REAL NOT NULL,
            `rebellionPercentile` INTEGER NOT NULL,
            `participationPercentile` INTEGER NOT NULL,
            `questionsPercentile` INTEGER NOT NULL,
            `speechesPercentile` INTEGER NOT NULL,
            `committeesPercentile` INTEGER NOT NULL,
            PRIMARY KEY(`memberId`)
        );

        CREATE TABLE IF NOT EXISTS `peer_averages` (
            `house` INTEGER NOT NULL,
            `avgQuestions` REAL NOT NULL,
            `avgSpeeches` REAL NOT NULL,
            `avgCommittees` REAL NOT NULL,
            `avgParticipation` REAL NOT NULL,
            `avgRebellion` REAL NOT NULL,
            `mpCount` INTEGER NOT NULL,
            PRIMARY KEY(`house`)
        );
        """
    )
    conn.commit()


def compute_per_mp_metrics(conn):
    """Compute raw per-MP metrics via SQL aggregation.

    Returns a list of dicts with keys:
      memberId, house, isActive, questionCount, speechCount, committeeCount,
      financeCount, voteParticipationRate, rebellionRate, rebellionCount,
      totalDivisionsVoted, monthsSinceTenureStart
    """
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get all MPs with their house and active flag
    cursor.execute("SELECT id, house, partyName, isActive FROM mps")
    mps = cursor.fetchall()
    logger.info("Computing metrics for %d MPs", len(mps))

    results = []
    for mp in mps:
        member_id = mp["id"]
        house = mp["house"]
        party_name = mp["partyName"]
        is_active = mp["isActive"]

        # questionCount — read from hansard_contributions summary row
        # build_hansard.py stores one row per MP with debateSection='Summary'
        # and the question count in debateSectionId
        cursor.execute(
            """SELECT debateSectionId FROM hansard_contributions
               WHERE memberId = ? AND debateSection = 'Summary'""",
            (member_id,),
        )
        row = cursor.fetchone()
        question_count = row[0] if row else 0

        # speechCount — count debate speeches (non-intervention)
        cursor.execute(
            "SELECT COUNT(*) FROM debate_speeches WHERE memberId = ? AND isIntervention = 0",
            (member_id,),
        )
        speech_count = cursor.fetchone()[0]

        # committeeCount — count committee cross-refs
        cursor.execute(
            "SELECT COUNT(*) FROM mp_committee_cross_ref WHERE memberId = ?",
            (member_id,),
        )
        committee_count = cursor.fetchone()[0]

        # financeCount — interests + expenses declared by this MP.
        # NB: interests uses memberId, expenses uses mpId.
        cursor.execute(
            "SELECT COUNT(*) FROM interests WHERE memberId = ?",
            (member_id,),
        )
        interest_count = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM expenses WHERE mpId = ?",
            (member_id,),
        )
        expense_count = cursor.fetchone()[0]
        finance_count = interest_count + expense_count

        # Tenure start date — only count divisions from this date onward so
        # new MPs (e.g. Hannah Spencer) are not penalized for divisions they
        # couldn't vote in, and long-serving MPs get credit for full tenure.
        tenure_start = get_tenure_start(conn, member_id)
        logger.debug("MP %d tenure start: %s", member_id, tenure_start)

        # voteParticipationRate — voted divisions / total divisions in house
        # since the MP's tenure start. Must filter by house: an MP should only
        # be credited for voting in divisions that belong to their house.
        # Without this filter, a Commons MP who voted in Lords divisions gets
        # a rate > 100%.
        cursor.execute(
            """SELECT COUNT(DISTINCT dv.divisionId)
               FROM division_votes dv
               JOIN divisions d ON dv.divisionId = d.id
               WHERE dv.memberId = ? AND d.house = ? AND d.date >= ?""",
            (member_id, house, tenure_start),
        )
        voted_count = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM divisions WHERE house = ? AND date >= ?",
            (house, tenure_start),
        )
        total_divisions = cursor.fetchone()[0]
        participation_rate = voted_count / total_divisions if total_divisions > 0 else 0.0

        # rebellionRate — party-majority method via SQL GROUP BY
        rebellion_rate = 0.0
        rebellion_count = 0
        total_divisions_voted = voted_count

        if party_name and voted_count > 0:
            # Get the MP's votes (only for divisions in their house since tenure)
            cursor.execute(
                """SELECT dv.divisionId, dv.vote
                   FROM division_votes dv
                   JOIN divisions d ON dv.divisionId = d.id
                   WHERE dv.memberId = ? AND d.house = ? AND d.date >= ?""",
                (member_id, house, tenure_start),
            )
            mp_votes = cursor.fetchall()

            # Get party vote counts per division (single GROUP BY)
            division_ids = [v["divisionId"] for v in mp_votes]
            if division_ids:
                placeholders = ",".join("?" * len(division_ids))
                cursor.execute(
                    f"""
                    SELECT divisionId,
                           SUM(CASE WHEN UPPER(vote) = 'AYE' THEN 1 ELSE 0 END) AS partyAyes,
                           SUM(CASE WHEN UPPER(vote) = 'NO' THEN 1 ELSE 0 END) AS partyNoes
                    FROM division_votes
                    WHERE divisionId IN ({placeholders})
                        AND partyName = ?
                        AND UPPER(vote) NOT IN ('NOVOTERECORDED', 'NO VOTE RECORDED')
                    GROUP BY divisionId
                    """,
                    division_ids + [party_name],
                )
                party_counts = {r["divisionId"]: r for r in cursor.fetchall()}

                rebellions = 0
                scored = 0
                for vote in mp_votes:
                    div_id = vote["divisionId"]
                    mp_vote = (vote["vote"] or "").upper()
                    if mp_vote not in ("AYE", "NO"):
                        continue  # skip no-vote-recorded
                    pc = party_counts.get(div_id)
                    if pc is None:
                        continue
                    ayes = pc["partyAyes"]
                    noes = pc["partyNoes"]
                    if ayes == noes:
                        continue  # tie — no rebellion
                    party_majority = "AYE" if ayes > noes else "NO"
                    if mp_vote != party_majority:
                        rebellions += 1
                    scored += 1

                if scored > 0:
                    rebellion_rate = rebellions / scored
                    rebellion_count = rebellions

        results.append({
            "memberId": member_id,
            "house": house,
            "isActive": is_active,
            "questionCount": question_count,
            "speechCount": speech_count,
            "committeeCount": committee_count,
            "financeCount": finance_count,
            "voteParticipationRate": participation_rate,
            "rebellionRate": rebellion_rate,
            "rebellionCount": rebellion_count,
            "totalDivisionsVoted": total_divisions_voted,
            "monthsSinceTenureStart": get_months_since_tenure_start(conn, member_id),
            "yearsServed": get_years_served(conn, member_id),
        })

    return results


def compute_avg_years_served(conn, house):
    """Average years served across active MPs in a house (matches MpDao.getAverageYearsServed)."""
    cursor = conn.cursor()
    cursor.execute(
        """SELECT AVG((julianday('now') - julianday(substr(membershipStartDate, 1, 10))) / 365.25)
           FROM mps WHERE isActive = 1 AND house = ? AND membershipStartDate IS NOT NULL""",
        (house,),
    )
    row = cursor.fetchone()[0]
    return max(0.5, row if row is not None else 5.0)


def compute_avg_finance_count(conn, house):
    """Average (interests + expenses) count across active MPs in a house.

    Matches InterestDao.getAverageInterestCount + ExpenseDao.getAverageExpenseCount:
    AVG over per-MP COUNT(*) for active MPs only.
    """
    cursor = conn.cursor()
    cursor.execute(
        """SELECT AVG(cnt) FROM (
               SELECT COUNT(*) as cnt FROM interests
               WHERE memberId IN (SELECT id FROM mps WHERE isActive = 1 AND house = ?)
               GROUP BY memberId
           )""",
        (house,),
    )
    avg_interests = cursor.fetchone()[0] or 0.0
    cursor.execute(
        """SELECT AVG(cnt) FROM (
               SELECT COUNT(*) as cnt FROM expenses
               WHERE mpId IN (SELECT id FROM mps WHERE isActive = 1 AND house = ?)
               GROUP BY mpId
           )""",
        (house,),
    )
    avg_expenses = cursor.fetchone()[0] or 0.0
    return avg_interests + avg_expenses


def compute_finance_trait_score(finance_count, years_served, avg_finance_count, avg_years):
    """Finance trait score 0-100, rate-vs-average (matches StatsRepository.computeFinanceTraitScore).

    score = (mpFinanceCount / mpYears) / (avgFinanceCount / avgYears) * 100
    An MP declaring at the peer-average rate scores 100.
    """
    mp_years = max(0.5, years_served)
    avg_years = max(0.5, avg_years)
    avg_f_rate = avg_finance_count / avg_years
    if avg_f_rate <= 0:
        return 0
    score = (finance_count / mp_years) / avg_f_rate * 100.0
    return max(0, min(100, int(score)))


def compute_activity_score(participation_rate, question_count, speech_count,
                           months_since_tenure, finance_trait_score):
    """Compute activity score using the self-performance formula (matches ActivityScoreCalculator.kt).
    Returns a float 0.0-10.0.

    Weights: votes 3.0, questions 2.5, speeches 2.5, finance 2.0.
    Questions/speeches scored as per-month rates. Finance is the
    rate-vs-average trait score (0-100) scaled to the weight.
    """
    vote_contrib = min(participation_rate * VOTE_WEIGHT, VOTE_WEIGHT)

    questions_per_month = question_count / max(1, months_since_tenure)
    questions_contrib = _scale_rate(questions_per_month, FULL_MARKS_QUESTIONS_PER_MONTH, QUESTIONS_WEIGHT)

    speeches_per_month = speech_count / max(1, months_since_tenure)
    speeches_contrib = _scale_rate(speeches_per_month, FULL_MARKS_SPEECHES_PER_MONTH, SPEECHES_WEIGHT)

    finance_contrib = min(finance_trait_score / 100.0 * FINANCE_WEIGHT, FINANCE_WEIGHT)

    total = vote_contrib + questions_contrib + speeches_contrib + finance_contrib
    return max(0.0, min(10.0, total))


def _scale_rate(rate, full_marks, weight):
    """Scale a rate linearly: 0 → 0% of weight, full_marks → 100% of weight."""
    if full_marks <= 0:
        return 0.0
    ratio = rate / full_marks
    return min(ratio * weight, weight)


def compute_percentile(value, peer_values):
    """Compute percentile rank (matches PercentileCalculator.kt).

    percentile = (below + equal/2) / total * 100
    """
    if not peer_values:
        return 50
    below = sum(1 for v in peer_values if v < value)
    equal = sum(1 for v in peer_values if v == value)
    percentile = (below + equal / 2.0) / len(peer_values) * 100.0
    return max(0, min(100, int(percentile)))


def populate_mp_stats(conn, mp_metrics):
    """Populate mp_stats table with per-MP metrics, activity scores, and percentiles."""
    # Group by house for percentile + finance-average computation
    by_house = {}
    for m in mp_metrics:
        by_house.setdefault(m["house"], []).append(m)

    # Per-house finance averages (rate-vs-average scoring needs peer rates)
    house_finance = {}
    for house in by_house:
        house_finance[house] = (
            compute_avg_finance_count(conn, house),
            compute_avg_years_served(conn, house),
        )

    # Compute percentiles and activity scores
    rows = []
    for house, mps in by_house.items():
        avg_finance_count, avg_years = house_finance[house]

        # Collect peer value lists for percentile computation
        rebellion_values = [m["rebellionRate"] for m in mps]
        participation_values = [m["voteParticipationRate"] for m in mps]
        question_values = [float(m["questionCount"]) for m in mps]
        speech_values = [float(m["speechCount"]) for m in mps]
        committee_values = [float(m["committeeCount"]) for m in mps]

        for m in mps:
            finance_trait = compute_finance_trait_score(
                m["financeCount"], m["yearsServed"], avg_finance_count, avg_years,
            )
            activity_score = compute_activity_score(
                m["voteParticipationRate"],
                m["questionCount"],
                m["speechCount"],
                m["monthsSinceTenureStart"],
                finance_trait,
            )
            rebellion_pct = compute_percentile(m["rebellionRate"], rebellion_values)
            participation_pct = compute_percentile(m["voteParticipationRate"], participation_values)
            questions_pct = compute_percentile(float(m["questionCount"]), question_values)
            speeches_pct = compute_percentile(float(m["speechCount"]), speech_values)
            committees_pct = compute_percentile(float(m["committeeCount"]), committee_values)

            rows.append((
                m["memberId"],
                m["house"],
                m["questionCount"],
                m["speechCount"],
                m["committeeCount"],
                m["voteParticipationRate"],
                m["rebellionRate"],
                m["rebellionCount"],
                m["totalDivisionsVoted"],
                activity_score,
                rebellion_pct,
                participation_pct,
                questions_pct,
                speeches_pct,
                committees_pct,
            ))

    conn.executemany(
        """
        INSERT OR REPLACE INTO mp_stats (
            memberId, house, questionCount, speechCount, committeeCount,
            voteParticipationRate, rebellionRate, rebellionCount, totalDivisionsVoted,
            activityScore, rebellionPercentile, participationPercentile,
            questionsPercentile, speechesPercentile, committeesPercentile
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    logger.info("Populated mp_stats with %d rows", len(rows))


def populate_peer_averages(conn, mp_metrics):
    """Populate peer_averages table with per-house aggregate averages."""
    by_house = {}
    for m in mp_metrics:
        by_house.setdefault(m["house"], []).append(m)

    rows = []
    for house in (COMMONS, LORDS):
        mps = by_house.get(house, [])
        n = len(mps)
        if n == 0:
            rows.append((house, 0.0, 0.0, 0.0, 0.0, 0.0, 0))
            continue
        avg_q = sum(m["questionCount"] for m in mps) / n
        avg_s = sum(m["speechCount"] for m in mps) / n
        avg_c = sum(m["committeeCount"] for m in mps) / n
        avg_p = sum(m["voteParticipationRate"] for m in mps) / n
        avg_r = sum(m["rebellionRate"] for m in mps) / n
        rows.append((house, avg_q, avg_s, avg_c, avg_p, avg_r, n))

    conn.executemany(
        """
        INSERT OR REPLACE INTO peer_averages (
            house, avgQuestions, avgSpeeches, avgCommittees,
            avgParticipation, avgRebellion, mpCount
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    logger.info("Populated peer_averages with %d rows", len(rows))


def main():
    parser = argparse.ArgumentParser(
        description="Post-merge precomputation: produces mp_stats and peer_averages tables."
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to the merged goveye.db (modified in-place).",
    )
    parser.add_argument(
        "--schema", required=False,
        help="Path to the Room exported schema JSON (for reference, not used directly).",
    )
    parser.add_argument(
        "--changed-apis", default=None,
        help="Comma-separated list of changed per-API streams (from check_seed.py). "
             "If provided and none of this script's dependencies are in the list, "
             "precompute is skipped. If omitted, full rebuild runs.",
    )
    args = parser.parse_args()

    # Delta skip: if changed_apis is provided and none of our dependencies changed,
    # the precomputed tables are unchanged — exit early.
    # Dependencies (source tables actually read by this script):
    #   mps (id/house/partyName/isActive/membershipStartDate),
    #   commons_votes+lords_votes (divisions, division_votes),
    #   committees (mp_committee_cross_ref), debates (debate_speeches),
    #   hansard (hansard_contributions), interests+expenses (financeCount),
    #   bio_data+historical_members (tenure start dates).
    PRECOMPUTE_DEPENDENCIES = {"mps", "commons_votes", "lords_votes",
                                "committees", "debates", "hansard",
                                "interests", "expenses", "bio_data",
                                "historical_members"}
    if args.changed_apis is not None:
        changed = {a.strip() for a in args.changed_apis.split(",") if a.strip()}
        if not changed.intersection(PRECOMPUTE_DEPENDENCIES):
            logger.info("Skipping precompute — none of %s changed (changed: %s)",
                        sorted(PRECOMPUTE_DEPENDENCIES), sorted(changed))
            return
        logger.info("Running precompute — changed APIs include dependencies: %s",
                    sorted(changed.intersection(PRECOMPUTE_DEPENDENCIES)))

    if not os.path.exists(args.output):
        print(f"ERROR: {args.output} does not exist — run merge_dbs.py first.")
        sys.exit(1)

    conn = sqlite3.connect(args.output)
    try:
        logger.info("Creating precompute tables...")
        create_precompute_tables(conn)

        logger.info("Computing per-MP metrics...")
        mp_metrics = compute_per_mp_metrics(conn)

        logger.info("Populating mp_stats table...")
        populate_mp_stats(conn, mp_metrics)

        logger.info("Populating peer_averages table...")
        populate_peer_averages(conn, mp_metrics)

        # Verify
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM mp_stats")
        mp_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM peer_averages")
        pa_count = cursor.fetchone()[0]
        logger.info("Done: mp_stats has %d rows, peer_averages has %d rows", mp_count, pa_count)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
