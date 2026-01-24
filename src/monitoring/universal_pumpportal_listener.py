"""
Universal PumpPortal listener that works with multiple platforms.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable

import websockets

from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from utils.logger import get_logger
from utils.watchdog_mixin import WatchdogMixin

logger = get_logger(__name__)


class UniversalPumpPortalListener(WatchdogMixin, BaseTokenListener):
    """Universal PumpPortal listener that works with multiple platforms."""

    watchdog_timeout = 90.0  # 90 секунд без сообщений = reconnect
    watchdog_check_interval = 15.0

    def __init__(
        self,
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        platforms: list[Platform] | None = None,
        api_key: str | None = None,
        raise_on_max_errors: bool = False,
        max_consecutive_errors: int = 5,
    ):
        """Initialize universal PumpPortal listener.

        Args:
            pumpportal_url: PumpPortal WebSocket URL
            platforms: List of platforms to monitor (if None, monitor all supported platforms)
            api_key: PumpPortal API key for PumpSwap data (requires 0.02 SOL on linked wallet)
            raise_on_max_errors: If True, raise exception after max errors (for FallbackListener)
            max_consecutive_errors: Max errors before raising/resetting
        """
        super().__init__()
        self.logger = logger  # Для WatchdogMixin
        self._websocket = None  # Для reconnect
        
        # Add API key to URL if provided
        if api_key:
            self.pumpportal_url = f"{pumpportal_url}?api-key={api_key}"
            logger.info("PumpPortal API key configured - PumpSwap data enabled")
        else:
            self.pumpportal_url = pumpportal_url
        self.ping_interval = 20  # seconds
        self.raise_on_max_errors = raise_on_max_errors
        self.max_consecutive_errors = max_consecutive_errors

        # Get platform-specific processors
        from platforms.pumpfun.pumpportal_processor import PumpFunPumpPortalProcessor
        from platforms.letsbonk.pumpportal_processor import LetsBonkPumpPortalProcessor

        all_processors = [
            PumpFunPumpPortalProcessor(),
            LetsBonkPumpPortalProcessor(),
        ]

        if platforms is None:
            self.processors = all_processors
        else:
            self.processors = [p for p in all_processors if p.platform in platforms]

        self.pool_to_processors: dict[str, list] = {}
        for processor in self.processors:
            for pool_name in processor.supported_pool_names:
                if pool_name not in self.pool_to_processors:
                    self.pool_to_processors[pool_name] = []
                self.pool_to_processors[pool_name].append(processor)

        logger.info(
            f"Initialized Universal PumpPortal listener for platforms: {[p.platform.value for p in self.processors]}"
        )
        logger.info(f"Monitoring pools: {list(self.pool_to_processors.keys())}")

    async def _trigger_reconnect(self) -> None:
        """Реализация для WatchdogMixin - переподключение WebSocket"""
        logger.warning("Watchdog triggering reconnect...")
        if self._websocket:
            try:
                await self._websocket.close()
            except Exception:
                pass
        self._websocket = None

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """Listen for new token creations using PumpPortal WebSocket."""
        consecutive_errors = 0

        while True:
            try:
                async with websockets.connect(
                    self.pumpportal_url,
                    ping_interval=30,
                    ping_timeout=60,
                    close_timeout=10,
                ) as websocket:
                    self._websocket = websocket
                    await self._subscribe_to_new_tokens(websocket)
                    ping_task = asyncio.create_task(self._ping_loop(websocket))
                    
                    # Запускаем watchdog
                    await self._start_watchdog()
                    consecutive_errors = 0

                    try:
                        while True:
                            token_info = await self._wait_for_token_creation(websocket)
                            
                            # Обновляем watchdog при любом ответе
                            self._update_last_message_time()
                            
                            if not token_info:
                                continue

                            logger.info(
                                f"New token detected: {token_info.name} ({token_info.symbol}) on {token_info.platform.value}"
                            )

                            if match_string and not (
                                match_string.lower() in token_info.name.lower()
                                or match_string.lower() in token_info.symbol.lower()
                            ):
                                logger.info(f"Token does not match filter '{match_string}'. Skipping...")
                                continue

                            if creator_address:
                                creator_str = str(token_info.creator) if token_info.creator else ""
                                user_str = str(token_info.user) if token_info.user else ""
                                if creator_address not in [creator_str, user_str]:
                                    logger.info(f"Token not created by {creator_address}. Skipping...")
                                    continue

                            try:
                                await asyncio.wait_for(token_callback(token_info), timeout=30)
                            except asyncio.TimeoutError:
                                logger.warning(f"Token callback timeout (30s) for {token_info.symbol} - skipping")
                                continue

                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("PumpPortal WebSocket connection closed. Reconnecting...")
                    except asyncio.CancelledError:
                        logger.info("PumpPortal listener cancelled")
                        raise
                    finally:
                        await self._stop_watchdog()
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                        self._websocket = None

            except asyncio.CancelledError:
                logger.info("PumpPortal listener task cancelled")
                raise
            except asyncio.TimeoutError:
                consecutive_errors += 1
                logger.warning(f"PumpPortal WebSocket timeout (error {consecutive_errors}/{self.max_consecutive_errors})")
            except Exception as e:
                consecutive_errors += 1
                logger.exception(f"PumpPortal WebSocket connection error (error {consecutive_errors}/{self.max_consecutive_errors})")

            if consecutive_errors >= self.max_consecutive_errors:
                if self.raise_on_max_errors:
                    raise ConnectionError(f"PumpPortal failed after {consecutive_errors} consecutive errors")
                logger.error(f"Too many consecutive errors ({consecutive_errors}), waiting 30s...")
                await asyncio.sleep(30)
                consecutive_errors = 0
            else:
                backoff = min(5 * (2 ** consecutive_errors), 30)
                logger.info(f"Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)

    async def _subscribe_to_new_tokens(self, websocket) -> None:
        """Subscribe to new token events from PumpPortal."""
        subscription_message = json.dumps({"method": "subscribeNewToken", "params": []})
        await websocket.send(subscription_message)
        logger.info("Subscribed to PumpPortal new token events")

    async def _ping_loop(self, websocket) -> None:
        """Keep connection alive with pings."""
        ping_failures = 0
        max_ping_failures = 3

        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                try:
                    pong_waiter = await websocket.ping()
                    await asyncio.wait_for(pong_waiter, timeout=30)
                    ping_failures = 0
                    self._update_last_message_time()  # Ping success = connection alive
                except TimeoutError:
                    ping_failures += 1
                    logger.warning(f"Ping timeout ({ping_failures}/{max_ping_failures})")
                    if ping_failures >= max_ping_failures:
                        logger.warning("Too many ping failures, closing connection")
                        await websocket.close()
                        return
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Ping error")

    def _detect_platform_from_mint(self, mint: str) -> str | None:
        """Detect platform from mint address suffix."""
        mint_lower = mint.lower()
        if mint_lower.endswith("pump"):
            return "pump"
        if mint_lower.endswith("bonk"):
            return "bonk"
        return None

    async def _wait_for_token_creation(self, websocket) -> TokenInfo | None:
        """Wait for token creation event from PumpPortal."""
        try:
            response = await asyncio.wait_for(websocket.recv(), timeout=60)
            data = json.loads(response)

            token_data = None
            if "method" in data and data["method"] == "newToken":
                params = data.get("params", [])
                if params and len(params) > 0:
                    token_data = params[0]
            elif "signature" in data and "mint" in data and "pool" in data:
                token_data = data

            if not token_data:
                return None

            mint = token_data.get("mint", "")
            detected_pool = self._detect_platform_from_mint(mint)
            pool_name = detected_pool or token_data.get("pool", "").lower()

            if pool_name not in self.pool_to_processors:
                logger.debug(f"Ignoring token from unsupported pool: {pool_name} (mint: {mint[:16]}...)")
                return None

            original_pool = token_data.get("pool", "").lower()
            if detected_pool and detected_pool != original_pool:
                logger.info(
                    f"[PLATFORM] Detected {detected_pool.upper()} token from mint suffix "
                    f"(PumpPortal sent pool={original_pool}): {mint[:16]}..."
                )

            for processor in self.pool_to_processors[pool_name]:
                if processor.can_process(token_data):
                    token_info = processor.process_token_data(token_data)
                    if token_info:
                        logger.debug(f"Successfully processed token using {processor.platform.value} processor")
                        return token_info

            logger.debug(f"No processor could handle token data from pool {pool_name}")
            return None

        except asyncio.TimeoutError:
            logger.debug("No data received from PumpPortal for 60 seconds")
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed:
            logger.warning("PumpPortal WebSocket connection closed")
            raise
        except json.JSONDecodeError:
            logger.debug("Failed to decode PumpPortal message")
        except Exception:
            logger.exception("Error processing PumpPortal WebSocket message")

        return None
