"""
Sales Monitoring Engine - Intelligent Sales Data Generator
Fixed and Improved Version

This module generates realistic daily sales data for multiple stores and products,
including seasonal effects, holidays, promotions, stockouts, and AI-powered patterns.
"""

import os
import json
import sqlite3
import calendar
import logging
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional

import pandas as pd
import numpy as np
from numpy.random import default_rng
from openai import OpenAI

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Global random generator for better performance and reproducibility
rng = default_rng()

# ============================================================================
# PRODUCT CONFIGURATION (25 products with fixed codes)
# Structure: product_id: (name, category, base_demand, promo_sensitivity,
#                         weekend_sensitivity, max_stock)
# ============================================================================

PRODUCTS = {
    1: ("Arroz", "almacen", 120, 0.4, 1.15, 600),
    2: ("Fideos", "almacen", 100, 0.6, 1.35, 500),
    3: ("Aceite", "almacen", 80, 0.3, 1.10, 400),
    4: ("Harina", "almacen", 90, 0.5, 1.12, 500),
    5: ("Azucar", "almacen", 95, 0.4, 1.18, 500),
    6: ("Galletas", "almacen", 150, 0.8, 1.50, 400),
    7: ("Leche", "lacteos", 110, 0.5, 1.20, 450),
    8: ("Queso", "lacteos", 70, 0.5, 1.30, 300),
    9: ("Mantequilla", "lacteos", 60, 0.4, 1.15, 250),
    10: ("Yogurt", "lacteos", 85, 0.6, 1.25, 350),
    11: ("Pan", "panaderia", 200, 0.3, 1.40, 200),
    12: ("Pasteles", "panaderia", 60, 0.7, 1.45, 150),
    13: ("Carnes", "frescos", 70, 0.2, 1.45, 200),
    14: ("Frutas", "frescos", 120, 0.4, 1.30, 300),
    15: ("Verduras", "frescos", 140, 0.3, 1.30, 300),
    16: ("Pescado", "frescos", 45, 0.2, 1.40, 150),
    17: ("Huevos", "frescos", 100, 0.3, 1.40, 350),
    18: ("Gaseosas", "bebidas", 130, 0.9, 1.60, 500),
    19: ("Jugos", "bebidas", 80, 0.7, 1.35, 350),
    20: ("Yerba Mate", "bebidas", 85, 0.2, 1.22, 400),
    21: ("Vinos", "alcohol", 50, 0.8, 1.80, 300),
    22: ("Cerveza", "alcohol", 80, 0.9, 2.00, 400),
    23: ("Detergentes", "limpieza", 60, 0.6, 1.20, 400),
    24: ("Cloro", "limpieza", 40, 0.5, 1.10, 300),
    25: ("Papel Higienico", "limpieza", 85, 0.7, 1.30, 500),
}

# ============================================================================
# STORE CONFIGURATION (5 stores with distinct profiles)
# ============================================================================

STORES = {
    1: {
        "name": "Santiago Centro",
        "zone": "urban_center",
        "ticket_multiplier": 0.9,
        "promo_sensitivity_bonus": 0.10,
        "location_factors": {11: 1.3, 21: 1.4, 22: 1.3},
        "vacation_factor_summer": 0.9,
        "payday_bonus": 0.20,
    },
    2: {
        "name": "Alto Santiago",
        "zone": "premium",
        "ticket_multiplier": 1.4,
        "promo_sensitivity_bonus": -0.15,
        "location_factors": {8: 1.6, 21: 1.8, 13: 1.5, 1: 0.8},
        "vacation_factor_summer": 0.7,
        "payday_bonus": 0.15,
    },
    3: {
        "name": "Santiago Popular",
        "zone": "popular",
        "ticket_multiplier": 0.7,
        "promo_sensitivity_bonus": 0.25,
        "location_factors": {1: 1.4, 2: 1.4, 3: 1.3, 4: 1.3, 5: 1.3, 11: 1.5},
        "vacation_factor_summer": 1.0,
        "payday_bonus": 0.35,
        "cyberday_multiplier": 2.0,
    },
    4: {
        "name": "Rancagua",
        "zone": "regional_center",
        "ticket_multiplier": 0.85,
        "promo_sensitivity_bonus": 0.15,
        "location_factors": {4: 1.3, 17: 1.3, 20: 1.4, 13: 1.2},
        "vacation_factor_summer": 1.1,
        "payday_bonus": 0.20,
    },
    5: {
        "name": "Vina del Mar",
        "zone": "coastal_touristic",
        "ticket_multiplier": 1.0,
        "promo_sensitivity_bonus": 0.05,
        "location_factors": {22: 2.2, 16: 1.8, 14: 1.6, 15: 1.6},
        "vacation_factor_summer": 1.5,
        "winter_factor": 0.85,
        "payday_bonus": 0.20,
    }
}

