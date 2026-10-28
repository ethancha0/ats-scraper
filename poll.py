"""
Poll job boards across multiple ATS platforms (Workday, Greenhouse, Lever,
Ashby, SmartRecruiters) plus community-maintained GitHub listing repos
(Simplify Jobs and friends) for new postings, and notify a Discord webhook
when new ones appear.

Usage:
    python poll.py                        # full run, all sources
    python poll.py --ats=workday          # only poll one ATS type
    python poll.py --ats=greenhouse,lever # only poll a subset
    python poll.py --ats=github           # only poll the GitHub listing sources
    python poll.py --dry-run              # fetch + diff, print instead of posting to Discord
    python poll.py --limit=10             # only process the first N companies (for testing)
"""
import asyncio
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
# Seen-state is {company: {hashed_job_id: last_seen_day}}. Every id in the
# current fetch is always kept (dropping a live id is what makes it look
# "new" again on the next pass); ids that stop showing up are pruned after
# SEEN_TTL_DAYS. MAX_SEEN_PER_COMPANY is only a ceiling on that stale tail.
SEEN_TTL_DAYS = 45
MAX_SEEN_PER_COMPANY = 5000
# Ids migrated from the old list-format state are stamped this many days
# before expiry: ones still live get refreshed on the next successful
# fetch, and the rest (mostly ids the old 300 cap kept at random) age out
# within a week instead of a full TTL.
LEGACY_SEEN_GRACE_DAYS = 7
DISCORD_EMBEDS_PER_MESSAGE = 10  # Discord's hard limit per message
DISCORD_MAX_ATTEMPTS = 4
# Discord webhooks allow roughly 5 requests per 2 seconds.
DISCORD_SECONDS_BETWEEN_POSTS = 0.5
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.5

# Max request *starts* per second across a whole lane (0 = no limit). The
# workflow sets this for Greenhouse, whose firewall 403s an entire pass
# when ~6,000 boards are hit at full concurrency.
POLL_RPS = float(os.environ.get("POLL_RPS", "0") or 0)
# Statuses that can mean "you're being rate limited" rather than "this
# board is broken". A lane-wide block pauses the whole lane for a cooldown
# (doubling up to the max) instead of hammering through the rest of the
# company list. A 429 always counts; a 403 only once BLOCK_THRESHOLD of
# them arrive in a row with no success in between, so one board that's
# genuinely forbidden doesn't stall the lane every pass.
DEFAULT_RATE_LIMIT_STATUSES = {429}
RATE_LIMIT_STATUSES = {"greenhouse": {403, 429}}
BLOCK_THRESHOLD = 5
BLOCK_COOLDOWN_SECONDS = 30
MAX_BLOCK_COOLDOWN_SECONDS = 300

ATS_TYPES = ("workday", "greenhouse", "lever", "ashby", "smartrecruiters", "github")
DIRECT_ATS_TYPES = tuple(t for t in ATS_TYPES if t != "github")

# Human-readable origin for Discord / dry-run. Direct ATS polls are tagged
# "(direct)" so they aren't confused with the same company showing up via a
# GitHub listing feed.
ATS_SOURCE_LABELS = {
    "workday": "Workday",
    "greenhouse": "Greenhouse (direct)",
    "lever": "Lever (direct)",
    "ashby": "Ashby (direct)",
    "smartrecruiters": "SmartRecruiters (direct)",
}

# --- Community GitHub listing sources ---------------------------------------
# Repos like SimplifyJobs' Summer-Internships list aggregate postings across
# thousands of companies (including many with no ATS at all, or a career
# page too custom to poll directly), maintained by a community + their own
# scraper. `--ats=github` polls these directly from a raw.githubusercontent
# JSON file, same dedup-by-id / filter / notify pipeline as everything else.
# Add more sources here (e.g. a personal test repo) as {owner, repo, ref,
# path, label} -- `path` should point at a JSON array of objects shaped like
# Simplify's `listings.json` (id, title, company_name, url, category,
# active, is_visible, date_posted, locations).
GITHUB_LISTING_SOURCES = [
    {
        "label": "SimplifyJobs/Summer2027-Internships",
        "owner": "SimplifyJobs",
        "repo": "Summer2027-Internships",
        "ref": "dev",
        "path": ".github/scripts/listings.json",
    },
]
# Category labels in listings.json that count as "software" for
# SOFTWARE_ROLES_ONLY -- the repo's taxonomy drifted over time ("Software"
# is current, "Software Engineering" is an older/inconsistent label still
# used by some entries), so both are accepted. Entries in any other
# category still count if their title passes SOFTWARE_ROLE_RE: Simplify
# files plenty of plain "Software Engineer Intern" roles under Hardware or
# AI/ML/Data.
GITHUB_SOFTWARE_CATEGORIES = {"software", "software engineering"}
# Skip a listing-feed posting when it links to a board we already poll
# directly and our own title filters would have notified it from there --
# otherwise the same job pings twice (once per lane). Postings our title
# regex would reject still come through, since that's what the curated
# feed is for. Set GITHUB_SKIP_DIRECT_COVERED=false to disable.
GITHUB_SKIP_DIRECT_COVERED = os.environ.get("GITHUB_SKIP_DIRECT_COVERED", "true").strip().lower() != "false"

