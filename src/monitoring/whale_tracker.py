"""
Whale Tracker - отслеживает транзакции китов в реальном времени.
Когда кит покупает токен - отправляет сигнал на покупку.

Использует стандартный Solana RPC WebSocket (logsSubscribe) для мгновенного получения.
Helius HTTP API используется только для получения деталей транзакции.
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
    """Отслеживает покупки китов через Solana RPC WebSocket (logsSubscribe)."""

    def __init__(
        self,
        wallets_file: str = "smart_money_wallets.json",
        min_buy_amount: float = 0.5,  # Минимум SOL для копирования
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
        self._ws_connections: list[aiohttp.ClientWebSocketResponse] = []
        self._processed_txs: set[str] = set()
        self._subscription_ids: dict[str, int] = {}  # wallet -> subscription_id
        
        self._load_wallets()
        
        logger.info(f"WhaleTracker initialized with {len(self.whale_wallets)} wallets")
        if self.wss_endpoint:
            logger.info(f"Using Solana RPC WebSocket: {self.wss_endpoint[:50]}...")
        if self.helius_api_key:
            logger.info("Helius HTTP API enabled for transaction details")

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
        """Получить WSS endpoint из конфига или сконструировать из RPC."""
        if self.wss_endpoint:
            return self.wss_endpoint
        
        # Попробовать сконструировать из HTTP endpoint
        if self.rpc_endpoint:
            if "https://" in self.rpc_endpoint:
                return self.rpc_endpoint.replace("https://", "wss://")
            elif "http://" in self.rpc_endpoint:
                return self.rpc_endpoint.replace("http://", "ws://")
        
        return None

    async def start(self):
        """Запустить отслеживание через Solana RPC WebSocket."""
        if not self.whale_wallets:
            logger.warning("No whale wallets to track")
            return
        
        wss_url = self._get_wss_endpoint()
        if not wss_url:
            logger.error("Cannot start whale tracker without WSS endpoint")
            return
        
        self.running = True
        self._session = aiohttp.ClientSession()
        
        logger.info(f"Starting whale tracker for {len(self.whale_wallets)} wallets")
        logger.info(f"Using Solana RPC WebSocket: {wss_url[:50]}...")
        logger.info(f"Min buy amount to copy: {self.min_buy_amount} SOL")
        
        # Запускаем WebSocket подписки для каждого кошелька
        await self._track_with_logs_subscribe(wss_url)

    async def stop(self):
        """Остановить отслеживание."""
        self.running = False
        
        # Закрыть все WebSocket соединения
        for ws in self._ws_connections:
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_connections.clear()
        
        if self._session:
            await self._session.close()
            self._session = None
        
        logger.info("Whale tracker stopped")

    async def _track_with_logs_subscribe(self, wss_url: str):
        """Отслеживание через Solana RPC logsSubscribe."""
        wallets = list(self.whale_wallets.keys())
        logger.info(f"Subscribing to logs for {len(wallets)} whale wallets...")
        
        # Создаём задачи для каждого кошелька
        tasks = []
        for wallet in wallets:
            task = asyncio.create_task(
                self._subscribe_to_wallet_logs(wss_url, wallet)
            )
            tasks.append(task)
        
        # Ждём все задачи (они будут работать бесконечно пока running=True)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.exception(f"Error in logs subscribe: {e}")

    async def _subscribe_to_wallet_logs(self, wss_url: str, wallet: str):
        """Подписаться на логи конкретного кошелька."""
        whale_info = self.whale_wallets.get(wallet, {})
        whale_label = whale_info.get("label", "whale")
        
        while self.running:
            try:
                async with self._session.ws_connect(
                    wss_url,
                    heartbeat=30,
                    timeout=aiohttp.ClientTimeout(total=None),
                ) as ws:
                    self._ws_connections.append(ws)
                    
                    # Подписываемся на логи с упоминанием кошелька
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [wallet]},
                            {"commitment": "processed"}
                        ]
                    }
                    
                    await ws.send_json(subscribe_msg)
                    logger.info(f"Subscribed to logs for {whale_label} ({wallet[:8]}...)")
                    
                    # Слушаем сообщения
                    async for msg in ws:
                        if not self.running:
                            break
                        
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_log_notification(wallet, data)
                            except json.JSONDecodeError:
                                pass
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning(f"WebSocket error for {wallet[:8]}...")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning(f"WebSocket closed for {wallet[:8]}...")
                            break
                    
                    if ws in self._ws_connections:
                        self._ws_connections.remove(ws)
                        
            except aiohttp.ClientError as e:
                logger.warning(f"WebSocket connection error for {wallet[:8]}...: {e}")
            except Exception as e:
                logger.exception(f"Error in wallet subscription {wallet[:8]}...: {e}")
            
            if self.running:
                logger.info(f"Reconnecting to {wallet[:8]}... in 3s")
                await asyncio.sleep(3)

    async def _handle_log_notification(self, wallet: str, data: dict):
        """Обработать уведомление о логах."""
        # Проверяем что это уведомление (не ответ на подписку)
        if "method" not in data or data.get("method") != "logsNotification":
            return
        
        try:
            params = data.get("params", {})
            result = params.get("result", {})
            value = result.get("value", {})
            
            signature = value.get("signature", "")
            logs = value.get("logs", [])
            err = value.get("err")
            
            # Пропускаем неудачные транзакции
            if err:
                return
            
            # Пропускаем уже обработанные
            if signature in self._processed_txs:
                return
            
            # Проверяем что это pump.fun транзакция (ищем в логах)
            is_pump_tx = False
            for log in logs:
                if "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P" in log:  # pump.fun program
                    is_pump_tx = True
                    break
                if "Program log: Instruction: Buy" in log:
                    is_pump_tx = True
                    break
            
            if not is_pump_tx:
                return
            
            logger.info(f"🔔 Detected pump.fun tx from whale {wallet[:8]}...: {signature[:16]}...")
            
            # Получаем детали транзакции через Helius HTTP API
            await self._fetch_and_process_transaction(wallet, signature)
            
        except Exception as e:
            logger.debug(f"Error handling log notification: {e}")

    async def _fetch_and_process_transaction(self, wallet: str, signature: str):
        """Получить детали транзакции и обработать."""
        if signature in self._processed_txs:
            return
        
        self._processed_txs.add(signature)
        
        # Ограничиваем кэш
        if len(self._processed_txs) > 1000:
            self._processed_txs = set(list(self._processed_txs)[-500:])
        
        whale_info = self.whale_wallets.get(wallet, {})
        whale_label = whale_info.get("label", "whale")
        
        # Пробуем получить детали через Helius
        if self.helius_api_key:
            tx_details = await self._get_tx_details_helius(signature)
            if tx_details:
                await self._process_helius_tx(wallet, tx_details, whale_label)
                return
        
        # Fallback: используем стандартный RPC для получения деталей
        if self.rpc_endpoint:
            tx_details = await self._get_tx_details_rpc(signature)
            if tx_details:
                await self._process_rpc_tx(wallet, tx_details, whale_label, signature)
                return
        
        logger.warning(f"Could not fetch tx details for {signature[:16]}...")

    async def _get_tx_details_helius(self, signature: str) -> dict | None:
        """Получить детали транзакции через Helius API."""
        url = f"https://api.helius.xyz/v0/transactions"
        params = {"api-key": self.helius_api_key}
        payload = {"transactions": [signature]}
        
        try:
            async with self._session.post(
                url, 
                params=params, 
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return data[0]
        except Exception as e:
            logger.debug(f"Helius API error: {e}")
        
        return None

    async def _get_tx_details_rpc(self, signature: str) -> dict | None:
        """Получить детали транзакции через стандартный RPC."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ]
        }
        
        try:
            async with self._session.post(
                self.rpc_endpoint,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result")
        except Exception as e:
            logger.debug(f"RPC error: {e}")
        
        return None

    async def _process_helius_tx(self, wallet: str, tx: dict, whale_label: str):
        """Обработать транзакцию от Helius."""
        try:
            # Проверяем что это SWAP/BUY
            tx_type = tx.get("type", "")
            if tx_type not in ["SWAP", "UNKNOWN"]:
                return
            
            signature = tx.get("signature", "")
            token_transfers = tx.get("tokenTransfers", [])
            native_transfers = tx.get("nativeTransfers", [])
            
            # Считаем потраченные SOL
            sol_spent = 0
            for transfer in native_transfers:
                if transfer.get("fromUserAccount") == wallet:
                    sol_spent += transfer.get("amount", 0) / 1e9
            
            # Находим полученный токен
            token_mint = None
            for transfer in token_transfers:
                if transfer.get("toUserAccount") == wallet:
                    token_mint = transfer.get("mint")
                    break
            
            if sol_spent >= self.min_buy_amount and token_mint:
                await self._emit_whale_buy(
                    wallet=wallet,
                    token_mint=token_mint,
                    sol_spent=sol_spent,
                    signature=signature,
                    whale_label=whale_label,
                )
                
        except Exception as e:
            logger.debug(f"Error processing Helius tx: {e}")

    async def _process_rpc_tx(self, wallet: str, tx: dict, whale_label: str, signature: str):
        """Обработать транзакцию от стандартного RPC."""
        try:
            meta = tx.get("meta", {})
            if meta.get("err"):
                return
            
            # Считаем изменение SOL баланса
            pre_balances = meta.get("preBalances", [])
            post_balances = meta.get("postBalances", [])
            
            account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            
            sol_spent = 0
            wallet_index = None
            
            for i, key in enumerate(account_keys):
                key_str = key.get("pubkey", "") if isinstance(key, dict) else str(key)
                if key_str == wallet:
                    wallet_index = i
                    break
            
            if wallet_index is not None and wallet_index < len(pre_balances):
                sol_diff = (pre_balances[wallet_index] - post_balances[wallet_index]) / 1e9
                if sol_diff > 0:
                    sol_spent = sol_diff
            
            # Ищем токен в postTokenBalances
            token_mint = None
            post_token_balances = meta.get("postTokenBalances", [])
            for balance in post_token_balances:
                owner = balance.get("owner", "")
                if owner == wallet:
                    token_mint = balance.get("mint")
                    break
            
            if sol_spent >= self.min_buy_amount and token_mint:
                await self._emit_whale_buy(
                    wallet=wallet,
                    token_mint=token_mint,
                    sol_spent=sol_spent,
                    signature=signature,
                    whale_label=whale_label,
                )
                
        except Exception as e:
            logger.debug(f"Error processing RPC tx: {e}")

    async def _emit_whale_buy(
        self, 
        wallet: str, 
        token_mint: str, 
        sol_spent: float, 
        signature: str,
        whale_label: str,
    ):
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
            f"🐋 WHALE BUY DETECTED: {whale_label} ({wallet[:8]}...) "
            f"bought {token_mint[:8]}... for {sol_spent:.2f} SOL"
        )
        
        if self.on_whale_buy:
            await self.on_whale_buy(whale_buy)

    async def check_wallet_activity(self, wallet: str) -> list[WhaleBuy]:
        """Проверить последнюю активность кошелька (для ручной проверки)."""
        if not self._session:
            self._session = aiohttp.ClientSession()
        
        buys = []
        
        if self.helius_api_key:
            url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
            params = {"api-key": self.helius_api_key, "limit": 10, "type": "SWAP"}
            
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 200:
                        txs = await resp.json()
                        for tx in txs:
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
