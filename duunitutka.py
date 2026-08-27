"""Duunitutka — part-time customer service job radar for the greater Helsinki area.

One linear pass, run on a schedule (Railway Cron), then exit:

    search Tavily  ->  filter out anything already in SQLite  ->  Telegram  ->  store

No framework, no queue, no web UI. See README.md for deployment notes — in
particular the Railway Volume requirement for the SQLite file.
"""

import hashlib
import html
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("duunitutka")


def env_list(name, default):
    """Comma-separated env var -> list of trimmed items, or `default` if unset/blank."""
    items = [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]
    return items or default


TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH = os.getenv("DB_PATH", "/data/duunitutka.db")

DEFAULT_LOCATIONS = ["Helsinki", "Espoo", "Kauniainen", "Vantaa", "Kirkkonummi"]
DEFAULT_PHRASES = ["osa-aikainen asiakaspalvelu", "part-time customer service"]

DEFAULT_ROLE_KEYWORDS = ["asiakaspalvelu", "asiakaspalvelija", "customer service"]

LOCATIONS = env_list("LOCATIONS", DEFAULT_LOCATIONS)
PHRASES = env_list("PHRASES", DEFAULT_PHRASES)
ROLE_KEYWORDS = env_list("ROLE_KEYWORDS", DEFAULT_ROLE_KEYWORDS)
LOCATION_FALLBACK = "Greater Helsinki" if LOCATIONS == DEFAULT_LOCATIONS else "Unknown"

# Kill switch: checked before anything else in main(), so flipping this one
# Railway variable silences the script even if another one is broken.
ENABLED = os.getenv("ENABLED", "true").strip().lower() in ("1", "true", "yes")

# Tavily recency bound: "day" | "week" | "month" | "year". Empty disables it.
TIME_RANGE = os.getenv("TIME_RANGE", "month").strip()

# Hardcoded on purpose: adding a job site means a new domain with its own
# indexing quirks, which is a code change rather than a config tweak.
SOURCES = [
    ("Duunitori", "duunitori.fi"),
    ("Oikotie", "tyopaikat.oikotie.fi"),
    ("Indeed", "fi.indeed.com"),
    ("LinkedIn", "linkedin.com/jobs"),
]

TAVILY_URL = "https://api.tavily.com/search"
MAX_RESULTS_PER_QUERY = 10
SNIPPET_CHARS = 280
SEND_DELAY_SECONDS = 1
REQUEST_TIMEOUT = 30

SCHEMA = """
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
"""

COMPANY_RE = re.compile(r"\s(?:[-–—|@])\s+(.+?)\s*$")


# --- parsing helpers -------------------------------------------------------

def normalize(text):
    return " ".join((text or "").lower().split())


def guess_company(title):
    """Best-effort company from a title like 'Asiakaspalvelija - Firma Oy'.

    Only needs to be consistent enough to feed the content hash; it is not
    reliable structured data.
    """
    match = COMPANY_RE.search(title or "")
    return match.group(1).strip() if match else ""


def content_hash(title, company):
    payload = normalize(title) + "|" + normalize(company)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def guess_location(text):
    haystack = normalize(text)
    for city in LOCATIONS:
        if city.lower() in haystack:
            return city
    return LOCATION_FALLBACK


def matches_target_location(title, snippet):
    """True if a target city is named anywhere in the title or snippet."""
    haystack = normalize(f"{title} {snippet}")
    return any(city.lower() in haystack for city in LOCATIONS)


def matches_target_role(title):
    """True if the title names a target role.

    Title only, deliberately: nearly every Finnish retail ad lists "hyvat
    asiakaspalvelutaidot" among its requirements, so matching on the snippet
    would let Myyja postings straight back through.
    """
    return any(keyword.lower() in normalize(title) for keyword in ROLE_KEYWORDS)


def build_query(phrase, site):
    return f'{phrase} ({" OR ".join(LOCATIONS)}) site:{site}'


# --- external services -----------------------------------------------------

def tavily_search(query):
    """Return Tavily results for `query`, or [] if the call failed."""
    payload = {
        "query": query,
        "max_results": MAX_RESULTS_PER_QUERY,
        "search_depth": "basic",
    }
    if TIME_RANGE:
        payload["time_range"] = TIME_RANGE

    try:
        response = requests.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json().get("results", [])
    except (requests.RequestException, ValueError) as exc:
        log.warning("Tavily search failed for %r: %s", query, exc)
        return []


