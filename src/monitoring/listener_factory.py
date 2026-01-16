"""
Factory for creating platform-aware token listeners.
"""

from interfaces.core import Platform
from monitoring.base_listener import BaseTokenListener
from utils.logger import get_logger

logger = get_logger(__name__)


class ListenerFactory:
    """Factory for creating appropriate token listeners based on configuration."""

    @staticmethod
    def create_listener(
        listener_type: str,
        wss_endpoint: str | None = None,
        rpc_endpoint: str | None = None,
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        pumpportal_api_key: str | None = None,
        platforms: list[Platform] | None = None,
        enable_fallback: bool = True,
    ) -> BaseTokenListener:
        """Create a token listener based on the specified type.

        Args:
            listener_type: Type of listener ('logs', 'blocks', 'geyser', 'pumpportal',
                          'bonk_logs', 'bags_logs', or 'fallback')
            wss_endpoint: WebSocket endpoint URL (for logs/blocks listeners)
            rpc_endpoint: HTTP RPC endpoint (for bonk/bags listener transaction fetching)
            geyser_endpoint: Geyser gRPC endpoint URL (for geyser listener)
            geyser_api_token: Geyser API token (for geyser listener)
            geyser_auth_type: Geyser authentication type
            pumpportal_url: PumpPortal WebSocket URL (for pumpportal listener)
            pumpportal_api_key: PumpPortal API key for PumpSwap data
            platforms: List of platforms to monitor (if None, monitor all)
            enable_fallback: If True and listener fails, auto-switch to fallback

        Returns:
            Configured token listener

        Raises:
            ValueError: If listener type is invalid or required parameters are missing
        """
        """Create a token listener based on the specified type.

        Args:
            listener_type: Type of listener ('logs', 'blocks', 'geyser', 'pumpportal', or 'fallback')
            wss_endpoint: WebSocket endpoint URL (for logs/blocks listeners)
            rpc_endpoint: HTTP RPC endpoint (for bonk listener transaction fetching)
            geyser_endpoint: Geyser gRPC endpoint URL (for geyser listener)
            geyser_api_token: Geyser API token (for geyser listener)
            geyser_auth_type: Geyser authentication type
            pumpportal_url: PumpPortal WebSocket URL (for pumpportal listener)
            pumpportal_api_key: PumpPortal API key for PumpSwap data
            platforms: List of platforms to monitor (if None, monitor all)
            enable_fallback: If True and listener fails, auto-switch to fallback

        Returns:
            Configured token listener

        Raises:
            ValueError: If listener type is invalid or required parameters are missing
        """
        listener_type = listener_type.lower()

        # Explicit bags_logs listener type
        if listener_type == "bags_logs":
            if not wss_endpoint:
                raise ValueError("WebSocket endpoint required for bags_logs listener")

            from monitoring.bags_logs_listener import BagsLogsListener

            # Try to get RPC endpoint from WSS endpoint
            if not rpc_endpoint and wss_endpoint:
                rpc_endpoint = wss_endpoint.replace("wss://", "https://").replace(
                    "ws://", "http://"
                )

            listener = BagsLogsListener(
                wss_endpoint=wss_endpoint,
                rpc_endpoint=rpc_endpoint or "",
            )
            logger.info("Created BagsLogsListener for bags.fm tokens")
            return listener

        # Explicit bonk_logs listener type
        if listener_type == "bonk_logs":
            if not wss_endpoint:
                raise ValueError("WebSocket endpoint required for bonk_logs listener")

            from monitoring.bonk_logs_listener import BonkLogsListener

            # Try to get RPC endpoint from WSS endpoint
            if not rpc_endpoint and wss_endpoint:
                rpc_endpoint = wss_endpoint.replace("wss://", "https://").replace(
                    "ws://", "http://"
                )

            listener = BonkLogsListener(
                wss_endpoint=wss_endpoint,
                rpc_endpoint=rpc_endpoint or "",
            )
            logger.info("Created BonkLogsListener for bonk.fun tokens")
            return listener

        # Check if ONLY bags platform is requested - use specialized listener
        if platforms and len(platforms) == 1 and platforms[0] == Platform.BAGS:
            if listener_type in ["logs", "fallback", "pumpportal"]:
                if not wss_endpoint:
                    raise ValueError("WebSocket endpoint required for bags listener")

                # Use specialized BagsLogsListener for better detection
                from monitoring.bags_logs_listener import BagsLogsListener

                # Try to get RPC endpoint from WSS endpoint
                if not rpc_endpoint and wss_endpoint:
                    rpc_endpoint = wss_endpoint.replace("wss://", "https://").replace(
                        "ws://", "http://"
                    )

                listener = BagsLogsListener(
                    wss_endpoint=wss_endpoint,
                    rpc_endpoint=rpc_endpoint or "",
                )
                logger.info("Created specialized BagsLogsListener for bags platform")
                return listener

        # Check if ONLY lets_bonk platform is requested - use specialized listener
        if platforms and len(platforms) == 1 and platforms[0] == Platform.LETS_BONK:
            if listener_type in ["logs", "fallback", "pumpportal"]:
                if not wss_endpoint:
                    raise ValueError("WebSocket endpoint required for bonk listener")

                # Use specialized BonkLogsListener for better detection
                from monitoring.bonk_logs_listener import BonkLogsListener

                # Try to get RPC endpoint from WSS endpoint
                if not rpc_endpoint and wss_endpoint:
                    rpc_endpoint = wss_endpoint.replace("wss://", "https://").replace(
                        "ws://", "http://"
                    )

                listener = BonkLogsListener(
                    wss_endpoint=wss_endpoint,
                    rpc_endpoint=rpc_endpoint or "",
                )
                logger.info(
                    "Created specialized BonkLogsListener for lets_bonk platform"
                )
                return listener

        # Fallback listener - auto-switches between sources
        if listener_type == "fallback" or (
            enable_fallback and listener_type in ["pumpportal", "logs"]
        ):
            if not wss_endpoint:
                raise ValueError("WebSocket endpoint required for fallback listener")

            from monitoring.fallback_listener import FallbackListener

            # Determine primary based on requested type
            primary = listener_type if listener_type != "fallback" else "pumpportal"
            fallbacks = ["logs", "pumpportal", "blocks"]
            fallbacks = [f for f in fallbacks if f != primary]

            listener = FallbackListener(
                wss_endpoint=wss_endpoint,
                platforms=platforms,
                pumpportal_url=pumpportal_url,
                pumpportal_api_key=pumpportal_api_key,
                primary_listener=primary,
                fallback_listeners=fallbacks,
            )
            logger.info(
                f"Created FallbackListener: primary={primary}, fallbacks={fallbacks}"
            )
            return listener

        if listener_type == "geyser":
            if not geyser_endpoint or not geyser_api_token:
                raise ValueError(
                    "Geyser endpoint and API token are required for geyser listener"
                )

            from monitoring.universal_geyser_listener import UniversalGeyserListener

            listener = UniversalGeyserListener(
                geyser_endpoint=geyser_endpoint,
                geyser_api_token=geyser_api_token,
                geyser_auth_type=geyser_auth_type,
                platforms=platforms,
            )
            logger.info("Created Universal Geyser listener for token monitoring")
            return listener

        elif listener_type == "logs":
            if not wss_endpoint:
                raise ValueError("WebSocket endpoint is required for logs listener")

            from monitoring.universal_logs_listener import UniversalLogsListener

            listener = UniversalLogsListener(
                wss_endpoint=wss_endpoint,
                platforms=platforms,
            )
            logger.info("Created Universal Logs listener for token monitoring")
            return listener

        elif listener_type == "blocks":
            if not wss_endpoint:
                raise ValueError("WebSocket endpoint is required for blocks listener")

            from monitoring.universal_block_listener import UniversalBlockListener

            listener = UniversalBlockListener(
                wss_endpoint=wss_endpoint,
                platforms=platforms,
            )
            logger.info("Created Universal Block listener for token monitoring")
            return listener

        elif listener_type == "pumpportal":
            # Import the new universal PumpPortal listener
            from monitoring.universal_pumpportal_listener import (
                UniversalPumpPortalListener,
            )

            # Validate that requested platforms support PumpPortal
            # Note: BAGS (bags.fm) and LETS_BONK (bonk.fun) are NOT supported by PumpPortal!
            supported_pumpportal_platforms = [Platform.PUMP_FUN]

            if platforms:
                unsupported = [
                    p for p in platforms if p not in supported_pumpportal_platforms
                ]
                if unsupported:
                    logger.warning(
                        f"Platforms {[p.value for p in unsupported]} do not support PumpPortal. "
                        f"Use 'bonk_logs' for bonk.fun, 'logs' for bags.fm tokens."
                    )

                # Filter to only supported platforms
                filtered_platforms = [
                    p for p in platforms if p in supported_pumpportal_platforms
                ]
                if not filtered_platforms:
                    raise ValueError(
                        "No supported platforms specified for PumpPortal listener"
                    )
                platforms = filtered_platforms

            listener = UniversalPumpPortalListener(
                pumpportal_url=pumpportal_url,
                platforms=platforms,
                api_key=pumpportal_api_key,
            )
            logger.info(
                f"Created Universal PumpPortal listener for platforms: {[p.value for p in (platforms or supported_pumpportal_platforms)]}"
            )
            return listener

        else:
            raise ValueError(
                f"Invalid listener type '{listener_type}'. "
                f"Must be one of: 'logs', 'blocks', 'geyser', 'pumpportal', 'bonk_logs'"
            )

    @staticmethod
    def get_supported_listener_types() -> list[str]:
        """Get list of supported listener types.

        Returns:
            List of supported listener type strings
        """
        return ["logs", "blocks", "geyser", "pumpportal", "bonk_logs", "bags_logs"]

    @staticmethod
    def get_platform_compatible_listeners(platform: Platform) -> list[str]:
        """Get list of listener types compatible with a specific platform.

        Args:
            platform: Platform to check compatibility for

        Returns:
            List of compatible listener types
        """
        if platform == Platform.PUMP_FUN:
            return ["logs", "blocks", "geyser", "pumpportal"]
        elif platform == Platform.LETS_BONK:
            # IMPORTANT: PumpPortal does NOT send bonk.fun tokens!
            # Use bonk_logs for direct Raydium LaunchLab subscription
            return ["bonk_logs", "logs", "blocks", "geyser"]
        elif platform == Platform.BAGS:
            # BAGS uses Meteora DBC - PumpPortal does NOT support bags.fm!
            # Use bags_logs for direct Meteora DBC subscription.
            return ["bags_logs", "logs", "blocks", "geyser"]
        else:
            return ["blocks", "geyser"]  # Default universal listeners

    @staticmethod
    def get_pumpportal_supported_platforms() -> list[Platform]:
        """Get list of platforms that support PumpPortal listener.

        Returns:
            List of platforms with PumpPortal support

        Note:
            BAGS (bags.fm) is NOT supported by PumpPortal!
            LETS_BONK (bonk.fun) is NOT supported by PumpPortal!
            Both require direct program subscription via logsSubscribe.
        """
        return [Platform.PUMP_FUN]  # Only pump.fun is supported by PumpPortal
