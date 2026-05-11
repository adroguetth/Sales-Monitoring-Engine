#!/usr/bin/env python3
"""
event_date_fetcher.py

Event Date Fetcher for key Chilean e-commerce dates with strict validation.

Features:
- Ignores dates equal to or near current date (avoids publication dates).
- Strict plausible date ranges per event:
    * CyberDay:     May 20 – June 15
    * CyberMonday:  October 1 – October 20
    * BlackFriday:  November 20 – December 10
- Contextual check: requires event‑related phrases around the date.
- Multi‑source consensus: needs at least two independent sources.
- Source weighting (higher for official sites).
- DeepSeek web search as arbitrator when consensus is weak.
- Historical fallback when nothing reliable found.

Output: JSON with event dates, confidence, and metadata.
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

DATE_RANGE_RE = re.compile(
    r"(\d{1,2})\s*(?:al?\s*(\d{1,2}))?\s*de\s*"
    r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|octubre|noviembre|diciembre)",
    re.IGNORECASE,
)

# Expected plausible date ranges per event (inclusive)
EVENT_PLAUSIBLE_RANGE = {
    "CyberDay":     (5, 20, 6, 15),   # May 20 – June 15
    "CyberMonday":  (10, 1, 10, 20),  # October 1 – October 20
    "BlackFriday":  (11, 20, 12, 10), # November 20 – December 10
}

# Source weights
SOURCE_WEIGHTS = {
    "cyber.cl": 10,
    "ccs.cl": 8,
    "El Mostrador": 6,
    "Meganoticias": 6,
    "T13": 6,
    "ADN": 6,
    "Emol": 5,
    "La Tercera": 5,
    "DF.cl": 5,
    "BioBioChile": 5,
    "google_snippet": 3,
    "llm_web_search": 7,
    "historical_estimation": 1,
}

# Minimum required weight and distinct sources for high confidence
MIN_WEIGHT_HIGH = 12
MIN_SOURCES_HIGH = 2

# Context keywords that suggest a date is the event date (not publication date)
CONTEXT_KEYWORDS = [
    "se realizará", "las fechas son", "del", "al", "entre el", "y el",
    "próximo cyberday", "fecha oficial", "anunció", "será el", "comienza",
    "inicia", "termina", "durará", "evento", "oferta", "descuento"
]

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def today() -> datetime:
    """Return current date without time (UTC naive)."""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

def is_plausible(event: str, date: datetime) -> bool:
    """Return True if date falls within the plausible range for the event."""
    if event not in EVENT_PLAUSIBLE_RANGE:
        return True
    sm, sd, em, ed = EVENT_PLAUSIBLE_RANGE[event]
    start_ok = datetime(date.year, sm, sd)
    end_ok   = datetime(date.year, em, ed)
    return start_ok <= date <= end_ok

def is_too_close_to_today(date: datetime, days_threshold: int = 2) -> bool:
    """Ignore dates that are today or within the next few days (publication dates)."""
    diff = (date - today()).days
    return 0 <= diff <= days_threshold

def has_event_context(text: str, event: str) -> bool:
    """Check if the text contains typical event announcement phrases."""
    text_lower = text.lower()
    event_lower = event.lower()
    # Must contain the event name
    if event_lower not in text_lower:
        return False
    # Must contain at least one context keyword
    return any(kw in text_lower for kw in CONTEXT_KEYWORDS)

def parse_date_range(text: str, year: int, event: str) -> Optional[Tuple[datetime, datetime]]:
    """
    Extract date range from text, then validate plausibility and context.
    Returns (start, end) or None.
    """
    for match in DATE_RANGE_RE.finditer(text.lower()):
        try:
            day1 = int(match.group(1))
            day2 = int(match.group(2)) if match.group(2) else day1
            month = MONTH_ES[match.group(3)]
            start = datetime(year, month, day1)
            end = datetime(year, month, day2)
            if end < start:
                end = start + timedelta(days=2)
            # Validation chain
            if not is_plausible(event, start):
                logger.debug(f"Date {start.date()} not in plausible range for {event}")
                continue
            if is_too_close_to_today(start):
                logger.debug(f"Date {start.date()} is too close to today (publication date)")
                continue
            if not has_event_context(text, event):
                logger.debug(f"Missing event context for {event} in text snippet")
                continue
            return start, end
        except (ValueError, KeyError):
            continue
    return None

def normalize_date_str(date_str: str) -> str:
    """Convert various date formats to YYYY-MM-DD."""
    if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
        return date_str
    for sep in ["-", "/"]:
        parts = date_str.split(sep)
        if len(parts) == 3 and len(parts[0]) <= 2:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
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
# Scrapers (each returns list of (start, end, source) – already validated)
# ---------------------------------------------------------------------------

def scrape_cyber_cl(event: str, year: int) -> List[Tuple[datetime, datetime, str]]:
    url = "https://cyber.cl"
    logger.info(f"Scraping {url}")
    resp = safe_get(url)
    if not resp:
        return []
    time.sleep(POLITE_DELAY)
    soup = BeautifulSoup(resp.text, "html.parser")
    candidates = []
    # Look in event-specific elements
    for selector in [".event-date", ".countdown", ".fecha", "time", "[class*='date']"]:
        for el in soup.select(selector):
            text = el.get_text(" ", strip=True)
            parsed = parse_date_range(text, year, event)
            if parsed:
                candidates.append((parsed[0], parsed[1], "cyber.cl"))
    # Search near event keyword
    keyword = "cyberday" if "day" in event.lower() else "cybermonday"
    full_text = soup.get_text(" ", strip=True)
    idx = full_text.lower().find(keyword)
    if idx != -1:
        window = full_text[max(0, idx-80):idx+350]
        parsed = parse_date_range(window, year, event)
        if parsed:
            candidates.append((parsed[0], parsed[1], "cyber.cl"))
    return candidates

def scrape_ccs(event: str, year: int) -> List[Tuple[datetime, datetime, str]]:
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
        for tag in soup.find_all(["article", "div", "section", "p", "h2", "h3", "li"]):
            text = tag.get_text(" ", strip=True)
            if any(kw in text.lower() for kw in keywords):
                parsed = parse_date_range(text, year, event)
                if parsed:
                    candidates.append((parsed[0], parsed[1], "ccs.cl"))
        for meta in soup.find_all("meta", attrs={"name": ["description", "keywords"]}):
            content = meta.get("content", "")
            if any(kw in content.lower() for kw in keywords):
                parsed = parse_date_range(content, year, event)
                if parsed:
                    candidates.append((parsed[0], parsed[1], "ccs.cl"))
    return candidates

def scrape_media(event: str, year: int) -> List[Tuple[datetime, datetime, str]]:
    media_sites = [
        ("El Mostrador", "https://www.elmostrador.cl/buscador/?q={q}"),
        ("Meganoticias", "https://www.meganoticias.cl/buscador/?q={q}"),
        ("T13", "https://www.t13.cl/buscador?q={q}"),
        ("ADN", "https://www.adnradio.cl/buscador/?q={q}"),
        ("Emol", "https://www.emol.com/noticias/busqueda/?q={q}"),
        ("La Tercera", "https://www.latercera.com/buscador/?q={q}"),
        ("DF.cl", "https://www.df.cl/buscador?q={q}"),
        ("BioBioChile", "https://www.biobiochile.cl/?s={q}"),
    ]
    # Use more specific queries
    query_map = {
        "CyberDay": f"CyberDay {year} fecha oficial",
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
        # Search in result snippets
        for snippet in soup.select(".resultado, .search-result, article, .noticia, .card"):
            text = snippet.get_text(" ", strip=True)
            if text and (str(year) in text or event.lower() in text.lower()):
                parsed = parse_date_range(text, year, event)
                if parsed:
                    candidates.append((parsed[0], parsed[1], name))
    return candidates

def scrape_google_snippet(event: str, year: int) -> List[Tuple[datetime, datetime, str]]:
    query_map = {
        "CyberDay": f"CyberDay Chile {year} fecha oficial",
        "CyberMonday": f"CyberMonday Chile {year} fecha",
        "BlackFriday": f"Black Friday Chile {year} fecha",
    }
    q = requests.utils.quote(query_map.get(event, f"{event} Chile {year}"))
    url = f"https://www.google.com/search?q={q}&hl=es&gl=cl&num=5"
    resp = safe_get(url)
    if not resp:
        return []
    time.sleep(POLITE_DELAY)
    soup = BeautifulSoup(resp.text, "html.parser")
    candidates = []
    for snippet in soup.select(".BNeawe, .IsZvec, .VwiC3b, .g .r"):
        text = snippet.get_text(" ", strip=True)
        parsed = parse_date_range(text, year, event)
        if parsed:
            candidates.append((parsed[0], parsed[1], "google_snippet"))
    return candidates

# ---------------------------------------------------------------------------
# Weighted candidate aggregation
# ---------------------------------------------------------------------------

class WeightedDateAggregator:
    def __init__(self, event: str):
        self.event = event
        self.candidates: List[Tuple[datetime, datetime, str]] = []
        self.votes = defaultdict(int)               # (start_str, end_str) -> total weight
        self.source_count = defaultdict(set)        # (start_str, end_str) -> set of sources

    def add_candidates(self, candidates: List[Tuple[datetime, datetime, str]]):
        for start, end, source in candidates:
            key = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            weight = SOURCE_WEIGHTS.get(source, 1)
            self.votes[key] += weight
            self.source_count[key].add(source)
            self.candidates.append((start, end, source))

    def best_candidate(self) -> Optional[Tuple[datetime, datetime, str, int, int]]:
        """Return (start, end, source, total_weight, sources_count) of best candidate."""
        best_key = None
        best_weight = 0
        best_sources = 0
        for key, weight in self.votes.items():
            sources_cnt = len(self.source_count[key])
            if weight > best_weight or (weight == best_weight and sources_cnt > best_sources):
                best_weight = weight
                best_sources = sources_cnt
                best_key = key
        if not best_key:
            return None
        for start, end, source in self.candidates:
            if (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")) == best_key:
                return (start, end, source, best_weight, best_sources)
        return None

# ---------------------------------------------------------------------------
# DeepSeek arbitration (only when consensus is weak)
# ---------------------------------------------------------------------------

def deepseek_resolve(event: str, year: int, aggregator: WeightedDateAggregator,
                     api_key: str, base_url: str = "https://api.deepseek.com",
                     model: str = "deepseek-chat") -> Optional[Dict]:
    """Use DeepSeek with web search to find the most reliable date."""
    client = OpenAI(api_key=api_key, base_url=base_url)

    # Build candidate summary
    candidate_lines = []
    for (start_str, end_str), weight in aggregator.votes.items():
        sources = ", ".join(aggregator.source_count[(start_str, end_str)])
        candidate_lines.append(f"  - {start_str} to {end_str} (weight {weight}, sources: {sources})")
    candidates_text = "\n".join(candidate_lines) if candidate_lines else "No candidates found."

    prompt = f"""
