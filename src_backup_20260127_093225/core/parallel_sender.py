"""
Parallel Transaction Sender - отправка транзакций через все RPC одновременно.

Увеличивает вероятность попадания в блок за счёт:
1. Параллельной отправки через все доступные RPC
2. Асинхронного подтверждения через несколько RPC
3. Race condition - первый успешный ответ побеждает
"""

import asyncio
import base64
import time
from typing import Optional, List, Tuple
from dataclasses import dataclass
from enum import Enum

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)


class ConfirmationStatus(Enum):
    """Статусы подтверждения транзакции."""
    PENDING = "pending"
    PROCESSED = "processed"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"
    FAILED = "failed"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass
class SendResult:
    """Результат отправки транзакции."""
    success: bool
    signature: Optional[str] = None
    endpoint: Optional[str] = None
    latency_ms: float = 0
    error: Optional[str] = None


@dataclass
class ConfirmResult:
    """Результат подтверждения транзакции."""
    success: bool
    status: ConfirmationStatus
    slot: Optional[int] = None
    confirmations: Optional[int] = None
    error: Optional[str] = None
    confirmed_by: Optional[str] = None


class ParallelTransactionSender:
    """
    Параллельная отправка и подтверждение транзакций.
    """

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._endpoints: List[str] = []
        self._initialized = False

    async def initialize(self) -> None:
        """Инициализация с получением эндпоинтов из RPC Manager."""
        if self._initialized:
            return

        try:
            from core.rpc_manager import RPCManager
            rpc_manager = await RPCManager.get_instance()

            self._endpoints = []
            for name, provider in rpc_manager.providers.items():
                if provider.http_endpoint and provider.enabled:
                    self._endpoints.append(provider.http_endpoint)

            logger.info(f"[ParallelSender] Initialized with {len(self._endpoints)} endpoints")

        except Exception as e:
            logger.warning(f"[ParallelSender] Failed to get RPC Manager: {e}")
            self._load_endpoints_from_env()

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            connector=aiohttp.TCPConnector(limit=50, limit_per_host=10)
        )
        self._initialized = True

    def _load_endpoints_from_env(self) -> None:
        """Загрузка эндпоинтов из переменных окружения."""
        import os

        env_keys = [
            "CHAINSTACK_RPC_ENDPOINT",
            "DRPC_RPC_ENDPOINT",
            "SYNDICA_RPC_ENDPOINT",
            "ALCHEMY_RPC_ENDPOINT",
        ]

        self._endpoints = []
        for key in env_keys:
            url = os.getenv(key)
            if url:
                self._endpoints.append(url)

        if not self._endpoints:
            self._endpoints = ["https://api.mainnet-beta.solana.com"]

    async def close(self) -> None:
        """Закрытие сессии."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._initialized = False

    async def send_parallel(
        self,
        serialized_tx: bytes,
        skip_preflight: bool = True
    ) -> List[SendResult]:
        """
        Отправляет транзакцию параллельно через все RPC.
        """
        if not self._initialized:
            await self.initialize()

        tx_base64 = base64.b64encode(serialized_tx).decode('utf-8')

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                tx_base64,
                {
                    "encoding": "base64",
                    "skipPreflight": skip_preflight,
                    "preflightCommitment": "processed",
                    "maxRetries": 0
                }
            ]
        }

        async def send_to_endpoint(endpoint: str) -> SendResult:
            start = time.time()
            try:
                async with self._session.post(
                    endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as resp:
                    data = await resp.json()
                    latency = (time.time() - start) * 1000

                    if "result" in data:
                        sig = data["result"]
                        logger.debug(f"[ParallelSender] ✓ {endpoint[:35]}... ({latency:.0f}ms)")
                        return SendResult(
                            success=True,
                            signature=sig,
                            endpoint=endpoint,
                            latency_ms=latency
                        )
                    else:
                        error = data.get("error", {})
                        error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                        logger.debug(f"[ParallelSender] ✗ {endpoint[:35]}...: {error_msg[:50]}")
                        return SendResult(
                            success=False,
                            endpoint=endpoint,
                            latency_ms=latency,
                            error=error_msg
                        )

            except asyncio.TimeoutError:
                return SendResult(success=False, endpoint=endpoint, error="Timeout")
            except Exception as e:
                return SendResult(success=False, endpoint=endpoint, error=str(e)[:50])

        tasks = [send_to_endpoint(ep) for ep in self._endpoints]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        send_results = []
        for r in results:
            if isinstance(r, SendResult):
                send_results.append(r)
            elif isinstance(r, Exception):
                send_results.append(SendResult(success=False, error=str(r)[:50]))

        successful = sum(1 for r in send_results if r.success)
        logger.info(f"[ParallelSender] Sent to {successful}/{len(self._endpoints)} RPC endpoints")

        return send_results

    async def confirm_parallel(
        self,
        signature: str,
        timeout: float = 60.0,
        target_commitment: str = "confirmed"
    ) -> ConfirmResult:
        """
        Подтверждает транзакцию, опрашивая все RPC параллельно.
        """
        if not self._initialized:
            await self.initialize()

        confirmed_event = asyncio.Event()
        result_holder = {"result": None}

        async def poll_endpoint(endpoint: str) -> None:
            start_time = time.time()
            poll_count = 0

            while not confirmed_event.is_set():
                if (time.time() - start_time) > timeout:
                    return

                poll_count += 1

                try:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getSignatureStatuses",
                        "params": [[signature], {"searchTransactionHistory": False}]
                    }

                    async with self._session.post(
                        endpoint,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        data = await resp.json()

                        if "result" not in data:
                            continue

                        value = data["result"].get("value", [])
                        if not value or value[0] is None:
                            # Транзакция ещё не найдена - продолжаем опрос
                            await asyncio.sleep(0.3)
                            continue

                        status = value[0]

                        # Проверяем на ошибку транзакции
                        if status.get("err"):
                            result_holder["result"] = ConfirmResult(
                                success=False,
                                status=ConfirmationStatus.FAILED,
                                error=str(status["err"]),
                                confirmed_by=endpoint
                            )
                            confirmed_event.set()
                            return

                        confirmation_status = status.get("confirmationStatus", "")
                        slot = status.get("slot")
                        confirmations = status.get("confirmations")

                        # Finalized - всегда успех
                        if confirmation_status == "finalized":
                            result_holder["result"] = ConfirmResult(
                                success=True,
                                status=ConfirmationStatus.FINALIZED,
                                slot=slot,
                                confirmations=confirmations,
                                confirmed_by=endpoint
                            )
                            confirmed_event.set()
                            return

                        # Confirmed
                        if confirmation_status == "confirmed":
                            if target_commitment in ["confirmed", "processed"]:
                                result_holder["result"] = ConfirmResult(
                                    success=True,
                                    status=ConfirmationStatus.CONFIRMED,
                                    slot=slot,
                                    confirmations=confirmations,
                                    confirmed_by=endpoint
                                )
                                confirmed_event.set()
                                return

                        # Processed
                        if confirmation_status == "processed":
                            if target_commitment == "processed":
                                result_holder["result"] = ConfirmResult(
                                    success=True,
                                    status=ConfirmationStatus.PROCESSED,
                                    slot=slot,
                                    confirmed_by=endpoint
                                )
                                confirmed_event.set()
                                return

                except asyncio.CancelledError:
                    return
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    logger.debug(f"[ParallelSender] Poll error: {e}")

                await asyncio.sleep(0.5)

        tasks = [asyncio.create_task(poll_endpoint(ep)) for ep in self._endpoints]

        try:
            await asyncio.wait_for(confirmed_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if result_holder["result"]:
            return result_holder["result"]

        return ConfirmResult(
            success=False,
            status=ConfirmationStatus.EXPIRED,
            error=f"Timeout after {timeout}s"
        )

    async def send_and_confirm(
        self,
        serialized_tx: bytes,
        timeout: float = 60.0,
        target_commitment: str = "confirmed",
        skip_preflight: bool = True
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Отправляет транзакцию параллельно и ждёт подтверждения.

        Returns:
            (success, signature, error)
        """
        # 1. Параллельная отправка
        send_results = await self.send_parallel(serialized_tx, skip_preflight)

        signature = None
        for r in send_results:
            if r.success and r.signature:
                signature = r.signature
                break

        if not signature:
            errors = [r.error for r in send_results if r.error][:3]
            return False, None, f"All sends failed: {errors}"

        logger.info(f"[ParallelSender] TX sent: {signature[:20]}... Confirming...")

        # 2. Параллельное подтверждение
        confirm_result = await self.confirm_parallel(
            signature,
            timeout=timeout,
            target_commitment=target_commitment
        )

        if confirm_result.success:
            logger.info(f"[ParallelSender] ✓ {confirm_result.status.value.upper()} in slot {confirm_result.slot}")
            return True, signature, None
        else:
            logger.warning(f"[ParallelSender] ✗ {confirm_result.status.value}: {confirm_result.error}")
            return False, signature, confirm_result.error


# =============================================================================
# Singleton
# =============================================================================

_parallel_sender: Optional[ParallelTransactionSender] = None


async def get_parallel_sender() -> ParallelTransactionSender:
    """Получение singleton экземпляра."""
    global _parallel_sender
    if _parallel_sender is None:
        _parallel_sender = ParallelTransactionSender()
        await _parallel_sender.initialize()
    return _parallel_sender


async def send_transaction_parallel(
    serialized_tx: bytes,
    timeout: float = 60.0,
    confirm: bool = True
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Удобная функция для отправки транзакции.

    Returns:
        (success, signature, error)
    """
    sender = await get_parallel_sender()

    if confirm:
        return await sender.send_and_confirm(serialized_tx, timeout)
    else:
        results = await sender.send_parallel(serialized_tx)
        for r in results:
            if r.success:
                return True, r.signature, None
        return False, None, "All sends failed"