# ============================================================================
# HOLIDAY CONFIGURATION
# ============================================================================

TOTAL_CLOSURE_HOLIDAYS = [
    ("01-01", "New Year"),
    ("05-01", "Labor Day"),
    ("09-18", "Independence Day"),
    ("09-19", "Army Day"),
    ("12-25", "Christmas"),
]

EARLY_CLOSURE_HOLIDAYS = {
    "04-30": {"name": "Labor Day Eve", "factor": 0.50},
    "09-17": {"name": "Independence Eve", "factor": 0.45},
    "12-24": {"name": "Christmas Eve", "factor": 0.40},
    "12-31": {"name": "New Year Eve", "factor": 0.35},
}

PRE_HOLIDAY_EFFECTS = {
    "new_year": {
        "date_func": lambda y: datetime(y, 1, 1),
        "days_pre": 3,
        "increases": {21: (1.8, 2.5), 22: (1.8, 2.5), 13: (1.5, 2.0), 18: (1.3, 1.8), 19: (1.3, 1.6), 15: (1.2, 1.5)},
        "decreases": {1: 0.7, 2: 0.7, 4: 0.7, 5: 0.8}
    },
    "easter": {
        "date_func": lambda y: get_good_friday(y),
        "days_pre": 7,
        "increases": {16: (2.0, 2.8), 4: (1.5, 1.8), 17: (1.4, 1.6), 3: (1.3, 1.5), 6: (1.3, 1.5), 12: (1.3, 1.6)},
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
        "increases": {13: (2.5, 3.5), 21: (2.5, 3.2), 22: (2.5, 3.2), 18: (1.8, 2.5), 19: (1.6, 2.0), 4: (1.5, 1.8), 3: (1.4, 1.6), 17: (1.3, 1.5), 15: (1.2, 1.4)},
        "decreases": {}
    },
    "christmas": {
        "date_func": lambda y: datetime(y, 12, 25),
        "days_pre": 7,
        "increases": {13: (1.8, 2.5), 21: (1.8, 2.8), 22: (1.8, 2.5), 18: (1.5, 2.0), 19: (1.4, 1.8), 6: (1.4, 1.7), 7: (1.2, 1.4), 17: (1.3, 1.5), 4: (1.3, 1.5), 12: (1.5, 2.0)},
        "decreases": {1: 0.8, 2: 0.8}
    }
}

POST_HOLIDAY_EFFECTS = {
    "new_year": {"days_post": 3, "base_factor": 0.85},
    "easter": {"days_post": 3, "base_factor": 0.85},
    "labor_day": {"days_post": 0, "base_factor": 1.0},
    "fiestas_patrias": {"days_post": 7, "base_factor": 0.70},
    "christmas": {"days_post": 2, "base_factor": 0.85}
}

# ============================================================================
# PROMOTION CONFIGURATION
# ============================================================================

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

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_today() -> datetime:
    """Returns current date without time component."""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

def get_yesterday(date: datetime = None) -> datetime:
    """Returns yesterday's date. If no date provided, uses today."""
    if date is None:
        date = get_today()
    return date - timedelta(days=1)

def get_good_friday(year: int) -> datetime:
    """
    Calculates Good Friday date using Computus algorithm (Gregorian).
    Returns date of Good Friday (Friday before Easter Sunday).
    """
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
    """Returns Maundy Thursday (Thursday before Easter, early closure day)."""
    return get_good_friday(year) - timedelta(days=1)

def get_store_status(date: datetime) -> Tuple[str, float]:
    """
    Determines store operating status for a given date.
    Returns: (status, operation_factor)
    status: "closed" | "early_close" | "full_day"
    """
    date_str = date.strftime("%m-%d")
    year = date.year

    # Total closure check
    for closure_date, _ in TOTAL_CLOSURE_HOLIDAYS:
        if date_str == closure_date:
            return ("closed", 0.0)

    # Good Friday total closure
    if date == get_good_friday(year):
        return ("closed", 0.0)

    # Early closure (fixed dates)
    if date_str in EARLY_CLOSURE_HOLIDAYS:
        return ("early_close", EARLY_CLOSURE_HOLIDAYS[date_str]["factor"])

    # Maundy Thursday early closure
    if date == get_maundy_thursday(year):
        return ("early_close", 0.40)

    return ("full_day", 1.0)

