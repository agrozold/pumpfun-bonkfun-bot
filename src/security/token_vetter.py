"""
Token Vetter - Проверка безопасности токена перед покупкой.

Проверки:
1. Freeze Authority - должен быть отозван (КРИТИЧНО!)
2. Mint Authority - желательно отозван
3. RugCheck.xyz API - комплексная проверка (БЕСПЛАТНО!)
4. LP Lock % - только для НЕ-launchpad токенов с малой ликвидностью
5. Top Holder % - опционально
6. Holder Count - опционально

UPDATED: Now uses RPCManager with caching to save RPC calls!
"""

import asyncio
import logging
import time
import base64
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import aiohttp
from solders.pubkey import Pubkey

# Use RPCManager for cached RPC calls
from core.rpc_manager import get_rpc_manager, RPCManager

logger = logging.getLogger(__name__)

MINT_LAYOUT_SIZE = 82
LAUNCHPAD_PLATFORMS = {"pump_fun", "lets_bonk", "bags"}


class VetResult(Enum):
    SAFE = "safe"
    RISKY = "risky"
    DANGEROUS = "dangerous"
    SKIP = "skip"
    ERROR = "error"


@dataclass
class TokenVetReport:
    mint: str
    symbol: str
    result: VetResult

    mint_authority_revoked: bool = False
    freeze_authority_revoked: bool = False

    rugcheck_score: int = 0
    rugcheck_risks: list = field(default_factory=list)
    rugcheck_risk_level: str = ""

    lp_locked_pct: float = 0.0
    lp_locked_usd: float = 0.0
    liquidity_usd: float = 0.0

    top_holder_pct: float = 0.0
    holder_count: int = 0
    dev_holding_pct: float = 0.0

    reason: str = ""
    check_time_ms: float = 0
    fail_reasons: list = field(default_factory=list)


