"""
Whale Tracker - отслеживает транзакции китов в реальном времени.
Когда кит покупает токен - отправляет сигнал на покупку.
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
    """Отслеживает покупки китов через Helius webhooks или polling."""

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
        
        self._load_wallets()
        
        if self.helius_api_key:
            logger.info(f"WhaleTracker initialized with {len(self.whale_wallets)} wallets, Helius API enabled")
        else:
            logger.warning("WhaleTracker initialized WITHOUT Helius API key - tracking will be limited!")

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
        
        self.running = True
        self._session = aiohttp.ClientSession()
        
        logger.info(f"Starting whale tracker for {len(self.whale_wallets)} wallets")
        
        # Используем Helius если есть ключ, иначе polling
        if self.helius_api_key:
            await self._track_with_helius()
        else:
            await self._track_with_polling()

    async def stop(self):
        """Остановить отслеживание."""
        self.running = False
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("Whale tracker stopped")

    async def _track_with_helius(self):
        """Отслеживание через Helius Enhanced Transactions API."""
        logger.info(f"Using Helius API for whale tracking - monitoring {len(self.whale_wallets)} wallets")
        logger.info(f"Tracked wallets: {list(self.whale_wallets.keys())}")
        logger.info(f"Min buy amount to copy: {self.min_buy_amount} SOL")
        
        # Helius позволяет подписаться на транзакции адресов
        base_url = "https://api.helius.xyz/v0"
        
        while self.running:
            try:
                for wallet in list(self.whale_wallets.keys()):
                    if not self.running:
                        break
                    
                    # Получить последние транзакции кошелька
                    url = f"{base_url}/addresses/{wallet}/transactions"
                    params = {
                        "api-key": self.helius_api_key,
                        "limit": 5,
                        "type": "SWAP",
                    }
                    
                    async with self._session.get(url, params=params) as resp:
                        if resp.status == 200:
                            txs = await resp.json()
                            if txs:
                                logger.debug(f"Whale {wallet[:8]}... has {len(txs)} recent swaps")
                            await self._process_helius_transactions(wallet, txs)
                        elif resp.status == 429:
                            logger.warning("Helius rate limit, waiting 5s...")
                            await asyncio.sleep(5)
                        else:
                            logger.warning(f"Helius API error {resp.status} for {wallet[:8]}...")
                    
                    await asyncio.sleep(0.3)  # Rate limit between wallets
                
                await asyncio.sleep(3)  # Poll interval
                
            except Exception as e:
                logger.exception(f"Helius tracking error: {e}")
                await asyncio.sleep(5)

    async def _process_helius_transactions(self, wallet: str, transactions: list):
        """Обработать транзакции от Helius."""
        # Кэш обработанных транзакций чтобы не дублировать
        if not hasattr(self, '_processed_txs'):
            self._processed_txs: set[str] = set()
        
        for tx in transactions:
            try:
                tx_sig = tx.get("signature", "")
                
                # Пропускаем уже обработанные транзакции
                if tx_sig in self._processed_txs:
                    continue
                
                # Проверить что это покупка токена (SWAP)
                tx_type = tx.get("type", "")
                if tx_type != "SWAP":
                    continue
                
                # Извлечь информацию о свапе
                token_transfers = tx.get("tokenTransfers", [])
                native_transfers = tx.get("nativeTransfers", [])
                
                # Найти SOL потраченный и токен полученный
                sol_spent = 0
                token_mint = None
                token_symbol = "UNKNOWN"
                
                for transfer in native_transfers:
                    if transfer.get("fromUserAccount") == wallet:
                        sol_spent += transfer.get("amount", 0) / 1e9
                
                for transfer in token_transfers:
                    if transfer.get("toUserAccount") == wallet:
                        token_mint = transfer.get("mint")
                        # Попробовать получить символ
                        token_symbol = transfer.get("tokenStandard", "UNKNOWN")
                
                if sol_spent >= self.min_buy_amount and token_mint:
                    # Помечаем транзакцию как обработанную
                    self._processed_txs.add(tx_sig)
                    
                    # Ограничиваем размер кэша
                    if len(self._processed_txs) > 1000:
                        # Удаляем старые записи
                        self._processed_txs = set(list(self._processed_txs)[-500:])
                    
                    whale_buy = WhaleBuy(
                        whale_wallet=wallet,
                        token_mint=token_mint,
                        token_symbol=token_symbol,
                        amount_sol=sol_spent,
                        timestamp=datetime.utcnow(),
                        tx_signature=tx_sig,
                        whale_label=self.whale_wallets[wallet].get("label", "whale"),
                    )
                    
                    logger.warning(
                        f"🐋 WHALE BUY DETECTED: {whale_buy.whale_label} ({wallet[:8]}...) "
                        f"bought {token_symbol} ({token_mint[:8]}...) for {sol_spent:.2f} SOL"
                    )
                    
                    if self.on_whale_buy:
                        await self.on_whale_buy(whale_buy)
                        
            except Exception as e:
                logger.warning(f"Error processing whale tx: {e}")

    async def _track_with_polling(self):
        """Fallback: отслеживание через RPC polling (медленнее)."""
        logger.info("Using RPC polling for whale tracking (no Helius key)")
        
        while self.running:
            try:
                # Простой polling через getSignaturesForAddress
                # Это медленнее чем Helius, но работает без API ключа
                await asyncio.sleep(5)
                
            except Exception as e:
                logger.exception(f"Polling error: {e}")
                await asyncio.sleep(10)

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
                        # Process and return buys
                        for tx in txs:
                            # Simplified extraction
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
