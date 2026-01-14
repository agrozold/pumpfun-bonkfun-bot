"""
Whale Tracker - отслеживает транзакции китов в реальном времени.
Когда кит покупает токен - отправляет сигнал на покупку.

Использует Helius WebSocket для мгновенного получения транзакций.
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
    """Отслеживает покупки китов через Helius WebSocket (real-time)."""

    def __init__(
        self,
        wallets_file: str = "smart_money_wallets.json",
        min_buy_amount: float = 0.5,  # Минимум SOL для копирования
        helius_api_key: str | None = None,
        rpc_endpoint: str | None = None,
    ):
        self.wallets_file = wallets_file
        self.min_buy_amount = min_buy_amount
        self.helius_api_key = helius_api_key
        self.rpc_endpoint = rpc_endpoint
        
        self.whale_wallets: dict[str, dict] = {}  # wallet -> info
        self.on_whale_buy: Callable | None = None
        self.running = False
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._processed_txs: set[str] = set()
        
        self._load_wallets()
        
        if self.helius_api_key:
            logger.info(f"WhaleTracker initialized with {len(self.whale_wallets)} wallets, Helius WebSocket enabled")
        else:
            logger.warning("WhaleTracker initialized WITHOUT Helius API key - tracking disabled!")

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

    async def start(self):
        """Запустить отслеживание."""
        if not self.whale_wallets:
            logger.warning("No whale wallets to track")
            return
        
        if not self.helius_api_key:
            logger.error("Cannot start whale tracker without Helius API key")
            return
        
        self.running = True
        self._session = aiohttp.ClientSession()
        
        logger.info(f"Starting whale tracker WebSocket for {len(self.whale_wallets)} wallets")
        
        # Запускаем WebSocket подключение
        await self._track_with_websocket()

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

    async def _track_with_websocket(self):
        """Real-time отслеживание через Helius WebSocket."""
        ws_url = f"wss://atlas-mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        
        while self.running:
            try:
                logger.info("Connecting to Helius WebSocket...")
                
                async with self._session.ws_connect(ws_url) as ws:
                    self._ws = ws
                    logger.info("Connected to Helius WebSocket")
                    
                    # Подписываемся на транзакции всех whale кошельков
                    for wallet in self.whale_wallets.keys():
                        subscribe_msg = {
                            "jsonrpc": "2.0",
                            "id": f"whale_{wallet[:8]}",
                            "method": "transactionSubscribe",
                            "params": [
                                {
                                    "accountInclude": [wallet],
                                },
                                {
                                    "commitment": "confirmed",
                                    "encoding": "jsonParsed",
                                    "transactionDetails": "full",
                                    "maxSupportedTransactionVersion": 0,
                                }
                            ]
                        }
                        await ws.send_json(subscribe_msg)
                        logger.info(f"Subscribed to whale: {wallet[:8]}...")
                    
                    # Слушаем сообщения
                    async for msg in ws:
                        if not self.running:
                            break
                        
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._process_ws_message(data)
                            except json.JSONDecodeError:
                                logger.warning(f"Invalid JSON from WebSocket: {msg.data[:100]}")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"WebSocket error: {ws.exception()}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning("WebSocket closed")
                            break
                
            except Exception as e:
                logger.exception(f"WebSocket error: {e}")
            
            if self.running:
                logger.info("Reconnecting WebSocket in 3 seconds...")
                await asyncio.sleep(3)

    async def _process_ws_message(self, data: dict):
        """Обработать сообщение от WebSocket."""
        # Проверяем что это уведомление о транзакции
        if data.get("method") != "transactionNotification":
            return
        
        params = data.get("params", {})
        result = params.get("result", {})
        
        tx_sig = result.get("signature", "")
        if not tx_sig or tx_sig in self._processed_txs:
            return
        
        # Помечаем как обработанную
        self._processed_txs.add(tx_sig)
        if len(self._processed_txs) > 1000:
            self._processed_txs = set(list(self._processed_txs)[-500:])
        
        # Парсим транзакцию
        tx = result.get("transaction", {})
        meta = tx.get("meta", {})
        
        if meta.get("err"):
            return  # Пропускаем failed транзакции
        
        # Ищем какой whale сделал транзакцию
        account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        whale_wallet = None
        whale_info = None
        
        for acc in account_keys:
            pubkey = acc.get("pubkey") if isinstance(acc, dict) else acc
            if pubkey in self.whale_wallets:
                whale_wallet = pubkey
                whale_info = self.whale_wallets[pubkey]
                break
        
        if not whale_wallet:
            return
        
        # Анализируем баланс изменения
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        pre_token_balances = meta.get("preTokenBalances", [])
        post_token_balances = meta.get("postTokenBalances", [])
        
        # Находим индекс whale кошелька
        whale_index = None
        for i, acc in enumerate(account_keys):
            pubkey = acc.get("pubkey") if isinstance(acc, dict) else acc
            if pubkey == whale_wallet:
                whale_index = i
                break
        
        if whale_index is None:
            return
        
        # Считаем потраченный SOL
        sol_spent = 0
        if whale_index < len(pre_balances) and whale_index < len(post_balances):
            sol_diff = (pre_balances[whale_index] - post_balances[whale_index]) / 1e9
            if sol_diff > 0:
                sol_spent = sol_diff
        
        # Находим полученный токен
        token_mint = None
        token_received = 0
        
        for post_bal in post_token_balances:
            if post_bal.get("owner") == whale_wallet:
                mint = post_bal.get("mint")
                post_amount = float(post_bal.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                
                # Проверяем был ли токен до транзакции
                pre_amount = 0
                for pre_bal in pre_token_balances:
                    if pre_bal.get("owner") == whale_wallet and pre_bal.get("mint") == mint:
                        pre_amount = float(pre_bal.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                        break
                
                received = post_amount - pre_amount
                if received > 0 and received > token_received:
                    token_mint = mint
                    token_received = received
        
        # Проверяем минимальную сумму покупки
        if sol_spent >= self.min_buy_amount and token_mint:
            whale_buy = WhaleBuy(
                whale_wallet=whale_wallet,
                token_mint=token_mint,
                token_symbol="Fungible",  # Helius не даёт символ в WS
                amount_sol=sol_spent,
                timestamp=datetime.utcnow(),
                tx_signature=tx_sig,
                whale_label=whale_info.get("label", "whale"),
            )
            
            logger.warning(
                f"🐋 WHALE BUY DETECTED: {whale_buy.whale_label} ({whale_wallet[:8]}...) "
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