# --- Filtering -------------------------------------------------------------
# Only notify about internships/co-ops, in software/full-stack-ish roles,
# in the US, and only ones that still look freshly posted. Set
# INTERNSHIPS_ONLY=false, SOFTWARE_ROLES_ONLY=false, or US_ONLY=false in
# the environment to loosen a filter.
INTERNSHIPS_ONLY = os.environ.get("INTERNSHIPS_ONLY", "true").strip().lower() != "false"
SOFTWARE_ROLES_ONLY = os.environ.get("SOFTWARE_ROLES_ONLY", "true").strip().lower() != "false"
# Drop postings whose location is clearly outside the US (e.g. Sydney,
# Australia). Remote / unspecified locations still pass — most ATS boards
# in this list are US companies that omit a country. Set US_ONLY=false to
# disable.
US_ONLY = os.environ.get("US_ONLY", "true").strip().lower() != "false"

# Workday only returns 20 postings per request and SmartRecruiters' first
# page is 100, so at big employers an internship can sit outside that
# window. When INTERNSHIPS_ONLY is on, each of those companies also gets a
# keyword-searched request whose results are merged with the newest page.
# Set the env var to an empty string to skip the extra request.
WORKDAY_SEARCH_TEXT = os.environ.get("WORKDAY_SEARCH_TEXT", "intern" if INTERNSHIPS_ONLY else "").strip()
SMARTRECRUITERS_QUERY = os.environ.get("SMARTRECRUITERS_QUERY", "intern" if INTERNSHIPS_ONLY else "").strip()

# Word-boundary matched so "International", "cooperation", etc. don't
# false-positive. Co-ops are explicitly included here (matches "co-op",
# "co-ops", "coop", "coops"), as are apprenticeships, student roles, and
# "Summer 2027"-style titles that never say "intern".
INTERNSHIP_RE = re.compile(
    r"\b("
    r"intern|interns|internship|internships|co-?ops?|"
    r"apprentice(?:ship)?s?|student|undergrad(?:uate)?|"
    r"summer\s+20\d\d"
    r")\b",
    re.IGNORECASE,
)
# Roles that mention interns without being one ("Intern Program Manager",
# "University Recruiter - Interns", "Senior Engineer, Intern Mentor").
NOT_INTERN_ROLE_RE = re.compile(
    r"\b(senior|sr|staff|principal|manager|director|head\s+of|recruit(?:er|ing)|coordinator|mentor)\b",
    re.IGNORECASE,
)

# Title must also look software/full-stack-related, so e.g. "Marketing
# Intern" or "Sales Development Co-op" don't slip through just because they
# matched INTERNSHIP_RE. Deliberately an allow-list of software-engineering
# terms rather than a bare "engineer" match, since that would also catch
# mechanical/electrical/civil engineering internships. Measured against
# Simplify's hand-labeled listings, a bare "Engineering Intern" title was
# about as likely to be hardware as software, so it isn't matched; neither
# are "systems engineer" / "test engineer" on their own (mostly hardware
# roles) -- "Software Systems/Test Engineer" still match via "software".
SOFTWARE_ROLE_RE = re.compile(
    r"\b("
    r"software|swe|sde|"
    r"full[\s-]?stack|front[\s-]?end|back[\s-]?end|"
    r"web\s?dev(?:eloper|elopment)?s?|application\s+develop\w*|"
    r"(?<!business\s)(?<!estate\s)developers?|"
    r"devops|dev\s?ops|site\s+reliability|sre|"
    r"mobile|ios|android|"
    r"(?:machine\s+learning|ml|ai|ai/ml|ml/ai|llm|gen\s?ai)\s+(?:engineer\w*|developer|software)|"
    r"data\s+engineer\w*|platform\s+engineer\w*|infrastructure\s+engineer\w*|cloud\s+engineer\w*|"
    r"security\s+engineer\w*|qa\s+engineer\w*|"
    r"computer\s+science|programmer|programming|"
    r"forward[\s-]deployed|compilers?|kernel|distributed\s+systems|"
    r"(?<!information\s)tech(?:nology)?\s+(?:intern\w*|co-?op|analyst|summer)"
    r")\b",
    re.IGNORECASE,
)

