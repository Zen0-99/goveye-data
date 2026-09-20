#!/usr/bin/env python3
"""Per-API build script for Companies House enrichment (officers/appointments).

Searches the Companies House public API for officer records matching each
current Commons MP, disambiguates candidates deterministically (declared
interests cross-match, resignation-near-election timing, region proximity)
with a Jev `choice` call only for genuinely ambiguous sets, then records:

  mp_officer_identity — one row per confidently matched MP: CH officer id,
      partial DOB (month/year — CH never exposes the day), nationality,
      country of residence, disqualification flag + detail, match audit
      fields (method + confidence).
  mp_appointments — full appointment history per matched MP: company
      name/number, role, appointed/resigned dates, company status/type/SIC
      codes/incorporation date, PSC natures-of-control.

Matching is deliberately conservative — a missed match (NULL row) is far
better than a wrong identity. `matchMethod`/`matchConfidence` persist the
audit trail.

Partial DOB writeback: `--mode dob-backfill` fills `bio_data.dateOfBirth`
with "YYYY-MM" where NULL — full dates from Wikidata/Wikipedia are never
overwritten. It runs inside update-bio-data.yml against the published
companies_house.db so the change ships on the bio-data stream (the owning
stream for dateOfBirth). The app renders YYYY-MM as "Jan 1964" +
approximate age (see DobUtils.kt).

Auth: COMPANIES_HOUSE_API_KEY env var (HTTP Basic, key = username, blank
password). Jev disambiguation uses TYPESAFE_API_KEY — if unset, ambiguous
sets resolve to no-match instead of guessing.

Modes:
  seed  — create fresh DB, match all MPs (checkpoint-resumable)
  delta — copy previous DB, re-match all MPs, upsert

Usage:
  python build_companies_house.py --output companies_house.db \
      --schema schemas/bundled_schema.json --mps-db mps.db \
      --interests-db interests.db --mode seed
  python build_companies_house.py --mode dob-backfill \
      --previous-db companies_house.db --bio-db bio_data.db
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import time

import requests

import schema as schema_module
from api_helper import logger

# --- Constants ---

CH_BASE = "https://api.company-information.service.gov.uk"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
CH_DELAY = 0.55  # 600 req / 5min = 2/sec; 0.55s keeps us under
CH_MAX_RETRIES = 4
SEARCH_ITEMS = 10          # officer candidates to consider per MP
MAX_CANDIDATE_FETCHES = 8  # appointments lookups per MP
APPTS_PAGE = 50

TABLE_NAMES = ["mp_officer_identity", "mp_appointments"]

# Pipeline-only bookkeeping (never merged/diffed — see _wq_fulltext_verified)
PROCESSED_TABLE = "_ch_processed"

# Deterministic scoring weights
W_INTEREST_MATCH = 3      # appointment company intersects declared interests
W_RESIGN_AT_ELECTION = 2  # resigned within 180 days after membershipStartDate
W_REGION = 1              # address snippet shares token with constituency
ACCEPT_SCORE = 4          # accept without Jev when top >= this and margin >= 2
ACCEPT_MARGIN = 2
JEV_MIN_SCORE = 2         # only ask Jev when top score >= this (else: no match)
JEV_MARGIN = 1            # ambiguous when top2 within this

RESIGN_WINDOW_DAYS = 180

# Company-name suffixes stripped for comparison
_COMPANY_SUFFIXES = re.compile(
    r"\b(limited|ltd|plc|llp|lp|cic|cio|uk|company|co|group|holdings|"
    r"the|and|&|of)\b", re.IGNORECASE)


# --- HTTP ---

def ch_get(path, api_key, params=None):
    """GET against the CH API with Basic auth + retry on 429/5xx."""
    url = f"{CH_BASE}{path}"
    for attempt in range(CH_MAX_RETRIES):
        try:
            r = requests.get(url, params=params, auth=(api_key, ""),
                             timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 5 * (attempt + 1)
                logger.warning("CH %s on %s — waiting %ds",
                               r.status_code, path, wait)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            time.sleep(CH_DELAY)
            return r.json()
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as e:
            logger.warning("CH %s attempt %d failed: %s", path, attempt, e)
            time.sleep(2 ** attempt)
    return None


def jev_choice(api_key, instructions, criteria):
    """Ask Jev to pick among criteria keys. Returns (choice_key, confidence)
    or (None, 0) on failure/insufficient evidence."""
    body = {
        "state": {"task": "officer-identity-disambiguation"},
        "model": JEV_MODEL,
        "questions": {"pick": {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        }},
    }
    try:
        r = requests.post(
            JEV_URL, json=body, timeout=60,
            headers={"Authorization": f"Bearer {api_key}"})
        r.raise_for_status()
        ans = r.json().get("answers", {}).get("pick", {})
        return ans.get("choice"), ans.get("confidence", 0.0)
    except Exception as e:
        logger.warning("Jev call failed: %s", e)
        return None, 0.0


# --- MP + interests inputs ---

def fetch_mps(mps_db):
    """Current Commons MPs: id, plain name, constituency, election date."""
    conn = sqlite3.connect(mps_db)
    rows = conn.execute(
        "SELECT id, nameDisplayAs, constituencyName, membershipStartDate "
        "FROM mps WHERE house = 1 AND isActive = 1"
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "name": r[1], "constituency": r[2] or "",
         "elected": (r[3] or "")[:10]}
        for r in rows
    ]


_TITLES = re.compile(
    r"^(sir|dame|dr|mr|mrs|ms|miss|rt hon|professor|prof)\.?\s+",
    re.IGNORECASE)


def plain_name(display):
    """'Sir John Whittingdale' -> 'John Whittingdale' for CH search."""
    n = display.strip()
    prev = None
    while prev != n:
        prev = n
        n = _TITLES.sub("", n).strip()
    return n


# fieldsJson field names that hold an organisation/company name. Other
# fields (addresses, "role unpaid", hours, dates) would pollute the
# declared set with junk tokens.
_COMPANY_FIELD_NAMES = {
    "employer", "payername", "ultimatepayername", "organisationname",
    "companyname", "donorname", "nameofcompany", "name",
}


def load_declared_companies(interests_db):
    """memberId -> set of normalised name fragments from interests
    fieldsJson (whitelisted org-name fields) + summaries. Used to
    cross-match appointment company names."""
    if not interests_db or not os.path.exists(interests_db):
        logger.warning("No interests DB — interest cross-match disabled")
        return {}
    conn = sqlite3.connect(interests_db)
    rows = conn.execute(
        "SELECT memberId, summary, fieldsJson FROM interests"
    ).fetchall()
    conn.close()

    out = {}
    for member_id, summary, fields_json in rows:
        texts = set()
        if summary:
            texts.add(summary)
        try:
            for field in json.loads(fields_json or "[]"):
                name = (field.get("name") or "").lower()
                v = field.get("value")
                if (name in _COMPANY_FIELD_NAMES
                        and isinstance(v, str) and len(v) > 2):
                    texts.add(v)
        except (json.JSONDecodeError, AttributeError):
            pass
        bucket = out.setdefault(member_id, set())
        for t in texts:
            norm = normalise_company(t)
            if norm:
                bucket.add(norm)
    return out


def normalise_company(name):
    """Lowercase, strip suffixes/punctuation -> token set for comparison."""
    n = _COMPANY_SUFFIXES.sub(" ", name.lower())
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    tokens = {t for t in n.split() if len(t) > 2}
    return frozenset(tokens) if tokens else None


def companies_overlap(ch_company, declared_set):
    """True when the CH company name's tokens intersect a declared text
    substantially (>=60% of company tokens present, rounded up — so a
    3-token name needs 2 hits, never 1)."""
    import math
    cn = normalise_company(ch_company)
    if not cn:
        return False
    needed = max(1, math.ceil(0.6 * len(cn)))
    for d in declared_set:
        if len(cn & d) >= needed:
            return True
    return False


# --- Matching ---

def days_between(iso_a, iso_b):
    import datetime
    try:
        a = datetime.date.fromisoformat(iso_a[:10])
        b = datetime.date.fromisoformat(iso_b[:10])
        return (a - b).days
    except (ValueError, TypeError):
        return None


def score_candidate(mp, officer, appointments, declared):
    """Score one officer candidate. Returns (score, reasons)."""
    score, reasons = 0, []
    elected = mp["elected"]

    for appt in appointments:
        company = (appt.get("appointed_to") or {}).get("company_name", "")
        if company and companies_overlap(company, declared):
            score += W_INTEREST_MATCH
            reasons.append(f"declared:{company}")
        resigned = appt.get("resigned_on")
        if resigned and elected:
            gap = days_between(resigned, elected)
            if gap is not None and 0 <= gap <= RESIGN_WINDOW_DAYS:
                score += W_RESIGN_AT_ELECTION
                reasons.append(f"resigned-at-election:{company}")

    snippet = (officer.get("address_snippet") or "").lower()
    for tok in re.findall(r"[a-z]+", (mp["constituency"] or "").lower()):
        if len(tok) > 3 and tok in snippet:
            score += W_REGION
            reasons.append(f"region:{tok}")
            break

    return score, reasons


def describe_candidate(officer, appointments):
    """Human-readable candidate summary for Jev criteria."""
    dob = officer.get("date_of_birth") or {}
    dob_s = (f"{dob['month']:02d}/{dob['year']}"
             if dob.get("month") and dob.get("year") else "unknown DOB")
    lines = [
        f"Name: {officer.get('title', '?')} ({dob_s})",
        f"Address: {officer.get('address_snippet', '?')}",
        "Appointments:",
    ]
    for a in appointments[:15]:
        co = (a.get("appointed_to") or {})
        line = (f"  - {co.get('company_name', '?')} "
                f"({a.get('officer_role', '?')}, "
                f"appointed {a.get('appointed_on', '?')}"
                + (f", resigned {a['resigned_on']}"
                   if a.get("resigned_on") else ", current") + ")")
        lines.append(line)
    return "\n".join(lines)


def officer_id_of(item):
    """Extract officer id from a search item. links.self is
    '/officers/{id}/appointments' — the id is the middle segment."""
    href = (item.get("links") or {}).get("self", "")
    m = re.search(r"/officers/([^/]+)", href)
    return m.group(1) if m else None


def match_mp(mp, declared, ch_key, jev_key, appt_cache):
    """Returns (officer_dict, appointments, method, confidence) or
    (None, [], 'none', 0)."""
    query = plain_name(mp["name"])
    res = ch_get("/search/officers", ch_key,
                 params={"q": query, "items_per_page": SEARCH_ITEMS})
    if not res or not res.get("items"):
        return None, [], "none", 0.0

    candidates = []
    for item in res["items"][:MAX_CANDIDATE_FETCHES]:
        # Skip corporate officers — search returns companies too
        if re.search(r"\b(ltd|limited|plc|llp|inc|llc)\b\.?$",
                     item.get("title", "").lower()):
            continue
        officer_id = officer_id_of(item)
        if not officer_id:
            continue
        if officer_id not in appt_cache:
            appt_cache[officer_id] = fetch_all_appointments(ch_key, officer_id)
        appts = appt_cache[officer_id]
        score, reasons = score_candidate(mp, item, appts, declared)
        candidates.append({
            "id": officer_id, "item": item, "appts": appts,
            "score": score, "reasons": reasons,
        })

    if not candidates:
        return None, [], "none", 0.0
    candidates.sort(key=lambda c: -c["score"])
    top = candidates[0]
    second = candidates[1]["score"] if len(candidates) > 1 else 0

    # Deterministic accept
    if top["score"] >= ACCEPT_SCORE and top["score"] - second >= ACCEPT_MARGIN:
        method = "interests" if any(r.startswith("declared:")
                                    for r in top["reasons"]) else "timing"
        return top["item"], top["appts"], method, min(top["score"] / 6.0, 1.0)

    # Clearly nothing there
    if top["score"] < JEV_MIN_SCORE:
        return None, [], "none", 0.0

    # Ambiguous — ask Jev if available
    if not jev_key:
        logger.info("MP %s (%s): ambiguous, no Jev key — no match",
                    mp["id"], mp["name"])
        return None, [], "none", 0.0

    pool = [c for c in candidates if top["score"] - c["score"] <= JEV_MARGIN]
    criteria = {f"candidate_{i}": describe_candidate(c["item"], c["appts"])
                for i, c in enumerate(pool[:4])}
    criteria["none"] = ("None of these is the MP — insufficient or "
                        "contradictory evidence.")
    instructions = (
        f"Which Companies House officer is the UK MP {mp['name']} "
        f"(constituency: {mp['constituency']}, first elected "
        f"{mp['elected'] or 'unknown'})? MPs commonly resign directorships "
        f"on election. Pick 'none' unless the evidence is convincing.")
    choice, conf = jev_choice(jev_key, instructions, criteria)
    if choice and choice.startswith("candidate_") and conf >= 0.6:
        idx = int(choice.rsplit("_", 1)[1])
        pick = pool[idx]
        logger.info("MP %s: Jev picked candidate %d (conf %.2f)",
                    mp["id"], idx, conf)
        return pick["item"], pick["appts"], "jev", conf

    return None, [], "none", 0.0


def fetch_all_appointments(ch_key, officer_id):
    items, start = [], 0
    while True:
        res = ch_get(f"/officers/{officer_id}/appointments", ch_key,
                     params={"items_per_page": APPTS_PAGE,
                             "start_index": start})
        if not res:
            break
        items.extend(res.get("items", []))
        if len(items) >= res.get("total_results", 0):
            break
        start += APPTS_PAGE
        if start > 500:  # safety
            break
    return items


# --- Company + PSC enrichment ---

def fetch_company(ch_key, company_number, cache):
    if company_number in cache:
        return cache[company_number]
    data = ch_get(f"/company/{company_number}", ch_key)
    cache[company_number] = data
    return data


def fetch_psc_natures(ch_key, company_number, officer_name, cache):
    """Natures of control where the PSC is the officer (corporate PSCs of the
    appointed company are also recorded — they describe control structure)."""
    if company_number in cache:
        return cache[company_number]
    res = ch_get(
        f"/company/{company_number}/persons-with-significant-control",
        ch_key)
    natures = set()
    if res:
        for psc in res.get("items", []):
            for n in psc.get("natures_of_control", []):
                natures.add(n)
    result = sorted(natures)
    cache[company_number] = result
    return result


# --- DB writes ---

def ensure_processed_table(conn):
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {PROCESSED_TABLE} "
        "(mpId INTEGER PRIMARY KEY, matched INTEGER NOT NULL, "
        "timestamp INTEGER NOT NULL)")
    conn.commit()


def get_processed_ids(conn):
    ensure_processed_table(conn)
    return {r[0] for r in
            conn.execute(f"SELECT mpId FROM {PROCESSED_TABLE}")}


def upsert_identity(conn, mp_id, officer, officer_id, appointments,
                    method, conf, disq, timestamp):
    dob = officer.get("date_of_birth") or {}
    # nationality / country_of_residence live on appointment items,
    # not the search result — take the first non-null.
    nationality = country = None
    for a in appointments:
        nationality = nationality or a.get("nationality")
        country = country or a.get("country_of_residence")
    disq_json = json.dumps(disq) if disq else None
    conn.execute(
        "INSERT OR REPLACE INTO mp_officer_identity "
        "(mpId, officerId, officerName, dobMonth, dobYear, nationality, "
        "countryOfResidence, matchMethod, matchConfidence, isDisqualified, "
        "disqualificationJson, lastUpdated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (mp_id, officer_id, officer.get("title", ""),
         dob.get("month"), dob.get("year"),
         nationality, country,
         method, conf, 1 if disq else 0, disq_json, timestamp))


def upsert_appointments(conn, mp_id, officer_id, appointments, ch_key,
                        company_cache, psc_cache, timestamp):
    # Clear stale appointments for this MP, then insert current truth.
    conn.execute("DELETE FROM mp_appointments WHERE mpId = ?", (mp_id,))
    rows = []
    for a in appointments:
        co = a.get("appointed_to") or {}
        number = co.get("company_number")
        if not number:
            continue
        detail = fetch_company(ch_key, number, company_cache) or {}
        psc = fetch_psc_natures(ch_key, number, officer_id, psc_cache)
        resigned = a.get("resigned_on")
        rows.append((
            mp_id, number, a.get("officer_role", "unknown"),
            a.get("appointed_on", ""), officer_id,
            co.get("company_name", ""), resigned,
            0 if resigned else 1,
            co.get("company_status") or detail.get("company_status"),
            detail.get("type"),
            json.dumps(detail.get("sic_codes")) if detail.get("sic_codes")
            else None,
            detail.get("date_of_creation"),
            json.dumps(psc) if psc else None,
            timestamp,
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO mp_appointments "
        "(mpId, companyNumber, officerRole, appointedOn, officerId, "
        "companyName, resignedOn, isCurrent, companyStatus, companyType, "
        "companySicCodes, companyIncorporated, pscNatures, lastUpdated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def backfill_bio_from_ch(ch_db_path, bio_db_path):
    """Standalone backfill: copy partial DOBs from an existing
    companies_house.db into bio_data.db NULLs. Run inside the bio-data
    workflow so the change ships on the bio-data stream (the owning stream
    for dateOfBirth — a per-API DB can only be patched by its own release)."""
    src = sqlite3.connect(ch_db_path)
    rows = src.execute(
        "SELECT mpId, dobMonth, dobYear FROM mp_officer_identity "
        "WHERE dobMonth IS NOT NULL AND dobYear IS NOT NULL").fetchall()
    src.close()
    conn = sqlite3.connect(bio_db_path)
    filled = 0
    for mp_id, month, year in rows:
        cur = conn.execute(
            "UPDATE bio_data SET dateOfBirth = ? "
            "WHERE mpId = ? AND (dateOfBirth IS NULL OR dateOfBirth = '')",
            (f"{year:04d}-{month:02d}", mp_id))
        filled += cur.rowcount
    conn.commit()
    conn.close()
    logger.info("DOB backfill: %d/%d bio_data rows filled from "
                "Companies House", filled, len(rows))


def fetch_disqualifications(ch_key, officer_id):
    res = ch_get(f"/officers/{officer_id}/disqualifications", ch_key)
    if not res:
        return None
    items = res.get("items") or []
    return items if items else None


# --- Build modes ---

def run_pipeline(conn, mps, declared_map, ch_key, jev_key,
                 skip_ids=None, mp_limit=None):
    timestamp = int(time.time() * 1000)
    appt_cache, company_cache, psc_cache = {}, {}, {}
    skip_ids = skip_ids or set()

    processed = matched = 0
    for mp in mps:
        if mp["id"] in skip_ids:
            continue
        if mp_limit and processed >= mp_limit:
            break
        processed += 1

        officer, appts, method, conf = match_mp(
            mp, declared_map.get(mp["id"], set()),
            ch_key, jev_key, appt_cache)

        ensure_processed_table(conn)
        conn.execute(
            f"INSERT OR REPLACE INTO {PROCESSED_TABLE} "
            "(mpId, matched, timestamp) VALUES (?,?,?)",
            (mp["id"], 0 if officer is None else 1, timestamp))

        if officer is not None:
            officer_id = officer_id_of(officer)
            if not officer_id:
                logger.warning("MP %s: matched item lacks officer id — "
                               "skipping", mp["id"])
                continue
            disq = fetch_disqualifications(ch_key, officer_id)
            upsert_identity(conn, mp["id"], officer, officer_id, appts,
                            method, conf, disq, timestamp)
            n = upsert_appointments(conn, mp["id"], officer_id, appts,
                                    ch_key, company_cache, psc_cache,
                                    timestamp)
            matched += 1
            logger.info("MP %s %s: matched %s via %s (%.2f), %d appts",
                        mp["id"], mp["name"], officer.get("title"),
                        method, conf, n)
        else:
            logger.info("MP %s %s: no confident match", mp["id"], mp["name"])

        if processed % 25 == 0:
            conn.commit()
            logger.info("Progress: %d processed, %d matched",
                        processed, matched)
    conn.commit()
    logger.info("Done: %d processed, %d matched", processed, matched)


def build_seed(output_path, schema_path, mps_db, interests_db,
               mp_limit=None, checkpoint_db=None):
    ch_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if not ch_key:
        raise SystemExit("COMPANIES_HOUSE_API_KEY env var required")
    jev_key = os.environ.get("TYPESAFE_API_KEY")
    if not jev_key:
        logger.warning("TYPESAFE_API_KEY unset — ambiguous matches will "
                       "resolve to none")

    skip_ids = set()
    if checkpoint_db and os.path.exists(checkpoint_db):
        if os.path.abspath(checkpoint_db) != os.path.abspath(output_path):
            shutil.copy2(checkpoint_db, output_path)
        conn = sqlite3.connect(output_path)
        skip_ids = get_processed_ids(conn)
        logger.info("Resuming: %d MPs already processed", len(skip_ids))
    else:
        conn = schema_module.create_database_with_tables(
            output_path, schema_path, TABLE_NAMES)
        ensure_processed_table(conn)

    mps = fetch_mps(mps_db)
    declared = load_declared_companies(interests_db)
    run_pipeline(conn, mps, declared, ch_key, jev_key,
                 skip_ids=skip_ids, mp_limit=mp_limit)

    logger.info("VACUUMing database...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Seed build complete: %s", output_path)


def build_delta(output_path, previous_db, schema_path, mps_db, interests_db,
                mp_limit=None):
    ch_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if not ch_key:
        raise SystemExit("COMPANIES_HOUSE_API_KEY env var required")
    jev_key = os.environ.get("TYPESAFE_API_KEY")

    shutil.copy2(previous_db, output_path)
    conn = sqlite3.connect(output_path)
    schema_module.ensure_schema(conn, schema_path, TABLE_NAMES)
    ensure_processed_table(conn)

    mps = fetch_mps(mps_db)
    declared = load_declared_companies(interests_db)
    run_pipeline(conn, mps, declared, ch_key, jev_key,
                 mp_limit=mp_limit)

    logger.info("VACUUMing database...")
    conn.execute("VACUUM")
    conn.close()
    logger.info("Delta build complete: %s", output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Build the GovEye Companies House per-API DB.")
    parser.add_argument("--output", default="companies_house.db")
    parser.add_argument("--schema")
    parser.add_argument("--mps-db",
                        help="mps.db — MP list + election dates")
    parser.add_argument("--interests-db",
                        help="interests.db — declared companies for matching")
    parser.add_argument("--bio-db",
                        help="bio_data.db — partial DOB writeback target "
                             "(dob-backfill mode)")
    parser.add_argument("--mode", choices=["seed", "delta", "dob-backfill"],
                        default="seed")
    parser.add_argument("--previous-db")
    parser.add_argument("--mp-limit", type=int, default=None)
    parser.add_argument("--checkpoint-db",
                        help="Resume seed from checkpoint DB")
    args = parser.parse_args()

    if args.mode == "delta" and not args.previous_db:
        parser.error("--previous-db is required for delta mode")
    if args.mode == "dob-backfill" and not (args.previous_db and args.bio_db):
        parser.error("dob-backfill needs --previous-db (companies_house.db) "
                     "and --bio-db")
    if args.mode in ("seed", "delta") and not args.schema:
        parser.error("--schema is required for seed/delta modes")

    if args.mode == "dob-backfill":
        backfill_bio_from_ch(args.previous_db, args.bio_db)
        return
    if not args.mps_db:
        parser.error("--mps-db is required for seed/delta modes")
    if args.mode == "seed":
        build_seed(args.output, args.schema, args.mps_db, args.interests_db,
                   mp_limit=args.mp_limit,
                   checkpoint_db=args.checkpoint_db)
    else:
        build_delta(args.output, args.previous_db, args.schema, args.mps_db,
                    args.interests_db, mp_limit=args.mp_limit)


if __name__ == "__main__":
    main()