def get_payday_multiplier(date: datetime, store_id: int) -> float:
    """
    Calculates payday effect multiplier.
    End of month (last 3 days) and first 5 days of month.
    Fixed to correctly handle December using calendar.monthrange.
    """
    day = date.day
    # Safe way to get last day of any month
    _, last_day = calendar.monthrange(date.year, date.month)

    if day <= 5:
        return 1.0 + STORES[store_id]["payday_bonus"]
    elif day >= (last_day - 2):
        return 1.0 + STORES[store_id]["payday_bonus"]
    return 1.0

def get_seasonal_factor(date: datetime, product_id: int, store_id: int) -> float:
    """
    Calculates seasonal effects based on month and store location.
    Summer: Dec-Feb, Winter: Jun-Aug
    """
    month = date.month
    store_data = STORES[store_id]
    product_name, category, _, _, _, _ = PRODUCTS[product_id]

    # Summer season (December - February)
    if month in [12, 1, 2]:
        # Coastal store gets extra summer boost
        if store_id == 5 and product_id in [14, 15, 18, 19, 22]:
            if product_id == 22:  # Beer
                return rng.uniform(1.8, 2.2)
            return rng.uniform(1.5, 1.8)

        if product_id in [14, 15]:  # Fruits, Vegetables
            return rng.uniform(1.15, 1.35)
        if product_id in [18, 19]:  # Sodas, Juices
            return rng.uniform(1.2, 1.4)
        if product_id == 22:  # Beer
            return rng.uniform(1.3, 1.6)

    # Winter season (June - August)
    elif month in [6, 7, 8]:
        if store_id == 5:  # Coastal winter reduction
            return STORES[5].get("winter_factor", 0.85)
        if product_id == 20:  # Yerba Mate
            return rng.uniform(1.2, 1.4)
        if category == "almacen":
            return rng.uniform(1.05, 1.15)

    return 1.0

def get_holiday_pre_factor(date: datetime, product_id: int, year: int) -> float:
    """Calculates pre-holiday demand multiplier with progressive increase."""
    factor = 1.0

    for holiday_key, config in PRE_HOLIDAY_EFFECTS.items():
        holiday_date = config["date_func"](year)
        days_diff = (holiday_date - date).days

        if 1 <= days_diff <= config["days_pre"]:
            progress = (config["days_pre"] - days_diff + 1) / config["days_pre"]

            if product_id in config["increases"]:
                min_mult, max_mult = config["increases"][product_id]
                boost = min_mult + (max_mult - min_mult) * progress
                factor *= boost
            elif product_id in config.get("decreases", {}):
                factor *= config["decreases"][product_id]

    return min(3.5, max(0.5, factor))

def get_holiday_post_factor(date: datetime, product_id: int, year: int) -> float:
    """Calculates post-holiday demand reduction with progressive recovery."""
    factor = 1.0

    for holiday_key, config in POST_HOLIDAY_EFFECTS.items():
        holiday_date = PRE_HOLIDAY_EFFECTS[holiday_key]["date_func"](year)
        days_diff = (date - holiday_date).days

        if 1 <= days_diff <= config["days_post"]:
            recovery_progress = days_diff / config["days_post"]
            post_factor = config["base_factor"] + (1 - config["base_factor"]) * recovery_progress
            factor *= post_factor

    return min(1.0, max(0.6, factor))

