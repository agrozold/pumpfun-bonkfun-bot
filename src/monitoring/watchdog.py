"""
Dual-channel watchdog for gRPC + Webhook.
Detects stale gRPC (ping alive but no TX data).
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class DualChannelWatchdog:

    def __init__(self, alert_after_seconds: int = 300, check_interval: int = 60):
        self._last_grpc_activity: float = 0.0
        self._last_grpc_data: float = 0.0
        self._last_webhook_activity: float = 0.0
        self._alert_after = alert_after_seconds
        self._check_interval = check_interval
        self._running = False
        self._grpc_touch_count: int = 0
        self._grpc_data_count: int = 0
        self._webhook_touch_count: int = 0
        self._alerts_fired: int = 0
        self._on_grpc_stale = None

    def set_reconnect_callback(self, callback):
        self._on_grpc_stale = callback

    def touch_grpc(self):
        self._last_grpc_activity = time.monotonic()
        self._grpc_touch_count += 1

    def touch_grpc_data(self):
        self._last_grpc_data = time.monotonic()
        self._grpc_data_count += 1

    def touch_webhook(self):
        self._last_webhook_activity = time.monotonic()
        self._webhook_touch_count += 1

    async def run(self):
        self._running = True
        start_time = time.monotonic()
        try:
            while self._running:
                await asyncio.sleep(self._check_interval)
                if not self._running:
                    break
                now = time.monotonic()
                uptime = now - start_time
                if uptime < self._alert_after:
                    continue
                grpc_ping_ago = now - self._last_grpc_activity if self._last_grpc_activity > 0 else uptime
                grpc_data_ago = now - self._last_grpc_data if self._last_grpc_data > 0 else uptime
                webhook_ago = now - self._last_webhook_activity if self._last_webhook_activity > 0 else uptime

                if grpc_ping_ago < 60 and grpc_data_ago > self._alert_after:
                    self._alerts_fired += 1
                    logger.error(
                        f"[WATCHDOG] gRPC STALE: ping OK but 0 TX for {grpc_data_ago:.0f}s - reconnecting"
                    )
                    if self._on_grpc_stale:
                        try:
                            self._on_grpc_stale()
                        except Exception:
                            pass
                elif grpc_ping_ago > self._alert_after and webhook_ago > self._alert_after:
                    self._alerts_fired += 1
                    logger.error(f"[WATCHDOG] BOTH CHANNELS SILENT {grpc_ping_ago:.0f}s")
        except asyncio.CancelledError:
            pass

    def stop(self):
        self._running = False

    def get_stats(self) -> dict:
        now = time.monotonic()
        return {
            "grpc_pings": self._grpc_touch_count,
            "grpc_tx": self._grpc_data_count,
            "webhooks": self._webhook_touch_count,
            "alerts": self._alerts_fired,
        }
