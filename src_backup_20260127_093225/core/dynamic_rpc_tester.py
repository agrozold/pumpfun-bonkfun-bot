"""
Dynamic RPC Latency Tester - uses getSlot for universal compatibility.
"""

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Any

import aiohttp

from utils.logger import get_logger

if TYPE_CHECKING:
    from core.rpc_manager import RPCManager, ProviderConfig

logger = get_logger(__name__)


class DynamicRPCTester:
    def __init__(self, rpc_manager: "RPCManager", test_interval: int = 30):
        self.rpc_manager = rpc_manager
        self.test_interval = test_interval
        self._running = False
        self._latency_history: dict[str, deque] = {}
        self._success_history: dict[str, deque] = {}
        self._slot_history: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._best_endpoint: str | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._test_loop())
        logger.info(f"[RPC TESTER] Started (interval: {self.test_interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _test_single_endpoint(self, name: str, provider: "ProviderConfig") -> tuple[float, bool, int | None]:
        """Test endpoint using getSlot. Returns (latency_ms, success, slot)."""
        if not provider.enabled or not provider.http_endpoint:
            return float("inf"), False, None
        if time.time() < provider.disabled_until:
            return float("inf"), False, None

        session = self.rpc_manager._session
        if not session:
            return float("inf"), False, None

        try:
            start = time.monotonic()
            async with session.post(
                provider.http_endpoint,
                json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"},
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                latency_ms = (time.monotonic() - start) * 1000
                if resp.status == 200:
                    data = await resp.json()
                    if "result" in data:
                        return latency_ms, True, data["result"]
                    return float("inf"), False, None
                elif resp.status == 429:
                    return 5000.0, True, None
                return float("inf"), False, None
        except Exception as e:
            logger.debug(f"[RPC TESTER] {name}: {e}")
            return float("inf"), False, None

    async def _test_loop(self) -> None:
        await asyncio.sleep(5)
        while self._running:
            try:
                await self._test_all_endpoints()
                await asyncio.sleep(self.test_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RPC TESTER] Error: {e}")
                await asyncio.sleep(5)

    async def _test_all_endpoints(self) -> None:
        if not self.rpc_manager._session:
            return

        tasks = {
            name: self._test_single_endpoint(name, prov)
            for name, prov in self.rpc_manager.providers.items()
        }
        if not tasks:
            return

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        report = []
        best_score = float("inf")
        best_name = None

        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                latency, success, slot = float("inf"), False, None
            else:
                latency, success, slot = result

            if name not in self._latency_history:
                self._latency_history[name] = deque(maxlen=10)
            if name not in self._success_history:
                self._success_history[name] = deque(maxlen=10)

            self._success_history[name].append(success)
            if latency < float("inf"):
                self._latency_history[name].append(latency)
            if slot:
                self._slot_history[name] = slot

            hist = self._latency_history.get(name, [])
            succ = self._success_history.get(name, [])
            if hist:
                avg = sum(hist) / len(hist)
                rate = sum(1 for s in succ if s) / len(succ) * 100 if succ else 0
                score = avg * (2 - rate / 100)
                report.append(f"  {name}: {avg:.0f}ms, {rate:.0f}% ok")
                if score < best_score and rate >= 50:
                    best_score = score
                    best_name = name

        if report:
            logger.info("[RPC TESTER] " + " | ".join(report))
        if best_name:
            self._best_endpoint = best_name

    def get_latency_score(self, provider_name: str) -> float:
        hist = self._latency_history.get(provider_name, [])
        succ = self._success_history.get(provider_name, [])
        if not hist:
            return 1000.0
        avg = sum(hist) / len(hist)
        rate = sum(1 for s in succ if s) / len(succ) * 100 if succ else 50
        score = avg / 200
        if rate < 80:
            score *= 2 - rate / 100
        return score

    def get_endpoint_stats(self) -> dict[str, Any]:
        stats = {}
        for name, hist in self._latency_history.items():
            succ = self._success_history.get(name, [])
            if hist:
                stats[name] = {
                    "avg_latency_ms": round(sum(hist) / len(hist), 1),
                    "min_latency_ms": round(min(hist), 1),
                    "max_latency_ms": round(max(hist), 1),
                    "success_rate": round(sum(1 for s in succ if s) / len(succ) * 100, 1) if succ else 0,
                    "score": round(self.get_latency_score(name), 2),
                    "last_slot": self._slot_history.get(name),
                }
        return stats

    @property
    def best_endpoint(self) -> str | None:
        return self._best_endpoint
