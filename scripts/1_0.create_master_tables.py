#!/usr/bin/env python3
"""
1_0 Create master tables
============================
Creates static reference datasets for the Sales Monitoring Engine:

  - data/products/products_master.db  → table: products (25 products)
  - data/stores/stores_master.db      → tables: stores, location_factors (5 stores, 21 product-store factors)

Usage:
    python 1_0.create_master_tables.py
    python 1_0.create_master_tables.py --products-db custom/products.db --stores-db custom/stores.db
"""

import sqlite3
import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("MasterTables")

# =============================================================================
# PRODUCTS (product_id, name, category, base_demand,
#            promo_sensitivity, weekend_sensitivity, max_stock)
# =============================================================================
PRODUCTS = [
    (1,  "Rice",           "grocery",     120, 0.4, 1.15, 600),
    (2,  "Noodles",        "grocery",     100, 0.6, 1.35, 500),
    (3,  "Oil",            "grocery",      80, 0.3, 1.10, 400),
    (4,  "Flour",          "grocery",      90, 0.5, 1.12, 500),
    (5,  "Sugar",          "grocery",      95, 0.4, 1.18, 500),
    (6,  "Cookies",        "grocery",     150, 0.8, 1.50, 400),
    (7,  "Milk",           "dairy",       110, 0.5, 1.20, 450),
    (8,  "Cheese",         "dairy",        70, 0.5, 1.30, 300),
    (9,  "Butter",         "dairy",        60, 0.4, 1.15, 250),
    (10, "Yogurt",         "dairy",        85, 0.6, 1.25, 350),
    (11, "Bread",          "bakery",      200, 0.3, 1.40, 200),
    (12, "Pastries",       "bakery",       60, 0.7, 1.45, 150),
    (13, "Meat",           "fresh",        70, 0.2, 1.45, 200),
    (14, "Fruits",         "fresh",       120, 0.4, 1.30, 300),
    (15, "Vegetables",     "fresh",       140, 0.3, 1.30, 300),
    (16, "Fish",           "fresh",        45, 0.2, 1.40, 150),
    (17, "Eggs",           "fresh",       100, 0.3, 1.40, 350),
    (18, "Soda",           "beverages",   130, 0.9, 1.60, 500),
    (19, "Juices",         "beverages",    80, 0.7, 1.35, 350),
    (20, "Yerba Mate",     "beverages",    85, 0.2, 1.22, 400),
    (21, "Wines",          "alcohol",      50, 0.8, 1.80, 300),
    (22, "Beer",           "alcohol",      80, 0.9, 2.00, 400),
    (23, "Detergent",      "cleaning",     60, 0.6, 1.20, 400),
    (24, "Bleach",         "cleaning",     40, 0.5, 1.10, 300),
    (25, "Toilet Paper",   "cleaning",     85, 0.7, 1.30, 500),
]

# =============================================================================
# STORES (store_id, name, zone, ticket_multiplier, promo_sensitivity_bonus,
#          vacation_factor_summer, payday_bonus, winter_factor, cyberday_multiplier)
# =============================================================================
STORES = [
    (1, "Santiago Centro", "urban_center",      0.90,  0.10, 0.90, 0.20, 1.00, 1.00),
    (2, "Alto Santiago",   "premium",           1.40, -0.15, 0.70, 0.15, 1.00, 1.00),
    (3, "Santiago Popular", "popular",          0.70,  0.25, 1.00, 0.35, 1.00, 2.00),
    (4, "Rancagua",        "regional_center",   0.85,  0.15, 1.10, 0.20, 1.00, 1.00),
    (5, "Vina del Mar",    "coastal_touristic", 1.00,  0.05, 1.50, 0.20, 0.85, 1.00),
]

