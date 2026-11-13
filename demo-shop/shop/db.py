import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
 price INTEGER NOT NULL CHECK(price>0), file TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
 product_id INTEGER NOT NULL REFERENCES products(id), name TEXT NOT NULL,
 amount INTEGER NOT NULL CHECK(amount>0), file TEXT NOT NULL, email TEXT NOT NULL,
 provider TEXT NOT NULL, payment_id TEXT UNIQUE, idempotence_key TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','pending','paid','delivered','canceled')),
 created_at REAL NOT NULL, paid_at REAL, delivered_at REAL, admin_notified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS downloads (
 token TEXT PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
 expires_at REAL NOT NULL, uses INTEGER NOT NULL DEFAULT 0 CHECK(uses BETWEEN 0 AND 5));
"""


@contextmanager
def connect(path):
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
