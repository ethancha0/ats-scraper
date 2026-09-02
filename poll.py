"""
Poll job boards across multiple ATS platforms (Workday, Greenhouse, Lever,
Ashby, SmartRecruiters) for new postings and notify a Discord webhook when
new ones appear.

Usage:
    python poll.py                        # full run, all ATS types
    python poll.py --ats=workday          # only poll one ATS type
    python poll.py --ats=greenhouse,lever # only poll a subset
    python poll.py --dry-run              # fetch + diff, print instead of posting to Discord
    python poll.py --limit=10             # only process the first N companies (for testing)
"""
import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

COMPANIES_CSV = Path("data/companies.csv")
STATE_DIR = Path("state")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

CONCURRENCY = int(os.environ.get("POLL_CONCURRENCY", "40"))
REQUEST_TIMEOUT = 15.0
# Workday hard-caps `limit` at 20 per request -- anything higher returns
# HTTP 400. (Confirmed against kalil0321/ats-scrapers' Workday adapter.)
WORKDAY_PAGE_LIMIT = 20
RESULTS_PER_COMPANY = min(int(os.environ.get("RESULTS_PER_COMPANY", "20")), WORKDAY_PAGE_LIMIT)
# SmartRecruiters paginates in pages of up to 100; we only ever want the
# newest page, same philosophy as the Workday cap above.
SMARTRECRUITERS_PAGE_LIMIT = 100
MAX_SEEN_PER_COMPANY = 300  # bound state file growth over time
DISCORD_EMBEDS_PER_MESSAGE = 10  # Discord's hard limit per message
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.5

ATS_TYPES = ("workday", "greenhouse", "lever", "ashby", "smartrecruiters")

# --- Filtering -------------------------------------------------------------
# Only notify about internships/co-ops, and only in software/full-stack-ish
# roles -- and only ones that still look freshly posted. Set
# INTERNSHIPS_ONLY=false or SOFTWARE_ROLES_ONLY=false in the environment to
# loosen either filter.
INTERNSHIPS_ONLY = os.environ.get("INTERNSHIPS_ONLY", "true").strip().lower() != "false"
SOFTWARE_ROLES_ONLY = os.environ.get("SOFTWARE_ROLES_ONLY", "true").strip().lower() != "false"

# Word-boundary matched so "International", "cooperation", etc. don't
# false-positive. Co-ops are explicitly included here (matches "co-op",
# "co-ops", "coop", "coops").
INTERNSHIP_RE = re.compile(r"\b(intern|interns|internship|internships|co-?ops?)\b", re.IGNORECASE)

# Title must also look software/full-stack-related, so e.g. "Marketing
# Intern" or "Sales Development Co-op" don't slip through just because they
# matched INTERNSHIP_RE. Deliberately an allow-list of software-engineering
# terms rather than a bare "engineer" match, since that would also catch
# mechanical/electrical/civil engineering internships.
SOFTWARE_ROLE_RE = re.compile(
    r"\b("
    r"software|swe|sde|"
    r"full[\s-]?stack|front[\s-]?end|back[\s-]?end|"
    r"web\s?dev(?:eloper|elopment)?s?|application\s+develop\w*|"
    r"devops|dev\s?ops|site\s+reliability|\bsre\b|"
    r"mobile\s+(?:developer|engineer)s?|ios\s+(?:developer|engineer)s?|android\s+(?:developer|engineer)s?|"
    r"data\s+engineer\w*|machine\s+learning\s+engineer\w*|\bml\s+engineer\w*|ai\s+engineer\w*|"
    r"platform\s+engineer\w*|infrastructure\s+engineer\w*|systems?\s+engineer\w*|cloud\s+engineer\w*|"
    r"security\s+engineer\w*|qa\s+engineer\w*|test\s+engineer\w*|"
    r"computer\s+science|programmer|coding|"
    r"\bcs\b"
    r")\b",
    re.IGNORECASE,
)

