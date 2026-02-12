"""
Periodic dust cleanup - runs every 60 minutes.
Burns worthless tokens (< threshold USD) except NO_SL mints.
First run 300s after boot to let bot warm up.
"""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DUST_INTERVAL = 3600   # 60 minutes
INITIAL_DELAY = 300    # 5 minutes after boot
DUST_THRESHOLD = "0.30"
CLEANUP_SCRIPT = Path(__file__).resolve().parent.parent.parent / "cleanup_dust.py"
VENV_PYTHON = Path(__file__).resolve().parent.parent.parent / "venv" / "bin" / "python3"


async def run_periodic_dust():
    """Main dust cleanup loop."""
    logger.warning(f"[DUST] Periodic dust scheduled (every {DUST_INTERVAL}s, first run in {INITIAL_DELAY}s)")
    await asyncio.sleep(INITIAL_DELAY)

    while True:
        try:
            if not CLEANUP_SCRIPT.exists():
                logger.error(f"[DUST] Script not found: {CLEANUP_SCRIPT}")
                await asyncio.sleep(DUST_INTERVAL)
                continue

            logger.info(f"[DUST] Running cleanup (threshold ${DUST_THRESHOLD})...")

            proc = await asyncio.create_subprocess_exec(
                str(VENV_PYTHON), str(CLEANUP_SCRIPT), DUST_THRESHOLD, "--quiet",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(CLEANUP_SCRIPT.parent),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            output = stdout.decode().strip() if stdout else ""
            errors = stderr.decode().strip() if stderr else ""

            if proc.returncode == 0:
                if output:
                    logger.warning(f"[DUST] {output}")
                else:
                    logger.info("[DUST] Cleanup done - nothing to burn")
            else:
                logger.error(f"[DUST] Script failed (rc={proc.returncode}): {errors or output}")

        except asyncio.TimeoutError:
            logger.error("[DUST] Script timed out (120s)")
        except Exception as e:
            logger.error(f"[DUST] Error: {e}")

        await asyncio.sleep(DUST_INTERVAL)


def start_periodic_dust():
    """Start periodic dust cleanup task."""
    asyncio.create_task(run_periodic_dust())
