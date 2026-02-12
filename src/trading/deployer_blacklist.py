"""Deployer blacklist — preloads recent mints from blacklisted deployers.
Check is O(1) set lookup = instant.
Background refresh every 5 min catches new scam tokens.
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
MINTS_PER_DEPLOYER = 10  # last 10 tokens per deployer
INITIAL_DELAY = 5  # start fetching 5s after bot start

_blocked_mints: set[str] = set()
_deployer_wallets: dict[str, str] = {}  # wallet -> label


def is_mint_blacklisted(mint: str) -> bool:
    """Instant O(1) check."""
    return mint in _blocked_mints


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


async def _fetch_recent_mints(session, helius_url, deployer, limit=10):
    """Fetch last N mints from a deployer via Helius."""
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
            data = await resp.json()

        mints = set()
        for item in data.get("result", {}).get("items", []):
            mint_id = item.get("id", "")
            if mint_id:
                mints.add(mint_id)
        return mints
    except Exception as e:
        logger.error(f"[BLACKLIST] Helius error for {deployer[:12]}...: {e}")
        return set()


async def refresh_blacklist():
    """Refresh blocked mints from all deployers (parallel)."""
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

    async with aiohttp.ClientSession() as session:
        tasks = [
            _fetch_recent_mints(session, helius_url, d["wallet"], MINTS_PER_DEPLOYER)
            for d in deployers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    new_mints = set()
    for r in results:
        if isinstance(r, set):
            new_mints |= r

    old_count = len(_blocked_mints)
    _blocked_mints = new_mints
    if new_mints:
        logger.warning(f"[BLACKLIST] Loaded {len(new_mints)} mints from {len(deployers)} deployers"
                       + (f" (+{len(new_mints) - old_count} new)" if old_count else ""))


async def run_periodic_blacklist():
    """Background task: refresh blacklist every 5 min."""
    logger.warning(f"[BLACKLIST] Scheduled (every {REFRESH_INTERVAL}s, first in {INITIAL_DELAY}s)")
    await asyncio.sleep(INITIAL_DELAY)
    while True:
        await refresh_blacklist()
        await asyncio.sleep(REFRESH_INTERVAL)


def start_deployer_blacklist():
    """Call from bot_runner.py at startup."""
    asyncio.create_task(run_periodic_blacklist())
