#!/usr/bin/env python3
"""
event_date_fetcher.py

Event Date Fetcher for Chilean e-commerce dates (CyberDay, CyberMonday, Black Friday)
with multi‑source weighting and DeepSeek arbitration.

Strategy:
1. Only run scraping inside active search windows (e.g., May–June for CyberDay).
2. Collect candidate dates from multiple sources (cyber.cl, ccs.cl, media, Google snippet).
3. Assign a weight to each candidate (source reliability, exact match, multiple occurrences).
4. If high‑confidence candidate exists (weight > threshold), return it.
5. Otherwise, use DeepSeek to evaluate ambiguous candidates or search fresh.
6. Fallback: historical estimation.

Search windows:
- CyberDay:     May 1 – June 30
- CyberMonday:  Sep 1 – Oct 31
- BlackFriday:  Nov 1 – Dec 31

Outside windows: returns historical estimation without any network calls.
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
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from urllib.parse import urljoin, urlparse
from collections import defaultdict

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
REQUEST_TIMEOUT = 12
POLITE_DELAY = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

# Spanish month mapping
MONTH_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# Date range regex
DATE_RANGE_RE = re.compile(
    r"(\d{1,2})\s*(?:al?\s*(\d{1,2}))?\s*de\s*"
    r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|octubre|noviembre|diciembre)",
    re.IGNORECASE,
)

# Search windows (month-day)
SEARCH_WINDOWS = {
    "CyberDay":     (5, 1, 6, 30),     # May 1 – Jun 30
    "CyberMonday":  (9, 1, 10, 31),    # Sep 1 – Oct 31
    "BlackFriday":  (11, 1, 12, 31),   # Nov 1 – Dec 31
}

# Source weights (higher = more trustworthy)
SOURCE_WEIGHTS = {
    "cyber.cl": 10,
    "ccs.cl": 8,
    "Emol": 5,
    "La Tercera": 5,
    "DF.cl": 5,
    "BioBioChile": 5,
    "google_snippet": 3,
    "llm_web_search": 7,
    "historical_estimation": 1,
}

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def in_search_window(event: str, today: datetime) -> bool:
    """Return True if today is within the search window for the given event."""
    if event not in SEARCH_WINDOWS:
        return False
    sm, sd, em, ed = SEARCH_WINDOWS[event]
    start = datetime(today.year, sm, sd)
    end = datetime(today.year, em, ed)
    return start <= today <= end

def parse_date_range(text: str, year: int) -> Optional[Tuple[datetime, datetime]]:
    """Extract first Spanish date range from text."""
    for match in DATE_RANGE_RE.finditer(text.lower()):
        try:
            day1 = int(match.group(1))
            day2 = int(match.group(2)) if match.group(2) else day1
            month = MONTH_ES[match.group(3)]
            start = datetime(year, month, day1)
            end = datetime(year, month, day2)
            if end < start:
                end = start + timedelta(days=2)
            return start, end
        except (ValueError, KeyError):
            continue
    return None

def normalize_date_str(date_str: str) -> str:
    """Convert various date formats to YYYY-MM-DD."""
    # Already YYYY-MM-DD
    if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
        return date_str
    # DD-MM-YYYY or DD/MM/YYYY
    for sep in ["-", "/"]:
        parts = date_str.split(sep)
        if len(parts) == 3 and len(parts[0]) <= 2:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    # Try to parse as datetime
    for fmt in ["%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"]:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str

# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------
def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

SESSION = build_session()
_ROBOTS_CACHE = {}

def can_fetch(url: str) -> bool:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _ROBOTS_CACHE:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.set_url(urljoin(base, "/robots.txt"))
            rp.read()
        except Exception:
            pass
        _ROBOTS_CACHE[base] = rp
    return _ROBOTS_CACHE[base].can_fetch(HEADERS["User-Agent"], url)

def safe_get(url: str) -> Optional[requests.Response]:
    if not can_fetch(url):
        logger.warning(f"robots.txt blocks: {url}")
        return None
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp
    except Exception as e:
        logger.debug(f"GET error {url}: {e}")
        return None

# ---------------------------------------------------------------------------
# Scrapers (each returns a list of (start_date, end_date, source_name) )
# ---------------------------------------------------------------------------

def scrape_cyber_cl(event: str, year: int) -> List[Tuple[datetime, datetime, str]]:
    """Scrape cyber.cl for date ranges."""
    url = "https://cyber.cl"
    logger.info(f"Scraping {url}")
    resp = safe_get(url)
    if not resp:
        return []
    time.sleep(POLITE_DELAY)
    soup = BeautifulSoup(resp.text, "html.parser")
    candidates = []
    # Look for specific date elements
    for selector in [".event-date", ".countdown", ".fecha", "time", "[class*='date']"]:
        for el in soup.select(selector):
            text = el.get_text(" ", strip=True)
            parsed = parse_date_range(text, year)
            if parsed:
                candidates.append((parsed[0], parsed[1], "cyber.cl"))
    # If nothing, search text near keyword
    keyword = "cyberday" if "day" in event.lower() else "cybermonday"
    full_text = soup.get_text(" ", strip=True).lower()
    idx = full_text.find(keyword)
    if idx != -1:
        window = full_text[max(0, idx-80):idx+350]
        parsed = parse_date_range(window, year)
        if parsed:
            candidates.append((parsed[0], parsed[1], "cyber.cl"))
    return candidates

def scrape_ccs(event: str, year: int) -> List[Tuple[datetime, datetime, str]]:
    """Scrape CCS site for dates."""
    urls = ["https://www.ccs.cl/ecommerce/", "https://www.ccs.cl/"]
    keywords = {
        "CyberDay": ["cyberday", "cyber day"],
        "CyberMonday": ["cybermonday", "cyber monday"],
        "BlackFriday": ["black friday", "viernes negro"],
    }.get(event, [event.lower()])
    candidates = []
    for url in urls:
        resp = safe_get(url)
        if not resp:
            continue
        time.sleep(POLITE_DELAY)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Look inside articles and divs containing keywords
        for tag in soup.find_all(["article", "div", "section", "p", "h2", "h3"]):
            text = tag.get_text(" ", strip=True).lower()
            if any(kw in text for kw in keywords):
                parsed = parse_date_range(text, year)
                if parsed:
                    candidates.append((parsed[0], parsed[1], "ccs.cl"))
        # Also check meta tags
        for meta in soup.find_all("meta", attrs={"name": ["description", "keywords"]}):
            content = meta.get("content", "")
            if any(kw in content.lower() for kw in keywords):
                parsed = parse_date_range(content, year)
                if parsed:
                    candidates.append((parsed[0], parsed[1], "ccs.cl"))
    return candidates

def scrape_media(event: str, year: int) -> List[Tuple[datetime, datetime, str]]:
    """Search Chilean media sites."""
    media_sites = [
        ("Emol", "https://www.emol.com/noticias/busqueda/?q={q}"),
        ("La Tercera", "https://www.latercera.com/buscador/?q={q}"),
        ("DF.cl", "https://www.df.cl/buscador?q={q}"),
        ("BioBioChile", "https://www.biobiochile.cl/?s={q}"),
    ]
    query_map = {
        "CyberDay": f"CyberDay {year} fecha",
        "CyberMonday": f"CyberMonday {year} fecha",
        "BlackFriday": f"Black Friday Chile {year} fecha",
    }
    query = requests.utils.quote(query_map.get(event, f"{event} Chile {year}"))
    candidates = []
    for name, url_tpl in media_sites:
        url = url_tpl.format(q=query)
        resp = safe_get(url)
        if not resp:
            continue
        time.sleep(POLITE_DELAY)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Extract snippets from results
        for snippet in soup.select(".resultado, .search-result, article, .noticia"):
            text = snippet.get_text(" ", strip=True)
            if text and (str(year) in text or event.lower() in text.lower()):
                parsed = parse_date_range(text, year)
                if parsed:
                    candidates.append((parsed[0], parsed[1], name))
    return candidates

def scrape_google_snippet(event: str, year: int) -> List[Tuple[datetime, datetime, str]]:
    """Fetch Google snippet (no API)."""
    query_map = {
        "CyberDay": f"CyberDay Chile {year} fecha oficial",
        "CyberMonday": f"CyberMonday Chile {year} fecha",
        "BlackFriday": f"Black Friday Chile {year} fecha",
    }
    q = requests.utils.quote(query_map.get(event, f"{event} Chile {year}"))
    url = f"https://www.google.com/search?q={q}&hl=es&gl=cl&num=3"
    resp = safe_get(url)
    if not resp:
        return []
    time.sleep(POLITE_DELAY)
    soup = BeautifulSoup(resp.text, "html.parser")
    candidates = []
    for snippet in soup.select(".BNeawe, .IsZvec, .VwiC3b"):
        text = snippet.get_text(" ", strip=True)
        parsed = parse_date_range(text, year)
        if parsed:
            candidates.append((parsed[0], parsed[1], "google_snippet"))
    return candidates

# ---------------------------------------------------------------------------
# Weighted candidate aggregation
# ---------------------------------------------------------------------------

class WeightedDateAggregator:
    """Collects date candidates, assigns weights, and selects the best."""

    def __init__(self, event: str, year: int):
        self.event = event
        self.year = year
        self.candidates: List[Tuple[datetime, datetime, str]] = []
        self.votes = defaultdict(int)  # (start_date_str, end_date_str) -> total weight

    def add_candidates(self, candidates: List[Tuple[datetime, datetime, str]]):
        for start, end, source in candidates:
            key = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            weight = SOURCE_WEIGHTS.get(source, 1)
            self.votes[key] += weight
            self.candidates.append((start, end, source))

    def best_candidate(self) -> Optional[Tuple[datetime, datetime, str]]:
        """Return the candidate with highest total weight."""
        if not self.votes:
            return None
        best_key = max(self.votes.items(), key=lambda x: x[1])[0]
        # Find first candidate with that key to get source
        for start, end, source in self.candidates:
            if (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")) == best_key:
                return (start, end, source)
        return None

    def total_weight(self) -> int:
        return sum(self.votes.values())

# ---------------------------------------------------------------------------
# DeepSeek arbitration
# ---------------------------------------------------------------------------

def deepseek_resolve(event: str, year: int, aggregator: WeightedDateAggregator,
                     api_key: str, base_url: str = "https://api.deepseek.com",
                     model: str = "deepseek-chat") -> Optional[Dict]:
    """Use DeepSeek to choose the most probable date or search fresh."""
    client = OpenAI(api_key=api_key, base_url=base_url)

    # Build context from weighted candidates
    candidates_list = []
    for (start_str, end_str), weight in aggregator.votes.items():
        candidates_list.append(f"  - {start_str} to {end_str} (weight {weight})")

    candidates_text = "\n".join(candidates_list) if candidates_list else "No candidates found."

    prompt = f"""