# The diff against state/seen_*.json already means "new" = "appeared since
# our last poll" (~10 min), which is far tighter than "a few hours." This
# is just a safety net for the one edge case that isn't covered by diffing:
# if a run is ever skipped/delayed (Actions outage, repo paused, etc.), the
# next run would otherwise treat a multi-hour or multi-day backlog as "new."
_STALE_POSTED_MARKERS = ("yesterday", "day ago", "days ago", "week", "month", "30+")


def _looks_recent(posted_text):
    if not posted_text:
        return True  # no signal -- trust the diff
    t = posted_text.strip().lower()
    if "today" in t or "just posted" in t or "hour" in t:
        return True
    return not any(marker in t for marker in _STALE_POSTED_MARKERS)


def _relative_posted_text(posted_at):
    """Turn an exact timestamp (Greenhouse/Lever/Ashby/SmartRecruiters all
    expose one) into the same "Posted Today" / "Posted N Days Ago" style
    Workday gives us as plain text, so _looks_recent() can stay one
    ATS-agnostic function."""
    if posted_at is None:
        return ""
    now = datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    delta_days = (now - posted_at).total_seconds() / 86400
    if delta_days < 1:
        return "today"
    if delta_days < 2:
        return "yesterday"
    return f"{int(delta_days)} days ago"


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_epoch_ms(value):
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def load_companies(limit=None, ats_filter=None):
    """Parse companies.csv into endpoint-ready records."""
    companies = []
    with COMPANIES_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            slug = (row.get("slug") or "").strip()
            url = (row.get("url") or "").strip()
            ats = (row.get("ats") or "workday").strip().lower()
            if not (name and slug) or ats not in ATS_TYPES:
                continue
            if ats_filter and ats not in ats_filter:
                continue

            if ats == "workday":
                if "/" not in slug:
                    continue
                tenant, site = slug.split("/", 1)
                host = urlparse(url).netloc
                if not host:
                    continue
                companies.append(
                    {
                        "name": name,
                        "ats": ats,
                        "tenant": tenant,
                        "site": site,
                        "host": host,
                        "endpoint": f"https://{host}/wday/cxs/{slug}/jobs",
                    }
                )
            else:
                companies.append({"name": name, "ats": ats, "slug": slug})
    return companies[:limit] if limit else companies


def state_path_for(ats_filter):
    """One state file per ATS type when a single type is selected (this is
    how the GitHub Actions matrix runs it -- each lane only ever touches its
    own file, so parallel lanes can never conflict on the same state file).
    Falls back to a single combined file for ad-hoc/local multi-ATS runs."""
    if ats_filter and len(ats_filter) == 1:
        return STATE_DIR / f"seen_{next(iter(ats_filter))}.json"
    return STATE_DIR / "seen.json"


def load_state(state_file):
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {}


def save_state(state_file, state):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, sort_keys=True))


async def _post_json(client, url, payload):
    return await client.post(url, json=payload, timeout=REQUEST_TIMEOUT)


async def _get_json(client, url, params=None):
    return await client.get(url, params=params, timeout=REQUEST_TIMEOUT)


