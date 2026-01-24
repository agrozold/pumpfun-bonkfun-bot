import asyncio
import json
from typing import Optional

from .cache_manager import cache_creator_status, get_cached_creator_status
from .helius_optimizer import get_creator_stats_optimized

RISK_THRESHOLDS = {
    "max_tokens_created": 10,
    "max_tokens_sold_ratio": 0.8,
}

def calculate_risk_score(stats: dict) -> float:
    score = 0.0
    tokens_created = stats.get("tokens_created", 0)
    if tokens_created > RISK_THRESHOLDS["max_tokens_created"]:
        score += min(30, (tokens_created - RISK_THRESHOLDS["max_tokens_created"]) * 3)

    tokens_sold = stats.get("tokens_sold", 0)
    if tokens_created > 0:
        sell_ratio = tokens_sold / tokens_created
        if sell_ratio > RISK_THRESHOLDS["max_tokens_sold_ratio"]:
            score += 30 * sell_ratio

    large_sells = stats.get("large_sells", 0)
    score += min(20, large_sells * 5)

    return min(100, score)

async def is_creator_safe(creator_address: str, cache_ttl_seconds: int = 3600, risk_threshold: float = 50.0) -> tuple:
    print(f"Checking creator: {creator_address[:8]}...")

    is_risky_cached, risk_score_cached, found = get_cached_creator_status(creator_address, ttl_seconds=cache_ttl_seconds)

    if found:
        is_safe = not is_risky_cached
        details = {"source": "cache", "risk_score": risk_score_cached, "is_risky": is_risky_cached}
        print(f"Cache result: safe={is_safe}, score={risk_score_cached}")
        return is_safe, details

    stats = await get_creator_stats_optimized(creator_address)

    if stats is None:
        print("Could not analyze creator, skipping")
        return False, {"source": "api_error"}

    risk_score = calculate_risk_score(stats)
    is_risky = risk_score >= risk_threshold

    cache_creator_status(
        address=creator_address,
        is_risky=is_risky,
        risk_score=risk_score,
        tokens_created=stats.get("tokens_created", 0),
        tokens_sold=stats.get("tokens_sold", 0),
    )

    is_safe = not is_risky
    details = {
        "source": "helius_api",
        "risk_score": risk_score,
        "is_risky": is_risky,
        "tokens_created": stats.get("tokens_created", 0),
        "tokens_sold": stats.get("tokens_sold", 0),
    }

    if is_risky:
        print(f"RISKY creator: score={risk_score:.1f}")
    else:
        print(f"Safe creator: score={risk_score:.1f}")

    return is_safe, details

async def batch_check_creators(creator_addresses: list, cache_ttl_seconds: int = 3600, risk_threshold: float = 50.0, concurrency: int = 5) -> dict:
    semaphore = asyncio.Semaphore(concurrency)

    async def check_with_limit(address):
        async with semaphore:
            return address, await is_creator_safe(address, cache_ttl_seconds, risk_threshold)

    tasks = [check_with_limit(addr) for addr in creator_addresses]
    results = await asyncio.gather(*tasks)
    return dict(results)