# Country / region names that mean the posting is not US-only. Full names
# (not 2-letter codes) so "CA" stays California and "IN" stays Indiana.
_NON_US_LOCATION_RE = re.compile(
    r"\b("
    r"australia|canada|united kingdom|\bu\.?k\.?\b|england|scotland|wales|"
    r"northern ireland|ireland|germany|france|india|singapore|japan|china|"
    r"netherlands|sweden|switzerland|spain|italy|brazil|(?<!new\s)mexico|"
    r"south korea|korea|south africa|new zealand|hong kong|taiwan|poland|"
    r"israel|denmark|norway|finland|belgium|austria|portugal|philippines|"
    r"indonesia|malaysia|thailand|vietnam|colombia|argentina|chile|"
    r"czechia|czech republic|romania|hungary|greece|turkey|egypt|"
    r"nigeria|kenya|pakistan|bangladesh|saudi arabia|qatar|uae|"
    r"united arab emirates|luxembourg|estonia|lithuania|latvia|"
    r"new south wales|queensland|victoria(?!\s*,?\s*(?:tx|texas))|"
    r"ontario|british columbia|quebec|alberta|manitoba|saskatchewan|"
    r"nova scotia|nsw\b|europe|emea|apac|latam|"
    r"worldwide|global(?!\s*entry)|international|"
    r"sydney|melbourne|brisbane|perth|adelaide|auckland|"
    r"toronto|montreal|montréal|ottawa|calgary|edmonton|"
    r"bangalore|bengaluru|hyderabad|mumbai|new delhi|pune|chennai|noida|gurgaon|gurugram|kolkata|"
    r"(?<!new\s)london(?!\s*,\s*(?:ky|oh|kentucky|ohio)\b)|paris|edinburgh|belfast|"
    r"berlin|munich|hamburg|frankfurt|amsterdam|zurich|geneva|stockholm|copenhagen|oslo|helsinki|"
    r"warsaw|krakow|kraków|prague|bucharest|budapest|brussels|madrid|barcelona|lisbon|milan|"
    r"istanbul|tel aviv|dubai|abu dhabi|riyadh|doha|cairo|lagos|nairobi|cape town|johannesburg|"
    r"karachi|lahore|dhaka|tokyo|osaka|seoul|beijing|shanghai|shenzhen|hangzhou|taipei|"
    r"manila|jakarta|kuala lumpur|ho chi minh|hanoi|"
    r"são paulo|sao paulo|buenos aires|bogota|bogotá"
    r")\b",
    re.IGNORECASE,
)
# "Toronto, ON" / "Vancouver, BC" style province suffixes. Case-sensitive
# and separate from the list above: none of these codes is a US state, but
# lowercase "on" shows up in plenty of US strings ("Remote, on-site").
_CANADA_PROVINCE_RE = re.compile(r",\s*(?:ON|BC|QC|AB|MB|SK|NS|NB|NL|PE)\b(?!-)")
# Separators between the sites in a multi-location string like
# "New York, NY; London, UK" or "Remote - US | Toronto". " or " is
# lowercase-only so "Portland, OR" isn't split.
_MULTI_LOCATION_SPLIT_RE = re.compile(r"\s*(?:;|\||\n|\s+or\s+)\s*")
_US_COUNTRY_VALUES = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}

# The diff against state/seen_*.json already means "new" = "appeared since
# our last poll" (~10 min), which is far tighter than "a few hours." This
# is just a safety net for the one edge case that isn't covered by diffing:
# if a run is ever skipped/delayed (Actions outage, repo paused, etc.), the
# next run would otherwise treat a multi-hour or multi-day backlog as "new."
_STALE_POSTED_MARKERS = ("yesterday", "day ago", "days ago", "week", "month", "30+")


def _is_us_location(location):
    """True unless the location string names a non-US country, region, or
    city. Empty / Remote / "San Francisco, CA" all pass; "Sydney, New
    South Wales, Australia" does not. A multi-site string passes if any
    one of its sites does ("New York, NY; London, UK" is still a US role).
    """
    if not location or not str(location).strip():
        return True
    sites = [s for s in _MULTI_LOCATION_SPLIT_RE.split(str(location)) if s.strip()]
    return any(
        _NON_US_LOCATION_RE.search(site) is None and _CANADA_PROVINCE_RE.search(site) is None
        for site in sites
    ) if sites else True


def _job_is_us(job):
    """True if any of the job's locations is (or might be) in the US.
    Each location is a (text, country) pair: when the ATS gives a
    structured country (SmartRecruiters, Lever, Ashby) that decides it,
    otherwise the text goes through _is_us_location()."""
    locations = job.get("locations") or [(job.get("location", ""), None)]
    for text, country in locations:
        if country:
            if str(country).strip().lower() in _US_COUNTRY_VALUES:
                return True
        elif _is_us_location(text):
            return True
    return False


def _is_software_title(title):
    return bool(SOFTWARE_ROLE_RE.search(title))


