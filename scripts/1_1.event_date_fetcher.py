#!/usr/bin/env python3
"""
event_date_fetcher.py - Corregido con validación de rango y filtrado agresivo.
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

# Expected month ranges per event (1‑based)
EVENT_MONTH_RANGE = {
    "CyberDay":     (5, 6),    # May - June
    "CyberMonday":  (9, 10),   # September - October
    "BlackFriday":  (11, 12),  # November - December
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

def is_plausible(event: str, date: datetime) -> bool:
    """Return True if date's month is within expected range for the event."""
    if event not in EVENT_MONTH_RANGE:
        return True
    min_month, max_month = EVENT_MONTH_RANGE[event]
    return min_month <= date.month <= max_month

def parse_date_range(text: str, year: int, event: str) -> Optional[Tuple[datetime, datetime]]:
    """Extract date range and immediately validate plausibility."""
    for match in DATE_RANGE_RE.finditer(text.lower()):
        try:
            day1 = int(match.group(1))
            day2 = int(match.group(2)) if match.group(2) else day1
            month = MONTH_ES[match.group(3)]
            start = datetime(year, month, day1)
            end = datetime(year, month, day2)
            if end < start:
                end = start + timedelta(days=2)
            # Validity check
            if is_plausible(event, start):
                return start, end
            else:
                logger.debug(f"Ignored implausible date for {event}: {start.date()}")
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
# Scrapers (each returns a list of (start, end, source) – already validated)
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
    for selector in [".event-date", ".countdown", ".fecha", "time", "[class*='date']"]:
        for el in soup.select(selector):
            text = el.get_text(" ", strip=True)
            parsed = parse_date_range(text, year, event)
            if parsed:
                candidates.append((parsed[0], parsed[1], "cyber.cl"))
    # Search near keyword
    keyword = "cyberday" if "day" in event.lower() else "cybermonday"
    full_text = soup.get_text(" ", strip=True).lower()
    idx = full_text.find(keyword)
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
        for tag in soup.find_all(["article", "div", "section", "p", "h2", "h3"]):
            text = tag.get_text(" ", strip=True).lower()
            if any(kw in text for kw in keywords):
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
        for snippet in soup.select(".resultado, .search-result, article, .noticia"):
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
    url = f"https://www.google.com/search?q={q}&hl=es&gl=cl&num=3"
    resp = safe_get(url)
    if not resp:
        return []
    time.sleep(POLITE_DELAY)
    soup = BeautifulSoup(resp.text, "html.parser")
    candidates = []
    for snippet in soup.select(".BNeawe, .IsZvec, .VwiC3b"):
        text = snippet.get_text(" ", strip=True)
        parsed = parse_date_range(text, year, event)
        if parsed:
            candidates.append((parsed[0], parsed[1], "google_snippet"))
    return candidates

# ---------------------------------------------------------------------------
# Weighted candidate aggregation with source diversity requirement
# ---------------------------------------------------------------------------

