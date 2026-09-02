# Workday Job Alerts

Polls ~3,500 Workday-hosted career sites directly (skipping GitHub aggregators
and LinkedIn entirely) and pings a Discord channel the moment a new posting
appears.

## How it works

- `data/companies.csv` — your list of companies (`name,slug,url`). `slug` is
  `tenant/site`, which maps directly onto Workday's CXS jobs API:
  `https://{host}/wday/cxs/{tenant}/{site}/jobs`
- `poll.py` — hits every company's endpoint concurrently, diffs the returned
  postings against `state/seen.json`, and posts anything new to Discord.
- `.github/workflows/poll.yml` — runs `poll.py` on a cron schedule and commits
  the updated state file back to the repo (this is what makes state persist
  between runs on GitHub's ephemeral runners).

## Filtering

By default (`INTERNSHIPS_ONLY=true`) only postings with "intern," "internship,"
or "co-op" in the title are sent to Discord — word-boundary matched, so
"International" or "Cooperative" won't false-positive. Set
`INTERNSHIPS_ONLY=false` in the workflow's `env:` block to notify on every
role instead.

Recency comes from two layers:
- **Primary**: a posting only counts as "new" if its ID wasn't in
  `state/seen.json` from the *previous* run — with a 10-minute cron, that
  means "new" already means "posted in roughly the last 10 minutes," well
  inside "a few hours."
- **Safety net**: Workday's own `postedOn` text ("Posted Today" / "Posted
  Yesterday" / "Posted 3 Days Ago") is checked as a backstop, so that if a
  run is ever skipped or delayed and the next run catches up on a backlog,
  postings Workday itself already calls a day+ old get skipped rather than
  flooding your Discord all at once.

## Setup

1. Push this folder to a new GitHub repo.
2. In the repo's **Settings → Secrets and variables → Actions**, add a secret
   named `DISCORD_WEBHOOK_URL` (Discord: Channel Settings → Integrations →
   Webhooks → New Webhook → Copy URL).
3. Enable Actions on the repo if prompted. The workflow runs automatically on
   its schedule, or trigger it manually from the **Actions** tab
   (`workflow_dispatch`).
4. **First run seeds state only** — it fetches current postings for every
   company and saves them as "already seen" without sending any Discord
   messages. Otherwise your first run would blast ~3,500 companies' worth of
   existing listings into your channel at once. Every run after that only
   notifies on genuinely new postings.

## Endpoint format

`poll.py` hits `POST https://{host}/wday/cxs/{tenant}/{site}/jobs` and builds
each posting's public link as `https://{host}/{site}{externalPath}`. This
matches the convention used by [kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers)'s
tested, open-source Workday adapter, including two hard limits worth knowing:

- `limit` is capped at **20** per request — Workday returns HTTP 400 above
  that. `poll.py` clamps `RESULTS_PER_COMPANY` to 20 automatically.
- Reported totals cap at **2,000**, and paginating past `offset=2000` silently
  wraps back to page 1. Doesn't affect this script (we only ever fetch page 1
  — the newest postings), but matters if you extend this into a full
  historical crawl per company later.

Still worth a quick sanity check before your first full run:
```bash
pip install -r requirements.txt
python poll.py --dry-run --limit=10
```
This hits only 10 companies and prints results instead of messaging Discord.

## Tuning

- `POLL_CONCURRENCY` (env var, default 40, set to 50 in the workflow) — how
  many requests run in parallel. Higher = faster full pass, but more load per
  batch.
- `RESULTS_PER_COMPANY` (env var, default 20) — how many recent postings to
  pull per company per run. New postings are almost always within the most
  recent handful, so you don't need to paginate through a company's entire
  job history every run.
- Cron schedule in `poll.yml` — every 10 minutes by default. GitHub Actions
  won't reliably go faster than ~5 minutes, and scheduled runs can lag further
  during peak GitHub load, so treat this as "close to real-time," not
  guaranteed-instant.

## Extending later

If you ever want to go beyond Workday, [`ats-scrapers`](https://github.com/kalil0321/ats-scrapers)
(`pip install ats-scrapers`) has tested adapters for 50+ ATS platforms
(Greenhouse, Lever, Ashby, SmartRecruiters, etc.) behind one interface
(`get_scraper(ats, slug).fetch()`), plus a `find_company()` lookup that could
help cross-check or expand `data/companies.csv`. It's overkill for this
script's job (it's built for pulling a company's *entire* catalog, which is
too heavy for frequent lightweight polling — that's why `poll.py` talks to
the Workday endpoint directly instead), but it's a solid option for a
one-off enrichment pass or for adding other ATS platforms down the line.

## Etiquette / rate limits

This only calls the same public JSON endpoints each company's own careers
page already calls in your browser — nothing scraped, no auth bypassed. Still,
be a good citizen:
- Don't drop `POLL_CONCURRENCY` much above ~50 or push the cron much below
  5 minutes; there's no benefit to you and it's inconsiderate to companies'
  infrastructure.
- If any company starts returning errors consistently, `poll.py` already
  logs and skips it rather than retrying aggressively — leave that behavior
  alone rather than adding retry loops.
