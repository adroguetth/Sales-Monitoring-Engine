#!/usr/bin/env python3
"""
event_date_fetcher.py - Intelligent event fetcher with cache and nearest‑event only.
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
DEFAULT_OUTPUT = "data/event_date/event_dates.json"
REQUEST_TIMEOUT = 12
POLITE_DELAY = 1.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "es-CL,es;q=0.9",
}

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

EVENT_PLAUSIBLE_RANGE = {
    "CyberDay":     (5, 20, 6, 15),   # May 20 – June 15
    "CyberMonday":  (10, 1, 10, 20),  # October 1 – October 20
    "BlackFriday":  (11, 20, 12, 10), # November 20 – December 10
}

SOURCE_WEIGHTS = {
    "cyber.cl": 10, "ccs.cl": 8,
    "El Mostrador": 6, "Meganoticias": 6, "T13": 6, "ADN": 6,
    "Emol": 5, "La Tercera": 5, "DF.cl": 5, "BioBioChile": 5,
    "google_snippet": 3, "deepseek_web_search": 8, "historical_estimation": 1,
}

MIN_WEIGHT_HIGH = 12
MIN_SOURCES_HIGH = 2

CONTEXT_KEYWORDS = [
    "se realizará", "las fechas son", "del", "al", "entre el", "y el",
    "próximo cyberday", "fecha oficial", "anunció", "será el", "comienza",
    "inicia", "termina", "durará", "evento", "oferta", "descuento"
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def today() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

def is_plausible(event: str, date: datetime) -> bool:
    if event not in EVENT_PLAUSIBLE_RANGE:
        return True
    sm, sd, em, ed = EVENT_PLAUSIBLE_RANGE[event]
    return datetime(date.year, sm, sd) <= date <= datetime(date.year, em, ed)

def is_too_close_to_today(date: datetime, days_threshold: int = 2) -> bool:
    return 0 <= (date - today()).days <= days_threshold

def has_event_context(text: str, event: str) -> bool:
    text_lower = text.lower()
    if event.lower() not in text_lower:
        return False
    return any(kw in text_lower for kw in CONTEXT_KEYWORDS)

def parse_date_range(text: str, year: int, event: str) -> Optional[Tuple[datetime, datetime]]:
    for match in DATE_RANGE_RE.finditer(text.lower()):
        try:
            day1 = int(match.group(1))
            day2 = int(match.group(2)) if match.group(2) else day1
            month = MONTH_ES[match.group(3)]
            start = datetime(year, month, day1)
            end = datetime(year, month, day2)
            if end < start:
                end = start + timedelta(days=2)
            if not is_plausible(event, start):
                continue
            if is_too_close_to_today(start):
                continue
            if not has_event_context(text, event):
                continue
            return start, end
        except (ValueError, KeyError):
            continue
    return None

# ---------------------------------------------------------------------------
# Session (robust)
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
# Scrapers (only for the event we need)
# ---------------------------------------------------------------------------
def scrape_cyber_cl(event: str, year: int) -> List[Tuple[datetime, datetime, str]]:
    url = "https://cyber.cl"
    resp = safe_get(url)
    if not resp:
        return []
    time.sleep(POLITE_DELAY)
    soup = BeautifulSoup(resp.text, "html.parser")
    candidates = []
    for selector in [".event-date", ".countdown", ".fecha", "time", "[class*='date']"]:
        for el in soup.select(selector):
            parsed = parse_date_range(el.get_text(" ", strip=True), year, event)
            if parsed:
                candidates.append((parsed[0], parsed[1], "cyber.cl"))
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
# Weighted aggregator
# ---------------------------------------------------------------------------
class WeightedDateAggregator:
    def __init__(self, event: str):
        self.event = event
        self.candidates = []
        self.votes = defaultdict(int)
        self.source_count = defaultdict(set)

    def add_candidates(self, candidates):
        for start, end, source in candidates:
            key = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            w = SOURCE_WEIGHTS.get(source, 1)
            self.votes[key] += w
            self.source_count[key].add(source)
            self.candidates.append((start, end, source))

    def best_candidate(self):
        best_key = None
        best_weight = 0
        best_sources = 0
        for key, w in self.votes.items():
            src_cnt = len(self.source_count[key])
            if w > best_weight or (w == best_weight and src_cnt > best_sources):
                best_weight = w
                best_sources = src_cnt
                best_key = key
        if not best_key:
            return None
        for start, end, source in self.candidates:
            if (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")) == best_key:
                return (start, end, source, best_weight, best_sources)
        return None

# ---------------------------------------------------------------------------
# DeepSeek web search (only for the nearest event)
# ---------------------------------------------------------------------------
def deepseek_fetch(event: str, year: int, api_key: str,
                   base_url: str = "https://api.deepseek.com",
                   model: str = "deepseek-chat") -> Optional[Dict]:
    client = OpenAI(api_key=api_key, base_url=base_url)
    prompt = f"""
