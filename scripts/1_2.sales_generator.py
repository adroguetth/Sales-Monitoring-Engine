#!/usr/bin/env python3
"""
Sales Monitoring Engine - Daily Sales Fact Table Generator
----------------------------------------------------------
Generates daily sales fact table for 5 stores and 25 products using
master reference data from `products_master.db` and `stores_master.db`.

All product and store attributes are loaded from the master databases,
ensuring a single source of truth. The output fact table (`sales_data`)
stores foreign key references logically (no physical FOREIGN KEY constraints
because the dimension tables reside in separate database files).

Output location: data/sales/YYYY-MM-DD.db
"""

import os
import json
import sqlite3
import calendar
import logging
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any
from pathlib import Path
from urllib.request import urlopen

import pandas as pd
import numpy as np
from numpy.random import default_rng
from openai import OpenAI

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SalesGenerator")

rng = default_rng()

# -----------------------------------------------------------------------------
# Paths to master databases (static reference data)
# -----------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
PRODUCTS_MASTER_DB = PROJECT_ROOT / "data" / "products" / "products_master.db"
STORES_MASTER_DB   = PROJECT_ROOT / "data" / "stores" / "stores_master.db"

# Path to event dates JSON (produced by script 1_1)
EVENT_JSON_PATH = PROJECT_ROOT / "data" / "event_date" / "event_dated.json"
EVENT_JSON_URL = "https://raw.githubusercontent.com/adroguetth/Sales-Monitoring-Engine/refs/heads/main/data/event_date/event_dated.json"


# -----------------------------------------------------------------------------
# Helper functions (date, holidays, etc.)
# -----------------------------------------------------------------------------
def get_today() -> datetime:
    """Return current date without time component."""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def get_yesterday(date: datetime = None) -> datetime:
    """Return yesterday's date; if no date provided, use today."""
    if date is None:
        date = get_today()
    return date - timedelta(days=1)