You are an expert in Chilean e-commerce dates.

Event: {event} {year}
Expected plausible period: {EVENT_PLAUSIBLE_RANGE[event][1]}/{EVENT_PLAUSIBLE_RANGE[event][0]} – {EVENT_PLAUSIBLE_RANGE[event][3]}/{EVENT_PLAUSIBLE_RANGE[event][2]}

Candidates found (already validated against plausible range and not publication dates):
{candidates_text}

Task:
- If at least two independent sources agree on the same date range with total weight ≥ {MIN_WEIGHT_HIGH}, select it.
- Otherwise, perform a web search (priority: cyber.cl, ccs.cl) to find the official announced date.
- The event is never the current day or within 2 days of current date.
- Return ONLY JSON with the following structure:
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
            # Final validation
            try:
                d = datetime.strptime(result["start_date"], "%Y-%m-%d")
                if is_too_close_to_today(d):
                    logger.warning("DeepSeek returned a date too close to today, discarding.")
                    return None
                if not is_plausible(event, d):
                    logger.warning("DeepSeek returned implausible date, discarding.")
                    return None
            except:
                pass
            return result
    except Exception as e:
        logger.error(f"DeepSeek arbitration failed: {e}")
    return None

# ---------------------------------------------------------------------------
# Historical fallback (last resort)
# ---------------------------------------------------------------------------