You are an expert in Chilean e-commerce. Find the official dates for {event} {year} in Chile.

Current date: {today().strftime('%Y-%m-%d')}

Rules:
- The event dates are NEVER the current day or within 2 days of current date.
- CyberDay occurs in late May or early June (plausible window: May 20 – June 15).
- DO NOT return the whole plausible window as answer.
- Use web search to find news articles, official announcements, or reliable media.
- Return ONLY concrete start and end dates in JSON format.

Example correct output for CyberDay 2026:
{{
  "event": "CyberDay",
  "year": 2026,
  "start_date": "2026-06-01",
  "end_date": "2026-06-03",
  "duration_days": 3,
  "source": "deepseek_web_search",
  "confidence": "medium",
  "notes": "Based on media reports (El Mostrador, Meganoticias)."
}}

Now search for {event} {year} and return ONLY the JSON.
"""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Return only valid JSON with concrete dates."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            extra_body={"enable_search": True}
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```json|```$", "", raw, flags=re.IGNORECASE).strip()
        result = json.loads(raw)
        if result.get("start_date"):
            s = datetime.strptime(result["start_date"], "%Y-%m-%d")
            if is_plausible(event, s) and not is_too_close_to_today(s):
                return result
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
    return None

# ---------------------------------------------------------------------------
# Historical fallback
# ---------------------------------------------------------------------------
def estimate_historically(event: str, year: int) -> Dict:
    if event == "CyberDay":
        may_31 = datetime(year, 5, 31)
        offset = (may_31.weekday() - 0) % 7
        start = may_31 - timedelta(days=offset)
        end = start + timedelta(days=2)
        notes = "Historical estimation: last Monday of May, 3 days."
    elif event == "CyberMonday":
        oct_1 = datetime(year, 10, 1)
        to_mon = (7 - oct_1.weekday()) % 7
        start = oct_1 + timedelta(days=to_mon + 7)
        end = start + timedelta(days=2)
        notes = "Historical estimation: second Monday of October."
    elif event == "BlackFriday":
        nov_30 = datetime(year, 11, 30)
        offset = (nov_30.weekday() - 4) % 7
        start = nov_30 - timedelta(days=offset)
        end = start + timedelta(days=3)
        notes = "Historical estimation: last Friday of November, 4 days."
    else:
        raise ValueError(f"Unknown {event}")
    return {
        "event": event, "year": year,
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
        "duration_days": (end - start).days + 1,
        "source": "historical_estimation",
        "confidence": "low",
        "notes": notes,
    }

# ---------------------------------------------------------------------------
# Main fetcher (nearest event only)
# ---------------------------------------------------------------------------
def get_events_to_fetch(year: int) -> List[str]:
    """Return list of events that are either near or already have reliable data."""
    today_date = today()
    # If we already have a valid cached date for an event, skip it
    cache_path = Path(DEFAULT_OUTPUT)
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                data = json.load(f)
                if data.get("year") == year:
                    events_data = data.get("events", {})
                    # If CyberDay has high/medium confidence and date is plausible, no need to fetch again
                    if "CyberDay" in events_data:
                        cd = events_data["CyberDay"]
                        if cd.get("confidence") in ["high", "medium"]:
                            return []  # nothing to fetch
        except:
            pass
    # Determine which event is closest (by start month)
    # Order by typical month: CyberDay (May/June), CyberMonday (Oct), BlackFriday (Nov)
    # If today < June 15, need CyberDay
    if today_date < datetime(year, 6, 15):
        return ["CyberDay"]
    elif today_date < datetime(year, 10, 20):
        return ["CyberMonday"]
    else:
        return ["BlackFriday"]

def fetch_event(event: str, year: int, api_key: Optional[str] = None,
                force_refresh: bool = False, llm_base_url: str = "https://api.deepseek.com",
                llm_model: str = "deepseek-chat") -> Dict:

    # Cache
    cache_path = Path(DEFAULT_OUTPUT)
    if not force_refresh and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if data.get("year") == year and event in data.get("events", {}):
                cached = data["events"][event]
                d = datetime.strptime(cached["start_date"], "%Y-%m-%d")
                if is_plausible(event, d) and cached.get("confidence") in ["high", "medium"]:
                    logger.info(f"Using cached date for {event}: {cached['start_date']}")
                    return cached
        except Exception:
            pass

    # Scrape
    aggregator = WeightedDateAggregator(event)
    for scraper in [scrape_cyber_cl, scrape_ccs, scrape_media, scrape_google_snippet]:
        try:
            candidates = scraper(event, year)
            if candidates:
                aggregator.add_candidates(candidates)
        except Exception as e:
            logger.warning(f"Scraper {scraper.__name__} error: {e}")

    best = aggregator.best_candidate()
    if best:
        start, end, source, weight, sources = best
        if weight >= MIN_WEIGHT_HIGH and sources >= MIN_SOURCES_HIGH:
            return {
                "event": event, "year": year,
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
                "duration_days": (end - start).days + 1,
                "source": source,
                "confidence": "high",
                "notes": f"Weight {weight} from {sources} sources.",
            }

    # DeepSeek
    if api_key:
        result = deepseek_fetch(event, year, api_key, llm_base_url, llm_model)
        if result:
            return result

    # Fallback
    logger.warning(f"No reliable data for {event}, using historical fallback.")
    return estimate_historically(event, year)

def fetch_all_events(year: int, api_key: Optional[str] = None,
                     output_file: str = DEFAULT_OUTPUT, force_refresh: bool = False,
                     llm_base_url: str = "https://api.deepseek.com",
                     llm_model: str = "deepseek-chat") -> Dict[str, Dict]:
    # Create output directory if needed
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)

    # Load existing data
    existing = {}
    if Path(output_file).exists() and not force_refresh:
        try:
            with open(output_file, "r") as f:
                existing = json.load(f)
                if existing.get("year") != year:
                    existing = {}
        except:
            existing = {}
    events = existing.get("events", {})

    # Determine which events to fetch
    events_to_fetch = get_events_to_fetch(year)
    if not events_to_fetch:
        logger.info("All needed events already have valid cached data. Skipping fetch.")
        return events

    for event in events_to_fetch:
        logger.info(f"Fetching {event} {year}")
        events[event] = fetch_event(event, year, api_key, force_refresh, llm_base_url, llm_model)

    # Fill missing events with historical estimation
    for event in ["CyberDay", "CyberMonday", "BlackFriday"]:
        if event not in events:
            logger.info(f"Using historical estimation for {event} (not fetched)")
            events[event] = estimate_historically(event, year)

    output = {"year": year, "fetched_at": datetime.now().isoformat(), "events": events}
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved to {output_file}")
    return events

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--deepseek-key", help="DeepSeek API key (or env DEEPSEEK_API_KEY)")
    parser.add_argument("--llm-base-url", default="https://api.deepseek.com")
    parser.add_argument("--llm-model", default="deepseek-chat")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    api_key = args.deepseek_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.info("No DeepSeek key – scraping + historical only.")
    fetch_all_events(args.year, api_key, args.output, args.force_refresh,
                     args.llm_base_url, args.llm_model)

if __name__ == "__main__":
    main()
