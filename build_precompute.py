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

Usage:
  python build_precompute.py --output goveye.db --schema schemas/bundled_schema.json
"""

import argparse
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
VOTE_WEIGHT = 40
QUESTIONS_WEIGHT = 20
SPEECHES_WEIGHT = 20
COMMITTEES_WEIGHT = 20

# Houses
COMMONS = 1
LORDS = 2


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
            `activityScore` INTEGER NOT NULL,
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
      memberId, house, questionCount, speechCount, committeeCount,
      voteParticipationRate, rebellionRate, rebellionCount, totalDivisionsVoted
    """
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get all MPs with their house
    cursor.execute("SELECT id, house, partyName FROM mps")
    mps = cursor.fetchall()
    logger.info("Computing metrics for %d MPs", len(mps))

    results = []
    for mp in mps:
        member_id = mp["id"]
        house = mp["house"]
        party_name = mp["partyName"]

        # questionCount — count all hansard contributions as proxy
        cursor.execute(
            "SELECT COUNT(*) FROM hansard_contributions WHERE memberId = ?",
            (member_id,),
        )
        question_count = cursor.fetchone()[0]

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

        # voteParticipationRate — voted divisions / total divisions in house
        cursor.execute(
            "SELECT COUNT(DISTINCT divisionId) FROM division_votes WHERE memberId = ?",
            (member_id,),
        )
        voted_count = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM divisions WHERE house = ?",
            (house,),
        )
        total_divisions = cursor.fetchone()[0]
        participation_rate = voted_count / total_divisions if total_divisions > 0 else 0.0

        # rebellionRate — party-majority method via SQL GROUP BY
        rebellion_rate = 0.0
        rebellion_count = 0
        total_divisions_voted = voted_count

        if party_name and voted_count > 0:
            # Get the MP's votes
            cursor.execute(
                "SELECT divisionId, vote FROM division_votes WHERE memberId = ?",
                (member_id,),
            )
            mp_votes = cursor.fetchall()

            # Get party vote counts per division (single GROUP BY)
            division_ids = [v["divisionId"] for v in mp_votes]
            if division_ids:
                placeholders = ",".join("?" * len(division_ids))
                cursor.execute(
                    f"""
                    SELECT divisionId,
                           SUM(CASE WHEN vote = 'Aye' THEN 1 ELSE 0 END) AS partyAyes,
                           SUM(CASE WHEN vote = 'No' THEN 1 ELSE 0 END) AS partyNoes
                    FROM division_votes
                    WHERE divisionId IN ({placeholders})
                        AND partyName = ?
                        AND vote != 'NoVoteRecorded'
                        AND vote != 'No Vote Recorded'
                    GROUP BY divisionId
                    """,
                    division_ids + [party_name],
                )
                party_counts = {r["divisionId"]: r for r in cursor.fetchall()}

                rebellions = 0
                scored = 0
                for vote in mp_votes:
                    div_id = vote["divisionId"]
                    mp_vote = vote["vote"]
                    if mp_vote not in ("Aye", "No"):
                        continue  # skip no-vote-recorded
                    pc = party_counts.get(div_id)
                    if pc is None:
                        continue
                    ayes = pc["partyAyes"]
                    noes = pc["partyNoes"]
                    if ayes == noes:
                        continue  # tie — no rebellion
                    party_majority = "Aye" if ayes > noes else "No"
                    if mp_vote != party_majority:
                        rebellions += 1
                    scored += 1

                if scored > 0:
                    rebellion_rate = rebellions / scored
                    rebellion_count = rebellions

        results.append({
            "memberId": member_id,
            "house": house,
            "questionCount": question_count,
            "speechCount": speech_count,
            "committeeCount": committee_count,
            "voteParticipationRate": participation_rate,
            "rebellionRate": rebellion_rate,
            "rebellionCount": rebellion_count,
            "totalDivisionsVoted": total_divisions_voted,
        })

    return results


def compute_activity_score(participation_rate, question_count, speech_count,
                           committee_count, avg_questions, avg_speeches, avg_committees):
    """Compute activity score using the same formula as ActivityScoreCalculator.kt."""
    vote_contrib = min(int(participation_rate * VOTE_WEIGHT), VOTE_WEIGHT)
    questions_contrib = _normalize(question_count, avg_questions, QUESTIONS_WEIGHT)
    speeches_contrib = _normalize(speech_count, avg_speeches, SPEECHES_WEIGHT)
    committees_contrib = _normalize(committee_count, avg_committees, COMMITTEES_WEIGHT)
    total = vote_contrib + questions_contrib + speeches_contrib + committees_contrib
    return max(0, min(100, total))


def _normalize(count, average, weight):
    """Normalize a count relative to peer average (matches ActivityScoreCalculator)."""
    if average <= 0:
        return weight if count > 0 else 0
    ratio = count / (average * 2.0)
    return min(int(ratio * weight), weight)


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
    # Group by house for percentile computation
    by_house = {}
    for m in mp_metrics:
        by_house.setdefault(m["house"], []).append(m)

    # Compute peer averages per house (needed for activity score)
    house_averages = {}
    for house, mps in by_house.items():
        n = len(mps)
        if n == 0:
            house_averages[house] = (0.0, 0.0, 0.0)
            continue
        avg_q = sum(m["questionCount"] for m in mps) / n
        avg_s = sum(m["speechCount"] for m in mps) / n
        avg_c = sum(m["committeeCount"] for m in mps) / n
        house_averages[house] = (avg_q, avg_s, avg_c)

    # Compute percentiles and activity scores
    rows = []
    for house, mps in by_house.items():
        avg_q, avg_s, avg_c = house_averages[house]

        # Collect peer value lists for percentile computation
        rebellion_values = [m["rebellionRate"] for m in mps]
        participation_values = [m["voteParticipationRate"] for m in mps]
        question_values = [float(m["questionCount"]) for m in mps]
        speech_values = [float(m["speechCount"]) for m in mps]
        committee_values = [float(m["committeeCount"]) for m in mps]

        for m in mps:
            activity_score = compute_activity_score(
                m["voteParticipationRate"],
                m["questionCount"],
                m["speechCount"],
                m["committeeCount"],
                avg_q, avg_s, avg_c,
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
    args = parser.parse_args()

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
