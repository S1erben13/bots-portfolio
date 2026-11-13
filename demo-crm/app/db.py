import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, lead TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT);
                CREATE TABLE IF NOT EXISTS deals (id INTEGER PRIMARY KEY, job_key TEXT UNIQUE, contact_id INTEGER, lead TEXT);
            ''')

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ('lead', 'state'):
            result[key] = json.loads(result[key])
        return result

    def enqueue(self, key, lead):
        with self.connect() as c:
            cursor = c.execute('INSERT OR IGNORE INTO jobs(key,lead,created_at) VALUES(?,?,?)', (key, json.dumps(lead, ensure_ascii=False), time.time()))
            row = c.execute('SELECT id,lead FROM jobs WHERE key=?', (key,)).fetchone()
            if json.loads(row['lead']) != lead:
                raise ValueError('Ключ уже использован для другой заявки')
            return row['id'], cursor.rowcount == 0

    def get(self, id):
        with self.connect() as c:
            return self.decode(c.execute('SELECT * FROM jobs WHERE id=?', (id,)).fetchone())

    def latest(self):
        with self.connect() as c:
            return [self.decode(r) for r in c.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 100')]

    def claim(self):
        now = time.time()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM jobs WHERE (status='pending' AND next_at<=?) OR (status='processing' AND lease_until<?) ORDER BY id LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='processing', attempts=attempts+1, lease_until=? WHERE id=?", (now + 300, row['id']))
            return self.decode(c.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())

    def save(self, id, **fields):
        if not fields or not fields.keys() <= {'status', 'attempts', 'next_at', 'lease_until', 'state', 'error'}:
            raise ValueError('Некорректные поля')
        if 'state' in fields:
            fields['state'] = json.dumps(fields['state'])
        with self.connect() as c:
            c.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', (*fields.values(), id))

    def retry(self, id):
        with self.connect() as c:
            return c.execute("UPDATE jobs SET status='pending',attempts=0,next_at=0,error='' WHERE id=? AND status='failed'", (id,)).rowcount > 0
