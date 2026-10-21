import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, stamp TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('start','end')),
 origin TEXT NOT NULL, precision TEXT NOT NULL DEFAULT 'minute', text_duration INTEGER,
 enabled INTEGER NOT NULL DEFAULT 1, reconciled_to INTEGER, UNIQUE(stamp,kind,origin));
CREATE TABLE IF NOT EXISTS intervals (
 start TEXT PRIMARY KEY, end TEXT, origin TEXT NOT NULL, valid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS issues (id INTEGER PRIMARY KEY, stamp TEXT, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_issues (stamp TEXT NOT NULL, message TEXT NOT NULL, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_runs (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, ok INTEGER NOT NULL, http_status INTEGER,
 changed INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecasts (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, model TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily (
 day TEXT PRIMARY KEY, starts INTEGER NOT NULL, minutes REAL NOT NULL, complete INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (at REAL NOT NULL, address TEXT NOT NULL);
'''


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


def dt(value):
    return datetime.fromisoformat(value) if value else None


@contextmanager
def connect(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(settings.database, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    # Default rollback journal works on PythonAnywhere's shared filesystem.
    try:
        db.executescript(SCHEMA)
        with db:
            yield db
    finally:
        db.close()


def getmeta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def setmeta(db, key, value):
    db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, str(value)))


def audit(db, action, payload):
    db.execute('INSERT INTO audit(at,action,payload) VALUES (?,?,?)',
               (iso(utcnow()), action, json.dumps(payload, ensure_ascii=False)))