def send_telegram(listing):
    """Send one listing to Telegram. Returns True on success."""
    company = listing["company"] or "tuntematon"
    snippet = listing["snippet"][:SNIPPET_CHARS]
    text = (
        f"🆕 <b>{html.escape(listing['title'])}</b> · {html.escape(company)}\n"
        f"📍 {html.escape(listing['location'])} · via {html.escape(listing['source'])}\n"
        f"{html.escape(snippet)}\n"
        f"{html.escape(listing['url'])}"
    )
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.warning("Telegram send failed for %s: %s", listing["url"], exc)
        return False


# --- storage ---------------------------------------------------------------

def init_db(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def is_seen(conn, url, chash):
    row = conn.execute(
        "SELECT 1 FROM listings WHERE url = ? OR content_hash = ? LIMIT 1",
        (url, chash),
    ).fetchone()
    return row is not None


def insert_listing(conn, listing, notified):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT OR IGNORE INTO listings
           (url, content_hash, title, company, source, location, snippet,
            first_seen_at, notified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            listing["url"],
            listing["content_hash"],
            listing["title"],
            listing["company"],
            listing["source"],
            listing["location"],
            listing["snippet"],
            now,
            now if notified else None,
        ),
    )
    conn.commit()


# --- run -------------------------------------------------------------------

def missing_env():
    return [
        name
        for name, value in (
            ("TAVILY_API_KEY", TAVILY_API_KEY),
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        )
        if not value
    ]


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Ahead of the key validation on purpose: this must silence the run even
    # when another variable is broken or a key was just revoked.
    if not ENABLED:
        log.info("ENABLED=false — exiting without contacting Tavily or Telegram.")
        return 0

    missing = missing_env()
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        return 1

    queries = [(phrase, name, site) for phrase in PHRASES for name, site in SOURCES]
    log.info("Locations: %s", ", ".join(LOCATIONS))
    log.info("Phrases: %s", ", ".join(PHRASES))
    log.info("Role keywords: %s", ", ".join(ROLE_KEYWORDS))
    log.info("Time range: %s", TIME_RANGE or "(unbounded)")
    log.info("Running %d Tavily queries, db=%s", len(queries), DB_PATH)

    conn = init_db(DB_PATH)
    seen_results = new_listings = notified_count = empty_queries = 0
    skipped_location = skipped_role = 0
    try:
        for phrase, source, site in queries:
            query = build_query(phrase, site)
            results = tavily_search(query)
            if not results:
                empty_queries += 1
            log.info("%s | %r -> %d results", source, phrase, len(results))

            for result in results:
                seen_results += 1
                url = (result.get("url") or "").strip()
                title = (result.get("title") or "").strip()
                if not url or not title:
                    continue

                snippet = (result.get("content") or "").strip()

                # Relevance gates run before the dedupe lookup: a rejected
                # result is never stored, so there is nothing for it to match.
                # Nothing is recorded for skips either — they cost only a
                # string check, so they are simply re-evaluated next run.
                if not matches_target_location(title, snippet):
                    skipped_location += 1
                    log.info("Skip (location): %s", title)
                    continue
                if not matches_target_role(title):
                    skipped_role += 1
                    log.info("Skip (role): %s", title)
                    continue

                company = guess_company(title)
                chash = content_hash(title, company)
                if is_seen(conn, url, chash):
                    continue

                listing = {
                    "url": url,
                    "title": title,
                    "company": company,
                    "content_hash": chash,
                    "source": source,
                    "location": guess_location(f"{title} {snippet}"),
                    "snippet": snippet,
                }
                new_listings += 1
                # Notify and store before moving on to the next result, so the
                # same posting surfacing again later in this run (e.g. via the
                # other language phrase) is already "seen" and cannot
                # double-notify.
                notified = send_telegram(listing)
                insert_listing(conn, listing, notified)
                if notified:
                    notified_count += 1
                time.sleep(SEND_DELAY_SECONDS)
    finally:
        conn.close()

    log.info(
        "Done: %d queries (%d empty/failed), %d results, %d skipped (location), "
        "%d skipped (role), %d new, %d notified",
        len(queries),
        empty_queries,
        seen_results,
        skipped_location,
        skipped_role,
        new_listings,
        notified_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
