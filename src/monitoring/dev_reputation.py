"""
Dev Reputation Checker WITHOUT Helius.
Uses: dRPC/Syndica/Alchemy RPC + DexScreener.

v4 - Fixed: Collect pump tokens by suffix, not by program check
"""

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

try:
    from utils.logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

DB_DIR = Path("data")
DB_FILE = DB_DIR / "creator_cache.db"
CACHE_TTL_SECONDS = 3600
HISTORY_CACHE_TTL = 86400


def _init_db():
    DB_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS creator_cache (
        address TEXT PRIMARY KEY, is_safe INTEGER NOT NULL,
        risk_score REAL DEFAULT 0.0, tokens_created INTEGER DEFAULT 0,
        reason TEXT, last_checked REAL NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS creator_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_wallet TEXT NOT NULL, mint TEXT UNIQUE NOT NULL,
        symbol TEXT, name TEXT, created_at REAL,
        reached_raydium INTEGER DEFAULT 0, max_market_cap_usd REAL DEFAULT 0,
        total_volume_usd REAL DEFAULT 0, lifetime_hours REAL DEFAULT 0,
        final_status TEXT DEFAULT 'unknown', creator_sold_pct REAL DEFAULT 0,
        last_updated REAL NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS creator_reputation (
        wallet TEXT PRIMARY KEY, total_tokens_created INTEGER DEFAULT 0,
        successful_tokens INTEGER DEFAULT 0, success_rate REAL DEFAULT 0,
        avg_max_market_cap REAL DEFAULT 0, avg_lifetime_hours REAL DEFAULT 0,
        total_volume_generated REAL DEFAULT 0, avg_creator_sell_pct REAL DEFAULT 0,
        reputation_score INTEGER DEFAULT 50, risk_level TEXT DEFAULT 'unknown',
        last_analyzed REAL NOT NULL
    )''')

    c.execute('CREATE INDEX IF NOT EXISTS idx_creator_wallet ON creator_tokens(creator_wallet)')
    conn.commit()
    conn.close()

_init_db()


@dataclass
class TokenHistory:
    mint: str
    symbol: str
    name: str
    created_at: datetime
    reached_raydium: bool
    max_market_cap_usd: float
    total_volume_usd: float
    lifetime_hours: float
    final_status: str
    creator_sold_pct: float


@dataclass
class CreatorReputation:
    wallet: str
    total_tokens_created: int
    successful_tokens: int
    success_rate: float
    avg_max_market_cap: float
    avg_lifetime_hours: float
    total_volume_generated: float
    avg_creator_sell_pct: float
    reputation_score: int
    risk_level: str
    last_analyzed: datetime
    tokens: list


class DevReputationChecker:
    """Dev Reputation Checker using dRPC/Syndica/Alchemy + DexScreener."""

    def __init__(
        self,
        rpc_endpoint: str | None = None,
        birdeye_api_key: str | None = None,
        max_tokens_created: int = 50,
        min_account_age_days: int = 1,
        enabled: bool = True,
        min_market_cap_for_success: float = 50000,
        min_success_rate_for_buy: float = 0.1,
        max_tokens_suspicious: int = 100,
    ):
        # dRPC/Syndica/Alchemy поддерживают archive, Chainstack - НЕТ!
        self.rpc_endpoint = rpc_endpoint or os.getenv("DRPC_RPC_ENDPOINT") or \
                           os.getenv("SYNDICA_RPC_ENDPOINT") or \
                           os.getenv("ALCHEMY_RPC_ENDPOINT") or \
                           "https://api.mainnet-beta.solana.com"

        self.fallback_endpoints = []
        for env_var in ["SYNDICA_RPC_ENDPOINT", "ALCHEMY_RPC_ENDPOINT"]:
            ep = os.getenv(env_var)
            if ep and ep != self.rpc_endpoint:
                self.fallback_endpoints.append(ep)
        if "api.mainnet-beta.solana.com" not in self.rpc_endpoint:
            self.fallback_endpoints.append("https://api.mainnet-beta.solana.com")

        self.birdeye_api_key = birdeye_api_key or os.getenv("BIRDEYE_API_KEY")
        self.max_tokens_created = max_tokens_created
        self.min_account_age_days = min_account_age_days
        self.enabled = enabled
        self.min_market_cap_for_success = min_market_cap_for_success
        self.min_success_rate_for_buy = min_success_rate_for_buy
        self.max_tokens_suspicious = max_tokens_suspicious

        self._api_calls = 0
        self._cache_hits = 0
        self._rpc_calls = 0
        self._session: aiohttp.ClientSession | None = None

        logger.info(f"[DEV] Init v4: RPC={self.rpc_endpoint[:50]}...")

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_cached(self, address: str) -> Optional[dict]:
        try:
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM creator_cache WHERE address = ?", (address,))
            row = c.fetchone()
            conn.close()
            if row and (time.time() - row["last_checked"]) < CACHE_TTL_SECONDS:
                self._cache_hits += 1
                return {"is_safe": bool(row["is_safe"]), "risk_score": row["risk_score"],
                        "tokens_created": row["tokens_created"], "reason": row["reason"] or "cache"}
        except Exception:
            pass
        return None

    def _save_cache(self, address: str, result: dict):
        try:
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0)
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO creator_cache VALUES (?,?,?,?,?,?)',
                (address, 1 if result.get("is_safe") else 0, result.get("risk_score", 0),
                 result.get("tokens_created", 0), result.get("reason", ""), time.time()))
            conn.commit()
            conn.close()
        except Exception:
            pass

    async def check_dev(self, creator_address: str) -> dict:
        if not self.enabled:
            return {"is_safe": True, "reason": "disabled", "risk_score": 0, "tokens_created": 0}
        cached = self._get_cached(creator_address)
        if cached:
            return cached
        result = await self._analyze_dev_rpc(creator_address)
        self._save_cache(creator_address, result)
        return result

    async def _rpc_call(self, method: str, params: list) -> dict | None:
        session = await self._ensure_session()
        endpoints = [self.rpc_endpoint] + self.fallback_endpoints

        for endpoint in endpoints:
            try:
                async with session.post(endpoint, json={
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params
                }, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    self._rpc_calls += 1
                    if resp.status == 429:
                        continue
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if "error" in data:
                        logger.debug(f"[DEV] RPC error: {data['error']}")
                        continue
                    return data
            except Exception as e:
                logger.debug(f"[DEV] RPC fail: {e}")
                continue
        return None

    async def _analyze_dev_rpc(self, creator_address: str) -> dict:
        data = await self._rpc_call("getSignaturesForAddress",
                                    [creator_address, {"limit": 100, "commitment": "confirmed"}])

        if not data:
            return {"is_safe": True, "reason": "RPC unavailable", "tokens_created": -1, "risk_score": 50}

        signatures = data.get("result", [])

        if not signatures:
            return {"is_safe": True, "reason": "New wallet", "tokens_created": 0, "risk_score": 20}

        tx_count = len(signatures)
        estimated_tokens = tx_count // 3

        risk_score = 0
        reasons = []

        if estimated_tokens > self.max_tokens_created:
            risk_score += 40
            reasons.append(f"Many txs: {tx_count}")
        elif estimated_tokens > 20:
            risk_score += 25
        elif estimated_tokens > 10:
            risk_score += 15

        if signatures:
            oldest = signatures[-1].get("blockTime")
            if oldest:
                age = (time.time() - oldest) / 86400
                if age < self.min_account_age_days:
                    risk_score += 20
                    reasons.append(f"New: {age:.1f}d")

        is_safe = risk_score < 50
        reason = "; ".join(reasons) if reasons else "Looks safe"

        logger.info(f"[DEV] {creator_address[:8]}: {tx_count} txs, risk={risk_score}")

        return {"is_safe": is_safe, "risk_score": risk_score,
                "tokens_created": estimated_tokens, "reason": reason}

    async def analyze_creator_full(self, creator_wallet: str) -> CreatorReputation:
        cached = self._get_cached_reputation(creator_wallet)
        if cached:
            logger.info(f"[DEV] Cache hit: {creator_wallet[:8]} score={cached.reputation_score}")
            return cached

        logger.info(f"[DEV] Full analysis: {creator_wallet[:8]}...")

        token_mints = await self._get_creator_tokens_rpc(creator_wallet)

        if not token_mints:
            logger.info("[DEV] No pump tokens found")
            return self._create_empty_reputation(creator_wallet)

        logger.info(f"[DEV] Found {len(token_mints)} pump tokens, analyzing via DexScreener...")

        token_histories = []
        for mint in token_mints[:20]:
            history = await self._analyze_token_dexscreener(mint, creator_wallet)
            if history:
                token_histories.append(history)
            await asyncio.sleep(0.35)

        reputation = self._calculate_reputation(creator_wallet, token_histories)
        self._save_reputation(reputation)
        return reputation

    async def _get_creator_tokens_rpc(self, creator_wallet: str) -> list[str]:
        """Get pump.fun token mints from wallet transactions."""
        data = await self._rpc_call("getSignaturesForAddress",
                                    [creator_wallet, {"limit": 100, "commitment": "confirmed"}])

        if not data:
            return []

        signatures = data.get("result", [])
        if not signatures:
            return []

        logger.info(f"[DEV] Parsing {len(signatures)} txs for pump tokens...")

        token_mints = []

        for i in range(0, min(len(signatures), 50), 5):
            batch = signatures[i:i+5]
            tasks = [self._get_tx_pump_tokens(s["signature"]) for s in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, list):
                    token_mints.extend(result)

            await asyncio.sleep(0.2)

        # Dedupe
        seen = set()
        unique = [m for m in token_mints if m and m not in seen and not seen.add(m)]
        logger.info(f"[DEV] Found {len(unique)} unique pump tokens")
        return unique

    async def _get_tx_pump_tokens(self, signature: str) -> list[str]:
        """
        Extract pump.fun token mints from transaction.

        v4: Check token addresses ending with 'pump' suffix
        (pump.fun token addresses end with 'pump')
        """
        data = await self._rpc_call("getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0
        }])

        if not data:
            return []

        tx = data.get("result")
        if not tx:
            return []

        mints = []
        meta = tx.get("meta", {})

        # Get all token mints from postTokenBalances
        for bal in meta.get("postTokenBalances", []):
            mint = bal.get("mint", "")
            # pump.fun tokens end with 'pump'
            if mint and mint.endswith("pump") and mint not in mints:
                mints.append(mint)

        # Also check preTokenBalances
        for bal in meta.get("preTokenBalances", []):
            mint = bal.get("mint", "")
            if mint and mint.endswith("pump") and mint not in mints:
                mints.append(mint)

        return mints

    async def _analyze_token_dexscreener(self, mint: str, creator: str) -> TokenHistory | None:
        cached = self._get_cached_token(mint)
        if cached:
            return cached

        session = await self._ensure_session()

        try:
            async with session.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                self._api_calls += 1
                if resp.status != 200:
                    return None

                data = await resp.json()
                pairs = data.get("pairs", [])

                if not pairs:
                    return TokenHistory(mint=mint, symbol="?", name="?", created_at=datetime.now(),
                        reached_raydium=False, max_market_cap_usd=0, total_volume_usd=0,
                        lifetime_hours=0, final_status="dead", creator_sold_pct=0)

                pair = pairs[0]
                base = pair.get("baseToken", {})
                dex = pair.get("dexId", "").lower()
                reached = dex in ("raydium", "pumpswap", "meteora", "orca")
                mc = float(pair.get("marketCap", 0) or 0)
                vol = float(pair.get("volume", {}).get("h24", 0) or 0)

                created = datetime.now()
                if pair.get("pairCreatedAt"):
                    try:
                        created = datetime.fromtimestamp(pair["pairCreatedAt"] / 1000)
                    except Exception:
                        pass

                lifetime = (datetime.now() - created).total_seconds() / 3600

                if reached and mc > 100000:
                    status = "successful"
                elif reached:
                    status = "migrated"
                elif mc > 10000:
                    status = "active"
                elif mc > 1000:
                    status = "declining"
                else:
                    status = "dead"

                history = TokenHistory(
                    mint=mint, symbol=base.get("symbol", "?"), name=base.get("name", "?"),
                    created_at=created, reached_raydium=reached, max_market_cap_usd=mc,
                    total_volume_usd=vol*7, lifetime_hours=lifetime, final_status=status,
                    creator_sold_pct=0
                )

                self._save_token(history, creator)
                return history

        except Exception as e:
            logger.debug(f"[DEV] DexScreener error: {e}")
            return None

    def _calculate_reputation(self, wallet: str, tokens: list[TokenHistory]) -> CreatorReputation:
        if not tokens:
            return self._create_empty_reputation(wallet)

        total = len(tokens)
        successful = sum(1 for t in tokens if t.reached_raydium or t.max_market_cap_usd >= self.min_market_cap_for_success)
        rate = successful / total if total else 0

        avg_mc = sum(t.max_market_cap_usd for t in tokens) / total
        avg_life = sum(t.lifetime_hours for t in tokens) / total
        total_vol = sum(t.total_volume_usd for t in tokens)
        avg_sell = sum(t.creator_sold_pct for t in tokens) / total

        score = 50
        if rate >= 0.5: score += 30
        elif rate >= 0.3: score += 20
        elif rate >= 0.1: score += 10
        elif rate == 0 and total >= 5: score -= 30

        if total > self.max_tokens_suspicious: score -= 25
        elif total > 50: score -= 15
        elif total > 20: score -= 5

        if avg_mc >= 100000: score += 15
        elif avg_mc >= 50000: score += 10
        elif avg_mc < 5000 and total >= 5: score -= 15

        if avg_sell >= 0.9: score -= 25
        elif avg_sell >= 0.7: score -= 15
        elif avg_sell <= 0.3: score += 10

        if avg_life < 1 and total >= 3: score -= 10

        score = max(0, min(100, score))
        risk = "low" if score >= 70 else "medium" if score >= 50 else "high" if score >= 30 else "extreme"

        return CreatorReputation(
            wallet=wallet, total_tokens_created=total, successful_tokens=successful,
            success_rate=rate, avg_max_market_cap=avg_mc, avg_lifetime_hours=avg_life,
            total_volume_generated=total_vol, avg_creator_sell_pct=avg_sell,
            reputation_score=score, risk_level=risk, last_analyzed=datetime.now(), tokens=tokens
        )

    def _create_empty_reputation(self, wallet: str) -> CreatorReputation:
        return CreatorReputation(wallet=wallet, total_tokens_created=0, successful_tokens=0,
            success_rate=0, avg_max_market_cap=0, avg_lifetime_hours=0,
            total_volume_generated=0, avg_creator_sell_pct=0, reputation_score=50,
            risk_level="unknown", last_analyzed=datetime.now(), tokens=[])

    async def should_buy_from_creator(self, creator_wallet: str) -> tuple[bool, str]:
        basic = await self.check_dev(creator_wallet)
        if not basic["is_safe"]:
            return False, basic["reason"]
        if basic["risk_score"] >= 70:
            return False, f"High risk: {basic['risk_score']}"
        if basic["tokens_created"] > self.max_tokens_suspicious:
            return False, "Too many tokens"
        return True, f"OK (risk={basic['risk_score']})"

    def _get_cached_reputation(self, wallet: str) -> CreatorReputation | None:
        try:
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM creator_reputation WHERE wallet = ?", (wallet,))
            row = c.fetchone()
            if row and (time.time() - row["last_analyzed"]) < HISTORY_CACHE_TTL:
                c.execute("SELECT * FROM creator_tokens WHERE creator_wallet = ?", (wallet,))
                tokens = [TokenHistory(
                    mint=t["mint"], symbol=t["symbol"] or "?", name=t["name"] or "?",
                    created_at=datetime.fromtimestamp(t["created_at"]) if t["created_at"] else datetime.now(),
                    reached_raydium=bool(t["reached_raydium"]), max_market_cap_usd=t["max_market_cap_usd"] or 0,
                    total_volume_usd=t["total_volume_usd"] or 0, lifetime_hours=t["lifetime_hours"] or 0,
                    final_status=t["final_status"] or "?", creator_sold_pct=t["creator_sold_pct"] or 0
                ) for t in c.fetchall()]
                conn.close()
                return CreatorReputation(
                    wallet=wallet, total_tokens_created=row["total_tokens_created"],
                    successful_tokens=row["successful_tokens"], success_rate=row["success_rate"],
                    avg_max_market_cap=row["avg_max_market_cap"], avg_lifetime_hours=row["avg_lifetime_hours"],
                    total_volume_generated=row["total_volume_generated"], avg_creator_sell_pct=row["avg_creator_sell_pct"],
                    reputation_score=row["reputation_score"], risk_level=row["risk_level"],
                    last_analyzed=datetime.fromtimestamp(row["last_analyzed"]), tokens=tokens
                )
            conn.close()
        except Exception:
            pass
        return None

    def _get_cached_token(self, mint: str) -> TokenHistory | None:
        try:
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM creator_tokens WHERE mint = ?", (mint,))
            row = c.fetchone()
            conn.close()
            if row and (time.time() - row["last_updated"]) < HISTORY_CACHE_TTL:
                return TokenHistory(
                    mint=row["mint"], symbol=row["symbol"] or "?", name=row["name"] or "?",
                    created_at=datetime.fromtimestamp(row["created_at"]) if row["created_at"] else datetime.now(),
                    reached_raydium=bool(row["reached_raydium"]), max_market_cap_usd=row["max_market_cap_usd"] or 0,
                    total_volume_usd=row["total_volume_usd"] or 0, lifetime_hours=row["lifetime_hours"] or 0,
                    final_status=row["final_status"] or "?", creator_sold_pct=row["creator_sold_pct"] or 0
                )
        except Exception:
            pass
        return None

    def _save_token(self, t: TokenHistory, creator: str):
        try:
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0)
            c = conn.cursor()
            c.execute('''INSERT OR REPLACE INTO creator_tokens
                (creator_wallet,mint,symbol,name,created_at,reached_raydium,max_market_cap_usd,
                 total_volume_usd,lifetime_hours,final_status,creator_sold_pct,last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (creator, t.mint, t.symbol, t.name, t.created_at.timestamp(),
                 1 if t.reached_raydium else 0, t.max_market_cap_usd, t.total_volume_usd,
                 t.lifetime_hours, t.final_status, t.creator_sold_pct, time.time()))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _save_reputation(self, r: CreatorReputation):
        try:
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0)
            c = conn.cursor()
            c.execute('''INSERT OR REPLACE INTO creator_reputation VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (r.wallet, r.total_tokens_created, r.successful_tokens, r.success_rate,
                 r.avg_max_market_cap, r.avg_lifetime_hours, r.total_volume_generated,
                 r.avg_creator_sell_pct, r.reputation_score, r.risk_level, time.time()))
            conn.commit()
            conn.close()
            logger.info(f"[DEV] Saved: {r.wallet[:8]} score={r.reputation_score} tokens={r.total_tokens_created}")
        except Exception:
            pass

    def get_stats(self) -> dict:
        return {"rpc_calls": self._rpc_calls, "api_calls": self._api_calls,
                "cache_hits": self._cache_hits, "enabled": self.enabled,
                "rpc": self.rpc_endpoint[:40] + "..."}