def _title_passes_filters(title):
    """The title-based half of the filters for direct ATS postings."""
    if INTERNSHIPS_ONLY and (not INTERNSHIP_RE.search(title) or NOT_INTERN_ROLE_RE.search(title)):
        return False
    return not SOFTWARE_ROLES_ONLY or _is_software_title(title)


def _job_passes_filters(job):
    return (
        (job.get("skip_title_filters") or _title_passes_filters(job["title"]))
        and _looks_recent(job.get("posted", ""))
        and (not US_ONLY or _job_is_us(job))
    )


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


def _parse_epoch_seconds(value):
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _humanize_delta(posted_at):
    """Fine-grained "N minutes/hours/days ago" for Discord display -- only
    used when we have a real timestamp (not Workday's day-level text), so
    it never overstates precision we don't actually have."""
    if posted_at is None:
        return None
    now = datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    seconds = max((now - posted_at).total_seconds(), 0)
    minutes = seconds / 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        n = int(minutes)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    hours = minutes / 60
    if hours < 24:
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    days = hours / 24
    n = int(days)
    return f"{n} day{'s' if n != 1 else ''} ago"


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


def _today():
    return int(time.time() // 86400)


def _seen_key(job_id):
    """Short stable hash of a job id. Workday ids are long URL paths, and
    the state file is pushed after every pass, so this keeps it small."""
    return hashlib.sha1(str(job_id).encode("utf-8")).hexdigest()[:16]


def load_state(state_file):
    """Load {company: {seen_key: last_seen_day}} and whether it had to be
    migrated. Older state files stored a plain list of raw job ids per
    company; those are converted in place (see LEGACY_SEEN_GRACE_DAYS)."""
    if not state_file.exists():
        return {}, False
    raw = json.loads(state_file.read_text())
    legacy_day = _today() - SEEN_TTL_DAYS + LEGACY_SEEN_GRACE_DAYS
    state = {}
    migrated = False
    for company, seen in raw.items():
        if isinstance(seen, list):
            state[company] = {_seen_key(job_id): legacy_day for job_id in seen}
            migrated = True
        else:
            state[company] = dict(seen)
    return state, migrated


def save_state(state_file, state):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")))


def _prune_seen(seen, today):
    """Drop ids that haven't shown up in SEEN_TTL_DAYS, then cap what's
    left. Ids seen today (i.e. in the current fetch) are never dropped."""
    cutoff = today - SEEN_TTL_DAYS
    kept = {key: day for key, day in seen.items() if day >= cutoff}
    if len(kept) > MAX_SEEN_PER_COMPANY:
        newest = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)
        kept = dict(kv for i, kv in enumerate(newest) if i < MAX_SEEN_PER_COMPANY or kv[1] == today)
    return kept


class RequestPacer:
    """Lane-wide request pacing. Spaces request starts to at most `rps`
    per second (no spacing when rps is 0), and after a rate-limit response
    pauses *every* request in the lane for a cooldown that doubles on
    repeated blocks -- so a blocked lane backs off instead of firing the
    rest of its few thousand requests into the block."""

    def __init__(self, rps):
        self.interval = 1 / rps if rps > 0 else 0.0
        self.next_slot = 0.0
        self.blocked_until = 0.0
        self.cooldown = BLOCK_COOLDOWN_SECONDS
        self.consecutive_blocks = 0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            start = max(time.monotonic(), self.next_slot, self.blocked_until)
            self.next_slot = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def record_block(self, definite):
        """Note a rate-limit-ish response. Returns True if the lane is now
        (or already was) cooling down, i.e. the request is worth retrying
        after the next wait()."""
        self.consecutive_blocks += 1
        now = time.monotonic()
        if now < self.blocked_until:
            return True
        if not definite and self.consecutive_blocks < BLOCK_THRESHOLD:
            return False
        # Many in-flight requests hit the same block at once; only the
        # first one past the check above starts (and escalates) it.
        self.blocked_until = now + self.cooldown
        print(f"Rate limited; pausing lane for {self.cooldown}s.")
        self.cooldown = min(self.cooldown * 2, MAX_BLOCK_COOLDOWN_SECONDS)
        return True

    def record_success(self):
        self.consecutive_blocks = 0
        if time.monotonic() >= self.blocked_until:
            self.cooldown = BLOCK_COOLDOWN_SECONDS