def get_promotion(product_id: int, date: datetime, store_id: int) -> Tuple[bool, Optional[str], float, Optional[str]]:
    """
    Determines if a promotion is active and returns its parameters.
    Returns: (is_active, promotion_type, multiplier, promotion_value)
    """
    product_name, category, _, promo_sens, _, _ = PRODUCTS[product_id]
    is_weekend = date.weekday() >= 5
    is_month_end = date.day >= 25
    store_data = STORES[store_id]

    # Check if pre-holiday (higher promo probability)
    year = date.year
    is_pre_holiday = False
    for config in PRE_HOLIDAY_EFFECTS.values():
        holiday_date = config["date_func"](year)
        days_diff = (holiday_date - date).days
        if 1 <= days_diff <= 3:
            is_pre_holiday = True
            break

    # Base probability calculation
    prob = 0.05
    prob += 0.08 if is_weekend else 0
    prob += 0.05 if is_month_end else 0
    prob += 0.10 if is_pre_holiday else 0
    prob += promo_sens * 0.05
    prob += store_data["promo_sensitivity_bonus"] * 0.5

    prob = min(0.35, max(0.02, prob))

    if rng.random() < prob:
        # Pre-holiday: aggressive promotions
        if is_pre_holiday:
            promo_type = rng.choice(["2x1", "discount_20", "discount_30"], p=[0.4, 0.4, 0.2])
            multiplier = PROMOTION_TYPES[promo_type]["base_multiplier"] * rng.uniform(1.0, 1.2)
            promo_value = None if "discount" not in promo_type else promo_type.split("_")[1]
            return True, promo_type, multiplier, promo_value

        # Weekend: focus on beverages and alcohol
        if is_weekend and category in ["bebidas", "alcohol"]:
            promo_type = rng.choice(["2x1", "discount_20"], p=[0.6, 0.4])
            multiplier = PROMOTION_TYPES[promo_type]["base_multiplier"] * rng.uniform(0.9, 1.1)
            promo_value = None if "discount" not in promo_type else promo_type.split("_")[1]
            return True, promo_type, multiplier, promo_value

        # Standard promotion selection
        if category == "frescos":
            promo_type = rng.choice(["discount_10", "discount_20"], p=[0.6, 0.4])
        elif product_id in [21, 22]:  # Wines, Beer
            promo_type = rng.choice(["2x1", "bogo_2+1", "discount_20"], p=[0.4, 0.3, 0.3])
        elif promo_sens > 0.7:
            promo_type = rng.choice(["2x1", "discount_30", "bogo_1+1"], p=[0.4, 0.3, 0.3])
        else:
            promo_type = rng.choice(["2x1", "3x2", "discount_15"], p=[0.4, 0.3, 0.3])

        multiplier = PROMOTION_TYPES.get(promo_type, PROMOTION_TYPES["2x1"])["base_multiplier"]
        promo_value = None if "discount" not in promo_type else promo_type.split("_")[1]
        return True, promo_type, multiplier, promo_value

    return False, None, 1.0, None

def get_price(product_id: int, promotion_type: Optional[str] = None) -> float:
    """Returns base price with discount if promotion is active."""
    base_prices = {
        1: 1200, 2: 800, 3: 1500, 4: 600, 5: 700, 6: 500, 7: 1000, 8: 2500, 9: 1800,
        10: 900, 11: 500, 12: 2500, 13: 4500, 14: 1800, 15: 1200, 16: 6000, 17: 400,
        18: 900, 19: 800, 20: 1800, 21: 4500, 22: 1200, 23: 1800, 24: 1200, 25: 2000
    }
    price = base_prices.get(product_id, 1000)

    if promotion_type and "discount" in promotion_type:
        discount_pct = int(promotion_type.split("_")[1]) / 100
        price = price * (1 - discount_pct)

    return price

# ============================================================================
# DEEPSEEK INTELLIGENCE ENGINE
# ============================================================================

