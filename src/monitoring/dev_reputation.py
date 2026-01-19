"""
OPTIMIZED Dev Reputation Checker with SQLite cache.
Replaces N+1 RPC calls with 1 Helius Enhanced API call + persistent cache.
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# Setup logging
try:
    from utils.logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# Cache settings
DB_DIR = Path("data")
DB_FILE = DB_DIR / "creator_cache.db"
CACHE_TTL_SECONDS = 3600  # 1 hour

def _init_db():
    """Initialize SQLite cache database."""
    DB_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS creator_cache (
            address TEXT PRIMARY KEY,
            is_safe INTEGER NOT NULL,
            risk_score REAL DEFAULT 0.0,
            tokens_created INTEGER DEFAULT 0,
            reason TEXT,
            last_checked REAL NOT NULL
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_last_checked ON creator_cache(last_checked)')
    conn.commit()
    conn.close()

_init_db()


class DevReputationChecker:
    """OPTIMIZED: Uses Helius Enhanced API (1 request) + SQLite persistent cache."""

    def __init__(
        self,
        helius_api_key: str | None = None,
        max_tokens_created: int = 50,
        min_account_age_days: int = 1,
        enabled: bool = True,
    ):
        self.api_key = helius_api_key or os.getenv("HELIUS_API_KEY")
        self.max_tokens_created = max_tokens_created
        self.min_account_age_days = min_account_age_days
        self.enabled = enabled
        self._api_calls = 0
        self._cache_hits = 0

        if not self.api_key:
            logger.warning("HELIUS_API_KEY not set, dev check disabled")
            self.enabled = False
        else:
            logger.info(f"[DEV] Optimized checker: max_tokens={max_tokens_created}, cache_ttl={CACHE_TTL_SECONDS}s")

    def _get_cached(self, address: str) -> Optional[dict]:
        """Check SQLite cache for creator."""
        try:
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM creator_cache WHERE address = ?", (address,))
            row = c.fetchone()
            conn.close()

            if row and (time.time() - row["last_checked"]) < CACHE_TTL_SECONDS:
                self._cache_hits += 1
                return {
                    "is_safe": bool(row["is_safe"]),
                    "risk_score": row["risk_score"],
                    "tokens_created": row["tokens_created"],
                    "reason": row["reason"] or "From cache",
                }
        except Exception as e:
            logger.debug(f"Cache read error: {e}")
        return None

    def _save_cache(self, address: str, result: dict):
        """Save result to SQLite cache."""
        try:
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0)
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO creator_cache 
                (address, is_safe, risk_score, tokens_created, reason, last_checked)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                address,
                1 if result.get("is_safe", True) else 0,
                result.get("risk_score", 0),
                result.get("tokens_created", 0),
                result.get("reason", ""),
                time.time()
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"Cache write error: {e}")

    async def check_dev(self, creator_address: str) -> dict:
        """Check creator reputation with caching."""
        if not self.enabled:
            return {"is_safe": True, "reason": "Dev check disabled", "risk_score": 0}

        # Check cache first (0 API calls)
        cached = self._get_cached(creator_address)
        if cached:
            logger.debug(f"[DEV] Cache hit for {creator_address[:8]}...")
            return cached

        # Make 1 API call to Helius Enhanced API
        result = await self._analyze_dev(creator_address)
        
        # Save to cache
        self._save_cache(creator_address, result)
        
        return result

    async def _analyze_dev(self, creator_address: str) -> dict:
        """Analyze creator using Helius Enhanced Transactions API (1 request)."""
        self._api_calls += 1
        
        url = f"https://api.helius.xyz/v0/addresses/{creator_address}/transactions"
        params = {"api-key": self.api_key, "limit": 50}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 429:
                        logger.warning(f"[DEV] Rate limited for {creator_address[:8]}")
                        return {"is_safe": True, "reason": "Rate limited", "risk_score": 50, "tokens_created": -1}
                    if resp.status != 200:
                        return {"is_safe": True, "reason": f"API error {resp.status}", "risk_score": 50, "tokens_created": -1}
                    transactions = await resp.json()
        except Exception as e:
            logger.warning(f"[DEV] API error: {e}")
            return {"is_safe": True, "reason": "API error", "risk_score": 50, "tokens_created": -1}

        if not transactions:
            return {"is_safe": True, "reason": "New wallet", "tokens_created": 0, "risk_score": 20}

        # Count token creations and sales
        tokens_created = 0
        tokens_sold = set()
        
        for tx in transactions:
            tx_type = tx.get("type", "")
            if tx_type in ("TOKEN_MINT", "COMPRESSED_NFT_MINT", "NFT_MINT"):
                tokens_created += 1
            
            for transfer in tx.get("tokenTransfers", []):
                if transfer.get("fromUserAccount") == creator_address:
                    mint = transfer.get("mint")
                    if mint:
                        tokens_sold.add(mint)

        # Calculate risk score
        risk_score = 0
        reasons = []

        if tokens_created > self.max_tokens_created:
            risk_score += 40
            reasons.append(f"Too many tokens: {tokens_created}")
        elif tokens_created > 10:
            risk_score += 20
            reasons.append(f"Many tokens: {tokens_created}")

        sold_count = len(tokens_sold)
        if tokens_created > 0 and sold_count / max(tokens_created, 1) > 0.8:
            risk_score += 30
            reasons.append(f"High sell ratio: {sold_count}/{tokens_created}")

        is_safe = risk_score < 50
        reason = "; ".join(reasons) if reasons else "Looks safe"

        logger.info(f"[DEV] {creator_address[:8]}...: tokens={tokens_created}, risk={risk_score}, safe={is_safe}")

        return {
            "is_safe": is_safe,
            "risk_score": risk_score,
            "tokens_created": tokens_created,
            "reason": reason,
        }

    def get_stats(self) -> dict:
        """Get checker statistics."""
        return {
            "api_calls": self._api_calls,
            "cache_hits": self._cache_hits,
            "enabled": self.enabled,
        }
