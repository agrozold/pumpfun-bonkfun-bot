"""
Dual-channel watchdog for gRPC + Webhook whale tracking.

Monitors activity on both channels and alerts if either or both
go silent for too long. Zero risk - read-only monitoring,
does not affect trading logic.

Phase 5.3
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class DualChannelWatchdog:
    """Monitors gRPC and Webhook channel health by tracking last activity time.

    Usage:
        watchdog = DualChannelWatchdog(alert_after_seconds=300)
        # Pass to receivers:
        geyser_receiver.set_watchdog(watchdog)
        webhook_receiver.set_watchdog(watchdog)
        # Run in background:
        asyncio.create_task(watchdog.run())
    """

    def __init__(self, alert_after_seconds: int = 300, check_interval: int = 60):
        self._last_grpc_activity: float = 0.0
        self._last_webhook_activity: float = 0.0
        self._alert_after = alert_after_seconds
        self._check_interval = check_interval
        self._running = False
        self._task: asyncio.Task | None = None

        # Stats
        self._grpc_touch_count: int = 0
        self._webhook_touch_count: int = 0
        self._alerts_fired: int = 0

        logger.info(
            f"[WATCHDOG] Initialized: alert_after={alert_after_seconds}s, "
            f"check_interval={check_interval}s"
        )

    def touch_grpc(self):
        """Called by gRPC receiver on ANY activity (tx, ping, pong)."""
        self._last_grpc_activity = time.monotonic()
        self._grpc_touch_count += 1

    def touch_webhook(self):
        """Called by webhook receiver on ANY incoming POST."""
        self._last_webhook_activity = time.monotonic()
        self._webhook_touch_count += 1

    async def run(self):
        """Periodic health check loop. Run as asyncio.create_task(watchdog.run())."""
        self._running = True
        logger.warning("[WATCHDOG] Dual-channel watchdog STARTED")

        # Grace period: don't alert for the first alert_after seconds
        # (channels may not have received any whale data yet)
        start_time = time.monotonic()

        try:
            while self._running:
                await asyncio.sleep(self._check_interval)

                if not self._running:
                    break

                now = time.monotonic()
                uptime = now - start_time

                # Don't alert during initial grace period
                if uptime < self._alert_after:
                    continue

                grpc_silent = (
                    now - self._last_grpc_activity
                    if self._last_grpc_activity > 0
                    else uptime
                )
                webhook_silent = (
                    now - self._last_webhook_activity
                    if self._last_webhook_activity > 0
                    else uptime
                )

                both_silent = (
                    grpc_silent > self._alert_after
                    and webhook_silent > self._alert_after
                )
                grpc_only_silent = (
                    grpc_silent > self._alert_after
                    and webhook_silent <= self._alert_after
                )
                webhook_only_silent = (
                    webhook_silent > self._alert_after
                    and grpc_silent <= self._alert_after
                )

                if both_silent:
                    self._alerts_fired += 1
                    logger.error(
                        f"[WATCHDOG] BOTH CHANNELS SILENT! "
                        f"gRPC: {grpc_silent:.0f}s, Webhook: {webhook_silent:.0f}s "
                        f"(threshold: {self._alert_after}s)"
                    )
                elif grpc_only_silent:
                    self._alerts_fired += 1
                    logger.warning(
                        f"[WATCHDOG] gRPC silent for {grpc_silent:.0f}s "
                        f"(webhook OK: {webhook_silent:.0f}s ago)"
                    )
                elif webhook_only_silent:
                    self._alerts_fired += 1
                    logger.warning(
                        f"[WATCHDOG] Webhook silent for {webhook_silent:.0f}s "
                        f"(gRPC OK: {grpc_silent:.0f}s ago)"
                    )
                # else: both active, no alert needed

        except asyncio.CancelledError:
            logger.info("[WATCHDOG] Run loop cancelled")
        except Exception as e:
            logger.error(f"[WATCHDOG] Unexpected error in run loop: {e}")

    def stop(self):
        """Stop the watchdog loop."""
        self._running = False
        logger.info("[WATCHDOG] Stopped")

    def get_stats(self) -> dict:
        """Return watchdog statistics."""
        now = time.monotonic()
        return {
            "grpc_touch_count": self._grpc_touch_count,
            "webhook_touch_count": self._webhook_touch_count,
            "alerts_fired": self._alerts_fired,
            "grpc_last_activity_ago_s": (
                round(now - self._last_grpc_activity, 1)
                if self._last_grpc_activity > 0
                else None
            ),
            "webhook_last_activity_ago_s": (
                round(now - self._last_webhook_activity, 1)
                if self._last_webhook_activity > 0
                else None
            ),
            "alert_threshold_s": self._alert_after,
        }