class DeepSeekIntelligence:
    """AI-powered pattern recognition and realistic behavior generation."""

    def __init__(self, api_key: str, db_path: str):
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        self.db_path = db_path
        self.patterns_cache = {}

    def learn_from_history(self, days_back: int = 90) -> Dict:
        """
        Analyzes historical sales data to extract real patterns.
        Returns pattern dictionary for intelligent generation.
        """
        conn = sqlite3.connect(self.db_path)

        query = f"""
        SELECT date, store_id, product_id, units_sold, promotion_flag, 
               promotion_type, stockout_flag, day_of_week, is_holiday
        FROM sales_data 
        WHERE date >= date('now', '-{days_back} days')
        ORDER BY date
        """

        df = pd.read_sql_query(query, conn)
        conn.close()

        if df.empty:
            return self._get_default_patterns()

        # Calculate key metrics for pattern analysis
        metrics = {
            "sales_by_dow": df.groupby('day_of_week')['units_sold'].mean().to_dict(),
            "promo_impact": df[df['promotion_flag']==1].groupby('promotion_type')['units_sold'].agg(['mean', 'count']).to_dict(),
            "stockout_by_dow": df[df['stockout_flag']==1]['day_of_week'].value_counts().to_dict(),
            "sales_by_product": df.groupby('product_id')['units_sold'].mean().to_dict(),
        }

        return metrics

    def _get_default_patterns(self) -> Dict:
        """Default patterns when no historical data exists."""
        return {
            "sales_by_dow": {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 1.4, 6: 1.3},
            "stockout_probability": 0.05,
            "promo_effectiveness": 1.6
        }

    def predict_stockout_risk(self, product_id: int, store_id: int,
                              date: datetime, current_stock: int,
                              demand: int) -> float:
        """
        Predicts stockout risk based on historical patterns and current conditions.
        Returns probability between 0 and 1.
        """
        is_weekend = date.weekday() >= 5

        base_risk = 0.03 if not is_weekend else 0.08

        # Store-specific risk adjustment
        store_risks = {1: 0.04, 2: 0.02, 3: 0.06, 4: 0.03, 5: 0.05}
        base_risk += store_risks.get(store_id, 0.03)

        # Stock level adjustment
        stock_ratio = current_stock / PRODUCTS[product_id][5]
        if stock_ratio < 0.15:
            base_risk += 0.15
        elif stock_ratio < 0.3:
            base_risk += 0.08

        # Demand spike adjustment
        if demand > PRODUCTS[product_id][2] * 1.5:
            base_risk += 0.10

        return min(0.5, base_risk)

# ============================================================================
# MAIN SALES GENERATOR CLASS
# ============================================================================