def _request_specs(company):
    """(method, url, params, json_body) for each request one company
    needs. Most ATSes need one; Workday and SmartRecruiters get a second,
    keyword-searched request when one is configured (see
    WORKDAY_SEARCH_TEXT)."""
    ats = company["ats"]
    if ats == "workday":
        body = {"appliedFacets": {}, "limit": RESULTS_PER_COMPANY, "offset": 0, "searchText": ""}
        specs = [("POST", company["endpoint"], None, body)]
        if WORKDAY_SEARCH_TEXT:
            specs.append(("POST", company["endpoint"], None, {**body, "searchText": WORKDAY_SEARCH_TEXT}))
        return specs
    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{company['slug']}/jobs"
        return [("GET", url, {"content": "false"}, None)]
    if ats == "lever":
        return [("GET", f"https://api.lever.co/v0/postings/{company['slug']}", {"mode": "json"}, None)]
    if ats == "ashby":
        return [("GET", f"https://api.ashbyhq.com/posting-api/job-board/{company['slug']}", None, None)]
    if ats == "smartrecruiters":
        url = f"https://api.smartrecruiters.com/v1/companies/{company['slug']}/postings"
        params = {"limit": SMARTRECRUITERS_PAGE_LIMIT, "offset": 0}
        specs = [("GET", url, params, None)]
        if SMARTRECRUITERS_QUERY:
            specs.append(("GET", url, {**params, "q": SMARTRECRUITERS_QUERY}, None))
        return specs
    raise ValueError(f"unknown ats: {ats}")  # pragma: no cover - guarded by load_companies()


