"""Deployer blacklist — preloads recent mints from blacklisted deployers.
Check is O(1) set lookup = instant.
Background refresh every 5 min catches new scam tokens.

PATCH 10 (Session 5):
- Sequential Helius requests with 200ms delay (was parallel → 429 errors)
- Retry with backoff on 429/5xx
- Cache preservation: never wipe _blocked_mints on failed refresh
- Added get_deployer_label() helper
"""
import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

BLACKLIST_FILE = Path(__file__).resolve().parents[2] / "blacklisted_deployers.json"
REFRESH_INTERVAL = 300  # 5 min
MINTS_PER_DEPLOYER = 50  # last 50 tokens per deployer
INITIAL_DELAY = 5  # start fetching 5s after bot start
REQUEST_DELAY = 0.25  # 250ms between Helius requests (sequential)
MAX_RETRIES = 3  # retries per deployer on 429/5xx

_blocked_mints: set[str] = set()
_deployer_wallets: dict[str, str] = {}  # wallet -> label


def is_mint_blacklisted(mint: str) -> bool:
    """Instant O(1) check."""
    return mint in _blocked_mints


def is_deployer_blacklisted(wallet: str) -> bool:
    """Instant O(1) check - wallet in deployer list."""
    return wallet in _deployer_wallets


def get_deployer_label(wallet: str) -> str:
    """Get label for deployer wallet."""
    return _deployer_wallets.get(wallet, "")


def get_stats() -> dict:
    return {"deployers": len(_deployer_wallets), "blocked_mints": len(_blocked_mints)}


def _load_deployers() -> list[dict]:
    if not BLACKLIST_FILE.exists():
        return []
    try:
        with open(BLACKLIST_FILE) as f:
            return json.load(f).get("deployers", [])
    except Exception as e:
        logger.error(f"[BLACKLIST] Failed to load {BLACKLIST_FILE}: {e}")
        return []


async def _fetch_recent_mints(session, helius_url, deployer, limit=50):
    """Fetch last N mints from a deployer via Helius. Retry on 429/5xx."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getAssetsByCreator",
                "params": {
                    "creatorAddress": deployer,
                    "onlyVerified": False,
                    "limit": limit,
                    "page": 1,
                }
            }
            async with session.post(helius_url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429 or resp.status >= 500:
                    backoff = attempt * 1.0  # 1s, 2s, 3s
                    logger.warning(
                        f"[BLACKLIST] Helius {resp.status} for {deployer[:12]}... "
                        f"(attempt {attempt}/{MAX_RETRIES}, backoff {backoff}s)"
                    )
                    await asyncio.sleep(backoff)
                    continue
                data = await resp.json()

            mints = set()
            for item in data.get("result", {}).get("items", []):
                mint_id = item.get("id", "")
                if mint_id:
                    mints.add(mint_id)
            return mints
        except Exception as e:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(attempt * 0.5)
                continue
            logger.error(f"[BLACKLIST] Helius error for {deployer[:12]}...: {e}")
            return set()
    return set()


async def refresh_blacklist():
    """Refresh blocked mints — SEQUENTIAL with delay (prevents Helius 429)."""
    global _blocked_mints, _deployer_wallets

    deployers = _load_deployers()
    if not deployers:
        return

    helius_key = os.environ.get("HELIUS_API_KEY", "")
    if not helius_key:
        logger.error("[BLACKLIST] HELIUS_API_KEY not set")
        return

    _helius_base = os.environ.get("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com")
    helius_url = f"{_helius_base}/?api-key={helius_key}"

    _deployer_wallets = {d["wallet"]: d.get("label", "unknown") for d in deployers}

    # PATCH 10: Sequential with delay instead of parallel gather
    new_mints = set()
    success_count = 0
    async with aiohttp.ClientSession() as session:
        for d in deployers:
            result = await _fetch_recent_mints(session, helius_url, d["wallet"], MINTS_PER_DEPLOYER)
            if isinstance(result, set) and result:
                new_mints |= result
                success_count += 1
            await asyncio.sleep(REQUEST_DELAY)  # 250ms between requests

    # PATCH 10: NEVER wipe cache on failed refresh
    if not new_mints and _blocked_mints:
        logger.warning(
            f"[BLACKLIST] Refresh returned 0 mints (0/{len(deployers)} succeeded) — "
            f"KEEPING previous cache ({len(_blocked_mints)} mints)"
        )
        return

    old_count = len(_blocked_mints)
    _blocked_mints = new_mints
    logger.warning(
        f"[BLACKLIST] Loaded {len(new_mints)} mints from {success_count}/{len(deployers)} deployers"
        + (f" (delta: {len(new_mints) - old_count:+d})" if old_count else "")
    )


async def run_periodic_blacklist():
    """Background task: refresh blacklist every 5 min."""
    logger.warning(f"[BLACKLIST] Scheduled (every {REFRESH_INTERVAL}s, first in {INITIAL_DELAY}s)")
    await asyncio.sleep(INITIAL_DELAY)
    while True:
        try:
            await refresh_blacklist()
        except Exception as e:
            logger.error(f"[BLACKLIST] Refresh crashed: {e}")
        await asyncio.sleep(REFRESH_INTERVAL)


def start_deployer_blacklist():
    """Call from bot_runner.py at startup."""
    asyncio.create_task(run_periodic_blacklist())