class SalesDataGenerator:
    """Intelligent sales data generator with DeepSeek integration."""

    # Products that are restocked daily (fresh goods)
    FRESH_PRODUCTS = [11, 12, 13, 14, 15, 16]  # Pan, Pasteles, Carnes, Frutas, Verduras, Pescado

    def __init__(self, db_path: str = "sales_monitoring.db", deepseek_api_key: str = None):
        self.conn = sqlite3.connect(db_path)
        self.stores = list(STORES.keys())
        self.products = PRODUCTS
        self.create_tables()
        self.current_stocks = self._initialize_stocks()
        # Fixed promotion key: no month component, promotions survive month boundaries
        self.active_promotions = {}  # key: (store_id, product_id) -> promotion data

        if deepseek_api_key:
            self.ai = DeepSeekIntelligence(deepseek_api_key, db_path)
            self.use_ai = True
            self.historical_patterns = self.ai.learn_from_history()
            logging.info("DeepSeek intelligence activated")
        else:
            self.use_ai = False
            logging.info("Running in offline mode (no AI)")

    def _initialize_stocks(self) -> Dict:
        """Initializes stock levels for all stores and products."""
        stocks = {}
        for store_id in self.stores:
            for product_id, (_, _, _, _, _, max_stock) in PRODUCTS.items():
                stocks[(store_id, product_id)] = int(max_stock * rng.uniform(0.7, 1.0))
        return stocks

    def create_tables(self):
        """Creates database tables and indexes if they don't exist."""
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_date ON sales_data(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_store_product ON sales_data(store_id, product_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_week ON sales_data(year, week_of_year)")

        self.conn.commit()
        logging.info("Database tables verified")

    def _get_promo_key(self, store_id: int, product_id: int) -> Tuple[int, int]:
        """Returns a stable key for active promotions (store+product only, no month)."""
        return (store_id, product_id)

    def _apply_promotion_decay(self, promo_key: Tuple[int, int], current_date: datetime) -> Tuple[bool, float]:
        """
        Manages active promotion decay over time.
        Returns (is_active, current_multiplier)
        """
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
        """
        Applies post-promotion demand penalty (15% drop for 2-3 days after promotion).
        """
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
            # First day after promo: 15% drop, recovers linearly
            cursor.execute(
                "SELECT MAX(date) FROM sales_data WHERE product_id=? AND store_id=? AND promotion_flag=1",
                (product_id, store_id)
            )
            row = cursor.fetchone()
            if row and row[0]:
                last_promo_date = datetime.strptime(row[0], "%Y-%m-%d")
                days_after = (date - last_promo_date).days
            else:
                days_after = 3  # safe default: no penalty applied
            penalty = 0.85 + (min(2, days_after) * 0.075)  # Returns to 1.0 after 2 days
            return min(1.0, penalty)

        return 1.0

    def _calculate_base_demand(self, product_id: int, date: datetime, store_id: int) -> int:
        """
        Calculates base demand with all seasonal and holiday factors.
        Does NOT include promotion multiplier (applied separately).
        """
        _, _, base_demand, _, weekend_sens, _ = PRODUCTS[product_id]
        is_weekend = date.weekday() >= 5
        year = date.year

        # Weekend factor
        weekend_factor = weekend_sens if is_weekend else 1.0

        # Month factor (first week up, last week down)
        if date.day <= 7:
            month_factor = 1.25
        elif date.day >= 22:
            month_factor = 0.85
        else:
            month_factor = 1.0

        # Holiday factors
        pre_factor = get_holiday_pre_factor(date, product_id, year)
        post_factor = get_holiday_post_factor(date, product_id, year)

        # Seasonal factor
        seasonal_factor = get_seasonal_factor(date, product_id, store_id)

        # Payday factor
        payday_factor = get_payday_multiplier(date, store_id)

        # Store location factor
        store_data = STORES[store_id]
        location_factor = store_data["location_factors"].get(product_id, 1.0)

        # Summer vacation factor for coastal store
        if store_id == 5 and date.month in [12, 1, 2]:
            location_factor *= store_data["vacation_factor_summer"]

        # Random noise (±8% normal distribution)
        noise = rng.normal(1.0, 0.08)

        demand = int(base_demand * weekend_factor * month_factor * pre_factor *
                     post_factor * seasonal_factor * payday_factor * location_factor * noise)

        return max(1, demand)

    def generate_daily_sales(self, target_date: datetime) -> pd.DataFrame:
        """
        Generates complete sales data for a single day across all stores and products.
        Returns DataFrame with all sales records.
        """
        logging.info(f"Generating sales for {target_date.strftime('%Y-%m-%d')}")

        status, operation_factor = get_store_status(target_date)

        if status == "closed":
            logging.info(f"{target_date.strftime('%Y-%m-%d')} - Store closed (holiday)")
            return pd.DataFrame()

        is_early_close = (status == "early_close")
        if is_early_close:
            logging.info(f"{target_date.strftime('%Y-%m-%d')} - Early closure at 18:00 (factor {operation_factor})")

        records = []
        year = target_date.year

        for store_id in self.stores:
            for product_id, product_info in PRODUCTS.items():
                current_stock = self.current_stocks.get((store_id, product_id), 500)

                # Calculate base demand
                base_demand = self._calculate_base_demand(product_id, target_date, store_id)

                # Apply early closure factor
                if is_early_close:
                    base_demand = int(base_demand * operation_factor)

                # Check for active promotion using month-independent key
                promo_key = self._get_promo_key(store_id, product_id)
                promo_active, promo_multiplier = self._apply_promotion_decay(promo_key, target_date)

                # If no active promotion, check if we should start a new one
                promo_type = None
                promo_value = None
                if not promo_active:
                    promo_active, promo_type, promo_multiplier, promo_value = get_promotion(
                        product_id, target_date, store_id
                    )
                    if promo_active:
                        promo_info = PROMOTION_TYPES.get(promo_type, PROMOTION_TYPES["2x1"])
                        self.active_promotions[promo_key] = {
                            "type": promo_type,
                            "value": promo_value,
                            "base_multiplier": promo_multiplier,
                            "decay_rate": promo_info["decay"],
                            "duration_days": promo_info["duration_days"],
                            "start_date": target_date
                        }
                        # Use base multiplier from config (without decay on first day)
                        promo_multiplier = promo_info["base_multiplier"]

                # Calculate theoretical demand with promotion
                theoretical_demand = int(base_demand * promo_multiplier)

                # Apply post-promotion penalty
                post_promo_penalty = self._get_post_promo_penalty(product_id, store_id, target_date)
                theoretical_demand = int(theoretical_demand * post_promo_penalty)

                # Predict stockout risk (AI or rule-based)
                if self.use_ai:
                    stockout_risk = self.ai.predict_stockout_risk(
                        product_id, store_id, target_date, current_stock, theoretical_demand
                    )
                else:
                    is_weekend = target_date.weekday() >= 5
                    store_risk = {1: 0.04, 2: 0.02, 3: 0.06, 4: 0.03, 5: 0.05}.get(store_id, 0.03)
                    stockout_risk = store_risk + (0.05 if is_weekend else 0)
                    stockout_risk = min(0.3, stockout_risk)

                # Apply stockout logic - FIXED: never sell more than available stock
                if current_stock < theoretical_demand * 0.15 or rng.random() < stockout_risk:
                    # Stockout occurred
                    if rng.random() < 0.5:
                        units_sold = current_stock          # sell everything
                    else:
                        units_sold = int(theoretical_demand * 0.6)
                        # Never sell more than available stock
                        units_sold = min(units_sold, current_stock)
                    stockout_flag = True
                else:
                    units_sold = min(theoretical_demand, current_stock)
                    stockout_flag = False

                # Update stock levels
                new_stock = max(0, current_stock - units_sold)
                self.current_stocks[(store_id, product_id)] = new_stock

                # Inventory restock logic (improved)
                _, _, _, _, _, max_stock = PRODUCTS[product_id]
                if product_id in self.FRESH_PRODUCTS:
                    # Fresh products restock every day
                    new_stock = max_stock
                else:
                    # Restock on Monday OR when stock falls below 20% of max
                    if target_date.weekday() == 0 or new_stock < max_stock * 0.2:
                        new_stock = max_stock
                self.current_stocks[(store_id, product_id)] = new_stock

                # Price calculation
                price = get_price(product_id, promo_type if promo_active else None)
                revenue = units_sold * price

                # Store location multiplier for revenue (ticket factor)
                location_revenue_factor = STORES[store_id]["ticket_multiplier"]
                revenue = revenue * location_revenue_factor

                # Collect factors for audit
                pre_factor = get_holiday_pre_factor(target_date, product_id, year)
                post_factor = get_holiday_post_factor(target_date, product_id, year)
                seasonal_factor = get_seasonal_factor(target_date, product_id, store_id)
                payday_factor = get_payday_multiplier(target_date, store_id)
                location_factor = STORES[store_id]["location_factors"].get(product_id, 1.0)

                record = {
                    "date": target_date.strftime("%Y-%m-%d"),
                    "store_id": store_id,
                    "product_id": product_id,
                    "units_sold": units_sold,
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
                }
                records.append(record)

        return pd.DataFrame(records)

    def insert_to_database(self, df: pd.DataFrame, target_date: datetime) -> bool:
        """Inserts generated sales data into SQLite database."""
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM sales_data WHERE date = ?", (target_date.strftime("%Y-%m-%d"),))

            if not df.empty:
                df.to_sql("sales_data", self.conn, if_exists="append", index=False)
                logging.info(f"Inserted {len(df)} records for {target_date.strftime('%Y-%m-%d')}")
            else:
                logging.info(f"No records for {target_date.strftime('%Y-%m-%d')} (store closed)")

            self.conn.commit()
            return True
        except Exception as e:
            logging.error(f"Database insertion error: {e}")
            return False

    def run_daily(self, target_date: datetime = None) -> bool:
        """
        Main execution method.
        If no date provided, generates for yesterday (previous day).
        Returns True if successful.
        """
        if target_date is None:
            target_date = get_yesterday()

        df = self.generate_daily_sales(target_date)
        return self.insert_to_database(df, target_date)

    def close(self):
        """Closes database connection."""
        self.conn.close()

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    """
    Main execution function for GitHub Actions.
    Reads DEEPSEEK_API_KEY from environment variables.
    Generates sales data for yesterday by default.
    """
    parser = argparse.ArgumentParser(description='Sales Monitoring Engine - Intelligent Data Generator')
    parser.add_argument('--date', type=str, help='Target date (YYYY-MM-DD). Defaults to yesterday')
    parser.add_argument('--db', type=str, default='sales_monitoring.db', help='Database file path')

    args = parser.parse_args()

    # Get DeepSeek API key from environment (GitHub Secrets)
    api_key = os.environ.get("DEEPSEEK_API_KEY")

    if api_key:
        logging.info("DeepSeek API key found - running in intelligent mode")
    else:
        logging.warning("DEEPSEEK_API_KEY not found - running in offline mode")

    generator = SalesDataGenerator(db_path=args.db, deepseek_api_key=api_key)

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = get_yesterday()
        logging.info(f"No date specified, generating for yesterday: {target_date.strftime('%Y-%m-%d')}")

    success = generator.run_daily(target_date)
    generator.close()

    if success:
        logging.info(f"Sales data generation completed for {target_date.strftime('%Y-%m-%d')}")
    else:
        logging.error(f"Sales data generation failed for {target_date.strftime('%Y-%m-%d')}")

    return 0 if success else 1

if __name__ == "__main__":
    exit(main())
