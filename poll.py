"""
Poll a list of Workday-hosted career sites for new job postings and
notify a Discord webhook when new ones appear.

Usage:
    python poll.py                  # full run
    python poll.py --dry-run        # fetch + diff, print instead of posting to Discord
    python poll.py --limit=10       # only process the first N companies (for testing)
"""
import asyncio
import csv
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

COMPANIES_CSV = Path("data/companies.csv")
STATE_FILE = Path("state/seen.json")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

CONCURRENCY = int(os.environ.get("POLL_CONCURRENCY", "40"))
REQUEST_TIMEOUT = 15.0
# Workday hard-caps `limit` at 20 per request -- anything higher returns
# HTTP 400. (Confirmed against kalil0321/ats-scrapers' Workday adapter.)
WORKDAY_PAGE_LIMIT = 20
RESULTS_PER_COMPANY = min(int(os.environ.get("RESULTS_PER_COMPANY", "20")), WORKDAY_PAGE_LIMIT)
MAX_SEEN_PER_COMPANY = 300  # bound state file growth over time
DISCORD_EMBEDS_PER_MESSAGE = 10  # Discord's hard limit per message
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.5

# --- Filtering -------------------------------------------------------------
# Only notify about internships, and only ones that still look freshly
# posted. Set INTERNSHIPS_ONLY=false in the environment to notify on every
# role instead.
INTERNSHIPS_ONLY = os.environ.get("INTERNSHIPS_ONLY", "true").strip().lower() != "false"
# Word-boundary matched so "International", "cooperation", etc. don't
# false-positive.
INTERNSHIP_RE = re.compile(r"\b(intern|interns|internship|internships|co-?ops?)\b", re.IGNORECASE)

# The diff against state/seen.json already means "new" = "appeared since our
# last poll" (~10 min), which is far tighter than "a few hours." This regex
# is just a safety net for the one edge case that isn't covered by diffing:
# if a run is ever skipped/delayed (Actions outage, repo paused, etc.), the
# next run would otherwise treat a multi-hour or multi-day backlog as "new."
# Workday's postedOn field is coarse (day-level, not hour-level: "Posted
# Today" / "Posted Yesterday" / "Posted 3 Days Ago"), so this can't enforce
# "hours" precisely -- it only filters out postings Workday itself is
# already calling a day or more old.
_STALE_POSTED_MARKERS = ("yesterday", "day ago", "days ago", "week", "month", "30+")


def _looks_recent(posted_text):
    if not posted_text:
        return True  # no signal from Workday -- trust the diff
    t = posted_text.strip().lower()
    if "today" in t or "just posted" in t or "hour" in t:
        return True
    return not any(marker in t for marker in _STALE_POSTED_MARKERS)


def load_companies(limit=None):
    """Parse companies.csv into endpoint-ready records."""
    companies = []
    with COMPANIES_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            slug = (row.get("slug") or "").strip()
            url = (row.get("url") or "").strip()
            if not (name and slug and url) or "/" not in slug:
                continue
            tenant, site = slug.split("/", 1)
            host = urlparse(url).netloc
            if not host:
                continue
            companies.append(
                {
                    "name": name,
                    "tenant": tenant,
                    "site": site,
                    "host": host,
                    "endpoint": f"https://{host}/wday/cxs/{slug}/jobs",
                }
            )
    return companies[:limit] if limit else companies


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, sort_keys=True))


async def fetch_company(client, sem, company):
    """Fetch the most recent postings for one company. Never raises."""
    payload = {"appliedFacets": {}, "limit": RESULTS_PER_COMPANY, "offset": 0, "searchText": ""}
    last_err = None
    async with sem:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await client.post(company["endpoint"], json=payload, timeout=REQUEST_TIMEOUT)
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

    postings = data.get("jobPostings", []) or []
    results = []
    for p in postings:
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
    return company["name"], results, None


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
                    "description": job.get("location", "") or "\u200b",
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
    for a in argv:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])

    companies = load_companies(limit=limit)
    print(f"Polling {len(companies)} companies...")

    state = load_state()
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
            f"internships_only={INTERNSHIPS_ONLY})."
        )
    else:
        print(
            f"{total_new_all} new postings found; {total_new} matched filters "
            f"(internships_only={INTERNSHIPS_ONLY}) across {len(new_by_company)} companies."
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

    save_state(state)
    print("State saved.")


if __name__ == "__main__":
    main()