async def fetch_company(client, sem, company):
    """Fetch the most recent postings for one company. Never raises."""
    ats = company["ats"]
    last_err = None
    data = None
    async with sem:
        for attempt in range(MAX_RETRIES + 1):
            try:
                if ats == "workday":
                    payload = {"appliedFacets": {}, "limit": RESULTS_PER_COMPANY, "offset": 0, "searchText": ""}
                    resp = await _post_json(client, company["endpoint"], payload)
                elif ats == "greenhouse":
                    url = f"https://boards-api.greenhouse.io/v1/boards/{company['slug']}/jobs"
                    resp = await _get_json(client, url, params={"content": "false"})
                elif ats == "lever":
                    url = f"https://api.lever.co/v0/postings/{company['slug']}"
                    resp = await _get_json(client, url, params={"mode": "json"})
                elif ats == "ashby":
                    url = f"https://api.ashbyhq.com/posting-api/job-board/{company['slug']}"
                    resp = await _get_json(client, url)
                elif ats == "smartrecruiters":
                    url = f"https://api.smartrecruiters.com/v1/companies/{company['slug']}/postings"
                    resp = await _get_json(client, url, params={"limit": SMARTRECRUITERS_PAGE_LIMIT, "offset": 0})
                else:  # pragma: no cover - guarded by load_companies()
                    return company["name"], None, f"unknown ats: {ats}"
                resp.raise_for_status()
                data = resp.json()
                last_err = None
                break
            except Exception as e:  # noqa: BLE001 - one bad company shouldn't kill the run
                last_err = f"{type(e).__name__}: {e}"
                # Retry transient failures (timeouts, 5xx); don't bother
                # retrying a hard 4xx like 404/400, it won't change.
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500:
                    break
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        if last_err is not None:
            return company["name"], None, last_err

    try:
        results = _parse_postings(company, data)
    except Exception as e:  # noqa: BLE001 - a malformed response shouldn't kill the run
        return company["name"], None, f"parse error ({type(e).__name__}): {e}"
    return company["name"], results, None


def _parse_postings(company, data):
    ats = company["ats"]
    results = []

    if ats == "workday":
        for p in data.get("jobPostings", []) or []:
            path = p.get("externalPath")
            if not path:
                continue
            results.append(
                {
                    "id": path,
                    "title": p.get("title", "Untitled role"),
                    "location": p.get("locationsText", ""),
                    "posted": p.get("postedOn", ""),
                    "url": f"https://{company['host']}/{company['site']}{path}",
                }
            )

    elif ats == "greenhouse":
        for j in data.get("jobs", []) or []:
            job_id = j.get("id")
            url = j.get("absolute_url")
            if job_id is None or not url:
                continue
            posted_at = _parse_iso(j.get("first_published")) or _parse_iso(j.get("updated_at"))
            results.append(
                {
                    "id": str(job_id),
                    "title": j.get("title", "Untitled role"),
                    "location": (j.get("location") or {}).get("name", ""),
                    "posted": _relative_posted_text(posted_at),
                    "url": url,
                }
            )

    elif ats == "lever":
        for j in data or []:
            job_id = j.get("id")
            url = j.get("hostedUrl")
            if not job_id or not url:
                continue
            posted_at = _parse_epoch_ms(j.get("createdAt"))
            results.append(
                {
                    "id": str(job_id),
                    "title": j.get("text", "Untitled role"),
                    "location": (j.get("categories") or {}).get("location", ""),
                    "posted": _relative_posted_text(posted_at),
                    "url": url,
                }
            )

    elif ats == "ashby":
        for j in data.get("jobs", []) or []:
            job_id = j.get("id")
            url = j.get("jobUrl") or j.get("applyUrl")
            if not job_id or not url:
                continue
            posted_at = _parse_iso(j.get("publishedAt"))
            results.append(
                {
                    "id": str(job_id),
                    "title": j.get("title", "Untitled role"),
                    "location": j.get("location", "") or "",
                    "posted": _relative_posted_text(posted_at),
                    "url": url,
                }
            )

    elif ats == "smartrecruiters":
        for j in data.get("content", []) or []:
            job_id = j.get("id")
            if not job_id:
                continue
            posted_at = _parse_iso(j.get("releasedDate"))
            results.append(
                {
                    "id": str(job_id),
                    "title": j.get("name", "Untitled role"),
                    "location": _format_smartrecruiters_location(j.get("location")),
                    "posted": _relative_posted_text(posted_at),
                    "url": f"https://jobs.smartrecruiters.com/{company['slug']}/{job_id}",
                }
            )

    return results


def _format_smartrecruiters_location(location):
    if not isinstance(location, dict):
        return ""
    parts = [
        str(location[k]).strip()
        for k in ("city", "region", "country")
        if isinstance(location.get(k), str) and location.get(k).strip()
    ]
    return ", ".join(parts)


async def poll_all(companies):
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {"User-Agent": "job-alert-bot/1.0 (personal use, low-frequency polling)"}
    async with httpx.AsyncClient(headers=headers) as client:
        tasks = [fetch_company(client, sem, c) for c in companies]
        return await asyncio.gather(*tasks)


