import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL CHECK(price >= 0), active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE IF NOT EXISTS bakery_settings (
    id INTEGER PRIMARY KEY CHECK(id=1), title TEXT NOT NULL,
    pickup_address TEXT NOT NULL, daily_limit INTEGER NOT NULL CHECK(daily_limit > 0)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    product_name TEXT NOT NULL, product_price INTEGER NOT NULL,
    starts TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','cancelled'))
);
CREATE INDEX IF NOT EXISTS orders_date_status ON orders(starts,status);
CREATE TABLE IF NOT EXISTS order_reminders (
    booking_id INTEGER NOT NULL REFERENCES orders(id), hours INTEGER NOT NULL,
    outbox_id INTEGER NOT NULL REFERENCES outbox(id), PRIMARY KEY(booking_id,hours)
);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    duration INTEGER NOT NULL CHECK(duration > 0 AND duration % 15 = 0),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS masters (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS schedule (
    master_id INTEGER NOT NULL REFERENCES masters(id), weekday INTEGER NOT NULL,
    start TEXT NOT NULL, end TEXT NOT NULL, break_start TEXT, break_end TEXT,
    PRIMARY KEY(master_id, weekday)
);
CREATE TABLE IF NOT EXISTS days_off (
    master_id INTEGER NOT NULL REFERENCES masters(id), day TEXT NOT NULL,
    PRIMARY KEY(master_id, day)
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, service_id INTEGER NOT NULL REFERENCES services(id),
    master_id INTEGER NOT NULL REFERENCES masters(id),
    starts TEXT NOT NULL, ends TEXT NOT NULL,
    name TEXT NOT NULL, phone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK(status IN ('confirmed', 'cancelled', 'attended', 'absent'))
);
CREATE UNIQUE INDEX IF NOT EXISTS booking_start ON bookings(master_id, starts)
    WHERE status != 'cancelled';
CREATE TABLE IF NOT EXISTS occupied (
    master_id INTEGER NOT NULL REFERENCES masters(id), tick TEXT NOT NULL,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    PRIMARY KEY(master_id, tick)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
    body TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
    booking_id INTEGER NOT NULL REFERENCES bookings(id), hours INTEGER NOT NULL,
    outbox_id INTEGER NOT NULL REFERENCES outbox(id),
    PRIMARY KEY(booking_id, hours)
);
"""


@contextmanager
def connect(path):
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
