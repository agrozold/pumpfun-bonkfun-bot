"""Mempool Listener - слушает транзакции на curve"""
import asyncio
import logging
from typing import Dict, Set
from datetime import datetime
import httpx

logger = logging.getLogger(__name__)

class MempoolListener:
    def __init__(self, rpc_url: str, helius_api_key: str, smart_money_detector):
        self.rpc_url = rpc_url
        self.helius_api_key = helius_api_key
        self.detector = smart_money_detector
        self.processed_txs: Set[str] = set()
        self.active_pools: Dict[str, str] = {}

    def add_pool_to_track(self, curve_address: str, token_id: str):
        self.active_pools[curve_address] = token_id
        logger.info(f"Added pool to track: {curve_address[:8]}... (token_id: {token_id})")

    async def start_listening(self):
        while True:
            try:
                for curve_address, token_id in list(self.active_pools.items()):
                    await self._poll_pool_transactions(curve_address, token_id)
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Error in mempool listener: {e}")
                await asyncio.sleep(5)

    async def _poll_pool_transactions(self, curve_address: str, token_id: str, limit: int = 50):
        try:
            async with httpx.AsyncClient() as client:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [curve_address, {"limit": limit}],
                }
                response = await client.post(self.rpc_url, json=payload, timeout=10.0)
                data = response.json()
                if "result" not in data:
                    return
                signatures = data.get("result", [])
                for sig_obj in signatures:
                    tx_signature = sig_obj.get("signature")
                    if tx_signature in self.processed_txs:
                        continue
                    self.processed_txs.add(tx_signature)
                    await self._parse_transaction(tx_signature, curve_address, token_id)
        except Exception as e:
            logger.error(f"Error polling transactions for {curve_address[:8]}...: {e}")

    async def _parse_transaction(self, tx_signature: str, curve_address: str, token_id: str):
        try:
            async with httpx.AsyncClient() as client:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [tx_signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                }
                response = await client.post(self.rpc_url, json=payload, timeout=10.0)
                data = response.json()
                if "result" not in data or data["result"] is None:
                    return
                tx = data["result"]
                meta = tx.get("transaction", {}).get("meta", {})
                if meta.get("err"):
                    return
                await self._extract_buy_info(tx_signature, tx, curve_address, token_id)
        except Exception as e:
            logger.debug(f"Error parsing transaction {tx_signature[:8]}...: {e}")

    async def _extract_buy_info(self, tx_signature: str, tx: dict, curve_address: str, token_id: str):
        try:
            message = tx.get("transaction", {}).get("message", {})
            instructions = message.get("instructions", [])
            for ix in instructions:
                accounts = ix.get("accounts", [])
                if curve_address not in accounts:
                    continue
                meta = tx.get("transaction", {}).get("meta", {})
                pre_balances = meta.get("preBalances", [])
                post_balances = meta.get("postBalances", [])
                signers = message.get("accountKeys", [])
                if len(pre_balances) > 0 and len(post_balances) > 0:
                    for i, (pre, post) in enumerate(zip(pre_balances, post_balances)):
                        change = pre - post
                        if change > 0:
                            wallet = signers[i] if i < len(signers) else None
                            if wallet and change >= 500_000_000:
                                amount_sol = change / 1_000_000_000
                                logger.debug(f"Detected buy: {wallet[:8]}... bought {amount_sol} SOL")
                                await self.detector.report_buy_transaction(
                                    token_id=token_id,
                                    wallet=wallet,
                                    amount_sol=amount_sol,
                                    tx_signature=tx_signature,
                                    tx_timestamp=datetime.utcnow().isoformat(),
                                )
                                break
        except Exception as e:
            logger.debug(f"Error extracting buy info: {e}")
