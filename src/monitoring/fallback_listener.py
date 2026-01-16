"""
Fallback listener that automatically switches between data sources.

Priority order:
1. PumpPortal (fastest for new tokens) - NOTE: Does NOT support bonk.fun!
2. Solana logsSubscribe (reliable fallback)
3. Solana blockSubscribe (last resort)

For bonk.fun (lets_bonk platform):
- Uses specialized BonkLogsListener that subscribes to Raydium LaunchLab program
- PumpPortal does NOT send bonk.fun tokens!
"""

import asyncio
from collections.abc import Awaitable, Callable

from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from utils.logger import get_logger

logger = get_logger(__name__)


class FallbackListener(BaseTokenListener):
    """Listener with automatic fallback between data sources."""

    def __init__(
        self,
        wss_endpoint: str,
        rpc_endpoint: str | None = None,
        platforms: list[Platform] | None = None,
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        pumpportal_api_key: str | None = None,
        primary_listener: str = "pumpportal",
        fallback_listeners: list[str] | None = None,
        max_errors_before_fallback: int = 5,
    ):
        """Initialize fallback listener.

        Args:
            wss_endpoint: Solana WebSocket endpoint
            rpc_endpoint: Solana HTTP RPC endpoint (for bonk listener)
            platforms: Platforms to monitor
            pumpportal_url: PumpPortal WebSocket URL
            pumpportal_api_key: PumpPortal API key
            primary_listener: Primary listener type
            fallback_listeners: Ordered list of fallback listeners
            max_errors_before_fallback: Errors before switching to fallback
        """
        super().__init__()
        self.wss_endpoint = wss_endpoint
        self.rpc_endpoint = rpc_endpoint or wss_endpoint.replace("wss://", "https://").replace("ws://", "http://")
        self.platforms = platforms
        self.pumpportal_url = pumpportal_url
        self.pumpportal_api_key = pumpportal_api_key
        self.primary_listener = primary_listener
        self.fallback_listeners = fallback_listeners or ["logs", "pumpportal"]
        self.max_errors_before_fallback = max_errors_before_fallback
        
        self._current_listener: BaseTokenListener | None = None
        self._current_listener_type: str = ""
        self._error_count = 0
        self._listener_index = -1  # Start with primary
        
        # Check if we need specialized bonk listener
        self._needs_bonk_listener = (
            platforms and 
            Platform.LETS_BONK in platforms
        )
        
        # Build listener order: primary first, then fallbacks
        self._listener_order = [primary_listener] + [
            l for l in self.fallback_listeners if l != primary_listener
        ]
        
        # For bonk platform, add bonk_logs as high priority fallback
        if self._needs_bonk_listener and "bonk_logs" not in self._listener_order:
            # Insert bonk_logs right after primary (or as primary if pumpportal)
            if primary_listener == "pumpportal":
                self._listener_order.insert(1, "bonk_logs")
            else:
                self._listener_order.append("bonk_logs")
        
        logger.info(
            f"FallbackListener initialized: primary={primary_listener}, "
            f"fallbacks={self.fallback_listeners}, needs_bonk={self._needs_bonk_listener}"
        )

    def _create_listener(self, listener_type: str) -> BaseTokenListener | None:
        """Create a specific listener type."""
        try:
            if listener_type == "bonk_logs":
                # Specialized listener for bonk.fun tokens
                from monitoring.bonk_logs_listener import BonkLogsListener
                return BonkLogsListener(
                    wss_endpoint=self.wss_endpoint,
                    rpc_endpoint=self.rpc_endpoint,
                    raise_on_max_errors=True,
                    max_consecutive_errors=3,
                )
            elif listener_type == "pumpportal":
                from monitoring.universal_pumpportal_listener import (
                    UniversalPumpPortalListener,
                )
                # Filter out LETS_BONK from pumpportal - it doesn't support bonk.fun!
                pumpportal_platforms = self.platforms
                if pumpportal_platforms and Platform.LETS_BONK in pumpportal_platforms:
                    pumpportal_platforms = [p for p in pumpportal_platforms if p != Platform.LETS_BONK]
                    if not pumpportal_platforms:
                        logger.warning("PumpPortal doesn't support bonk.fun, skipping...")
                        return None
                
                return UniversalPumpPortalListener(
                    pumpportal_url=self.pumpportal_url,
                    platforms=pumpportal_platforms,
                    api_key=self.pumpportal_api_key,
                    raise_on_max_errors=True,  # Allow FallbackListener to switch
                    max_consecutive_errors=3,  # Switch faster
                )
            elif listener_type == "logs":
                from monitoring.universal_logs_listener import UniversalLogsListener
                return UniversalLogsListener(
                    wss_endpoint=self.wss_endpoint,
                    platforms=self.platforms,
                )
            elif listener_type == "blocks":
                from monitoring.universal_block_listener import UniversalBlockListener
                return UniversalBlockListener(
                    wss_endpoint=self.wss_endpoint,
                    platforms=self.platforms,
                )
            else:
                logger.warning(f"Unknown listener type: {listener_type}")
                return None
        except Exception as e:
            logger.error(f"Failed to create {listener_type} listener: {e}")
            return None

    def _switch_to_next_listener(self) -> bool:
        """Switch to next available listener. Returns True if switched."""
        self._listener_index += 1
        
        while self._listener_index < len(self._listener_order):
            listener_type = self._listener_order[self._listener_index]
            listener = self._create_listener(listener_type)
            
            if listener:
                self._current_listener = listener
                self._current_listener_type = listener_type
                self._error_count = 0
                logger.warning(
                    f"[SWITCH] Switched to {listener_type} listener "
                    f"(index {self._listener_index}/{len(self._listener_order)-1})"
                )
                return True
            
            self._listener_index += 1
        
        # All listeners exhausted, restart from beginning
        logger.warning("All listeners failed, restarting from primary...")
        self._listener_index = -1
        return False

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """Listen for tokens with automatic fallback."""
        
        while True:
            # Initialize or switch listener
            if self._current_listener is None:
                if not self._switch_to_next_listener():
                    logger.error("No listeners available, waiting 30s...")
                    await asyncio.sleep(30)
                    continue
            
            try:
                logger.info(f"📡 Starting {self._current_listener_type} listener...")
                
                # Run listener with error tracking wrapper
                await self._run_with_error_tracking(
                    token_callback, match_string, creator_address
                )
                
            except asyncio.CancelledError:
                logger.info("FallbackListener cancelled")
                raise
            except ConnectionError as e:
                # ConnectionError from listener with raise_on_max_errors=True
                # Switch immediately to next listener
                logger.warning(
                    f"[WARN] {self._current_listener_type} connection failed: {e}"
                )
                logger.warning(f"[SWITCH] Switching to next listener...")
                self._current_listener = None
                # No sleep - switch immediately
            except Exception as e:
                self._error_count += 1
                logger.error(
                    f"[ERROR] {self._current_listener_type} error ({self._error_count}/"
                    f"{self.max_errors_before_fallback}): {e}"
                )
                
                if self._error_count >= self.max_errors_before_fallback:
                    logger.warning(
                        f"[WARN] {self._current_listener_type} failed "
                        f"{self.max_errors_before_fallback} times, switching..."
                    )
                    self._current_listener = None
                else:
                    # Brief pause before retry
                    await asyncio.sleep(5)

    async def _run_with_error_tracking(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None,
        creator_address: str | None,
    ) -> None:
        """Run current listener and track errors."""
        if not self._current_listener:
            return
        
        # Reset error count on successful connection
        connected = False
        
        async def wrapped_callback(token_info: TokenInfo) -> None:
            nonlocal connected
            if not connected:
                connected = True
                self._error_count = 0  # Reset on first successful token
                logger.info(f"[OK] {self._current_listener_type} connected and receiving data")
            await token_callback(token_info)
        
        await self._current_listener.listen_for_tokens(
            wrapped_callback, match_string, creator_address
        )
