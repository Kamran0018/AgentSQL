"""
Data-Agent AI — Database Setup Script
======================================
Generates an ecommerce.db SQLite database inside agent_app/ with dummy data.
  • products table — 20 rows (id, name, category, price)
  • orders   table — 100 rows (id, product_id, qty, date)

Usage:
    python setup_database.py
"""

import sqlite3
import random
import os
from datetime import datetime, timedelta

# ── Target path: agent_app/ecommerce.db ──
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_app", "ecommerce.db")


def create_database():
    """Create the database, tables, and seed with dummy data."""

    # Remove old database if it exists
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ── Create Tables ──
    cursor.execute("""
        CREATE TABLE products (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT    NOT NULL,
            category TEXT    NOT NULL,
            price    REAL    NOT NULL
        );
    """)

    cursor.execute("""
        CREATE TABLE orders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            qty        INTEGER NOT NULL,
            date       TEXT    NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products (id)
        );
    """)

    # ── Insert 20 Products ──
    products = [
        # Electronics
        ("Wireless Headphones",  "Electronics",    79.99),
        ("Smartphone Pro",       "Electronics",   999.99),
        ("Laptop Ultra",         "Electronics",  1299.99),
        ("Tablet Air",           "Electronics",   499.99),
        ("Smartwatch Elite",     "Electronics",   249.99),
        # Clothing
        ("Running Shoes",        "Clothing",       89.99),
        ("Leather Jacket",       "Clothing",      199.99),
        ("Cotton T-Shirt",       "Clothing",       19.99),
        ("Denim Jeans",          "Clothing",       49.99),
        ("Winter Coat",          "Clothing",      149.99),
        # Home & Kitchen
        ("Coffee Maker",         "Home & Kitchen",  59.99),
        ("Blender Pro",          "Home & Kitchen",  39.99),
        ("Air Purifier",         "Home & Kitchen", 129.99),
        ("Smart Lamp",           "Home & Kitchen",  34.99),
        ("Robot Vacuum",         "Home & Kitchen", 299.99),
        # Books
        ("Python Cookbook",       "Books",           44.99),
        ("Data Science Guide",   "Books",           39.99),
        ("AI Textbook",          "Books",           54.99),
        ("Marketing 101",        "Books",           29.99),
        ("Design Patterns",      "Books",           49.99),
    ]
    cursor.executemany(
        "INSERT INTO products (name, category, price) VALUES (?, ?, ?);",
        products,
    )

    # ── Insert 100 Random Orders ──
    random.seed(42)  # Reproducible results
    start_date = datetime(2024, 1, 1)
    end_date = datetime(2025, 6, 30)
    date_range = (end_date - start_date).days

    orders = []
    for _ in range(100):
        product_id = random.randint(1, 20)
        qty = random.randint(1, 10)
        order_date = start_date + timedelta(days=random.randint(0, date_range))
        orders.append((product_id, qty, order_date.strftime("%Y-%m-%d")))

    cursor.executemany(
        "INSERT INTO orders (product_id, qty, date) VALUES (?, ?, ?);",
        orders,
    )

    conn.commit()
    conn.close()

    print(f"✅ Database created successfully at: {DB_PATH}")
    print(f"   • 20 products inserted")
    print(f"   • 100 orders  inserted")


if __name__ == "__main__":
    create_database()
