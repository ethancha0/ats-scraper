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
  `state/seen_<ats>.json`, and posts anything new to Discord. State is
  `{company: {hashed_job_id: last_seen_day}}`: every id in the current
  fetch is kept, and ids that stop appearing expire after 45 days. Pass
  `--ats=workday,lever` to only poll a subset, or `--ats=github` to poll
  only the listing sources.
- `.github/workflows/poll.yml` — one long-running Actions job per source
  (a matrix: 5 ATS lanes + 1 GitHub-listings lane). Each lane loops
  `poll.py` for ~5.5 hours on a GitHub-hosted runner (6-hour hard cap),
  force-pushes `seen_<ats>.json` to that lane's own `state/<ats>` branch
  (at most every 10 minutes, plus once at the end), then the workflow
  re-dispatches itself so the next 6-hour block starts immediately. A
  sparse cron is only a dead-man switch if the chain ever dies. State is
  kept off `main` so it doesn't bloat the repo's history; the
  `state/*.json` files still on `main` are only read once, as a migration
  fallback before a lane's state branch exists.

## Filtering

Three filters apply, all on by default:

- **`INTERNSHIPS_ONLY=true`** — only postings with "intern," "internship,"
  "co-op," "apprentice," "student," "undergraduate," or "Summer 20XX" in
  the title (word-boundary matched, so "International" or "Cooperative"
  won't false-positive). Titles that mention interns without being one
  ("Intern Program Manager," "Senior …," "University Recruiter") are
  dropped.
- **`SOFTWARE_ROLES_ONLY=true`** — the title must *also* look
  software/full-stack-related: "software," "developer," "full-stack,"
  "front-end," "back-end," "DevOps," "SRE," "mobile/iOS/Android," "data
  engineer," "ML/AI engineer," "forward deployed," "compiler," "computer
  science," "technology intern," etc. This is an allow-list (not a bare
  "engineer" match — that would also catch mechanical/electrical/civil
  engineering internships), so it filters out things like "Marketing
  Intern" or "Sales Co-op" that only matched on the internship keyword.
  Against Simplify's hand-labeled listings, it matches ~79% of active
  "Software" internships, and bare "Systems/Test Engineering Intern" titles
  (mostly hardware) no longer match.
- **`US_ONLY=true`** — drop postings with no US location. Where the ATS
  gives a structured country (SmartRecruiters, Lever, Ashby), that decides
  it; otherwise the location text is checked against non-US countries,
  regions, cities, and Canadian province codes ("Toronto, ON"). Remote,
  empty, and US locations still notify, and a posting listed in both a US
  and a non-US site is kept. Workday's "2 Locations" summary can't be
  checked and passes. This filter applies to GitHub listing sources too.

Set any of these to `false` in the workflow's `env:` block to loosen it —
e.g. `SOFTWARE_ROLES_ONLY=false` to see all internships/co-ops regardless
of department, `INTERNSHIPS_ONLY=false` to see every software role
including full-time, or `US_ONLY=false` to include jobs outside the US.

GitHub listing sources ([below](#github-listing-sources)) skip the
internship regex — every entry there is already an internship/co-op by
definition of the feed. `SOFTWARE_ROLES_ONLY` still applies: an entry
counts if its `category` is Software, *or* its title passes the software
regex (Simplify files plenty of "Software Engineer Intern" roles under
Hardware or AI/ML/Data).

Recency comes from two layers:
- **Primary**: a posting only counts as "new" if its ID wasn't in that ATS
  type's `seen_<ats>.json` from the *previous* pass. Direct ATS lanes
  re-poll as soon as the last pass finishes (typically a few minutes);
  the GitHub listings lane targets ~30 seconds start-to-start.
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
3. Enable Actions on the repo if prompted. Trigger it once from the
   **Actions** tab (`workflow_dispatch`); after that it re-dispatches itself
   at the end of each ~6-hour block. The 2-hour cron is only there to
   restart the chain if a run is cancelled or GitHub drops it. To stop it,
   cancel the in-progress run and re-run with **keep_alive** unchecked.
4. **First run seeds state only** — for each source, it fetches current
   postings and saves them as "already seen" without sending any Discord
   messages. Otherwise your first run would blast ~18,000 companies' (plus
   ~900 GitHub-listing) worth of existing listings into your channel at
   once. Every run after that only notifies on genuinely new postings.
5. If Discord is down or rate limits a post, the undelivered postings are
   left unseen and retried on the next pass rather than dropped.

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
- Because of those two caps, an internship at a big Workday or
  SmartRecruiters employer can sit outside the first page. With
  `INTERNSHIPS_ONLY` on, each of those companies also gets a second,
  keyword-searched request (`searchText: "intern"` / `q=intern`) whose
  results are merged in. Override with `WORKDAY_SEARCH_TEXT` /
  `SMARTRECRUITERS_QUERY` (empty string = skip the extra request).

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
~13 MB) and treats each entry's `company_name` like any other company:
dedup by the entry's stable `id`, same Discord notification, same state
file (`seen_github.json`). Filtering there works a little differently, see
[Filtering](#filtering) above — no internship regex, and `category` (or a
software-looking title) does the software-role gating.

