"""SQLite storage: DAG records, an API-response cache, and rate-limit events."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import List, Optional

from . import config

_lock = threading.Lock()
_local = threading.local()  # one connection per thread: sharing one across threads crashes SQLite
_generation = 0             # bumped by reset_for_tests so threads reopen against the new path

SCHEMA = """
    CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY,
        pmcid TEXT UNIQUE,
        status TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cache (
        kind TEXT, key TEXT, value TEXT, created_at REAL,
        PRIMARY KEY (kind, key)
    );
    CREATE TABLE IF NOT EXISTS runs (ip TEXT, ts REAL);
"""


def _migrate(c: sqlite3.Connection) -> None:
    cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    if "kind" not in cols:
        c.execute("ALTER TABLE runs ADD COLUMN kind TEXT DEFAULT 'paper'")
        c.commit()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "gen", None) != _generation:
        c = sqlite3.connect(config.DB_PATH, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")   # readers don't block the writer
        c.execute("PRAGMA busy_timeout=15000")
        c.executescript(SCHEMA)
        _migrate(c)
        _local.conn, _local.gen = c, _generation
    return c


def reset_for_tests(path: str) -> None:
    global _generation
    config.DB_PATH = path
    _generation += 1


# --- records ---------------------------------------------------------------

def save_record(rec: dict) -> None:
    now = time.time()
    with _lock:
        conn().execute(
            """INSERT INTO records (id, pmcid, status, data, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data,
               updated_at=excluded.updated_at""",
            (rec["id"], rec["paper"].get("pmcid"), rec["status"], json.dumps(rec), now, now),
        )
        conn().commit()


def get_record(rec_id: str) -> Optional[dict]:
    row = conn().execute("SELECT data FROM records WHERE id=?", (rec_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def find_by_pmcid(pmcid: str) -> Optional[dict]:
    row = conn().execute("SELECT data FROM records WHERE pmcid=?", (pmcid,)).fetchone()
    return json.loads(row["data"]) if row else None


def list_records(status: Optional[str] = None) -> List[dict]:
    if status:
        rows = conn().execute(
            "SELECT data FROM records WHERE status=? ORDER BY updated_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn().execute("SELECT data FROM records ORDER BY updated_at DESC").fetchall()
    return [json.loads(r["data"]) for r in rows]


# --- cache -----------------------------------------------------------------

def cache_get(kind: str, key: str) -> Optional[str]:
    row = conn().execute("SELECT value FROM cache WHERE kind=? AND key=?", (kind, key)).fetchone()
    return row["value"] if row else None


def cache_set(kind: str, key: str, value: str) -> None:
    with _lock:
        conn().execute(
            "INSERT OR REPLACE INTO cache (kind, key, value, created_at) VALUES (?, ?, ?, ?)",
            (kind, key, value, time.time()),
        )
        conn().commit()


# --- rate limiting -----------------------------------------------------------

def count_runs(since: float, ip: Optional[str] = None, kind: str = "paper") -> int:
    q = "SELECT COUNT(*) FROM runs WHERE ts>=? AND COALESCE(kind, 'paper')=?"
    args = [since, kind]
    if ip:
        q += " AND ip=?"
        args.append(ip)
    return conn().execute(q, args).fetchone()[0]


def log_run(ip: str, kind: str = "paper") -> None:
    with _lock:
        conn().execute("INSERT INTO runs (ip, ts, kind) VALUES (?, ?, ?)", (ip, time.time(), kind))
        conn().commit()