You are an expert in Chilean e-commerce dates. Determine the official date for {event} {year}.

Context:
- {event} typically occurs in: {"May-June" if event=="CyberDay" else "September-October" if event=="CyberMonday" else "November-December"}.
- The event is announced about one month in advance.
- Below are candidate dates extracted from web sources with accumulated weights (higher = more reliable).

Candidates:
{candidates_text}

Task:
1. If the candidates have high consensus (one candidate with weight > 15), select it.
2. If ambiguous, use your web search capability to find the latest official announcement from reliable sources (cyber.cl, ccs.cl).
3. Return ONLY a JSON object:
{{
  "event": "{event}",
  "year": {year},
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "duration_days": integer,
  "source": "deepseek_arbitration",
  "confidence": "high|medium|low",
  "notes": "explanation"
}}
"""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant for Chilean e-commerce dates. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            extra_body={"enable_search": True}
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```json|```$", "", raw, flags=re.IGNORECASE).strip()
        result = json.loads(raw)
        if result.get("start_date"):
            return result
    except Exception as e:
        logger.error(f"DeepSeek arbitration failed: {e}")
    return None

# ---------------------------------------------------------------------------
# Historical fallback
# ---------------------------------------------------------------------------

def estimate_historically(event: str, year: int) -> Dict:
    """Fallback estimation when no reliable data."""
    if event == "CyberDay":
        # Last Monday of May
        may_31 = datetime(year, 5, 31)
        offset = (may_31.weekday() - 0) % 7
        start = may_31 - timedelta(days=offset)
        end = start + timedelta(days=4)
        notes = "Estimated: last Monday of May, 5 days."
    elif event == "CyberMonday":
        # Second Monday of October
        oct_1 = datetime(year, 10, 1)
        to_mon = (7 - oct_1.weekday()) % 7
        start = oct_1 + timedelta(days=to_mon + 7)
        end = start + timedelta(days=4)
        notes = "Estimated: second Monday of October, 5 days."
    elif event == "BlackFriday":
        # Last Friday of November
        nov_30 = datetime(year, 11, 30)
        offset = (nov_30.weekday() - 4) % 7
        start = nov_30 - timedelta(days=offset)
        end = start + timedelta(days=3)
        notes = "Estimated: last Friday of November, 4 days."
    else:
        raise ValueError(f"Unknown event {event}")
    return {
        "event": event,
        "year": year,
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
        "duration_days": (end - start).days + 1,
        "source": "historical_estimation",
        "confidence": "low",
        "notes": notes,
    }

# ---------------------------------------------------------------------------
# Main fetcher
# ---------------------------------------------------------------------------

def fetch_event(event: str, year: int, api_key: Optional[str] = None,
                force_refresh: bool = False, llm_base_url: str = "https://api.deepseek.com",
                llm_model: str = "deepseek-chat") -> Dict:
    """Main orchestration with search window, scraping, weighting, and DeepSeek."""

    # If outside search window and not forced, return historical immediately
    today = datetime.now()
    if not force_refresh and not in_search_window(event, today):
        logger.info(f"Outside search window for {event} (today={today.date()}). Using historical estimation.")
        return estimate_historically(event, year)

    # Load cache if not forced
    cache_path = Path(DEFAULT_OUTPUT)
    if not force_refresh and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if data.get("year") == year and event in data.get("events", {}):
                cached = data["events"][event]
                # Validate cached date
                start = datetime.strptime(cached["start_date"], "%Y-%m-%d")
                if in_search_window(event, start):
                    logger.info(f"Using cached date for {event}: {cached['start_date']}")
                    return cached
        except Exception:
            pass

    # Step 1: Scrape all sources
    aggregator = WeightedDateAggregator(event, year)

    scrapers = [
        scrape_cyber_cl,
        scrape_ccs,
        scrape_media,
        scrape_google_snippet,
    ]
    for scraper in scrapers:
        try:
            candidates = scraper(event, year)
            if candidates:
                aggregator.add_candidates(candidates)
                logger.debug(f"{scraper.__name__} found {len(candidates)} candidate(s)")
        except Exception as e:
            logger.warning(f"Scraper {scraper.__name__} failed: {e}")

    # Step 2: If high confidence (total weight > 15), return best candidate
    best = aggregator.best_candidate()
    if best and aggregator.total_weight() >= 15:
        start, end, source = best
        logger.info(f"High confidence candidate for {event}: {start.date()} (weight {aggregator.total_weight()})")
        return {
            "event": event,
            "year": year,
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": end.strftime("%Y-%m-%d"),
            "duration_days": (end - start).days + 1,
            "source": source,
            "confidence": "high",
            "notes": f"Aggregated weight: {aggregator.total_weight()}",
        }

    # Step 3: Use DeepSeek to arbitrate or search fresh
    if api_key:
        result = deepseek_resolve(event, year, aggregator, api_key, llm_base_url, llm_model)
        if result:
            logger.info(f"DeepSeek resolved {event}: {result['start_date']}")
            return result

    # Step 4: Fallback to historical estimation
    logger.warning(f"No reliable data for {event}, using historical fallback.")
    return estimate_historically(event, year)


def fetch_all_events(year: int, api_key: Optional[str] = None,
                     output_file: str = DEFAULT_OUTPUT, force_refresh: bool = False,
                     llm_base_url: str = "https://api.deepseek.com",
                     llm_model: str = "deepseek-chat") -> Dict[str, Dict]:
    """Fetch all three events and save to JSON."""
    events = {}
    for event in ["CyberDay", "CyberMonday", "BlackFriday"]:
        logger.info(f"Processing {event} {year}")
        events[event] = fetch_event(event, year, api_key, force_refresh, llm_base_url, llm_model)

    output = {
        "year": year,
        "fetched_at": datetime.now().isoformat(),
        "events": events,
    }
    Path(output_file).write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Saved to {output_file}")
    return events

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Smart event date fetcher for Chilean e-commerce.")
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--deepseek-key", help="API key for DeepSeek (also reads DEEPSEEK_API_KEY env)")
    parser.add_argument("--llm-base-url", default="https://api.deepseek.com")
    parser.add_argument("--llm-model", default="deepseek-chat")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG","INFO","WARNING","ERROR"])
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    api_key = args.deepseek_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.info("No DeepSeek key provided – will only use scraping + historical.")
    fetch_all_events(args.year, api_key, args.output, args.force_refresh,
                     args.llm_base_url, args.llm_model)

if __name__ == "__main__":
    main()
