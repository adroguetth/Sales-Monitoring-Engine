#!/usr/bin/env python3
"""
event_date_fetcher.py

Event Date Fetcher for key Chilean e-commerce dates:
- CyberDay (May/June)
- CyberMonday (October)
- Black Friday (November)

Strategy (in order):
1. If JSON cache exists and contains valid data → use it directly.
2. Robust scraping with Session (realistic headers, retries, robots.txt):
   a. cyber.cl
   b. ccs.cl/ecommerce + ccs.cl
   c. emol.com / latercera.com / df.cl / biobiochile.cl
   d. Google Search snippet (no API)
3. If all scraping fails → LLM with web search (DeepSeek or OpenAI-compatible).
4. Last resort → historical estimation.

Output: JSON with metadata for all events.
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
import argparse
import urllib.robotparser
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from openai import OpenAI

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger("EventDateFetcher")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT = "event_dates.json"
REQUEST_TIMEOUT = 12       # seconds
POLITE_DELAY   = 1.5       # pause between requests (courtesy)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Spanish month name → number mapping
MONTH_ES: Dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# Regex for Spanish date ranges: "3 al 5 de mayo" or "3 de mayo"
MONTH_ES_RANGE_RE = re.compile(
    r"(\d{1,2})\s*(?:al?\s*(\d{1,2}))?\s*de\s*"
    r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|octubre|noviembre|diciembre)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Robust session with automatic retries
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    """Create a requests session with retry strategy and default headers."""
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    return session


SESSION = build_session()

# ---------------------------------------------------------------------------
# robots.txt – basic respect (cached per domain)
# ---------------------------------------------------------------------------
_ROBOTS_CACHE: Dict[str, urllib.robotparser.RobotFileParser] = {}


def can_fetch(url: str) -> bool:
    """Check robots.txt permission for the given URL and user-agent."""
    parsed = urlparse(url)
    base   = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _ROBOTS_CACHE:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.set_url(urljoin(base, "/robots.txt"))
            rp.read()
        except Exception:
            pass
        _ROBOTS_CACHE[base] = rp
    return _ROBOTS_CACHE[base].can_fetch(HEADERS["User-Agent"], url)


def safe_get(url: str, **kwargs) -> Optional[requests.Response]:
    """
    Perform a GET request respecting robots.txt and handling errors.
    Returns None if blocked or request fails.
    """
    if not can_fetch(url):
        logger.warning(f"robots.txt blocks: {url}")
        return None
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        logger.warning(f"GET error {url}: {e}")
        return None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date_range(text: str, year: int) -> Optional[Tuple[datetime, datetime]]:
    """
    Extract the first Spanish date range found in the text.
    Handles both single day and range (e.g., '3 al 5 de mayo').
    Returns (start, end) datetimes or None.
    """
    for match in MONTH_ES_RANGE_RE.finditer(text.lower()):
        try:
            day1   = int(match.group(1))
            day2   = int(match.group(2)) if match.group(2) else day1
            month  = MONTH_ES[match.group(3)]
            start  = datetime(year, month, day1)
            end    = datetime(year, month, day2)
            if end < start:
                # Assume the range crosses into next month (e.g., 30 May - 2 Jun)
                # Simple fallback: add 2 days
                end = start + timedelta(days=2)
            return start, end
        except (ValueError, KeyError):
            continue
    return None


def make_result(event: str, year: int, start: datetime, end: datetime,
                source: str, confidence: str, notes: str = "") -> Dict:
    """Build a standard event result dictionary."""
    return {
        "event":         event,
        "year":          year,
        "start_date":    start.strftime("%Y-%m-%d"),
        "end_date":      end.strftime("%Y-%m-%d"),
        "duration_days": (end - start).days + 1,
        "source":        source,
        "confidence":    confidence,
        "notes":         notes,
    }

# ---------------------------------------------------------------------------
# Cache – avoid re‑scraping if JSON already exists for the year
# ---------------------------------------------------------------------------

def load_cached(output_file: str, event: str, year: int) -> Optional[Dict]:
    """Load event data from JSON cache if it matches the year."""
    path = Path(output_file)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("year") != year:
            return None
        cached = data.get("events", {}).get(event)
        if cached and cached.get("start_date"):
            logger.info(f"✔ Cache hit for {event} {year}: {cached['start_date']}")
            return cached
    except Exception as e:
        logger.warning(f"Failed to read cache: {e}")
    return None

# ---------------------------------------------------------------------------
# Source 1 – cyber.cl
# ---------------------------------------------------------------------------

def scrape_cyber_cl(event: str, year: int) -> Optional[Dict]:
    """Scrape cyber.cl for event dates."""
    url = "https://cyber.cl"
    logger.info(f"[cyber.cl] scraping for {event} {year}")
    resp = safe_get(url)
    if not resp:
        return None
    time.sleep(POLITE_DELAY)

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try semantic selectors first
    for selector in [".event-date", ".countdown", ".fecha", "time", ".banner-text",
                     "[class*='date']", "[class*='fecha']", "[class*='event']"]:
        for el in soup.select(selector):
            text = el.get_text(" ", strip=True)
            parsed = parse_date_range(text, year)
            if parsed:
                return make_result(event, year, *parsed, "cyber.cl", "high")

    # Fallback: text window around the keyword
    full_text = soup.get_text(" ", strip=True)
    keyword = "cyberday" if "day" in event.lower() else "cybermonday"
    idx = full_text.lower().find(keyword)
    window = full_text[max(0, idx - 80): idx + 350] if idx != -1 else full_text[:600]
    parsed = parse_date_range(window, year)
    if parsed:
        return make_result(event, year, *parsed, "cyber.cl", "medium",
                           "Extracted from text window near keyword.")
    return None

# ---------------------------------------------------------------------------
# Source 2 – ccs.cl
# ---------------------------------------------------------------------------

def scrape_ccs(event: str, year: int) -> Optional[Dict]:
    """Scrape Chilean Chamber of Commerce website for event dates."""
    urls = ["https://www.ccs.cl/ecommerce/", "https://www.ccs.cl/"]
    keyword_map = {
        "CyberDay":    ["cyberday", "cyber day"],
        "CyberMonday": ["cybermonday", "cyber monday"],
        "BlackFriday": ["black friday", "blackfriday", "viernes negro"],
    }
    keywords = keyword_map.get(event, [event.lower()])

    for url in urls:
        logger.info(f"[ccs.cl] scraping {url}")
        resp = safe_get(url)
        if not resp:
            continue
        time.sleep(POLITE_DELAY)

        soup = BeautifulSoup(resp.text, "html.parser")

        # Search inside relevant tags
        for tag in soup.find_all(["time", "article", "div", "p", "li", "span", "h2", "h3"]):
            text = tag.get_text(" ", strip=True).lower()
            if any(kw in text for kw in keywords):
                parsed = parse_date_range(text, year)
                if parsed:
                    return make_result(event, year, *parsed, "ccs.cl", "high")

        # Fallback: whole page text
        full_text = soup.get_text(" ", strip=True).lower()
        for kw in keywords:
            idx = full_text.find(kw)
            if idx != -1:
                window = full_text[max(0, idx - 60): idx + 300]
                parsed = parse_date_range(window, year)
                if parsed:
                    return make_result(event, year, *parsed, "ccs.cl", "medium")
    return None

# ---------------------------------------------------------------------------
# Source 3 – Chilean media (Emol, La Tercera, DF, BioBio)
# ---------------------------------------------------------------------------

MEDIA_SOURCES: List[Tuple[str, str]] = [
    ("Emol",        "https://www.emol.com/noticias/busqueda/?q={q}"),
    ("La Tercera",  "https://www.latercera.com/buscador/?q={q}"),
    ("DF.cl",       "https://www.df.cl/buscador?q={q}"),
    ("BioBioChile", "https://www.biobiochile.cl/?s={q}"),
]


def scrape_media(event: str, year: int) -> Optional[Dict]:
    """Search Chilean news media for event date announcements."""
    query_map = {
        "CyberDay":    f"CyberDay {year} fecha",
        "CyberMonday": f"CyberMonday {year} fecha",
        "BlackFriday": f"Black Friday Chile {year} fecha",
    }
    query = requests.utils.quote(query_map.get(event, f"{event} Chile {year}"))

    for name, template in MEDIA_SOURCES:
        url = template.format(q=query)
        logger.info(f"[{name}] searching '{event} {year}'")
        resp = safe_get(url)
        if not resp:
            continue
        time.sleep(POLITE_DELAY)

        soup = BeautifulSoup(resp.text, "html.parser")
        # Collect snippets from headlines and paragraphs that mention the year or event
        snippets = []
        for el in soup.find_all(["h2", "h3", "p", "li"])[:50]:
            el_text = el.get_text(" ", strip=True)
            if str(year) in el_text or "cyber" in el_text.lower() or "black friday" in el_text.lower():
                snippets.append(el_text)
        combined = " ".join(snippets).lower()
        parsed = parse_date_range(combined, year)
        if parsed:
            return make_result(event, year, *parsed, name, "medium",
                               f"Extracted from search results on {name}.")
    return None

# ---------------------------------------------------------------------------
# Source 4 – Google snippet (no API, last web resort)
# ---------------------------------------------------------------------------

def scrape_google_snippet(event: str, year: int) -> Optional[Dict]:
    """Fetch Google search result snippets for the event."""
    query_map = {
        "CyberDay":    f"CyberDay Chile {year} cuando es fecha oficial",
        "CyberMonday": f"CyberMonday Chile {year} fecha cuando",
        "BlackFriday": f"Black Friday Chile {year} fecha",
    }
    q   = requests.utils.quote(query_map.get(event, f"{event} Chile {year}"))
    url = f"https://www.google.com/search?q={q}&hl=es&gl=cl&num=5"

    logger.info(f"[Google] snippet for {event} {year}")
    resp = safe_get(url)
    if not resp:
        return None
    time.sleep(POLITE_DELAY)

    soup = BeautifulSoup(resp.text, "html.parser")
    parts = []
    # Common snippet classes used by Google
    for selector in ["div.BNeawe", "div.IsZvec", "span.hgKElc",
                     "div.kp-header", "div.yDYNvb", "div.VwiC3b"]:
        parts += [el.get_text(" ", strip=True) for el in soup.select(selector)[:10]]

    combined = " ".join(parts).lower()
    parsed = parse_date_range(combined, year)
    if parsed:
        return make_result(event, year, *parsed, "google_snippet", "medium",
                           "Extracted from Google snippet.")
    return None

# ---------------------------------------------------------------------------
# Source 5 – LLM with web search (DeepSeek or OpenAI-compatible)
# ---------------------------------------------------------------------------

def fetch_via_llm(event: str, year: int, api_key: str,
                  base_url: str = "https://api.deepseek.com",
                  model: str = "deepseek-chat") -> Optional[Dict]:
    """Use an LLM with web search capability to find event dates."""
    logger.info(f"[LLM:{model}] querying {event} {year}")
    client = OpenAI(api_key=api_key, base_url=base_url)

    prompt = (
        f"Search the web for the official dates of {event} in Chile for {year}.\n"
        "Priority sources: cyber.cl, ccs.cl/ecommerce, emol.com, latercera.com.\n\n"
        f"Context: CyberDay = late May/early June (3-5 days). "
        f"CyberMonday = October (3-5 days). BlackFriday = last Friday of November (4 days).\n\n"
        "Respond ONLY with valid JSON (no markdown, no extra text):\n"
        "{\n"
        f'  "event": "{event}",\n'
        f'  "year": {year},\n'
        '  "start_date": "YYYY-MM-DD",\n'
        '  "end_date": "YYYY-MM-DD",\n'
        '  "duration_days": <int>,\n'
        '  "source": "llm_web_search",\n'
        '  "confidence": "high|medium|low",\n'
        '  "notes": "<explanation>"\n'
        "}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system",
                 "content": ("You are an expert in Chilean e-commerce. "
                             "Respond ONLY with valid JSON, no markdown.")},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            extra_body={"enable_search": True},
        )
        raw = response.choices[0].message.content.strip()
        # Remove possible markdown code fences
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        result = json.loads(raw)
        if result.get("start_date"):
            return result
    except Exception as e:
        logger.error(f"LLM query failed: {e}")
    return None

# ---------------------------------------------------------------------------
# Source 6 – Historical estimation (last resort)
# ---------------------------------------------------------------------------

def estimate_historically(event: str, year: int) -> Dict:
    """Fallback: estimate dates based on historical patterns."""
    if event == "CyberDay":
        # Last Monday of May
        may_31 = datetime(year, 5, 31)
        offset = (may_31.weekday() - 0) % 7
        start  = may_31 - timedelta(days=offset)
        end    = start + timedelta(days=4)
        notes  = "Estimation: last Monday of May, duration 5 days."

    elif event == "CyberMonday":
        # Second Monday of October
        oct_1  = datetime(year, 10, 1)
        to_mon = (7 - oct_1.weekday()) % 7
        start  = oct_1 + timedelta(days=to_mon + 7)
        end    = start + timedelta(days=4)
        notes  = "Estimation: second Monday of October, duration 5 days."

    elif event == "BlackFriday":
        # Last Friday of November
        nov_30 = datetime(year, 11, 30)
        offset = (nov_30.weekday() - 4) % 7
        start  = nov_30 - timedelta(days=offset)
        end    = start + timedelta(days=3)
        notes  = "Estimation: last Friday of November, duration 4 days (Fri–Mon)."

    else:
        raise ValueError(f"Unknown event: {event}")

    return make_result(event, year, start, end,
                       "historical_estimation", "low", notes)

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def fetch_event(
    event: str,
    year: int,
    api_key: Optional[str] = None,
    output_file: str = DEFAULT_OUTPUT,
    force_refresh: bool = False,
    llm_base_url: str = "https://api.deepseek.com",
    llm_model: str = "deepseek-chat",
) -> Dict:
    """
    Fetch a single event using the cascade strategy.
    Returns event dictionary.
    """
    # 0. Cache
    if not force_refresh:
        cached = load_cached(output_file, event, year)
        if cached:
            return cached

    # 1. Scrapers
    scrapers = [
        ("cyber.cl",       lambda: scrape_cyber_cl(event, year)),
        ("ccs.cl",         lambda: scrape_ccs(event, year)),
        ("media",          lambda: scrape_media(event, year)),
        ("google_snippet", lambda: scrape_google_snippet(event, year)),
    ]

    for name, scraper_fn in scrapers:
        try:
            result = scraper_fn()
            if result and result.get("start_date"):
                logger.info(f"✔ {event}: [{name}] → {result['start_date']} – {result['end_date']}")
                return result
        except Exception as exc:
            logger.warning(f"Scraper '{name}' exception: {exc}")

    # 2. LLM
    if api_key:
        result = fetch_via_llm(event, year, api_key, llm_base_url, llm_model)
        if result:
            logger.info(f"✔ {event}: [LLM] → {result['start_date']} – {result['end_date']}")
            return result

    # 3. Historical
    logger.info(f"⚠ {event}: using historical estimation (last resort)")
    return estimate_historically(event, year)


def fetch_all_events(
    year: int,
    api_key: Optional[str] = None,
    output_file: str = DEFAULT_OUTPUT,
    force_refresh: bool = False,
    llm_base_url: str = "https://api.deepseek.com",
    llm_model: str = "deepseek-chat",
) -> Dict[str, Dict]:
    """
    Fetch all three events (CyberDay, CyberMonday, BlackFriday)
    and save/update the JSON file.
    Returns dictionary of events.
    """
    # Load existing state (preserve already resolved events)
    existing: Dict = {}
    path = Path(output_file)
    if path.exists() and not force_refresh:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("year") != year:
                existing = {}
        except Exception:
            existing = {}

    events: Dict[str, Dict] = dict(existing.get("events", {}))

    for name in ["CyberDay", "CyberMonday", "BlackFriday"]:
        logger.info(f"══ Processing {name} {year} ══")
        data = fetch_event(name, year, api_key, output_file,
                           force_refresh, llm_base_url, llm_model)
        events[name] = data

    output = {
        "year":       year,
        "fetched_at": datetime.now().isoformat(),
        "events":     events,
    }
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    logger.info(f"✔ JSON saved to {path.absolute()}")
    return events

# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch CyberDay, CyberMonday and Black Friday dates in Chile.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python event_date_fetcher.py
  python event_date_fetcher.py --year 2027 --output fechas.json
  python event_date_fetcher.py --deepseek-key sk-... --force-refresh
  python event_date_fetcher.py --llm-base-url https://api.openai.com/v1 --llm-model gpt-4o
        """,
    )
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT)
    parser.add_argument("--deepseek-key", type=str,
                        help="LLM API key (also reads DEEPSEEK_API_KEY env var)")
    parser.add_argument("--llm-base-url", type=str,
                        default="https://api.deepseek.com")
    parser.add_argument("--llm-model", type=str, default="deepseek-chat")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Ignore cache and re‑scrape everything")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    api_key = args.deepseek_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.info("No LLM API key provided – will use scraping + historical only.")

    fetch_all_events(
        year=args.year,
        api_key=api_key,
        output_file=args.output,
        force_refresh=args.force_refresh,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
    )


if __name__ == "__main__":
    main()