Many feed entries link straight to a Greenhouse/Lever/Ashby/
SmartRecruiters/Workday board that's already in `data/companies.csv`. When
the direct lane's own filters would also pass that title, the feed entry is
skipped so the same job doesn't ping twice; entries our title regex would
reject still come through. Set `GITHUB_SKIP_DIRECT_COVERED=false` to
disable.

Add more sources by appending to `GITHUB_LISTING_SOURCES` in `poll.py` —
each entry is `{label, owner, repo, ref, path}`, where `path` points at a
JSON array shaped like Simplify's (`id`, `title`, `company_name`, `url`,
`category`, `active`, `is_visible`, `date_posted`, `locations`). A repo
that publishes the same shape — including a personal/test one — plugs in
with just that one new entry.

## Tuning

- `POLL_CONCURRENCY` (env var) — how many requests run in parallel *within
  one ATS lane*. The workflow sets this per matrix lane (50 for Workday, 10
  for Greenhouse, 40 for Lever, 50 for Ashby, 40 for SmartRecruiters; unused
  for the `github` lane, which is a single feed fetch, not a per-company
  fan-out), roughly matched to each lane's company count. Higher = faster
  full pass, but more load per batch.
- `POLL_RPS` (env var, `matrix.rps`) — max request starts per second for a
  whole lane (0 = unlimited). Greenhouse is set to 8: at full speed its
  firewall returned 403 for every board on almost every pass. Any 429 (or
  a Greenhouse 403) also pauses the whole lane for 30s, doubling up to 5
  minutes while the block lasts.
- `RESULTS_PER_COMPANY` (env var, default 20, Workday only) — how many
  recent postings to pull per company per run.
- Inner-loop interval in `poll.yml` (`matrix.interval`) — minimum seconds
  between poll *starts* per lane. Most direct ATS lanes use 60s (and
  already take longer than that to finish a full pass, so they effectively
  loop immediately); Greenhouse uses 600s, though at 8 req/s a pass takes
  ~13 minutes anyway. The GitHub listings lane uses 30s because it's one feed
  fetch. See [Polling frequency](#polling-frequency).
- Only want a subset of sources? Trim the `matrix.include` list in
  `poll.yml`, or filter `data/companies.csv` down to the `ats` values you
  care about.

## Polling frequency

GitHub's `on.schedule` cron will not fire every 5 minutes in practice — it
lags, skips, and has a floor around that anyway. This workflow does not
rely on it for cadence.

Instead each matrix lane is a **single GitHub-hosted job that loops for
~5.5 hours** (the runner hard-cap is 6 hours), then a `retrigger` job
calls `gh workflow run` so the next block starts immediately.
`workflow_dispatch` and `repository_dispatch` are the two event types
`GITHUB_TOKEN` is allowed to chain without a PAT.

What that actually buys you:

- **GitHub listings lane (~30s).** One `raw.githubusercontent.com` fetch.
  Test-repo commits and Simplify feed updates should notify within about
  half a minute of the next pass, not "whenever cron feels like it."
- **Direct ATS lanes.** Still bounded by how long a full pass takes
  (Workday/Greenhouse especially — Greenhouse returns each company's
  entire posting list). If a lane needs 3 minutes, looping it is already
  as fast as that lane can go; shrinking `interval` further does nothing.
- **Minutes are still free on a public repo.** You are using 6 runners ×
  24 hours, not 6 runners × a few minutes per hour. That's intended.
- **Don't stack cron on top of the loop.** The 2-hour schedule is only a
  revive if the chain dies (cancelled run, Actions outage). The `guard`
  job no-ops when a poll is already in progress so you don't queue
  multiple 6-hour runs.

To halt the chain: cancel the current run, then **Run workflow** with
**keep_alive** turned off (or just leave it cancelled and don't start
another).

## Etiquette / rate limits

This only calls the same public JSON endpoints each company's own careers
page already calls in your browser — nothing scraped, no auth bypassed. Still,
be a good citizen:
- Don't push any one lane's `POLL_CONCURRENCY` much higher than what's
  already set, or drop an ATS lane's loop interval below how long a pass
  already takes; there's no benefit to you and it's inconsiderate to
  companies' infrastructure. The GitHub listings interval can stay low —
  that's one CDN fetch, not thousands of career-site APIs.
- If any company starts returning errors consistently, `poll.py` already
  logs and skips it rather than retrying aggressively — leave that behavior
  alone rather than adding retry loops.
- The GitHub listing source fetches an ~11 MB JSON file every run — fine for
  `raw.githubusercontent.com` (it's a CDN, and plenty of tools already poll
  this exact file), but don't add more large feeds to `GITHUB_LISTING_SOURCES`
  without considering the added bandwidth per run.
