import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Tuple

DB_DIR = Path("data")
DB_FILE = DB_DIR / "creator_cache.db"

def init_db():
    DB_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS creator_cache (
            address TEXT PRIMARY KEY,
            is_risky INTEGER NOT NULL,
            risk_score REAL DEFAULT 0.0,
            tokens_created INTEGER DEFAULT 0,
            tokens_sold INTEGER DEFAULT 0,
            last_checked REAL NOT NULL,
            details TEXT
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_last_checked ON creator_cache(last_checked)')
    conn.commit()
    conn.close()
    print(f"Cache database initialized at {DB_FILE}")

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(str(DB_FILE), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def get_cached_creator_status(address: str, ttl_seconds: int = 3600) -> Tuple[bool, int, bool]:
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT is_risky, risk_score, last_checked FROM creator_cache WHERE address = ?", (address,))
        row = c.fetchone()
    
    if row:
        is_risky = bool(row["is_risky"])
        risk_score = row["risk_score"] or 0
        last_checked = row["last_checked"]
        if time.time() - last_checked < ttl_seconds:
            return is_risky, int(risk_score), True
    return False, 0, False

def cache_creator_status(address: str, is_risky: bool, risk_score: float = 0.0, tokens_created: int = 0, tokens_sold: int = 0, details: Optional[str] = None):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO creator_cache 
            (address, is_risky, risk_score, tokens_created, tokens_sold, last_checked, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (address, 1 if is_risky else 0, risk_score, tokens_created, tokens_sold, time.time(), details))
        conn.commit()

def cleanup_expired_cache(max_age_hours: int = 24):
    cutoff_time = time.time() - (max_age_hours * 3600)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM creator_cache WHERE last_checked < ?", (cutoff_time,))
        conn.commit()

def get_cache_stats() -> dict:
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM creator_cache")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM creator_cache WHERE is_risky = 1")
        risky = c.fetchone()[0]
    return {"total_entries": total, "risky_creators": risky}

init_db()
