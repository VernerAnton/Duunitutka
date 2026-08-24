# Duunitutka

*"Job radar"* — a small scheduled script that watches for **new part-time
customer service job postings in the greater Helsinki area** and pushes each
one to Telegram, once.

Every run does exactly one linear pass and exits:

```
Tavily search  →  filter out what's already in SQLite  →  Telegram message  →  store
```

## Why it's built this way

It's a single linear pipeline with four steps and no branching, no
concurrency, and no state that outlives the process. LangGraph, CrewAI or n8n
would add an orchestration layer over something that is already just a `for`
loop — abstraction with nothing to abstract. So: plain Python, `requests`, and
the stdlib `sqlite3` module. One file, one dependency.

The script is **not** a polling loop. It assumes an external scheduler
(Railway Cron) starts it every few days; it runs once and exits.

## ⚠️ Railway Volume requirement — read this before deploying

**Railway Cron jobs start from a fresh container each run.** The container
filesystem is thrown away when the run finishes. If `duunitutka.db` sits on
that filesystem, every run begins with an empty database, decides that every
listing it finds is brand new, and re-sends the whole list to Telegram.

To avoid that, the database must live on a persistent volume:

1. In the Railway service: **Settings → Volumes → attach a volume**, mount
   path **`/data`**.
2. Set **`DB_PATH=/data/duunitutka.db`** in the service variables.

If you see the same postings arriving again after a run or two, this is why.

## Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TAVILY_API_KEY` | yes | — | https://app.tavily.com |
| `TELEGRAM_BOT_TOKEN` | yes | — | from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | yes | — | user or group chat id |
| `DB_PATH` | no | `/data/duunitutka.db` | must be on a Railway Volume in production |
| `LOCATIONS` | no | `Helsinki,Espoo,Kauniainen,Vantaa,Kirkkonummi` | comma-separated |
| `PHRASES` | no | `osa-aikainen asiakaspalvelu,part-time customer service` | comma-separated |

Missing any of the three required variables aborts the run before any network
call. `LOCATIONS` and `PHRASES` fall back to their defaults when unset, empty,
or all-whitespace; the effective values are logged at startup.

`PHRASES` drives how many searches each run costs: **one Tavily call per
(phrase × source)**, so the two default phrases across four sources is 8 calls
per run.

The job sources themselves are intentionally **hardcoded** in `SOURCES` in
`duunitutka.py`. Adding a site means a new domain with its own indexing
quirks and result shapes — that's a code change, not a quick env-var edit.

## How the search works

All locations are folded into a single query per (phrase, source) pair rather
than looping per city, which keeps a default run at 8 Tavily calls instead of
40:

```
osa-aikainen asiakaspalvelu (Helsinki OR Espoo OR Kauniainen OR Vantaa OR Kirkkonummi) site:duunitori.fi
```

| Source | `site:` filter | Note |
|---|---|---|
| Duunitori | `duunitori.fi` | well indexed |
| Oikotie Työpaikat | `tyopaikat.oikotie.fi` | well indexed |
| Indeed | `fi.indeed.com` | reasonable |
| LinkedIn | `linkedin.com/jobs` | often login-walled, expect fewer real hits |

A failed Tavily query is logged and skipped — the run continues with the next
one. Same for a failed Telegram send.

## Deduplication

A listing is skipped if **either** check matches an existing row:

1. its **URL** is already in the `listings` table, or
2. its **content hash** — `sha256(normalized title + "|" + normalized
   company)[:16]` — is already there, which catches the same posting reposted
   under a new URL or with tracking parameters.

The company name is a best-effort regex guess from the title (a trailing
`- Firma Oy`, `| Firma Oy`, `@ Firma Oy`). It only has to be *consistent*
enough to feed the hash, not accurate enough to display.

Each listing is stored right after it is processed, so a posting matched by
both the Finnish and English phrase in the same run notifies once.

## Storage

SQLite (`sqlite3`, stdlib — no extra dependency), one connection opened and
closed per run:

```sql
CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT UNIQUE NOT NULL,
    content_hash  TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT,
    source        TEXT NOT NULL,
    location      TEXT,
    snippet       TEXT,
    first_seen_at TEXT NOT NULL,
    notified_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_hash ON listings(content_hash);
```

A real (if tiny) database instead of a JSON blob means the history is
queryable later:

```sql
SELECT source, COUNT(*) FROM listings
WHERE first_seen_at >= '2026-08-01' GROUP BY source;
```

`notified_at` stays `NULL` when the Telegram send failed — the row still
blocks a re-notify, but you can see delivery didn't happen.

## Railway Cron Schedule

Cron has no "every N days" unit that survives month boundaries cleanly
(`*/3` restarts its count on the 1st), so these are approximations:

```
0 8 */3 * *     # ~every 3 days at 08:00 UTC  ← default
0 8 */2 * *     # ~every 2 days, fresher listings, ~50% more searches
```

Set it under **Settings → Cron Schedule** on the service.

## Local testing

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in the three required values
export $(grep -v '^#' .env | grep -v '^$' | xargs)
export DB_PATH=./duunitutka.db
python duunitutka.py
```

The first run notifies everything it finds. **Run it a second time** — it
should report `0 new`, which confirms dedupe and the database path are
working. Inspect what it stored with:

```bash
sqlite3 duunitutka.db "SELECT source, title, notified_at FROM listings;"
```

Docker:

```bash
docker build -t duunitutka .
docker run --rm --env-file .env -e DB_PATH=/data/duunitutka.db \
  -v "$PWD/data:/data" duunitutka
```

## Cost

8 Tavily searches per run:

| Cadence | Runs/month | Searches/month |
|---|---|---|
| `0 8 */3 * *` | ~10 | ~80 |
| `0 8 */2 * *` | ~15 | ~120 |

Tavily's free tier is 1,000 searches/month, so either cadence sits comfortably
inside it. Adding phrases to `PHRASES` scales this linearly (4 more searches
per run per phrase). Telegram is free.

## Relationship to the VAT Validation Agent project

**Reused:** the Tavily request/error-handling shape (post, `raise_for_status`,
log-and-continue rather than crash the run), the Telegram HTML `sendMessage`
pattern with escaped fields and a ~1s gap between sends, and the Railway
deployment know-how (env vars, cron scheduling, volumes).

**New here:** storage. That project used an atomic-JSON-write pattern for its
state; this one uses SQLite with a two-key (URL + content hash) dedupe, which
turns the state file from a write-only blob into something queryable.

**Not shared:** this is a standalone script, unrelated to any multi-agent job
search work. No task queue, no web UI, no MCP servers — none of that is needed
for four steps in a straight line.
