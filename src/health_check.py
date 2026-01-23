"""
RPC Health Check Module
Мониторинг здоровья RPC endpoints с автоматическим failover
"""

import asyncio
import time
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
import aiohttp
from datetime import datetime

from utils.logger import get_logger

logger = get_logger(__name__)


class HealthStatus(Enum):
    """Статусы здоровья endpoint"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class EndpointHealth:
    """Состояние здоровья endpoint"""
    name: str
    url: str
    status: HealthStatus = HealthStatus.UNKNOWN
    latency_ms: float = 0.0
    last_check: Optional[datetime] = None
    consecutive_failures: int = 0
    total_requests: int = 0
    failed_requests: int = 0
    last_error: Optional[str] = None
    slot: Optional[int] = None

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 100.0
        return (self.total_requests - self.failed_requests) / self.total_requests * 100

    @property
    def is_available(self) -> bool:
        return self.status in [HealthStatus.HEALTHY, HealthStatus.DEGRADED]


class RPCHealthChecker:
    """Health checker для RPC endpoints"""

    def __init__(
        self,
        check_interval: int = 30,
        timeout: float = 5.0,
        failover_threshold: int = 3,
        degraded_latency_ms: float = 500.0,
    ):
        self.check_interval = int(os.getenv("HEALTH_CHECK_INTERVAL", check_interval))
        self.timeout = float(os.getenv("HEALTH_CHECK_TIMEOUT", timeout))
        self.failover_threshold = int(os.getenv("FAILOVER_THRESHOLD", failover_threshold))
        self.degraded_latency_ms = degraded_latency_ms

        self.endpoints: Dict[str, EndpointHealth] = {}
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._callbacks: List[Callable] = []
        self._reference_slot: Optional[int] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def register_endpoint(self, name: str, url: str) -> None:
        """Зарегистрировать endpoint для мониторинга"""
        if url and url not in ["", "None"]:
            self.endpoints[name] = EndpointHealth(name=name, url=url)
            logger.info(f"[HealthCheck] Registered: {name}")

    def register_callback(self, callback: Callable) -> None:
        """Callback вызывается при смене статуса endpoint"""
        self._callbacks.append(callback)

    async def check_endpoint(self, name: str) -> EndpointHealth:
        """Проверить здоровье конкретного endpoint"""
        if name not in self.endpoints:
            raise ValueError(f"Unknown endpoint: {name}")

        endpoint = self.endpoints[name]
        session = await self._get_session()

        start_time = time.monotonic()
        old_status = endpoint.status

        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSlot",
                "params": [{"commitment": "processed"}]
            }

            async with session.post(
                endpoint.url,
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as response:
                latency_ms = (time.monotonic() - start_time) * 1000
                endpoint.latency_ms = latency_ms
                endpoint.total_requests += 1
                endpoint.last_check = datetime.now()

                if response.status == 200:
                    data = await response.json()

                    if "result" in data:
                        endpoint.slot = data["result"]
                        endpoint.consecutive_failures = 0
                        endpoint.last_error = None

                        if latency_ms > self.degraded_latency_ms:
                            endpoint.status = HealthStatus.DEGRADED
                        else:
                            endpoint.status = HealthStatus.HEALTHY

                        if self._reference_slot and endpoint.slot:
                            slot_diff = self._reference_slot - endpoint.slot
                            if slot_diff > 50:
                                endpoint.status = HealthStatus.DEGRADED

                        if endpoint.slot:
                            if self._reference_slot is None or endpoint.slot > self._reference_slot:
                                self._reference_slot = endpoint.slot
                    else:
                        self._handle_failure(endpoint, f"RPC error: {data.get('error', 'unknown')}")

                elif response.status == 429:
                    self._handle_failure(endpoint, "Rate limited (429)")
                    endpoint.status = HealthStatus.DEGRADED
                else:
                    self._handle_failure(endpoint, f"HTTP {response.status}")

        except asyncio.TimeoutError:
            self._handle_failure(endpoint, f"Timeout ({self.timeout}s)")
        except aiohttp.ClientError as e:
            self._handle_failure(endpoint, f"Connection: {str(e)[:50]}")
        except Exception as e:
            self._handle_failure(endpoint, f"Error: {str(e)[:50]}")

        if old_status != endpoint.status:
            await self._notify_status_change(name, old_status, endpoint.status)

        return endpoint

    def _handle_failure(self, endpoint: EndpointHealth, error: str) -> None:
        """Обработать неудачную проверку"""
        endpoint.consecutive_failures += 1
        endpoint.failed_requests += 1
        endpoint.last_error = error

        if endpoint.consecutive_failures >= self.failover_threshold:
            endpoint.status = HealthStatus.UNHEALTHY
            logger.error(f"[HealthCheck] {endpoint.name}: UNHEALTHY ({error})")
        else:
            endpoint.status = HealthStatus.DEGRADED
            logger.warning(f"[HealthCheck] {endpoint.name}: {error} (failures: {endpoint.consecutive_failures})")

    async def _notify_status_change(self, name: str, old: HealthStatus, new: HealthStatus) -> None:
        """Уведомить о смене статуса"""
        logger.info(f"[HealthCheck] {name}: {old.value} -> {new.value}")

        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(name, old, new)
                else:
                    callback(name, old, new)
            except Exception as e:
                logger.error(f"[HealthCheck] Callback error: {e}")

    async def check_all(self) -> Dict[str, EndpointHealth]:
        """Проверить все endpoints параллельно"""
        if not self.endpoints:
            return {}

        tasks = [self.check_endpoint(name) for name in self.endpoints]
        await asyncio.gather(*tasks, return_exceptions=True)
        return self.endpoints

    async def start(self) -> None:
        """Запустить периодические проверки"""
        if self._running:
            return

        self._running = True
        await self.check_all()
        self._check_task = asyncio.create_task(self._run_periodic_checks())
        logger.info(f"[HealthCheck] Started (interval: {self.check_interval}s)")

    async def stop(self) -> None:
        """Остановить проверки"""
        self._running = False

        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

        if self._session and not self._session.closed:
            await self._session.close()

        logger.info("[HealthCheck] Stopped")

    async def _run_periodic_checks(self) -> None:
        """Цикл периодических проверок"""
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                await self.check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HealthCheck] Loop error: {e}")
                await asyncio.sleep(5)

    def get_healthy_endpoints(self) -> List[str]:
        """Список здоровых endpoints"""
        return [name for name, h in self.endpoints.items() if h.is_available]

    def get_best_endpoint(self) -> Optional[str]:
        """Лучший endpoint по latency"""
        available = [(name, h) for name, h in self.endpoints.items() if h.is_available]

        if not available:
            return None

        available.sort(key=lambda x: (
            0 if x[1].status == HealthStatus.HEALTHY else 1,
            x[1].latency_ms
        ))

        return available[0][0]

    def get_status_report(self) -> Dict[str, Any]:
        """Отчёт о статусе всех endpoints"""
        return {
            name: {
                "status": h.status.value,
                "latency_ms": round(h.latency_ms, 1),
                "success_rate": f"{h.success_rate:.1f}%",
                "slot": h.slot,
                "last_check": h.last_check.strftime("%H:%M:%S") if h.last_check else None,
                "last_error": h.last_error,
            }
            for name, h in self.endpoints.items()
        }

    def print_status(self) -> None:
        """Вывести статус в лог"""
        logger.info("=" * 60)
        logger.info("[HealthCheck] RPC Status Report:")
        for name, h in self.endpoints.items():
            status_emoji = {
                HealthStatus.HEALTHY: "OK",
                HealthStatus.DEGRADED: "WARN",
                HealthStatus.UNHEALTHY: "FAIL",
                HealthStatus.UNKNOWN: "???",
            }
            emoji = status_emoji.get(h.status, "???")
            logger.info(
                f"  [{emoji}] {name}: {h.status.value} | "
                f"{h.latency_ms:.0f}ms | "
                f"{h.success_rate:.0f}% success"
            )
        logger.info("=" * 60)


_health_checker: Optional[RPCHealthChecker] = None


async def get_health_checker() -> RPCHealthChecker:
    """Получить глобальный health checker"""
    global _health_checker

    if _health_checker is None:
        _health_checker = RPCHealthChecker()

        endpoints_config = {
            "primary": os.getenv("SOLANA_NODE_RPC_ENDPOINT"),
            "chainstack": os.getenv("CHAINSTACK_RPC_ENDPOINT"),
            "alchemy": os.getenv("ALCHEMY_RPC_ENDPOINT"),
            "drpc": os.getenv("DRPC_RPC_ENDPOINT"),
            "public": "https://api.mainnet-beta.solana.com",
        }

        for name, url in endpoints_config.items():
            if url:
                _health_checker.register_endpoint(name, url)

    return _health_checker


async def integrate_health_checker_with_rpc_manager() -> None:
    """Интегрировать health checker с RPC Manager"""
    from src.core.rpc_manager import get_rpc_manager

    checker = await get_health_checker()
    rpc_manager = await get_rpc_manager()

    def on_status_change(name: str, old: HealthStatus, new: HealthStatus) -> None:
        provider = rpc_manager.providers.get(name)
        if not provider:
            return

        if new == HealthStatus.UNHEALTHY:
            provider.disabled_until = time.time() + 300
            logger.warning(f"[HealthCheck->RPC] Disabled {name} for 5 minutes")
        elif new == HealthStatus.HEALTHY and old == HealthStatus.UNHEALTHY:
            provider.disabled_until = 0
            provider.consecutive_errors = 0
            logger.info(f"[HealthCheck->RPC] Recovered {name}")

    checker.register_callback(on_status_change)
    await checker.start()

    logger.info("[HealthCheck] Integrated with RPCManager")


if __name__ == "__main__":
    async def main():
        checker = await get_health_checker()
        await checker.check_all()
        checker.print_status()

        print("\nDetailed report:")
        import json
        print(json.dumps(checker.get_status_report(), indent=2))

    asyncio.run(main())
