"""
PID Lock - prevents running multiple bot instances.
"""

import os
import sys
import atexit
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PID_FILE = Path("/tmp/whale-bot.pid")


def acquire_pid_lock() -> bool:
    """
    Try to acquire PID lock.
    Returns True if lock acquired, False if another instance running.
    """
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            try:
                os.kill(old_pid, 0)
                logger.error(f"[PID] Another bot instance running! PID: {old_pid}")
                logger.error(f"[PID] Kill it first: kill {old_pid}")
                return False
            except OSError:
                logger.warning(f"[PID] Removing stale PID file (old PID {old_pid} not running)")
                PID_FILE.unlink()
        except (ValueError, IOError) as e:
            logger.warning(f"[PID] Invalid PID file, removing: {e}")
            PID_FILE.unlink()
    
    try:
        PID_FILE.write_text(str(os.getpid()))
        logger.info(f"[PID] Lock acquired, PID: {os.getpid()}")
        return True
    except IOError as e:
        logger.error(f"[PID] Cannot write PID file: {e}")
        return False


def release_pid_lock():
    """Release PID lock on exit."""
    try:
        if PID_FILE.exists():
            current_pid = PID_FILE.read_text().strip()
            if current_pid == str(os.getpid()):
                PID_FILE.unlink()
                logger.info("[PID] Lock released")
    except IOError:
        pass


atexit.register(release_pid_lock)