class WeightedDateAggregator:
    def __init__(self, event: str):
        self.event = event
        self.candidates: List[Tuple[datetime, datetime, str]] = []
        self.votes = defaultdict(int)      # (start_str, end_str) -> total weight
        self.source_count = defaultdict(set)  # (start_str, end_str) -> set of sources

    def add_candidates(self, candidates: List[Tuple[datetime, datetime, str]]):
        for start, end, source in candidates:
            key = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            self.votes[key] += SOURCE_WEIGHTS.get(source, 1)
            self.source_count[key].add(source)
            self.candidates.append((start, end, source))

    def best_candidate(self) -> Optional[Tuple[datetime, datetime, str, int, int]]:
        """Returns (start, end, source, total_weight, unique_sources) or None."""
        best_key = None
        best_weight = 0
        best_sources = 0
        for key, weight in self.votes.items():
            sources_count = len(self.source_count[key])
            # Prefer higher weight and more diverse sources
            if weight > best_weight or (weight == best_weight and sources_count > best_sources):
                best_weight = weight
                best_sources = sources_count
                best_key = key
        if not best_key:
            return None
        # Find first candidate with that key to get source
        for start, end, source in self.candidates:
            if (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")) == best_key:
                return (start, end, source, best_weight, best_sources)
        return None

# ---------------------------------------------------------------------------
# DeepSeek arbitration (only when needed)
# ---------------------------------------------------------------------------

def deepseek_resolve(event: str, year: int, aggregator: WeightedDateAggregator,
                     api_key: str, base_url: str = "https://api.deepseek.com",
                     model: str = "deepseek-chat") -> Optional[Dict]:
    """Use DeepSeek to decide among plausible candidates or search fresh."""
    client = OpenAI(api_key=api_key, base_url=base_url)

    # Prepare candidate list with weights and sources
    candidate_lines = []
    for (start_str, end_str), weight in aggregator.votes.items():
        sources = ", ".join(aggregator.source_count[(start_str, end_str)])
        candidate_lines.append(f"  - {start_str} to {end_str} (weight {weight}, sources: {sources})")

    candidates_text = "\n".join(candidate_lines) if candidate_lines else "No candidates found."

    prompt = f"""
You are an expert in Chilean e-commerce dates.

Event: {event} {year}
Expected months: {EVENT_MONTH_RANGE[event][0]}-{EVENT_MONTH_RANGE[event][1]}

Candidates found (only those already within expected month range):
{candidates_text}

Task:
1. If there is a clear candidate with highest weight and at least two different sources, select it.
2. If ambiguous, use web search to find the official announcement on cyber.cl or ccs.cl.
3. Return ONLY JSON:
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
    if event == "CyberDay":
        may_31 = datetime(year, 5, 31)
        offset = (may_31.weekday() - 0) % 7
        start = may_31 - timedelta(days=offset)
        end = start + timedelta(days=4)
        notes = "Estimated: last Monday of May, 5 days."
    elif event == "CyberMonday":
        oct_1 = datetime(year, 10, 1)
        to_mon = (7 - oct_1.weekday()) % 7
        start = oct_1 + timedelta(days=to_mon + 7)
        end = start + timedelta(days=4)
        notes = "Estimated: second Monday of October, 5 days."
    elif event == "BlackFriday":
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

    # 1. Try cache
    cache_path = Path(DEFAULT_OUTPUT)
    if not force_refresh and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if data.get("year") == year and event in data.get("events", {}):
                cached = data["events"][event]
                # Verify plausibility
                start = datetime.strptime(cached["start_date"], "%Y-%m-%d")
                if is_plausible(event, start):
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
                logger.debug(f"{scraper.__name__} found {len(candidates)} plausible candidate(s)")
        except Exception as e:
            logger.warning(f"Scraper {scraper.__name__} failed: {e}")

    # 3. If we have at least one candidate with weight >= 10 and at least 2 sources, accept it
    best = aggregator.best_candidate()
    if best:
        start, end, source, weight, sources_count = best
        if weight >= 10 and sources_count >= 2:
            logger.info(f"High confidence for {event}: {start.date()} (weight {weight}, sources {sources_count})")
            return {
                "event": event,
                "year": year,
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
                "duration_days": (end - start).days + 1,
                "source": source,
                "confidence": "high",
                "notes": f"Weight: {weight}, sources: {sources_count}",
            }

    # 4. Use DeepSeek if API key available
    if api_key:
        result = deepseek_resolve(event, year, aggregator, api_key, llm_base_url, llm_model)
        if result:
            return result

    # 5. Fallback to historical
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
        logger.info("No DeepSeek key provided – will use scraping + historical.")
    fetch_all_events(args.year, api_key, args.output, args.force_refresh,
                     args.llm_base_url, args.llm_model)

if __name__ == "__main__":
    main()