class TokenVetter:
    """Проверка безопасности токенов - использует RPCManager с кешем!"""

    RUGCHECK_API = "https://api.rugcheck.xyz/v1"

    def __init__(
        self,
        rpc_endpoint: str = "",  # Kept for backwards compatibility, but not used
        require_freeze_revoked: bool = True,
        require_mint_revoked: bool = False,
        check_lp_locked: bool = True,
        min_lp_locked_pct: float = 50.0,
        min_liquidity_bypass: float = 10000.0,
        check_top_holder: bool = False,
        max_top_holder_pct: float = 25.0,
        check_holder_count: bool = False,
        min_holder_count: int = 10,
        min_rugcheck_score: int = 50,
        check_timeout: float = 3.0,
        skip_for_bonding_curve: bool = True,
        skip_lp_for_launchpads: bool = True,
        cache_ttl: float = 120.0,
    ):
        # NOTE: rpc_endpoint is ignored - we use RPCManager singleton!
        self.require_freeze_revoked = require_freeze_revoked
        self.require_mint_revoked = require_mint_revoked
        self.check_lp_locked = check_lp_locked
        self.min_lp_locked_pct = min_lp_locked_pct
        self.min_liquidity_bypass = min_liquidity_bypass
        self.skip_lp_for_launchpads = skip_lp_for_launchpads
        self.check_top_holder = check_top_holder
        self.max_top_holder_pct = max_top_holder_pct
        self.check_holder_count = check_holder_count
        self.min_holder_count = min_holder_count
        self.min_rugcheck_score = min_rugcheck_score
        self.check_timeout = check_timeout
        self.skip_for_bonding_curve = skip_for_bonding_curve
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[TokenVetReport, float]] = {}
        self._rpc_manager: RPCManager | None = None
        self._http: aiohttp.ClientSession | None = None

        logger.info(
            f"[VETTER] Init with RPCManager: freeze={require_freeze_revoked}, "
            f"lp_locked>={min_lp_locked_pct}% (bypass if liq>${min_liquidity_bypass/1000:.0f}k), "
            f"min_score={min_rugcheck_score}"
        )

    async def _get_rpc_manager(self) -> RPCManager:
        """Get RPCManager singleton - uses caching!"""
        if not self._rpc_manager:
            self._rpc_manager = await get_rpc_manager()
        return self._rpc_manager

    async def _get_http(self) -> aiohttp.ClientSession:
        if not self._http or self._http.closed:
            timeout = aiohttp.ClientTimeout(total=self.check_timeout)
            self._http = aiohttp.ClientSession(timeout=timeout)
        return self._http

    async def close(self):
        # Don't close RPCManager - it's a singleton used by others
        if self._http and not self._http.closed:
            await self._http.close()

    async def vet_token(
        self,
        mint_address: str,
        symbol: str = "",
        is_bonding_curve: bool = False,
    ) -> TokenVetReport:
        start_time = time.time()

        if mint_address in self._cache:
            cached, ts = self._cache[mint_address]
            if time.time() - ts < self.cache_ttl:
                return cached

        report = TokenVetReport(mint=mint_address, symbol=symbol, result=VetResult.SAFE)

        if is_bonding_curve and self.skip_for_bonding_curve:
            report.result = VetResult.SKIP
            report.reason = "Bonding curve token - skipped"
            return report

        try:
            auth_task = self._check_authorities(mint_address)
            rugcheck_task = self._fetch_rugcheck(mint_address)
            auth_result, rugcheck_data = await asyncio.gather(auth_task, rugcheck_task, return_exceptions=True)

            if isinstance(auth_result, dict) and "error" not in auth_result:
                report.mint_authority_revoked = auth_result.get("mint_revoked", False)
                report.freeze_authority_revoked = auth_result.get("freeze_revoked", False)

            if isinstance(rugcheck_data, dict) and rugcheck_data:
                self._parse_rugcheck(report, rugcheck_data)

            self._evaluate_report(report, is_bonding_curve)

        except Exception as e:
            logger.error(f"[VETTER] Error vetting {symbol}: {e}")
            report.result = VetResult.ERROR
            report.reason = str(e)

        report.check_time_ms = (time.time() - start_time) * 1000
        self._cache[mint_address] = (report, time.time())

        if len(self._cache) > 500:
            self._cleanup_cache()

        if report.result in (VetResult.DANGEROUS, VetResult.RISKY):
            logger.warning(
                f"[VET] {report.result.value.upper()}: {symbol} - {report.reason} "
                f"(freeze={report.freeze_authority_revoked}, mint={report.mint_authority_revoked}, "
                f"score={report.rugcheck_score}, liq=${report.liquidity_usd:.0f}, "
                f"top={report.top_holder_pct:.1f}% "
                f"({report.check_time_ms:.0f}ms)"
            )

        return report

    def _evaluate_report(self, report: TokenVetReport, is_bonding_curve: bool = False):
        report.fail_reasons = []

        if self.require_freeze_revoked and not report.freeze_authority_revoked:
            report.fail_reasons.append("Freeze authority NOT revoked")

        if self.require_mint_revoked and not report.mint_authority_revoked:
            report.fail_reasons.append("Mint authority NOT revoked")

        is_launchpad = is_bonding_curve
        skip_lp = is_launchpad and self.skip_lp_for_launchpads
        has_enough_liquidity = report.liquidity_usd >= self.min_liquidity_bypass

        if self.check_lp_locked and not skip_lp and not has_enough_liquidity:
            if report.lp_locked_pct < self.min_lp_locked_pct:
                report.fail_reasons.append(f"LP locked {report.lp_locked_pct:.1f}% < {self.min_lp_locked_pct}%")

        if self.check_top_holder and report.top_holder_pct > self.max_top_holder_pct:
            report.fail_reasons.append(f"Top holder {report.top_holder_pct:.1f}% > {self.max_top_holder_pct}%")

        if self.check_holder_count and report.holder_count > 0 and report.holder_count < self.min_holder_count:
            report.fail_reasons.append(f"Holders {report.holder_count} < {self.min_holder_count}")

        if report.rugcheck_score > 0 and report.rugcheck_score < self.min_rugcheck_score:
            report.fail_reasons.append(f"RugCheck score {report.rugcheck_score} < {self.min_rugcheck_score}")

        if report.fail_reasons:
            has_critical = any("Freeze" in r for r in report.fail_reasons)
            report.result = VetResult.DANGEROUS if has_critical else VetResult.RISKY
            report.reason = "; ".join(report.fail_reasons)
        else:
            report.result = VetResult.SAFE
            report.reason = "All checks passed"

    async def _check_authorities(self, mint_address: str) -> dict:
        """Check mint/freeze authorities using RPCManager (with caching!)."""
        try:
            rpc = await self._get_rpc_manager()
            result = await rpc.get_account_info(mint_address, use_cache=True)

            if not result or not result.get("value"):
                return {"error": "Mint not found"}

            data_b64 = result["value"].get("data", [])
            if isinstance(data_b64, list) and len(data_b64) >= 1:
                data = base64.b64decode(data_b64[0])
            else:
                return {"error": "Invalid data format"}

            if len(data) < MINT_LAYOUT_SIZE:
                return {"error": "Invalid mint data"}

            mint_auth_option = int.from_bytes(data[0:4], "little")
            freeze_auth_option = int.from_bytes(data[46:50], "little")

            return {
                "mint_revoked": mint_auth_option == 0,
                "freeze_revoked": freeze_auth_option == 0
            }
        except Exception as e:
            logger.debug(f"[VETTER] Authority check error: {e}")
            return {"error": str(e)}

    async def _fetch_rugcheck(self, mint_address: str) -> dict | None:
        try:
            session = await self._get_http()
            url = f"{self.RUGCHECK_API}/tokens/{mint_address}/report"
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.debug(f"[VETTER] RugCheck fetch error: {e}")
        return None

    def _parse_rugcheck(self, report: TokenVetReport, data: dict):
        try:
            report.rugcheck_score = int(data.get("score", 0) or 0)
            risk_level = data.get("riskLevel")
            report.rugcheck_risk_level = str(risk_level) if risk_level else "unknown"

            risks = data.get("risks", [])
            report.rugcheck_risks = []
            for r in risks[:5]:
                if isinstance(r, dict):
                    name = r.get("name", "")
                    level = r.get("level", "")
                    report.rugcheck_risks.append(f"{name} ({level})")
                else:
                    report.rugcheck_risks.append(str(r))

            markets = data.get("markets", [])
            if markets:
                best_market = None
                best_liq = 0
                for m in markets:
                    lp = m.get("lp", {})
                    liq = float(lp.get("quoteUSD", 0) or 0) + float(lp.get("baseUSD", 0) or 0)
                    if liq > best_liq:
                        best_liq = liq
                        best_market = m

                if best_market:
                    lp = best_market.get("lp", {})
                    lp_locked = lp.get("lpLockedPct", 0)
                    if lp_locked:
                        report.lp_locked_pct = float(lp_locked)
                    report.lp_locked_usd = float(lp.get("lpLockedUSD", 0) or 0)
                    report.liquidity_usd = float(lp.get("quoteUSD", 0) or 0) + float(lp.get("baseUSD", 0) or 0)

            total_liq = data.get("totalMarketLiquidity", 0)
            if total_liq and float(total_liq) > report.liquidity_usd:
                report.liquidity_usd = float(total_liq)

            top_holders = data.get("topHolders", [])
            if top_holders:
                top_holder = top_holders[0]
                pct = top_holder.get("pct", 0)
                if pct:
                    report.top_holder_pct = float(pct)

                creator = data.get("creator")
                if creator:
                    for holder in top_holders[:10]:
                        if holder.get("address") == creator:
                            dev_pct = holder.get("pct", 0)
                            if dev_pct:
                                report.dev_holding_pct = float(dev_pct)
                            break

            report.holder_count = int(data.get("totalHolders", 0) or 0)

        except Exception as e:
            logger.debug(f"[VETTER] RugCheck parse error: {e}")

    def _cleanup_cache(self):
        now = time.time()
        to_remove = [k for k, (_, ts) in self._cache.items() if now - ts > self.cache_ttl * 2]
        for k in to_remove:
            del self._cache[k]

    def should_buy(self, report: TokenVetReport) -> bool:
        if report.result == VetResult.DANGEROUS:
            return False
        if report.result == VetResult.ERROR:
            return False
        return True

    def clear_cache(self):
        self._cache.clear()
