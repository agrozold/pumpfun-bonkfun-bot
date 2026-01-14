"""
Whale Tracker - отслеживает транзакции китов в реальном времени.
Когда кит покупает токен - отправляет сигнал на покупку.

Использует ОДНО WebSocket соединение к Solana RPC с подпиской на логи pump.fun.
Фильтрует транзакции по кошелькам китов локально.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import aiohttp
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

# pump.fun program ID
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


@dataclass
class WhaleBuy:
    """Информация о покупке кита."""
    whale_wallet: str
    token_mint: str
    token_symbol: str
    amount_sol: float
    timestamp: datetime
    tx_signature: str
    whale_label: str = "whale"


class WhaleTracker:
    """Отслеживает покупки китов через одно WebSocket соединение к pump.fun."""

    def __init__(
        self,
        wallets_file: str = "smart_money_wallets.json",
        min_buy_amount: float = 0.5,
        helius_api_key: str | None = None,
        rpc_endpoint: str | None = None,
        wss_endpoint: str | None = None,
    ):
        self.wallets_file = wallets_file
        self.min_buy_amount = min_buy_amount
        self.helius_api_key = helius_api_key
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        
        self.whale_wallets: dict[str, dict] = {}  # wallet -> info
        self.on_whale_buy: Callable | None = None
        self.running = False
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._processed_txs: set[str] = set()
        
        self._load_wallets()
        
        logger.info(f"WhaleTracker initialized with {len(self.whale_wallets)} wallets")

    def _load_wallets(self):
        """Загрузить список кошельков китов."""
        path = Path(self.wallets_file)
        if not path.exists():
            logger.warning(f"Wallets file not found: {self.wallets_file}")
            return
        
        try:
            with open(path) as f:
                data = json.load(f)
            
            for whale in data.get("whales", []):
                wallet = whale.get("wallet", "")
                if wallet:
                    self.whale_wallets[wallet] = {
                        "label": whale.get("label", "whale"),
                        "win_rate": whale.get("win_rate", 0.5),
                        "source": whale.get("source", "manual"),
                    }
            
            logger.info(f"Loaded {len(self.whale_wallets)} whale wallets")
        except Exception as e:
            logger.exception(f"Failed to load wallets: {e}")

    def add_wallet(self, wallet: str, label: str = "whale", win_rate: float = 0.5):
        """Добавить кошелёк для отслеживания."""
        self.whale_wallets[wallet] = {
            "label": label,
            "win_rate": win_rate,
            "source": "runtime",
        }
        logger.info(f"Added whale wallet: {wallet[:8]}... ({label})")

    def set_callback(self, callback: Callable):
        """Установить callback для сигналов о покупках китов."""
        self.on_whale_buy = callback

    def _get_wss_endpoint(self) -> str | None:
        """Получить WSS endpoint."""
        if self.wss_endpoint:
            return self.wss_endpoint
        if self.rpc_endpoint:
            if "https://" in self.rpc_endpoint:
                return self.rpc_endpoint.replace("https://", "wss://")
            elif "http://" in self.rpc_endpoint:
                return self.rpc_endpoint.replace("http://", "ws://")
        return None

    async def start(self):
        """Запустить отслеживание."""
        if not self.whale_wallets:
            logger.warning("No whale wallets to track")
            return
        
        wss_url = self._get_wss_endpoint()
        if not wss_url:
            logger.error("Cannot start whale tracker without WSS endpoint")
            return
        
        self.running = True
        self._session = aiohttp.ClientSession()
        
        logger.info(f"Starting whale tracker (single connection mode)")
        logger.info(f"Tracking {len(self.whale_wallets)} whale wallets")
        logger.info(f"Min buy amount: {self.min_buy_amount} SOL")
        
        # Одно соединение, подписка на pump.fun программу
        await self._track_pump_fun_logs(wss_url)

    async def stop(self):
        """Остановить отслеживание."""
        self.running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("Whale tracker stopped")

    async def _track_pump_fun_logs(self, wss_url: str):
        """Отслеживание через подписку на логи pump.fun программы."""
        while self.running:
            try:
                async with self._session.ws_connect(
                    wss_url,
                    heartbeat=30,
                    timeout=aiohttp.ClientTimeout(total=None),
                ) as ws:
                    self._ws = ws
                    
                    # Подписываемся на ВСЕ логи pump.fun программы
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [PUMP_FUN_PROGRAM]},
                            {"commitment": "processed"}
                        ]
                    }
                    
                    await ws.send_json(subscribe_msg)
                    logger.info(f"Subscribed to pump.fun logs (filtering {len(self.whale_wallets)} whales locally)")
                    
                    async for msg in ws:
                        if not self.running:
                            break
                        
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_pump_log(data)
                            except json.JSONDecodeError:
                                pass
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            logger.warning("WebSocket closed, reconnecting...")
                            break
                    
                    self._ws = None
                    
            except aiohttp.ClientError as e:
                logger.warning(f"WebSocket error: {e}")
            except Exception as e:
                logger.exception(f"Error in pump.fun log subscription: {e}")
            
            if self.running:
                logger.info("Reconnecting in 3s...")
                await asyncio.sleep(3)

    async def _handle_pump_log(self, data: dict):
        """Обработать лог от pump.fun."""
        if data.get("method") != "logsNotification":
            return
        
        try:
            params = data.get("params", {})
            result = params.get("result", {})
            value = result.get("value", {})
            
            signature = value.get("signature", "")
            logs = value.get("logs", [])
            err = value.get("err")
            
            if err or not signature:
                return
            
            if signature in self._processed_txs:
                return
            
            # Проверяем что это Buy инструкция
            is_buy = False
            for log in logs:
                if "Instruction: Buy" in log:
                    is_buy = True
                    break
            
            if not is_buy:
                return
            
            # Получаем детали транзакции и проверяем кошелёк
            await self._check_if_whale_tx(signature)
            
        except Exception as e:
            logger.debug(f"Error handling pump log: {e}")

    async def _check_if_whale_tx(self, signature: str):
        """Проверить, является ли транзакция покупкой кита."""
        if signature in self._processed_txs:
            return
        
        self._processed_txs.add(signature)
        if len(self._processed_txs) > 1000:
            self._processed_txs = set(list(self._processed_txs)[-500:])
        
        # Получаем детали через Helius (быстрее и удобнее)
        if self.helius_api_key:
            tx = await self._get_tx_helius(signature)
            if tx:
                await self._process_helius_tx(tx)
                return
        
        # Fallback на стандартный RPC
        if self.rpc_endpoint:
            tx = await self._get_tx_rpc(signature)
            if tx:
                await self._process_rpc_tx(tx, signature)

    async def _get_tx_helius(self, signature: str) -> dict | None:
        """Получить транзакцию через Helius."""
        url = "https://api.helius.xyz/v0/transactions"
        params = {"api-key": self.helius_api_key}
        
        try:
            async with self._session.post(
                url, params=params, json={"transactions": [signature]},
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data[0] if data else None
        except Exception as e:
            logger.debug(f"Helius error: {e}")
        return None

    async def _get_tx_rpc(self, signature: str) -> dict | None:
        """Получить транзакцию через RPC."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        }
        
        try:
            async with self._session.post(
                self.rpc_endpoint, json=payload,
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result")
        except Exception as e:
            logger.debug(f"RPC error: {e}")
        return None

    async def _process_helius_tx(self, tx: dict):
        """Обработать транзакцию от Helius."""
        try:
            fee_payer = tx.get("feePayer", "")
            
            # Проверяем, является ли fee_payer китом
            if fee_payer not in self.whale_wallets:
                return
            
            whale_info = self.whale_wallets[fee_payer]
            signature = tx.get("signature", "")
            
            # Считаем SOL
            sol_spent = 0
            token_mint = None
            
            for transfer in tx.get("nativeTransfers", []):
                if transfer.get("fromUserAccount") == fee_payer:
                    sol_spent += transfer.get("amount", 0) / 1e9
            
            for transfer in tx.get("tokenTransfers", []):
                if transfer.get("toUserAccount") == fee_payer:
                    token_mint = transfer.get("mint")
                    break
            
            if sol_spent >= self.min_buy_amount and token_mint:
                await self._emit_whale_buy(
                    wallet=fee_payer,
                    token_mint=token_mint,
                    sol_spent=sol_spent,
                    signature=signature,
                    whale_label=whale_info.get("label", "whale"),
                )
                
        except Exception as e:
            logger.debug(f"Error processing Helius tx: {e}")

    async def _process_rpc_tx(self, tx: dict, signature: str):
        """Обработать транзакцию от RPC."""
        try:
            message = tx.get("transaction", {}).get("message", {})
            account_keys = message.get("accountKeys", [])
            
            if not account_keys:
                return
            
            # fee_payer - первый аккаунт
            first_key = account_keys[0]
            fee_payer = first_key.get("pubkey", "") if isinstance(first_key, dict) else str(first_key)
            
            if fee_payer not in self.whale_wallets:
                return
            
            whale_info = self.whale_wallets[fee_payer]
            meta = tx.get("meta", {})
            
            # Считаем SOL
            pre = meta.get("preBalances", [])
            post = meta.get("postBalances", [])
            sol_spent = (pre[0] - post[0]) / 1e9 if pre and post else 0
            
            # Ищем токен
            token_mint = None
            for bal in meta.get("postTokenBalances", []):
                if bal.get("owner") == fee_payer:
                    token_mint = bal.get("mint")
                    break
            
            if sol_spent >= self.min_buy_amount and token_mint:
                await self._emit_whale_buy(
                    wallet=fee_payer,
                    token_mint=token_mint,
                    sol_spent=sol_spent,
                    signature=signature,
                    whale_label=whale_info.get("label", "whale"),
                )
                
        except Exception as e:
            logger.debug(f"Error processing RPC tx: {e}")

    async def _emit_whale_buy(self, wallet: str, token_mint: str, sol_spent: float, signature: str, whale_label: str):
        """Отправить сигнал о покупке кита."""
        whale_buy = WhaleBuy(
            whale_wallet=wallet,
            token_mint=token_mint,
            token_symbol="TOKEN",
            amount_sol=sol_spent,
            timestamp=datetime.utcnow(),
            tx_signature=signature,
            whale_label=whale_label,
        )
        
        logger.warning(
            f"🐋 WHALE BUY: {whale_label} ({wallet[:8]}...) "
            f"bought {token_mint[:8]}... for {sol_spent:.2f} SOL"
        )
        
        if self.on_whale_buy:
            await self.on_whale_buy(whale_buy)

    async def check_wallet_activity(self, wallet: str) -> list[WhaleBuy]:
        """Проверить активность кошелька (для ручной проверки)."""
        if not self._session:
            self._session = aiohttp.ClientSession()
        
        buys = []
        if self.helius_api_key:
            url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
            params = {"api-key": self.helius_api_key, "limit": 10, "type": "SWAP"}
            
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 200:
                        for tx in await resp.json():
                            if tx.get("type") == "SWAP":
                                buys.append(WhaleBuy(
                                    whale_wallet=wallet,
                                    token_mint=tx.get("tokenTransfers", [{}])[0].get("mint", ""),
                                    token_symbol="UNKNOWN",
                                    amount_sol=0,
                                    timestamp=datetime.utcnow(),
                                    tx_signature=tx.get("signature", ""),
                                ))
            except Exception as e:
                logger.exception(f"Error checking wallet: {e}")
        
        return buys

    def get_tracked_wallets(self) -> list[str]:
        """Получить список отслеживаемых кошельков."""
        return list(self.whale_wallets.keys())
