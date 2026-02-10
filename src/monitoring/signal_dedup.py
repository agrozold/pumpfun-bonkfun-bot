"""
Signal deduplicator for multi-receiver whale tracking.

When gRPC and Webhook both catch the same whale transaction,
this ensures only the FIRST signal triggers a buy.
Dedup key: tx_signature (unique per blockchain transaction).
"""

import logging
import time

logger = logging.getLogger(__name__)


class SignalDedup:
    """Deduplicates whale buy signals across multiple receivers.

    Thread-safe for single-threaded asyncio (GIL protects dict operations).
    Uses tx_signature as the dedup key — unique per blockchain transaction.
    """

    def __init__(self, ttl_seconds: int = 300):
        self._seen: dict[str, tuple[float, str]] = {}  # signature -> (timestamp, source)
        self._ttl = ttl_seconds
        self._dedup_hits = 0
        self._dedup_passes = 0

    def is_new(self, signature: str, source: str = "") -> bool:
        """Returns True if this signature hasn't been seen within TTL.

        First caller wins — subsequent calls with same signature return False.
        """
        self._cleanup()
        if signature in self._seen:
            original_source = self._seen[signature][1]
            self._dedup_hits += 1
            logger.info(
                f"[SIGNAL-DEDUP] Duplicate TX {signature[:16]}... "
                f"(first from {original_source}, duplicate from {source})"
            )
            return False
        self._seen[signature] = (time.monotonic(), source)
        self._dedup_passes += 1
        return True

    def _cleanup(self):
        """Remove expired entries (older than TTL)."""
        if len(self._seen) < 50:
            return
        now = time.monotonic()
        cutoff = now - self._ttl
        expired = [sig for sig, (ts, _) in self._seen.items() if ts < cutoff]
        for sig in expired:
            del self._seen[sig]

    def get_stats(self) -> dict:
        """Return dedup statistics."""
        return {
            "seen_count": len(self._seen),
            "ttl_seconds": self._ttl,
            "dedup_hits": self._dedup_hits,
            "dedup_passes": self._dedup_passes,
        }