async def _fetch_json(client, pacer, ats, method, url, params, body):
    """One request with retries. Returns (data, None) or (None, error)."""
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        await pacer.wait()
        try:
            resp = await client.request(method, url, params=params, json=body, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            pacer.record_success()
            return data, None
        except Exception as e:  # noqa: BLE001 - one bad company shouldn't kill the run
            status = getattr(getattr(e, "response", None), "status_code", None)
            last_err = f"HTTP {status}" if status is not None else f"{type(e).__name__}: {e}"
            if status in RATE_LIMIT_STATUSES.get(ats, DEFAULT_RATE_LIMIT_STATUSES):
                if pacer.record_block(definite=status == 429):
                    continue  # the next pacer.wait() sleeps out the cooldown
            # Retry transient failures (timeouts, 5xx); don't bother
            # retrying a hard 4xx like 404/400, it won't change.
            if status is not None and 400 <= status < 500:
                break
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return None, last_err


async def fetch_company(client, sem, pacer, company):
    """Fetch the most recent postings for one company. Never raises."""
    pages = []
    async with sem:
        for method, url, params, body in _request_specs(company):
            data, err = await _fetch_json(client, pacer, company["ats"], method, url, params, body)
            if err is not None:
                return company["name"], None, err
            pages.append(data)

    results = []
    ids = set()
    try:
        for data in pages:
            for job in _parse_postings(company, data):
                if job["id"] not in ids:
                    ids.add(job["id"])
                    results.append(job)
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
            # Workday only ever gives us day-level text (no exact
            # timestamp in the list response) -- keep it as posted_display
            # verbatim rather than inventing false precision.
            posted_text = p.get("postedOn", "")
            location = p.get("locationsText", "")
            results.append(
                {
                    "id": path,
                    "title": p.get("title", "Untitled role"),
                    "location": location,
                    # Multi-site postings only say "2 Locations" here, which
                    # can't be checked and so passes US_ONLY.
                    "locations": [(location, None)],
                    "posted": posted_text,
                    "posted_at": None,
                    "posted_display": posted_text or "Unknown",
                    "url": f"https://{company['host']}/{company['site']}{path}",
                    "source": ATS_SOURCE_LABELS[ats],
                }
            )

    elif ats == "greenhouse":
        for j in data.get("jobs", []) or []:
            job_id = j.get("id")
            url = j.get("absolute_url")
            if job_id is None or not url:
                continue
            posted_at = _parse_iso(j.get("first_published")) or _parse_iso(j.get("updated_at"))
            location = (j.get("location") or {}).get("name", "") or ""
            results.append(
                {
                    "id": str(job_id),
                    "title": j.get("title", "Untitled role"),
                    "location": location,
                    "locations": [(location, None)],
                    "posted": _relative_posted_text(posted_at),
                    "posted_at": posted_at,
                    "posted_display": _humanize_delta(posted_at) or "Unknown",
                    "url": url,
                    "source": ATS_SOURCE_LABELS[ats],
                }
            )

    elif ats == "lever":
        for j in data or []:
            job_id = j.get("id")
            url = j.get("hostedUrl")
            if not job_id or not url:
                continue
            posted_at = _parse_epoch_ms(j.get("createdAt"))
            categories = j.get("categories") or {}
            location = categories.get("location", "") or ""
            # Lever's `country` is the primary location's ISO code; any
            # other sites are only listed as text in allLocations.
            locations = [(location, j.get("country"))]
            for other in categories.get("allLocations") or []:
                if isinstance(other, str) and other != location:
                    locations.append((other, None))
            results.append(
                {
                    "id": str(job_id),
                    "title": j.get("text", "Untitled role"),
                    "location": location,
                    "locations": locations,
                    "posted": _relative_posted_text(posted_at),
                    "posted_at": posted_at,
                    "posted_display": _humanize_delta(posted_at) or "Unknown",
                    "url": url,
                    "source": ATS_SOURCE_LABELS[ats],
                }
            )

    elif ats == "ashby":
        for j in data.get("jobs", []) or []:
            job_id = j.get("id")
            url = j.get("jobUrl") or j.get("applyUrl")
            if not job_id or not url:
                continue
            posted_at = _parse_iso(j.get("publishedAt"))
            location = j.get("location", "") or ""
            locations = [(location, _ashby_country(j))]
            for other in j.get("secondaryLocations") or []:
                if isinstance(other, dict):
                    locations.append((other.get("location", "") or "", _ashby_country(other)))
            results.append(
                {
                    "id": str(job_id),
                    "title": j.get("title", "Untitled role"),
                    "location": location,
                    "locations": locations,
                    "posted": _relative_posted_text(posted_at),
                    "posted_at": posted_at,
                    "posted_display": _humanize_delta(posted_at) or "Unknown",
                    "url": url,
                    "source": ATS_SOURCE_LABELS[ats],
                }
            )

    elif ats == "smartrecruiters":
        for j in data.get("content", []) or []:
            job_id = j.get("id")
            if not job_id:
                continue
            posted_at = _parse_iso(j.get("releasedDate"))
            sr_location = j.get("location") if isinstance(j.get("location"), dict) else {}
            location = _format_smartrecruiters_location(sr_location)
            results.append(
                {
                    "id": str(job_id),
                    "title": j.get("name", "Untitled role"),
                    "location": location,
                    # `country` is a lowercase ISO code ("us", "in"), which
                    # the country-name regex would never catch.
                    "locations": [(location, sr_location.get("country"))],
                    "posted": _relative_posted_text(posted_at),
                    "posted_at": posted_at,
                    "posted_display": _humanize_delta(posted_at) or "Unknown",
                    "url": f"https://jobs.smartrecruiters.com/{company['slug']}/{job_id}",
                    "source": ATS_SOURCE_LABELS[ats],
                }
            )

    return results


def _ashby_country(entry):
    address = entry.get("address") or {}
    return (address.get("postalAddress") or {}).get("addressCountry") or None


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
    pacer = RequestPacer(POLL_RPS)
    headers = {"User-Agent": "job-alert-bot/1.0 (personal use, low-frequency polling)"}
    async with httpx.AsyncClient(headers=headers) as client:
        tasks = [fetch_company(client, sem, pacer, c) for c in companies]
        return await asyncio.gather(*tasks)


def direct_board_keys(companies):
    """Keys (see _direct_board_key) for every board we poll directly."""
    keys = set()
    for c in companies:
        if c["ats"] == "workday":
            keys.add(("workday", c["host"].lower(), c["site"].lower()))
        else:
            keys.add((c["ats"], c["slug"].lower()))
    return keys


def _direct_board_key(url):
    """Map a posting URL to the board it lives on, in the same shape as
    direct_board_keys(), or None for anything we don't poll directly
    (custom career sites, EU-hosted Greenhouse/Lever boards, etc.)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        slug = parse_qs(parsed.query).get("for", [None])[0]
        if not slug and parts and parts[0] != "embed":
            slug = parts[0]
        return ("greenhouse", slug.lower()) if slug else None
    if not parts:
        return None
    if host == "jobs.lever.co":
        return ("lever", parts[0].lower())
    if host == "jobs.ashbyhq.com":
        return ("ashby", parts[0].lower())
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        return ("smartrecruiters", parts[0].lower())
    if host.endswith(".myworkdayjobs.com"):
        # Workday URLs sometimes carry a locale before the site: /en-US/<site>/job/...
        site = parts[1] if re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", parts[0]) and len(parts) > 1 else parts[0]
        return ("workday", host, site.lower())
    return None


async def fetch_github_listing_source(client, source, covered_boards=frozenset()):
    """Fetch one GitHub-hosted listings.json feed. Never raises -- returns
    ({company_name: [job, ...]}, None) on success or ({}, error) on
    failure, same never-crash contract as fetch_company()."""
    url = f"https://raw.githubusercontent.com/{source['owner']}/{source['repo']}/{source['ref']}/{source['path']}"
    try:
        # These feeds run to several MB (thousands of postings across
        # terms/categories we don't care about) -- give it real headroom.
        resp = await client.get(url, timeout=REQUEST_TIMEOUT * 4)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 - one bad source shouldn't kill the run
        return {}, f"{type(e).__name__}: {e}"

    by_company = {}
    for item in data or []:
        if not item.get("active") or not item.get("is_visible", True):
            continue
        job_id = item.get("id")
        job_url = item.get("url")
        company = item.get("company_name")
        title = item.get("title")
        if not (job_id and job_url and company and title):
            continue
        category = (item.get("category") or "").strip().lower()
        if SOFTWARE_ROLES_ONLY and category not in GITHUB_SOFTWARE_CATEGORIES and not _is_software_title(title):
            continue
        posted_at = _parse_epoch_seconds(item.get("date_posted"))
        site_list = [loc for loc in item.get("locations") or [] if isinstance(loc, str)]
        job = {
            "id": str(job_id),
            "title": title,
            "location": ", ".join(site_list),
            "locations": [(loc, None) for loc in site_list],
            # Deliberately left blank (not run through _relative_posted_text):
            # the staleness safety-net below exists to catch a *delayed
            # poll* of a source whose own postings are always fresh at
            # fetch time. That assumption doesn't hold here -- this feed's
            # community-sourced date_posted can legitimately be days old
            # for a company Simplify only just discovered/added, and the
            # whole point of including this source is to catch postings our
            # own direct ATS polling missed. An empty string skips the
            # staleness check (_looks_recent treats "no signal" as pass);
            # the id-based new/seen diff is still what gates notifications.
            "posted": "",
            "posted_at": posted_at,
            "posted_display": _humanize_delta(posted_at) or "Unknown",
            "url": job_url,
            "source": source["label"],
            # Every entry in this feed is already an internship/co-op by
            # definition (that's the whole repo), and we already gated on
            # `category` above for SOFTWARE_ROLES_ONLY -- re-running
            # INTERNSHIP_RE/SOFTWARE_ROLE_RE against the title would only
            # produce false negatives (e.g. "Software Engineer, Summer
            # 2027" has no literal "intern" in it) with no benefit.
            "skip_title_filters": True,
            "direct_covered": _direct_board_key(job_url) in covered_boards,
        }
        by_company.setdefault(company, []).append(job)
    return by_company, None


async def poll_github_listing_sources(covered_boards=frozenset()):
    """Fetch every configured GitHub listing source and flatten the result
    into the same (name, jobs, err) tuple shape fetch_company() produces,
    one tuple per company found in the feed -- so main()'s existing
    dedup/filter/notify loop needs no special-casing for this source."""
    if not GITHUB_LISTING_SOURCES:
        return []
    headers = {"User-Agent": "job-alert-bot/1.0 (personal use, low-frequency polling)"}
    results = []
    async with httpx.AsyncClient(headers=headers) as client:
        for source in GITHUB_LISTING_SOURCES:
            by_company, err = await fetch_github_listing_source(client, source, covered_boards)
            if err is not None:
                results.append((source["label"], None, err))
                continue
            for company, jobs in by_company.items():
                results.append((company, jobs, None))
    return results


def notify_discord(new_by_company):
    """Post new jobs to Discord. Returns the (company_name, job) pairs that
    couldn't be delivered, so the caller can leave them unseen and have the
    next pass try again instead of losing them."""
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set — skipping Discord notification.")
        return []
    items = []
    for company_name, jobs in new_by_company.items():
        for job in jobs:
            embed = {
                "title": f"{job['title']} — {company_name}"[:256],
                "description": job.get("location", "") or "​",
                "url": job["url"],
                "fields": [
                    {"name": "Posted", "value": job.get("posted_display") or "Unknown", "inline": True},
                    {"name": "Source", "value": job.get("source") or "Unknown", "inline": True},
                ],
            }
            # Only set Discord's native embed timestamp when we have a real
            # exact posted-at (Workday only gives day-level text) -- an
            # approximated timestamp would render as misleadingly precise.
            posted_at = job.get("posted_at")
            if posted_at is not None:
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
                embed["timestamp"] = posted_at.isoformat()
            items.append((company_name, job, embed))
    undelivered = []
    for i in range(0, len(items), DISCORD_EMBEDS_PER_MESSAGE):
        chunk = items[i : i + DISCORD_EMBEDS_PER_MESSAGE]
        if i:
            time.sleep(DISCORD_SECONDS_BETWEEN_POSTS)
        if not _post_discord_embeds([embed for _, _, embed in chunk]):
            undelivered.extend((company_name, job) for company_name, job, _ in chunk)
    return undelivered


def _post_discord_embeds(embeds):
    """Send one webhook message, honoring 429 retry_after and retrying
    network errors / 5xx. Returns False only when the message should be
    retried on a later pass; a non-429 4xx (a malformed embed) will never
    succeed, so it's logged and treated as done."""
    for attempt in range(DISCORD_MAX_ATTEMPTS):
        try:
            resp = httpx.post(DISCORD_WEBHOOK_URL, json={"embeds": embeds}, timeout=15)
        except httpx.HTTPError as e:
            print(f"Discord post error ({type(e).__name__}: {e}); attempt {attempt + 1}/{DISCORD_MAX_ATTEMPTS}")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 1))
            except (ValueError, AttributeError):
                retry_after = 1.0
            print(f"Discord rate limited; retrying in {retry_after:.1f}s")
            time.sleep(min(retry_after, 60) + 0.25)
            continue
        if resp.status_code >= 500:
            print(f"Discord post failed ({resp.status_code}); attempt {attempt + 1}/{DISCORD_MAX_ATTEMPTS}")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code >= 300:
            print(f"Discord rejected message ({resp.status_code}), not retrying: {resp.text[:200]}")
        return True
    return False


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
    poll_github = not ats_filter or "github" in ats_filter
    github_note = f" + {len(GITHUB_LISTING_SOURCES)} GitHub listing source(s)" if poll_github else ""
    print(f"Polling {len(companies)} companies ({ats_label}){github_note}...")

    state_file = state_path_for(ats_filter)
    state, migrated = load_state(state_file)
    # A migrated state file was written under the old filters/requests
    # (300-id cap, no intern search, narrower listing-feed categories), so
    # its first pass would surface a backlog of "new" ids at once -- treat
    # it like a first run and only re-seed.
    first_run = len(state) == 0 or migrated

    results = asyncio.run(poll_all(companies))
    if poll_github:
        covered = frozenset()
        if GITHUB_SKIP_DIRECT_COVERED:
            covered = frozenset(direct_board_keys(load_companies(ats_filter=set(DIRECT_ATS_TYPES))))
        results = results + asyncio.run(poll_github_listing_sources(covered))

    today = _today()
    new_by_company = {}
    errors = []
    total_new_all = 0
    would_match_first_run = 0
    skipped_as_direct = 0
    for name, jobs, err in results:
        if err is not None:
            errors.append((name, err))
            continue
        # setdefault + in-place update: a listing feed can yield the same
        # company name more than once per pass.
        seen = state.setdefault(name, {})
        new_jobs = [j for j in jobs if _seen_key(j["id"]) not in seen]
        for j in jobs:
            seen[_seen_key(j["id"])] = today
        state[name] = _prune_seen(seen, today)
        total_new_all += len(new_jobs)

        qualifying = []
        for j in new_jobs:
            if not _job_passes_filters(j):
                continue
            if j.get("direct_covered") and _title_passes_filters(j["title"]):
                # The direct lane for this board notifies it (or already did).
                skipped_as_direct += 1
                continue
            qualifying.append(j)
        if not qualifying:
            continue
        if first_run:
            would_match_first_run += len(qualifying)
        else:
            new_by_company.setdefault(name, []).extend(qualifying)

    print(f"{len(errors)} companies failed to fetch.")
    if errors:
        by_reason = {}
        for _, err in errors:
            reason = err if err.startswith("HTTP ") else err.split(":", 1)[0]
            by_reason[reason] = by_reason.get(reason, 0) + 1
        print("  by reason: " + ", ".join(f"{r}: {n}" for r, n in sorted(by_reason.items(), key=lambda kv: -kv[1])))
    for name, err in errors[:10]:
        print(f"  FAILED: {name}: {err}")
    if len(errors) > 10:
        print(f"  ...and {len(errors) - 10} more")
    if skipped_as_direct:
        print(f"{skipped_as_direct} listing-feed postings skipped (already covered by a direct ATS lane).")

    total_new = sum(len(v) for v in new_by_company.values())
    if first_run:
        print(
            f"{total_new_all} existing postings found while seeding "
            f"({would_match_first_run} would have matched filters -- "
            f"internships_only={INTERNSHIPS_ONLY}, software_roles_only={SOFTWARE_ROLES_ONLY}, "
            f"us_only={US_ONLY})."
        )
    else:
        print(
            f"{total_new_all} new postings found; {total_new} matched filters "
            f"(internships_only={INTERNSHIPS_ONLY}, software_roles_only={SOFTWARE_ROLES_ONLY}, "
            f"us_only={US_ONLY}) "
            f"across {len(new_by_company)} companies."
        )

    if first_run:
        if migrated:
            print("Migrated state from the old format — re-seeding this pass, no notifications sent.")
        else:
            print("First run — seeding state only, no notifications sent.")
    elif dry_run:
        print("Dry run — not posting to Discord. New postings found:")
        for name, jobs in new_by_company.items():
            for j in jobs:
                print(
                    f"  NEW: {name} — {j['title']} [{j.get('posted_display', 'Unknown')}] "
                    f"via {j.get('source', 'Unknown')} ({j['url']})"
                )
    elif total_new:
        undelivered = notify_discord(new_by_company)
        if undelivered:
            print(f"{len(undelivered)} postings couldn't be delivered to Discord; will retry next pass.")
        for name, job in undelivered:
            state.get(name, {}).pop(_seen_key(job["id"]), None)

    save_state(state_file, state)
    print(f"State saved to {state_file}.")


if __name__ == "__main__":
    main()
