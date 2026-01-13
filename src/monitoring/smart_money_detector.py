"""Smart Money Detector - отслеживает покупки whale'ов"""
import asyncio
import logging
from typing import Dict, Optional, Callable
from datetime import datetime, timedelta

from src.utils.whale_database import WhaleDatabase
from src.utils.smart_money_logger import SmartMoneyLogger

logger = logging.getLogger(__name__)

class SmartMoneyDetector:
    def __init__(self, rpc_url: str, wallets_file: str = "smart_money_wallets.json", log_file: str = "smart_money_log.json", tracking_window_minutes: int = 5, min_buy_amount_sol: float = 0.5):
        self.rpc_url = rpc_url
        self.whale_db = WhaleDatabase(wallets_file)
        self.smart_money_logger = SmartMoneyLogger(log_file)
        self.tracking_window = timedelta(minutes=tracking_window_minutes)
        self.min_buy_amount = min_buy_amount_sol
        self.active_tracks: Dict[str, dict] = {}
        self.on_smart_buy_callback: Optional[Callable] = None
        logger.info(f"SmartMoneyDetector initialized with {len(self.whale_db.get_all_whales())} whales")

    def set_on_smart_buy_callback(self, callback: Callable):
        self.on_smart_buy_callback = callback

    async def start_tracking_token(self, token_mint: str, token_symbol: str, platform: str, curve_address: str) -> str:
        token_id = self.smart_money_logger.log_token_detection(token_mint, token_symbol, platform, curve_address)
        self.active_tracks[token_id] = {
            "token_mint": token_mint,
            "token_symbol": token_symbol,
            "platform": platform,
            "curve_address": curve_address,
            "start_time": datetime.utcnow(),
            "smart_buys_found": [],
        }
        logger.info(f"Started tracking token {token_symbol} (ID: {token_id})")
        return token_id

    async def report_buy_transaction(self, token_id: str, wallet: str, amount_sol: float, tx_signature: str, tx_timestamp: str):
        if token_id not in self.active_tracks:
            return
        track = self.active_tracks[token_id]
        is_known_whale = self.whale_db.is_whale(wallet)
        is_large_buy = amount_sol >= self.min_buy_amount
        if not (is_known_whale or is_large_buy):
            return
        label = "whale" if is_known_whale else "smart_money"
        self.smart_money_logger.log_smart_buy(token_id=token_id, wallet=wallet, amount_sol=amount_sol, timestamp=tx_timestamp, label=label, tx_signature=tx_signature)
        track["smart_buys_found"].append({"wallet": wallet, "amount": amount_sol, "label": label, "tx": tx_signature})
        logger.info(f"[SMART MONEY DETECTED] {track['token_symbol']}: {wallet[:8]}... ({label}) bought {amount_sol} SOL")

    def has_smart_buys(self, token_id: str) -> bool:
        if token_id not in self.active_tracks:
            entry = self.smart_money_logger.get_entry(token_id) if hasattr(self.smart_money_logger, 'get_entry') else None
            if entry:
                return len(entry.get("detected_smart_buys", [])) > 0
            return False
        return len(self.active_tracks[token_id]["smart_buys_found"]) > 0

    def should_trade(self, token_id: str) -> bool:
        return self.has_smart_buys(token_id)

    def get_statistics(self) -> dict:
        return self.smart_money_logger.get_statistics()