# =============================================================================
# LOCATION FACTORS (store_id, product_id, factor)
# =============================================================================
LOCATION_FACTORS = [
    # Santiago Centro
    (1, 11, 1.3),   # Bread +30%
    (1, 21, 1.4),   # Wines +40%
    (1, 22, 1.3),   # Beer +30%
    # Alto Santiago
    (2,  8, 1.6),   # Cheese +60%
    (2, 21, 1.8),   # Wines +80%
    (2, 13, 1.5),   # Meat +50%
    (2,  1, 0.8),   # Rice -20%
    # Santiago Popular
    (3,  1, 1.4),   # Rice +40%
    (3,  2, 1.4),   # Noodles +40%
    (3,  3, 1.3),   # Oil +30%
    (3,  4, 1.3),   # Flour +30%
    (3,  5, 1.3),   # Sugar +30%
    (3, 11, 1.5),   # Bread +50%
    # Rancagua
    (4,  4, 1.3),   # Flour +30%
    (4, 17, 1.3),   # Eggs +30%
    (4, 20, 1.4),   # Yerba Mate +40%
    (4, 13, 1.2),   # Meat +20%
    # Vina del Mar
    (5, 22, 2.2),   # Beer +120%
    (5, 16, 1.8),   # Fish +80%
    (5, 14, 1.6),   # Fruits +60%
    (5, 15, 1.6),   # Vegetables +60%
]

# =============================================================================
# DATABASE CREATION FUNCTIONS
# =============================================================================

def create_products_db(path: Path) -> None:
    """Create products_master.db with table 'products'."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS products")
    cur.execute("""
        CREATE TABLE products (
            product_id          INTEGER PRIMARY KEY,
            name                TEXT    NOT NULL,
            category            TEXT    NOT NULL,
            base_demand         INTEGER NOT NULL,
            promo_sensitivity   REAL    NOT NULL,
            weekend_sensitivity REAL    NOT NULL,
            max_stock           INTEGER NOT NULL
        )
    """)
    cur.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?)", PRODUCTS)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)")
    conn.commit()
    conn.close()
    logger.info(f"products_master.db → {len(PRODUCTS)} products  [{path}]")


def create_stores_db(path: Path) -> None:
    """Create stores_master.db with tables 'stores' and 'location_factors'."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    # --- stores table ---
    cur.execute("DROP TABLE IF EXISTS stores")
    cur.execute("""
        CREATE TABLE stores (
            store_id                INTEGER PRIMARY KEY,
            name                    TEXT    NOT NULL,
            zone                    TEXT    NOT NULL,
            ticket_multiplier       REAL    NOT NULL,
            promo_sensitivity_bonus REAL    NOT NULL,
            vacation_factor_summer  REAL    NOT NULL,
            payday_bonus            REAL    NOT NULL,
            winter_factor           REAL    NOT NULL DEFAULT 1.0,
            cyberday_multiplier     REAL    NOT NULL DEFAULT 1.0
        )
    """)
    cur.executemany("INSERT INTO stores VALUES (?,?,?,?,?,?,?,?,?)", STORES)

    # --- location_factors table ---
    cur.execute("DROP TABLE IF EXISTS location_factors")
    cur.execute("""
        CREATE TABLE location_factors (
            store_id    INTEGER NOT NULL REFERENCES stores(store_id),
            product_id  INTEGER NOT NULL,
            factor      REAL    NOT NULL,
            PRIMARY KEY (store_id, product_id)
        )
    """)
    cur.executemany("INSERT INTO location_factors VALUES (?,?,?)", LOCATION_FACTORS)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_lf_product ON location_factors(product_id)")

    conn.commit()
    conn.close()
    logger.info(f"stores_master.db   → {len(STORES)} stores, {len(LOCATION_FACTORS)} location_factors  [{path}]")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Create static master datasets for Sales Monitoring Engine"
    )
    parser.add_argument(
        "--products-db", type=str,
        default="data/products/products_master.db",
        help="Output path for products database (default: data/products/products_master.db)"
    )
    parser.add_argument(
        "--stores-db", type=str,
        default="data/stores/stores_master.db",
        help="Output path for stores database (default: data/stores/stores_master.db)"
    )
    args = parser.parse_args()

    products_path = Path(args.products_db)
    stores_path = Path(args.stores_db)

    create_products_db(products_path)
    create_stores_db(stores_path)

    logger.info("✅ Master tables created successfully.")


if __name__ == "__main__":
    main()
