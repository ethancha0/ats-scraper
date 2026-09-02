# Job Alerts

Polls ~18,000 companies' career sites directly across five ATS platforms
(Workday, Greenhouse, Lever, Ashby, SmartRecruiters), plus community-curated
GitHub listing repos (SimplifyJobs' internship list) as a cross-check —
skipping LinkedIn entirely — and pings a Discord channel the moment a new
posting appears.

## How it works

- `data/companies.csv` — your list of companies (`name,ats,slug,url`).
  `ats` is one of `workday` / `greenhouse` / `lever` / `ashby` /
  `smartrecruiters`. `slug` means something different per ATS (see
  [Endpoint format](#endpoint-format) below) — for Workday it's
  `tenant/site`, which maps onto Workday's CXS jobs API:
  `https://{host}/wday/cxs/{tenant}/{site}/jobs`.
- `GITHUB_LISTING_SOURCES` in `poll.py` — a small hardcoded list of
  community-maintained GitHub repos that publish a `listings.json` feed
  (see [GitHub listing sources](#github-listing-sources) below). Unlike
  `companies.csv`, these aren't per-company rows — one feed covers thousands
  of companies at once, including many with no pollable ATS at all.
- `poll.py` — hits every company's endpoint concurrently plus every
  configured GitHub listing source, diffs the returned postings against
  `state/seen_<ats>.json`, and posts anything new to Discord. Pass
  `--ats=workday,lever` to only poll a subset, or `--ats=github` to poll
  only the listing sources.
- `.github/workflows/poll.yml` — runs `poll.py` on a cron schedule, once per
  source in parallel (a matrix job: 5 ATS lanes + 1 GitHub-listings lane),
  and commits each one's updated state file back to the repo (this is what
  makes state persist between runs on GitHub's ephemeral runners).

## Filtering

Two filters apply, both on by default:

- **`INTERNSHIPS_ONLY=true`** — only postings with "intern," "internship,"
  or "co-op" in the title (word-boundary matched, so "International" or
  "Cooperative" won't false-positive; "co-op"/"coop"/"co-ops"/"coops" all
  match).
- **`SOFTWARE_ROLES_ONLY=true`** — the title must *also* look
  software/full-stack-related: "software," "full-stack," "front-end,"
  "back-end," "web developer," "DevOps," "SRE," "mobile/iOS/Android
  engineer," "data engineer," "ML/AI engineer," "computer science," etc.
  This is an allow-list (not a bare "engineer" match — that would also
  catch mechanical/electrical/civil engineering internships), so it filters
  out things like "Marketing Intern" or "Sales Co-op" that only matched on
  the internship keyword.

Set either to `false` in the workflow's `env:` block to loosen it — e.g.
`SOFTWARE_ROLES_ONLY=false` to see all internships/co-ops regardless of
department, or `INTERNSHIPS_ONLY=false` to see every software role
including full-time.

GitHub listing sources ([below](#github-listing-sources)) skip both title
regexes — every entry there is already an internship/co-op by definition of
the feed, and already has its own `category` field, so re-running our title
regex against it would only produce false negatives (e.g. a title like
"Software Engineer, Summer 2027" has no literal "intern" in it).
`SOFTWARE_ROLES_ONLY` still applies to that source, just via `category`
instead of a title match.

Recency comes from two layers:
- **Primary**: a posting only counts as "new" if its ID wasn't in that ATS
  type's `state/seen_<ats>.json` from the *previous* run — with a 10-minute
  cron, that means "new" already means "posted in roughly the last 10
  minutes," well inside "a few hours."
- **Safety net**: each ATS's own posted-at signal (Workday's coarse
  "Posted Today" / "Posted Yesterday" text; exact timestamps from
  Greenhouse/Lever/Ashby/SmartRecruiters, normalized to the same "today" /
  "yesterday" / "N days ago" buckets) is checked as a backstop, so that if a
  run is ever skipped or delayed and the next run catches up on a backlog,
  postings already a day+ old get skipped rather than flooding your Discord
  all at once. This backstop is deliberately **not** applied to GitHub
  listing sources — their whole value is catching postings our own polling
  missed, which can legitimately have an older `date_posted` by the time
  Simplify's own community/scraper adds them; the id-based new/seen diff is
  what gates those instead.

Every Discord notification also shows how fresh the posting actually is: a
**Posted** field (e.g. "23 minutes ago," "3 hours ago," or Workday's own
"Posted Today" text when that's all we have), plus — for every source except
Workday, which never exposes an exact timestamp — Discord's native embed
timestamp, so hovering shows the exact original post time in your local
time zone.

## Setup

1. Push this folder to a new GitHub repo.
2. In the repo's **Settings → Secrets and variables → Actions**, add a secret
   named `DISCORD_WEBHOOK_URL` (Discord: Channel Settings → Integrations →
   Webhooks → New Webhook → Copy URL).
3. Enable Actions on the repo if prompted. The workflow runs automatically on
   its schedule, or trigger it manually from the **Actions** tab
   (`workflow_dispatch`).
4. **First run seeds state only** — for each source, it fetches current
   postings and saves them as "already seen" without sending any Discord
   messages. Otherwise your first run would blast ~18,000 companies' (plus
   ~900 GitHub-listing) worth of existing listings into your channel at
   once. Every run after that only notifies on genuinely new postings. (The
   existing Workday state carries over from before this expansion — every
   other source starts with a fresh seed run.)

## Endpoint format

| ATS | Request | Public link |
|---|---|---|
| Workday | `POST https://{host}/wday/cxs/{tenant}/{site}/jobs` | `https://{host}/{site}{externalPath}` |
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs` | `absolute_url` from the response |
| Lever | `GET https://api.lever.co/v0/postings/{slug}?mode=json` | `hostedUrl` from the response |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{slug}` | `jobUrl` (falls back to `applyUrl`) |
| SmartRecruiters | `GET https://api.smartrecruiters.com/v1/companies/{slug}/postings` | `https://jobs.smartrecruiters.com/{slug}/{id}` |

This matches the conventions used by
[kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers)'s
tested, open-source adapters, including the hard limits worth knowing:

- Workday's `limit` is capped at **20** per request — it returns HTTP 400
  above that. `poll.py` clamps `RESULTS_PER_COMPANY` to 20 automatically.
  Workday's reported totals also cap at **2,000**, and paginating past
  `offset=2000` silently wraps back to page 1 — doesn't affect this script
  (we only ever fetch page 1, the newest postings).
- Greenhouse, Lever, and Ashby's public board APIs return a company's
  *entire* open-postings list in one call — there's no "give me the newest
  N" mode like Workday's. Fine for polling (companies with huge boards are
  the exception, not the rule), but worth knowing if you're staring at
  response sizes.
- SmartRecruiters paginates; `poll.py` only fetches the first page
  (`limit=100&offset=0`), same "newest handful, not full history"
  philosophy as the Workday cap.

`data/companies.csv` was seeded from
[`kalil0321/ats-scrapers`'s `ats-companies/`](https://github.com/kalil0321/ats-scrapers/tree/main/ats-companies)
tenant lists (`workday.csv`, `greenhouse.csv`, `lever.csv`, `ashby.csv`,
`smartrecruiters.csv`) — same source the original Workday-only list came
from, just extended to the other four ATS types. It's the same
unfiltered-by-industry approach as before: the company list casts a wide net
and the title filters above do the narrowing.

Still worth a quick sanity check before your first full run:
```bash
pip install -r requirements.txt
python poll.py --dry-run --ats=greenhouse --limit=10
```
This hits only 10 Greenhouse companies and prints results instead of
messaging Discord. Swap `--ats=` for any of `workday` / `greenhouse` /
`lever` / `ashby` / `smartrecruiters` / `github`, or drop it entirely to hit
everything.

## GitHub listing sources

Direct ATS polling only catches companies actually in `data/companies.csv`.
Community-curated repos like
[SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships)
cast a wider net — thousands of companies, including ones with no ATS at
all or a career page too custom to poll — maintained by a community plus
their own scraper, and published as a machine-readable `listings.json`
(not scraped off the README table).

`poll.py` fetches that JSON directly
(`https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json`,
~11 MB, currently ~900 active software-category postings) and treats each
entry's `company_name` like any other company: dedup by the entry's stable
`id`, same Discord notification, same state file (`state/seen_github.json`).
Filtering there works a little differently, see
[Filtering](#filtering) above — no title regex, `category` field does the
software-role gating instead.

Add more sources by appending to `GITHUB_LISTING_SOURCES` in `poll.py` —
each entry is `{label, owner, repo, ref, path}`, where `path` points at a
JSON array shaped like Simplify's (`id`, `title`, `company_name`, `url`,
`category`, `active`, `is_visible`, `date_posted`, `locations`). A repo
that publishes the same shape — including a personal/test one — plugs in
with just that one new entry.

## Tuning

- `POLL_CONCURRENCY` (env var) — how many requests run in parallel *within
  one ATS lane*. The workflow sets this per matrix lane (50 for Workday, 60
  for Greenhouse, 40 for Lever, 50 for Ashby, 40 for SmartRecruiters; unused
  for the `github` lane, which is a single feed fetch, not a per-company
  fan-out), roughly matched to each lane's company count. Higher = faster
  full pass, but more load per batch.
- `RESULTS_PER_COMPANY` (env var, default 20, Workday only) — how many
  recent postings to pull per company per run.
- Cron schedule in `poll.yml` — every 10 minutes by default, run as six
  parallel matrix lanes (one per ATS type, plus one for GitHub listing
  sources) so a slow lane never blocks the others. GitHub Actions won't
  reliably go faster than ~5 minutes, and scheduled runs can lag further
  during peak GitHub load, so treat this as "close to real-time," not
  guaranteed-instant. See [Polling frequency](#polling-frequency) below
  before turning this down.
- Only want a subset of sources? Trim the `matrix.include` list in
  `poll.yml`, or filter `data/companies.csv` down to the `ats` values you
  care about.

## Polling frequency

Can you turn the cron from `*/10 * * * *` down to `*/5 * * * *`? Mechanically
yes — nothing breaks — but know what you're trading for it:

- **GitHub Actions minutes are free either way.** This repo is public, so
  scheduled-workflow minutes aren't billed regardless of frequency.
- **The real constraint is each lane's own run time, not the cron value.**
  Every matrix lane has `concurrency: {group: poll-<lane>, cancel-in-progress:
  false}` — if a lane is still running when its next scheduled trigger fires,
  that trigger just queues behind it rather than running in parallel. Lower
  the cron below how long a lane actually takes and you stop gaining
  anything: the lane settles into "however long it takes," not the cron
  interval. The small lanes (Lever ~2.4k companies, Ashby ~3.4k,
  SmartRecruiters ~2.7k, GitHub ~1 feed) should comfortably clear 5 minutes.
  Workday (~3.5k) and especially Greenhouse (~6k, and its board API returns
  a company's *entire* posting list with no "just the newest" mode) are the
  ones to actually watch — check their run durations in the **Actions** tab
  after a few cycles before trusting 5 minutes end-to-end.
- **It doubles request volume** against each ATS's shared public API — still
  within the realm of "reasonable," but worth knowing per the
  [etiquette](#etiquette--rate-limits) note below.
- If Workday/Greenhouse turn out too slow for 5 minutes, options other than
  "give up on 5 minutes everywhere": split `matrix.include` into two
  `on.schedule` cron entries (fast one for the small lanes, 10-minute one
  for Workday/Greenhouse, gated with `if: github.event.schedule == '...'`
  per lane), or shard a big lane's rows in `companies.csv` across two
  matrix entries that each poll half the companies.

## Etiquette / rate limits

This only calls the same public JSON endpoints each company's own careers
page already calls in your browser — nothing scraped, no auth bypassed. Still,
be a good citizen:
- Don't push any one lane's `POLL_CONCURRENCY` much higher than what's
  already set, or push the cron much below 5 minutes; there's no benefit to
  you and it's inconsiderate to companies' infrastructure.
- If any company starts returning errors consistently, `poll.py` already
  logs and skips it rather than retrying aggressively — leave that behavior
  alone rather than adding retry loops.
- The GitHub listing source fetches an ~11 MB JSON file every run — fine for
  `raw.githubusercontent.com` (it's a CDN, and plenty of tools already poll
  this exact file), but don't add more large feeds to `GITHUB_LISTING_SOURCES`
  without considering the added bandwidth per run.