def notify_discord(new_by_company):
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set — skipping Discord notification.")
        return
    embeds = []
    for company_name, jobs in new_by_company.items():
        for job in jobs:
            embeds.append(
                {
                    "title": f"{job['title']} — {company_name}"[:256],
                    "description": job.get("location", "") or "​",
                    "url": job["url"],
                }
            )
    for i in range(0, len(embeds), DISCORD_EMBEDS_PER_MESSAGE):
        chunk = embeds[i : i + DISCORD_EMBEDS_PER_MESSAGE]
        resp = httpx.post(DISCORD_WEBHOOK_URL, json={"embeds": chunk}, timeout=15)
        if resp.status_code >= 300:
            print(f"Discord post failed ({resp.status_code}): {resp.text[:200]}")


def main():
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    limit = None
    ats_filter = None
    for a in argv:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        elif a.startswith("--ats="):
            requested = {t.strip().lower() for t in a.split("=", 1)[1].split(",") if t.strip()}
            unknown = requested - set(ATS_TYPES)
            if unknown:
                sys.exit(f"Unknown --ats value(s): {', '.join(sorted(unknown))}. Choose from: {', '.join(ATS_TYPES)}")
            ats_filter = requested

    companies = load_companies(limit=limit, ats_filter=ats_filter)
    ats_label = ",".join(sorted(ats_filter)) if ats_filter else "all ATS types"
    print(f"Polling {len(companies)} companies ({ats_label})...")

    state_file = state_path_for(ats_filter)
    state = load_state(state_file)
    first_run = len(state) == 0

    results = asyncio.run(poll_all(companies))

    new_by_company = {}
    errors = []
    total_new_all = 0
    would_match_first_run = 0
    for name, jobs, err in results:
        if err is not None:
            errors.append((name, err))
            continue
        seen = set(state.get(name, []))
        new_jobs = [j for j in jobs if j["id"] not in seen]
        updated_seen = list({j["id"] for j in jobs} | seen)
        state[name] = updated_seen[:MAX_SEEN_PER_COMPANY]
        total_new_all += len(new_jobs)

        if not new_jobs:
            continue
        qualifying = [
            j
            for j in new_jobs
            if (not INTERNSHIPS_ONLY or INTERNSHIP_RE.search(j["title"]))
            and (not SOFTWARE_ROLES_ONLY or SOFTWARE_ROLE_RE.search(j["title"]))
            and _looks_recent(j.get("posted", ""))
        ]
        if not qualifying:
            continue
        if first_run:
            would_match_first_run += len(qualifying)
        else:
            new_by_company[name] = qualifying

    print(f"{len(errors)} companies failed to fetch.")
    for name, err in errors[:10]:
        print(f"  FAILED: {name}: {err}")
    if len(errors) > 10:
        print(f"  ...and {len(errors) - 10} more")

    total_new = sum(len(v) for v in new_by_company.values())
    if first_run:
        print(
            f"{total_new_all} existing postings found while seeding "
            f"({would_match_first_run} would have matched filters -- "
            f"internships_only={INTERNSHIPS_ONLY}, software_roles_only={SOFTWARE_ROLES_ONLY})."
        )
    else:
        print(
            f"{total_new_all} new postings found; {total_new} matched filters "
            f"(internships_only={INTERNSHIPS_ONLY}, software_roles_only={SOFTWARE_ROLES_ONLY}) "
            f"across {len(new_by_company)} companies."
        )

    if first_run:
        print("First run — seeding state only, no notifications sent.")
    elif dry_run:
        print("Dry run — not posting to Discord. New postings found:")
        for name, jobs in new_by_company.items():
            for j in jobs:
                print(f"  NEW: {name} — {j['title']} ({j['url']})")
    elif total_new:
        notify_discord(new_by_company)

    save_state(state_file, state)
    print(f"State saved to {state_file}.")


if __name__ == "__main__":
    main()
