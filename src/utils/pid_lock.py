"""
PID Lock - prevents running multiple bot instances.
Uses /proc check on Linux for reliable process detection.
"""

import os
import sys
import atexit
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PID_FILE = Path("/tmp/whale-bot.pid")


def is_process_running(pid: int) -> bool:
    """Check if process is actually running (not zombie)."""
    try:
        # First check if we can signal it
        os.kill(pid, 0)
    except OSError:
        return False
    
    # On Linux, also check /proc to ensure it's not a zombie
    proc_path = Path(f"/proc/{pid}")
    if proc_path.exists():
        try:
            status_file = proc_path / "status"
            if status_file.exists():
                status = status_file.read_text()
                # Check if zombie
                if "State:\tZ" in status:
                    return False
                # Check if it's our bot
                cmdline_file = proc_path / "cmdline"
                if cmdline_file.exists():
                    cmdline = cmdline_file.read_text()
                    if "bot_runner" in cmdline or "python" in cmdline:
                        return True
            return True
        except (IOError, PermissionError):
            return True  # Can't read, assume running
    
    return False


def acquire_pid_lock() -> bool:
    """
    Try to acquire PID lock.
    Returns True if lock acquired, False if another instance running.
    """
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            
            if is_process_running(old_pid):
                print(f"[PID] Another bot instance running! PID: {old_pid}")
                print(f"[PID] Kill it first: kill {old_pid}")
                return False
            else:
                print(f"[PID] Removing stale PID file (old PID {old_pid} not running)")
                PID_FILE.unlink()
                
        except (ValueError, IOError) as e:
            print(f"[PID] Invalid PID file, removing: {e}")
            try:
                PID_FILE.unlink()
            except:
                pass

    # Write new PID
    try:
        PID_FILE.write_text(str(os.getpid()))
        print(f"[PID] Lock acquired, PID: {os.getpid()}")
        return True
    except IOError as e:
        print(f"[PID] Cannot write PID file: {e}")
        return False


def release_pid_lock():
    """Release PID lock on exit."""
    try:
        if PID_FILE.exists():
            current_pid = PID_FILE.read_text().strip()
            if current_pid == str(os.getpid()):
                PID_FILE.unlink()
                print("[PID] Lock released")
    except IOError:
        pass


# Register cleanup on exit
atexit.register(release_pid_lock)