def estimate_historically(event: str, year: int) -> Dict:
    """Fallback estimation using historical patterns."""
    if event == "CyberDay":
        # Last Monday of May
        may_31 = datetime(year, 5, 31)
        offset = (may_31.weekday() - 0) % 7
        start = may_31 - timedelta(days=offset)
        end = start + timedelta(days=2)   # 3 days (Mon-Wed)
        notes = "Estimated: last Monday of May, duration 3 days (historical pattern)."
    elif event == "CyberMonday":
        # Second Monday of October
        oct_1 = datetime(year, 10, 1)
        to_mon = (7 - oct_1.weekday()) % 7
        start = oct_1 + timedelta(days=to_mon + 7)
        end = start + timedelta(days=2)
        notes = "Estimated: second Monday of October, duration 3 days."
    elif event == "BlackFriday":
        # Last Friday of November
        nov_30 = datetime(year, 11, 30)
        offset = (nov_30.weekday() - 4) % 7
        start = nov_30 - timedelta(days=offset)
        end = start + timedelta(days=3)   # Fri-Mon
        notes = "Estimated: last Friday of November, duration 4 days."
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

    # 1. Check cache
    cache_path = Path(DEFAULT_OUTPUT)
    if not force_refresh and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if data.get("year") == year and event in data.get("events", {}):
                cached = data["events"][event]
                # Validate cached date
                d = datetime.strptime(cached["start_date"], "%Y-%m-%d")
                if is_plausible(event, d) and not is_too_close_to_today(d):
                    logger.info(f"Using cached date for {event}: {cached['start_date']}")
                    return cached
        except Exception:
            pass

    # 2. Scrape all sources
    aggregator = WeightedDateAggregator(event)
    scrapers = [scrape_cyber_cl, scrape_ccs, scrape_media, scrape_google_snippet]
    for scraper in scrapers:
        try:
            candidates = scraper(event, year)
            if candidates:
                aggregator.add_candidates(candidates)
                logger.debug(f"{scraper.__name__} found {len(candidates)} candidate(s)")
        except Exception as e:
            logger.warning(f"Scraper {scraper.__name__} failed: {e}")

    best = aggregator.best_candidate()
    if best:
        start, end, source, weight, sources_cnt = best
        if weight >= MIN_WEIGHT_HIGH and sources_cnt >= MIN_SOURCES_HIGH:
            logger.info(f"High confidence for {event}: {start.date()} (weight {weight}, sources {sources_cnt})")
            return {
                "event": event,
                "year": year,
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
                "duration_days": (end - start).days + 1,
                "source": source,
                "confidence": "high",
                "notes": f"Weight {weight} from {sources_cnt} sources.",
            }

    # 3. Use DeepSeek if available
    if api_key:
        result = deepseek_resolve(event, year, aggregator, api_key, llm_base_url, llm_model)
        if result:
            return result

    # 4. Fallback to historical
    logger.warning(f"No reliable data for {event}, using historical fallback.")
    return estimate_historically(event, year)

def fetch_all_events(year: int, api_key: Optional[str] = None,
                     output_file: str = DEFAULT_OUTPUT, force_refresh: bool = False,
                     llm_base_url: str = "https://api.deepseek.com",
                     llm_model: str = "deepseek-chat") -> Dict[str, Dict]:
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
    parser = argparse.ArgumentParser(description="Robust event date fetcher for Chilean e-commerce.")
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
        logger.info("No DeepSeek key provided – will use scraping + historical only.")
    fetch_all_events(args.year, api_key, args.output, args.force_refresh,
                     args.llm_base_url, args.llm_model)

if __name__ == "__main__":
    main()
