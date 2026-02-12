"""
JITO Block Engine integration for faster transaction landing.
Sends transactions directly to validators, bypassing the mempool.
"""

import asyncio
import base64
import os
import random
from typing import Optional, List

import aiohttp
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction, Transaction
from solders.system_program import transfer, TransferParams

from utils.logger import get_logger

logger = get_logger(__name__)

# JITO tip accounts (choose random to reduce contention)
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"
]

DEFAULT_JITO_URL = "https://frankfurt.mainnet.block-engine.jito.wtf"


class JitoSender:
    def __init__(self, tip_lamports: int = None, block_engine_url: str = None, enabled: bool = None):
        self.enabled = enabled if enabled is not None else os.getenv("JITO_ENABLED", "true").lower() == "true"
        self.tip_lamports = tip_lamports or int(os.getenv("JITO_TIP_LAMPORTS", "10000"))
        self.block_engine_url = block_engine_url or os.getenv("JITO_BLOCK_ENGINE_URL", DEFAULT_JITO_URL)
        self._session: Optional[aiohttp.ClientSession] = None
        logger.info(f"[JITO] Init: enabled={self.enabled}, tip={self.tip_lamports} lamports")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def get_random_tip_account(self) -> Pubkey:
        return Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS))

    def create_tip_instruction(self, payer: Pubkey):
        return transfer(TransferParams(
            from_pubkey=payer,
            to_pubkey=self.get_random_tip_account(),
            lamports=self.tip_lamports
        ))

    async def send_transaction(self, tx, skip_preflight: bool = True) -> Optional[str]:
        """Send transaction via JITO Block Engine."""
        if not self.enabled:
            return None

        try:
            session = await self._get_session()
            tx_bytes = bytes(tx)
            tx_base64 = base64.b64encode(tx_bytes).decode('utf-8')

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [tx_base64, {"encoding": "base64"}]
            }

            url = f"{self.block_engine_url}/api/v1/transactions"
            async with session.post(url, json=payload) as resp:
                result = await resp.json()
                if "error" in result:
                    logger.error(f"[JITO] Error: {result.get('error')}")
                    return None
                sig = result.get("result")
                logger.info(f"[JITO] TX sent: {str(sig)[:20]}...")
                return sig

        except Exception as e:
            logger.error(f"[JITO] Error: {e}")
            return None

    async def send_bundle(self, transactions: list) -> Optional[str]:
        """Send bundle of transactions (max 5) via JITO."""
        if not self.enabled:
            return None
        if len(transactions) > 5:
            logger.error("[JITO] Bundle max 5 txs")
            return None

        try:
            session = await self._get_session()
            txs_base64 = [base64.b64encode(bytes(tx)).decode('utf-8') for tx in transactions]

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendBundle",
                "params": [txs_base64, {"encoding": "base64"}]
            }

            url = f"{self.block_engine_url}/api/v1/bundles"
            async with session.post(url, json=payload) as resp:
                result = await resp.json()
                if "error" in result:
                    logger.error(f"[JITO] Bundle error: {result.get('error')}")
                    return None
                bundle_id = result.get("result")
                logger.info(f"[JITO] Bundle sent: {str(bundle_id)[:20]}...")
                return bundle_id

        except Exception as e:
            logger.error(f"[JITO] Bundle error: {e}")
            return None

    async def get_bundle_status(self, bundle_id: str) -> Optional[dict]:
        """Check bundle status: Invalid, Pending, Failed, Landed"""
        try:
            session = await self._get_session()
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBundleStatuses",
                "params": [[bundle_id]]
            }
            url = f"{self.block_engine_url}/api/v1/bundles"
            async with session.post(url, json=payload) as resp:
                result = await resp.json()
                if "error" in result:
                    return None
                value = result.get("result", {}).get("value", [])
                return value[0] if value else None
        except Exception as e:
            logger.error(f"[JITO] Status error: {e}")
            return None

    async def wait_for_bundle(self, bundle_id: str, timeout: float = 30.0) -> bool:
        """Wait for bundle to land on-chain."""
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            status = await self.get_bundle_status(bundle_id)
            if status:
                state = status.get("status", "Unknown")
                if state == "Landed":
                    logger.info("[JITO] Bundle landed!")
                    return True
                elif state in ("Failed", "Invalid"):
                    logger.warning(f"[JITO] Bundle {state}")
                    return False
            await asyncio.sleep(1.0)
        logger.warning("[JITO] Bundle timeout")
        return False


# Singleton
_jito_sender: Optional[JitoSender] = None

def get_jito_sender() -> JitoSender:
    global _jito_sender
    if _jito_sender is None:
        _jito_sender = JitoSender()
    return _jito_sender