def get_good_friday(year: int) -> datetime:
    """Calculate Good Friday using the Computus algorithm (Gregorian)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter_sunday = datetime(year, month, day)
    return easter_sunday - timedelta(days=2)


def get_maundy_thursday(year: int) -> datetime:
    """Return Maundy Thursday (Thursday before Easter)."""
    return get_good_friday(year) - timedelta(days=1)


def get_store_status(date: datetime) -> Tuple[str, float]:
    """
    Determine store operating status for a given date.
    Returns (status, operation_factor) where status in {"closed","early_close","full_day"}.
    """
    TOTAL_CLOSURE = [
        ("01-01", "New Year"),
        ("05-01", "Labor Day"),
        ("09-18", "Fiestas Patrias"),
        ("09-19", "Army Day"),
        ("12-25", "Christmas"),
    ]
    EARLY_CLOSURE = {
        "04-30": 0.50,   # Labor Day Eve
        "09-17": 0.45,   # Fiestas Patrias Eve
        "12-24": 0.40,   # Christmas Eve
        "12-31": 0.35,   # New Year Eve
    }

    date_str = date.strftime("%m-%d")
    year = date.year

    for d, _ in TOTAL_CLOSURE:
        if date_str == d:
            return ("closed", 0.0)
    if date == get_good_friday(year):
        return ("closed", 0.0)
    if date_str in EARLY_CLOSURE:
        return ("early_close", EARLY_CLOSURE[date_str])
    if date == get_maundy_thursday(year):
        return ("early_close", 0.40)
    return ("full_day", 1.0)


def get_payday_multiplier(date: datetime, store: Dict) -> float:
    """
    Payday effect: first 5 days and last 3 days of month.
    store['payday_bonus'] is the percentage increase (e.g., 0.20 = +20%).
    """
    day = date.day
    _, last_day = calendar.monthrange(date.year, date.month)
    if day <= 5 or day >= (last_day - 2):
        return 1.0 + store["payday_bonus"]
    return 1.0


def get_seasonal_factor(date: datetime, product: Dict, store: Dict) -> float:
    """
    Apply seasonal multipliers: summer (Dec-Feb), winter (Jun-Aug).
    Includes coastal store special effects.
    """
    month = date.month
    if month in [12, 1, 2]:   # Summer
        if store["store_id"] == 5 and product["product_id"] in [14, 15, 18, 19, 22]:
            if product["product_id"] == 22:   # Beer
                return rng.uniform(1.8, 2.2)
            return rng.uniform(1.5, 1.8)
        if product["product_id"] in [14, 15]:   # Fruits, Vegetables
            return rng.uniform(1.15, 1.35)
        if product["product_id"] in [18, 19]:   # Soda, Juices
            return rng.uniform(1.2, 1.4)
        if product["product_id"] == 22:         # Beer
            return rng.uniform(1.3, 1.6)
    elif month in [6, 7, 8]:   # Winter
        if store["store_id"] == 5:
            return store.get("winter_factor", 0.85)
        if product["product_id"] == 20:   # Yerba Mate
            return rng.uniform(1.2, 1.4)
        if product["category"] == "grocery":
            return rng.uniform(1.05, 1.15)
    return 1.0


def get_holiday_pre_factor(date: datetime, product_id: int, year: int) -> float:
    """
    Pre‑holiday demand boost. Increases as the holiday approaches.
    Returns multiplier between 0.5 and 3.5.
    """
    PRE_HOLIDAY_EFFECTS = {
        "new_year": {
            "date_func": lambda y: datetime(y, 1, 1),
            "days_pre": 3,
            "increases": {21: (1.8, 2.5), 22: (1.8, 2.5), 13: (1.5, 2.0),
                          18: (1.3, 1.8), 19: (1.3, 1.6), 15: (1.2, 1.5)},
            "decreases": {1: 0.7, 2: 0.7, 4: 0.7, 5: 0.8}
        },
        "easter": {
            "date_func": lambda y: get_good_friday(y),
            "days_pre": 7,
            "increases": {16: (2.0, 2.8), 4: (1.5, 1.8), 17: (1.4, 1.6),
                          3: (1.3, 1.5), 6: (1.3, 1.5), 12: (1.3, 1.6)},
            "decreases": {13: 0.6, 21: 0.6, 22: 0.6}
        },
        "labor_day": {
            "date_func": lambda y: datetime(y, 5, 1),
            "days_pre": 3,
            "increases": {13: (1.3, 1.5), 21: (1.3, 1.5), 22: (1.3, 1.5), 12: (1.2, 1.4)},
            "decreases": {}
        },
        "fiestas_patrias": {
            "date_func": lambda y: datetime(y, 9, 18),
            "days_pre": 15,
            "increases": {13: (2.5, 3.5), 21: (2.5, 3.2), 22: (2.5, 3.2),
                          18: (1.8, 2.5), 19: (1.6, 2.0), 4: (1.5, 1.8),
                          3: (1.4, 1.6), 17: (1.3, 1.5), 15: (1.2, 1.4)},
            "decreases": {}
        },
        "christmas": {
            "date_func": lambda y: datetime(y, 12, 25),
            "days_pre": 7,
            "increases": {13: (1.8, 2.5), 21: (1.8, 2.8), 22: (1.8, 2.5),
                          18: (1.5, 2.0), 19: (1.4, 1.8), 6: (1.4, 1.7),
                          7: (1.2, 1.4), 17: (1.3, 1.5), 4: (1.3, 1.5),
                          12: (1.5, 2.0)},
            "decreases": {1: 0.8, 2: 0.8}
        }
    }
    factor = 1.0
    for config in PRE_HOLIDAY_EFFECTS.values():
        holiday = config["date_func"](year)
        days_diff = (holiday - date).days
        if 1 <= days_diff <= config["days_pre"]:
            progress = (config["days_pre"] - days_diff + 1) / config["days_pre"]
            if product_id in config["increases"]:
                min_m, max_m = config["increases"][product_id]
                factor *= min_m + (max_m - min_m) * progress
            elif product_id in config.get("decreases", {}):
                factor *= config["decreases"][product_id]
    return min(3.5, max(0.5, factor))


def get_holiday_post_factor(date: datetime, product_id: int, year: int) -> float:
    """
    Post‑holiday demand drop that recovers linearly over days_post.
    Returns multiplier between 0.6 and 1.0.
    """
    POST_HOLIDAY_EFFECTS = {
        "new_year": {"days_post": 3, "base_factor": 0.85},
        "easter": {"days_post": 3, "base_factor": 0.85},
        "labor_day": {"days_post": 0, "base_factor": 1.0},
        "fiestas_patrias": {"days_post": 7, "base_factor": 0.70},
        "christmas": {"days_post": 2, "base_factor": 0.85}
    }
    pre_effects = {
        "new_year": lambda y: datetime(y, 1, 1),
        "easter": lambda y: get_good_friday(y),
        "labor_day": lambda y: datetime(y, 5, 1),
        "fiestas_patrias": lambda y: datetime(y, 9, 18),
        "christmas": lambda y: datetime(y, 12, 25)
    }
    factor = 1.0
    for holiday, config in POST_HOLIDAY_EFFECTS.items():
        holiday_date = pre_effects[holiday](year)
        days_diff = (date - holiday_date).days
        if 1 <= days_diff <= config["days_post"]:
            recovery = days_diff / config["days_post"]
            post_factor = config["base_factor"] + (1 - config["base_factor"]) * recovery
            factor *= post_factor
    return min(1.0, max(0.6, factor))


def get_promotion(product: Dict, date: datetime, store: Dict) -> Tuple[bool, Optional[str], float, Optional[str]]:
    """
    Determine if a promotion is active and return its parameters.
    Returns (is_active, promotion_type, multiplier, promotion_value).
    """
    PROMOTION_TYPES = {
        "2x1": {"base_multiplier": 1.6, "decay": 0.15, "duration_days": 4},
        "3x2": {"base_multiplier": 1.4, "decay": 0.15, "duration_days": 5},
        "bogo_1+1": {"base_multiplier": 1.8, "decay": 0.15, "duration_days": 3},
        "bogo_2+1": {"base_multiplier": 1.5, "decay": 0.15, "duration_days": 3},
        "bogo_3+2": {"base_multiplier": 1.3, "decay": 0.15, "duration_days": 3},
        "discount_10": {"base_multiplier": 1.3, "decay": 0.10, "duration_days": 7},
        "discount_15": {"base_multiplier": 1.5, "decay": 0.10, "duration_days": 7},
        "discount_20": {"base_multiplier": 1.7, "decay": 0.10, "duration_days": 7},
        "discount_30": {"base_multiplier": 2.2, "decay": 0.10, "duration_days": 15},
    }
    category = product["category"]
    promo_sens = product["promo_sensitivity"]
    is_weekend = date.weekday() >= 5
    is_month_end = date.day >= 25
    store_bonus = store.get("promo_sensitivity_bonus", 0.0)

    year = date.year
    is_pre_holiday = False
    pre_events = [
        lambda y: datetime(y, 1, 1), lambda y: get_good_friday(y),
        lambda y: datetime(y, 5, 1), lambda y: datetime(y, 9, 18),
        lambda y: datetime(y, 12, 25)
    ]
    for func in pre_events:
        holiday = func(year)
        days = (holiday - date).days
        if 1 <= days <= 3:
            is_pre_holiday = True
            break

    prob = 0.05
    prob += 0.08 if is_weekend else 0
    prob += 0.05 if is_month_end else 0
    prob += 0.10 if is_pre_holiday else 0
    prob += promo_sens * 0.05
    prob += store_bonus * 0.5
    prob = min(0.35, max(0.02, prob))

    if rng.random() < prob:
        if is_pre_holiday:
            promo_type = rng.choice(["2x1", "discount_20", "discount_30"], p=[0.4, 0.4, 0.2])
            mult = PROMOTION_TYPES[promo_type]["base_multiplier"] * rng.uniform(1.0, 1.2)
            value = None if "discount" not in promo_type else promo_type.split("_")[1]
            return True, promo_type, mult, value
        if is_weekend and category in ["beverages", "alcohol"]:
            promo_type = rng.choice(["2x1", "discount_20"], p=[0.6, 0.4])
            mult = PROMOTION_TYPES[promo_type]["base_multiplier"] * rng.uniform(0.9, 1.1)
            value = None if "discount" not in promo_type else promo_type.split("_")[1]
            return True, promo_type, mult, value
        if category == "fresh":
            promo_type = rng.choice(["discount_10", "discount_20"], p=[0.6, 0.4])
        elif product["product_id"] in [21, 22]:
            promo_type = rng.choice(["2x1", "bogo_2+1", "discount_20"], p=[0.4, 0.3, 0.3])
        elif promo_sens > 0.7:
            promo_type = rng.choice(["2x1", "discount_30", "bogo_1+1"], p=[0.4, 0.3, 0.3])
        else:
            promo_type = rng.choice(["2x1", "3x2", "discount_15"], p=[0.4, 0.3, 0.3])
        mult = PROMOTION_TYPES[promo_type]["base_multiplier"]
        value = None if "discount" not in promo_type else promo_type.split("_")[1]
        return True, promo_type, mult, value
    return False, None, 1.0, None


def get_price(product_id: int, promotion_type: Optional[str] = None) -> float:
    """Return base price, applying discount if promotion is active."""
    base_prices = {
        1: 1200, 2: 800, 3: 1500, 4: 600, 5: 700, 6: 500,
        7: 1000, 8: 2500, 9: 1800, 10: 900, 11: 500, 12: 2500,
        13: 4500, 14: 1800, 15: 1200, 16: 6000, 17: 400, 18: 900,
        19: 800, 20: 1800, 21: 4500, 22: 1200, 23: 1800, 24: 1200,
        25: 2000
    }
    price = base_prices.get(product_id, 1000)
    if promotion_type and "discount" in promotion_type:
        discount_pct = int(promotion_type.split("_")[1]) / 100
        price = price * (1 - discount_pct)
    return price


def get_event_multiplier(date: datetime, event_dates: Dict[str, Dict]) -> float:
    """
    Apply demand multiplier if date falls within a known e‑commerce event.
    Returns 1.5-1.7x for high confidence, 1.3-1.5x for medium, 1.1-1.3x for low.
    """
    date_str = date.strftime("%Y-%m-%d")
    for info in event_dates.values():
        start = info["start_date"]
        end = info["end_date"]
        if start <= date_str <= end:
            confidence = info.get("confidence", "low")
            if confidence == "high":
                return rng.uniform(1.5, 1.7)
            elif confidence == "medium":
                return rng.uniform(1.3, 1.5)
            else:
                return rng.uniform(1.1, 1.3)
    return 1.0


def load_event_dates(year: int) -> Dict[str, Dict]:
    """Load event dates from local JSON or remote URL with fallback."""
    default = {
        "CyberDay": {"start_date": f"{year}-06-01", "end_date": f"{year}-06-03", "confidence": "low"},
        "CyberMonday": {"start_date": f"{year}-10-12", "end_date": f"{year}-10-14", "confidence": "low"},
        "BlackFriday": {"start_date": f"{year}-11-27", "end_date": f"{year}-11-30", "confidence": "low"},
    }
    if EVENT_JSON_PATH.exists():
        try:
            with open(EVENT_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("year") == year:
                return data.get("events", default)
        except Exception as e:
            logger.warning(f"Failed to read local event JSON: {e}")
    try:
        with urlopen(EVENT_JSON_URL) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("year") == year:
            return data.get("events", default)
    except Exception as e:
        logger.warning(f"Failed to fetch remote event JSON: {e}")
    logger.warning("Using default event dates (historical estimation).")
    return default


# -----------------------------------------------------------------------------
# DeepSeek Intelligence (optional)
# -----------------------------------------------------------------------------
class DeepSeekIntelligence:
    def __init__(self, api_key: str, db_path: str):
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        self.db_path = db_path

    def predict_stockout_risk(self, product_id: int, store_id: int,
                              date: datetime, current_stock: int,
                              demand: int, product: Dict) -> float:
        """Predict stockout probability using rule‑based model."""
        is_weekend = date.weekday() >= 5
        base_risk = 0.03 if not is_weekend else 0.08
        store_risks = {1: 0.04, 2: 0.02, 3: 0.06, 4: 0.03, 5: 0.05}
        base_risk += store_risks.get(store_id, 0.03)
        max_stock = product["max_stock"]
        stock_ratio = current_stock / max_stock if max_stock else 1.0
        if stock_ratio < 0.15:
            base_risk += 0.15
        elif stock_ratio < 0.3:
            base_risk += 0.08
        if demand > product["base_demand"] * 1.5:
            base_risk += 0.10
        return min(0.5, base_risk)


# -----------------------------------------------------------------------------
# Main Generator Class
# -----------------------------------------------------------------------------
class SalesDataGenerator:
    """Daily sales fact table generator using master dimension tables."""

    FRESH_PRODUCTS = [11, 12, 13, 14, 15, 16]  # daily restock

    def __init__(self, db_path: str, deepseek_api_key: str = None, year: int = None):
        self.db_path = db_path
        self.year = year or get_today().year
        self.event_dates = load_event_dates(self.year)
        logger.info(f"Loaded event dates: {self.event_dates}")

        # Load master reference data
        self.products = self._load_products_master()
        self.stores = self._load_stores_master()
        self.location_factors = self._load_location_factors()

        # Build fast lookup dictionaries
        self.product_dict = {p["product_id"]: p for p in self.products}
        self.store_dict = {s["store_id"]: s for s in self.stores}

        # Create output directory if needed
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.create_fact_table()
        self.current_stocks = self._initialize_stocks()
        self.active_promotions = {}

        self.use_ai = deepseek_api_key is not None
        if self.use_ai:
            self.ai = DeepSeekIntelligence(deepseek_api_key, db_path)
            logger.info("DeepSeek intelligence activated")
        else:
            logger.info("Running in offline mode (no AI)")

    def _load_products_master(self) -> List[Dict]:
        """Load all products from products_master.db."""
        if not PRODUCTS_MASTER_DB.exists():
            raise FileNotFoundError(f"Products master database not found: {PRODUCTS_MASTER_DB}")
        conn = sqlite3.connect(PRODUCTS_MASTER_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT product_id, name, category, base_demand, promo_sensitivity, weekend_sensitivity, max_stock FROM products")
        rows = cursor.fetchall()
        conn.close()
        products = []
        for row in rows:
            products.append({
                "product_id": row[0],
                "name": row[1],
                "category": row[2],
                "base_demand": row[3],
                "promo_sensitivity": row[4],
                "weekend_sensitivity": row[5],
                "max_stock": row[6],
            })
        logger.info(f"Loaded {len(products)} products from master database")
        return products

    def _load_stores_master(self) -> List[Dict]:
        """Load all stores from stores_master.db."""
        if not STORES_MASTER_DB.exists():
            raise FileNotFoundError(f"Stores master database not found: {STORES_MASTER_DB}")
        conn = sqlite3.connect(STORES_MASTER_DB)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT store_id, name, zone, ticket_multiplier, promo_sensitivity_bonus,
                   vacation_factor_summer, payday_bonus, winter_factor, cyberday_multiplier
            FROM stores
        """)
        rows = cursor.fetchall()
        conn.close()
        stores = []
        for row in rows:
            stores.append({
                "store_id": row[0],
                "name": row[1],
                "zone": row[2],
                "ticket_multiplier": row[3],
                "promo_sensitivity_bonus": row[4],
                "vacation_factor_summer": row[5],
                "payday_bonus": row[6],
                "winter_factor": row[7],
                "cyberday_multiplier": row[8],
            })
        logger.info(f"Loaded {len(stores)} stores from master database")
        return stores

    def _load_location_factors(self) -> Dict[Tuple[int, int], float]:
        """Load location factors mapping (store_id, product_id) -> factor."""
        if not STORES_MASTER_DB.exists():
            return {}
        conn = sqlite3.connect(STORES_MASTER_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT store_id, product_id, factor FROM location_factors")
        rows = cursor.fetchall()
        conn.close()
        factors = {}
        for store_id, product_id, factor in rows:
            factors[(store_id, product_id)] = factor
        logger.info(f"Loaded {len(factors)} location factors")
        return factors

    def _initialize_stocks(self) -> Dict:
        stocks = {}
        for store in self.stores:
            for prod in self.products:
                max_stock = prod["max_stock"]
                stocks[(store["store_id"], prod["product_id"])] = int(max_stock * rng.uniform(0.7, 1.0))
        return stocks

    def create_fact_table(self):
        """
        Create the sales fact table without foreign key constraints.
        Referential integrity is maintained by application logic because
        store_id and product_id are validated against loaded master data.
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sales_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                store_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                units_sold INTEGER NOT NULL,
                price DECIMAL(10,2) NOT NULL,
                revenue DECIMAL(10,2) NOT NULL,
                stock_level INTEGER NOT NULL,
                promotion_flag BOOLEAN NOT NULL,
                promotion_type VARCHAR(30),
                promotion_value VARCHAR(10),
                stockout_flag BOOLEAN NOT NULL,
                day_of_week INTEGER,
                week_of_month INTEGER,
                week_of_year INTEGER,
                month INTEGER,
                year INTEGER,
                is_holiday BOOLEAN,
                is_early_close BOOLEAN,
                operation_factor REAL,
                pre_holiday_factor REAL,
                post_holiday_factor REAL,
                seasonal_factor REAL,
                payday_factor REAL,
                location_factor REAL,
                event_multiplier REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Indexes for analytical queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_date ON sales_data(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_store_product ON sales_data(store_id, product_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_week ON sales_data(year, week_of_year)")
        self.conn.commit()
        logger.info(f"Sales fact table ready at {self.db_path}")

    def _get_promo_key(self, store_id: int, product_id: int) -> str:
        return f"{store_id}_{product_id}"

    def _apply_promotion_decay(self, promo_key: str, current_date: datetime) -> Tuple[bool, float]:
        if promo_key not in self.active_promotions:
            return False, 1.0
        promo = self.active_promotions[promo_key]
        days_active = (current_date - promo["start_date"]).days
        if days_active >= promo["duration_days"]:
            del self.active_promotions[promo_key]
            return False, 1.0
        decay_factor = 1 - (promo["decay_rate"] * days_active)
        current_multiplier = promo["base_multiplier"] * max(0.6, decay_factor)
        return True, current_multiplier

    def _get_post_promo_penalty(self, product_id: int, store_id: int, date: datetime) -> float:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM sales_data 
            WHERE product_id = ? AND store_id = ? 
            AND promotion_flag = 1 
            AND date >= date(?, '-3 days')
            AND date < ?
        """, (product_id, store_id, date.strftime("%Y-%m-%d"), date.strftime("%Y-%m-%d")))
        count = cursor.fetchone()[0]
        if count > 0:
            cursor.execute("SELECT MAX(date) FROM sales_data WHERE product_id=? AND store_id=? AND promotion_flag=1",
                           (product_id, store_id))
            row = cursor.fetchone()
            if row and row[0]:
                last_promo = datetime.strptime(row[0], "%Y-%m-%d")
                days_after = (date - last_promo).days
            else:
                days_after = 3
            penalty = 0.85 + (min(2, days_after) * 0.075)
            return min(1.0, penalty)
        return 1.0

    def _calculate_base_demand(self, product: Dict, date: datetime, store: Dict) -> int:
        base = product["base_demand"]
        is_weekend = date.weekday() >= 5
        weekend_factor = product["weekend_sensitivity"] if is_weekend else 1.0

        if date.day <= 7:
            month_factor = 1.25
        elif date.day >= 22:
            month_factor = 0.85
        else:
            month_factor = 1.0

        year = date.year
        pre_factor = get_holiday_pre_factor(date, product["product_id"], year)
        post_factor = get_holiday_post_factor(date, product["product_id"], year)
        seasonal_factor = get_seasonal_factor(date, product, store)
        payday_factor = get_payday_multiplier(date, store)

        # Location factor from master table
        location_factor = self.location_factors.get((store["store_id"], product["product_id"]), 1.0)
        # Summer vacation boost for coastal store
        if store["store_id"] == 5 and date.month in [12, 1, 2]:
            location_factor *= store.get("vacation_factor_summer", 1.0)

        event_mult = get_event_multiplier(date, self.event_dates)

        noise = rng.normal(1.0, 0.08)
        demand = int(base * weekend_factor * month_factor * pre_factor *
                     post_factor * seasonal_factor * payday_factor *
                     location_factor * event_mult * noise)
        return max(1, demand)

    def generate_daily_sales(self, target_date: datetime) -> pd.DataFrame:
        logger.info(f"Generating sales for {target_date.strftime('%Y-%m-%d')}")
        status, operation_factor = get_store_status(target_date)
        if status == "closed":
            logger.info(f"{target_date.strftime('%Y-%m-%d')} - Store closed (holiday)")
            return pd.DataFrame()
        is_early_close = (status == "early_close")
        if is_early_close:
            logger.info(f"{target_date.strftime('%Y-%m-%d')} - Early closure (factor {operation_factor})")

        records = []
        year = target_date.year
        for store in self.stores:
            store_id = store["store_id"]
            for product in self.products:
                product_id = product["product_id"]
                current_stock = self.current_stocks.get((store_id, product_id), 500)

                base_demand = self._calculate_base_demand(product, target_date, store)
                if is_early_close:
                    base_demand = int(base_demand * operation_factor)

                promo_key = self._get_promo_key(store_id, product_id)
                promo_active, promo_mult = self._apply_promotion_decay(promo_key, target_date)
                promo_type = None
                promo_value = None
                if not promo_active:
                    promo_active, promo_type, promo_mult, promo_value = get_promotion(product, target_date, store)
                    if promo_active:
                        promo_def = {
                            "2x1": {"base_multiplier": 1.6, "decay": 0.15, "duration_days": 4},
                            "3x2": {"base_multiplier": 1.4, "decay": 0.15, "duration_days": 5},
                            "bogo_1+1": {"base_multiplier": 1.8, "decay": 0.15, "duration_days": 3},
                            "bogo_2+1": {"base_multiplier": 1.5, "decay": 0.15, "duration_days": 3},
                            "bogo_3+2": {"base_multiplier": 1.3, "decay": 0.15, "duration_days": 3},
                            "discount_10": {"base_multiplier": 1.3, "decay": 0.10, "duration_days": 7},
                            "discount_15": {"base_multiplier": 1.5, "decay": 0.10, "duration_days": 7},
                            "discount_20": {"base_multiplier": 1.7, "decay": 0.10, "duration_days": 7},
                            "discount_30": {"base_multiplier": 2.2, "decay": 0.10, "duration_days": 15},
                        }
                        info = promo_def.get(promo_type, promo_def["2x1"])
                        self.active_promotions[promo_key] = {
                            "type": promo_type,
                            "value": promo_value,
                            "base_multiplier": promo_mult,
                            "decay_rate": info["decay"],
                            "duration_days": info["duration_days"],
                            "start_date": target_date
                        }
                        promo_mult = info["base_multiplier"]

                theoretical_demand = int(base_demand * promo_mult)
                post_penalty = self._get_post_promo_penalty(product_id, store_id, target_date)
                theoretical_demand = int(theoretical_demand * post_penalty)

                if self.use_ai:
                    stockout_risk = self.ai.predict_stockout_risk(
                        product_id, store_id, target_date, current_stock, theoretical_demand, product
                    )
                else:
                    is_weekend = target_date.weekday() >= 5
                    store_risk = {1: 0.04, 2: 0.02, 3: 0.06, 4: 0.03, 5: 0.05}.get(store_id, 0.03)
                    stockout_risk = store_risk + (0.05 if is_weekend else 0)
                    stockout_risk = min(0.3, stockout_risk)

                if current_stock < theoretical_demand * 0.15 or rng.random() < stockout_risk:
                    if rng.random() < 0.5:
                        units = current_stock
                    else:
                        units = int(theoretical_demand * 0.6)
                        units = min(units, current_stock)
                    stockout_flag = True
                else:
                    units = min(theoretical_demand, current_stock)
                    stockout_flag = False

                new_stock = max(0, current_stock - units)
                self.current_stocks[(store_id, product_id)] = new_stock

                # Restock logic (Monday or fresh products)
                max_stock = product["max_stock"]
                if product_id in self.FRESH_PRODUCTS:
                    new_stock = max_stock
                else:
                    if target_date.weekday() == 0 or new_stock < max_stock * 0.2:
                        new_stock = max_stock
                self.current_stocks[(store_id, product_id)] = new_stock

                price = get_price(product_id, promo_type if promo_active else None)
                revenue = units * price * store["ticket_multiplier"]

                # Capture factors for audit
                pre_factor = get_holiday_pre_factor(target_date, product_id, year)
                post_factor = get_holiday_post_factor(target_date, product_id, year)
                seasonal_factor = get_seasonal_factor(target_date, product, store)
                payday_factor = get_payday_multiplier(target_date, store)
                location_factor = self.location_factors.get((store_id, product_id), 1.0)
                event_mult = get_event_multiplier(target_date, self.event_dates)

                record = {
                    "date": target_date.strftime("%Y-%m-%d"),
                    "store_id": store_id,
                    "product_id": product_id,
                    "units_sold": units,
                    "price": round(price, 2),
                    "revenue": round(revenue, 2),
                    "stock_level": new_stock,
                    "promotion_flag": promo_active,
                    "promotion_type": promo_type if promo_active else None,
                    "promotion_value": promo_value if promo_active else None,
                    "stockout_flag": stockout_flag,
                    "day_of_week": target_date.weekday() + 1,
                    "week_of_month": (target_date.day - 1) // 7 + 1,
                    "week_of_year": target_date.isocalendar()[1],
                    "month": target_date.month,
                    "year": target_date.year,
                    "is_holiday": 1 if status == "closed" else 0,
                    "is_early_close": 1 if is_early_close else 0,
                    "operation_factor": operation_factor,
                    "pre_holiday_factor": round(pre_factor, 2),
                    "post_holiday_factor": round(post_factor, 2),
                    "seasonal_factor": round(seasonal_factor, 2),
                    "payday_factor": round(payday_factor, 2),
                    "location_factor": round(location_factor, 2),
                    "event_multiplier": round(event_mult, 2),
                }
                records.append(record)

        return pd.DataFrame(records)

    def insert_to_database(self, df: pd.DataFrame, target_date: datetime) -> bool:
        """Insert generated sales data into the SQLite fact table."""
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM sales_data WHERE date = ?", (target_date.strftime("%Y-%m-%d"),))
            if not df.empty:
                df.to_sql("sales_data", self.conn, if_exists="append", index=False)
                logger.info(f"Inserted {len(df)} records for {target_date.strftime('%Y-%m-%d')}")
            else:
                logger.info(f"No records for {target_date.strftime('%Y-%m-%d')} (store closed)")
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Database insertion error: {e}")
            return False

    def run_daily(self, target_date: datetime = None) -> bool:
        """Run daily sales generation and insertion. Defaults to yesterday."""
        if target_date is None:
            target_date = get_yesterday()
        df = self.generate_daily_sales(target_date)
        return self.insert_to_database(df, target_date)

    def close(self):
        self.conn.close()


# -----------------------------------------------------------------------------
# Main Entry Point
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Daily Sales Fact Table Generator using master dimensions")
    parser.add_argument("--date", type=str, help="Target date (YYYY-MM-DD). Defaults to yesterday")
    parser.add_argument("--db", type=str, default=None, help="Output database path. Default: data/sales/YYYY-MM-DD.db")
    parser.add_argument("--year", type=int, help="Year for event dates (default: current year)")
    args = parser.parse_args()

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = get_yesterday()
        logger.info(f"No date specified, using yesterday: {target_date.strftime('%Y-%m-%d')}")

    if args.db is None:
        db_dir = PROJECT_ROOT / "data" / "sales"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / f"{target_date.strftime('%Y-%m-%d')}.db"
    else:
        db_path = Path(args.db)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    generator = SalesDataGenerator(db_path=str(db_path), deepseek_api_key=api_key, year=args.year)
    success = generator.run_daily(target_date)
    generator.close()
    return 0 if success else 1


if __name__ == "__main__":
    exit(main())
