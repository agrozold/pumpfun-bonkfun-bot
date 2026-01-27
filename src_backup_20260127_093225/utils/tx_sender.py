"""
Universal transaction sender with JITO support.
Use this for all transaction sending in buy.py, sell.py and other scripts.
"""

import asyncio
import os
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.transaction import Transaction, VersionedTransaction
from solders.keypair import Keypair
from solders.signature import Signature

# Import JITO sender
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trading.jito_sender import get_jito_sender


async def send_transaction_with_jito(
    client: AsyncClient,
    tx: Transaction | VersionedTransaction,
    skip_preflight: bool = True,
    max_retries: int = 3,
    confirm: bool = True,
    confirm_timeout: float = 30.0,
) -> tuple[bool, str | None]:
    """
    Send transaction with JITO support and automatic fallback.

    Args:
        client: AsyncClient for fallback RPC
        tx: Transaction or VersionedTransaction to send
        skip_preflight: Skip preflight simulation
        max_retries: Number of retry attempts
        confirm: Whether to wait for confirmation
        confirm_timeout: Confirmation timeout in seconds

    Returns:
        Tuple of (success: bool, signature: str | None)
    """
    jito = get_jito_sender()
    opts = TxOpts(skip_preflight=skip_preflight, preflight_commitment=Confirmed)

    last_error = None

    for attempt in range(max_retries):
        try:
            sig = None

            # Try JITO first if enabled
            if jito.enabled:
                try:
                    jito_sig = await jito.send_transaction(tx, skip_preflight=skip_preflight)
                    if jito_sig:
                        sig = jito_sig
                        print(f"[JITO] TX sent: {sig[:20]}...")
                except Exception as jito_err:
                    print(f"[JITO] Failed: {jito_err}, trying regular RPC...")

            # Fallback to regular RPC if JITO failed or disabled
            if not sig:
                result = await client.send_transaction(tx, opts=opts)
                sig = str(result.value)
                print(f"[RPC] TX sent: {sig[:20]}...")

            # Confirm if requested
            if confirm and sig:
                print("Waiting for confirmation...")
                try:
                    await asyncio.wait_for(
                        client.confirm_transaction(
                            Signature.from_string(sig) if isinstance(sig, str) else sig,
                            commitment="confirmed",
                            sleep_seconds=0.5
                        ),
                        timeout=confirm_timeout
                    )
                    print(f"Confirmed: {sig[:20]}...")
                    return True, sig
                except asyncio.TimeoutError:
                    print(f"Confirmation timeout, TX may still land: {sig}")
                    return True, sig

            return True, sig

        except Exception as e:
            last_error = e
            error_str = str(e).lower()

            # Non-retryable errors
            if any(x in error_str for x in ["insufficient", "not enough", "0x1775", "0x1776", "slippage"]):
                print(f"Non-retryable error: {e}")
                return False, None

            # Rate limit - wait longer
            if "429" in error_str or "too many" in error_str:
                wait = 2 ** attempt
                print(f"Rate limited, waiting {wait}s...")
                await asyncio.sleep(wait)
            else:
                print(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)

    print(f"All {max_retries} attempts failed. Last error: {last_error}")
    return False, None


def get_jito_status() -> str:
    """Get JITO status string for display."""
    jito = get_jito_sender()
    if jito.enabled:
        return f"JITO ON (tip: {jito.tip_lamports} lamports)"
    return "JITO OFF"
