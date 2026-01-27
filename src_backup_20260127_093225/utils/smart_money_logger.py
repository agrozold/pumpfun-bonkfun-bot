"""Smart Money Logger - логирование сделок"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

class SmartMoneyLogger:
    def __init__(self, log_file: str = "smart_money_log.json"):
        self.log_file = Path(log_file)
        self.entries: Dict[str, dict] = {}
        self.load_log()

    def load_log(self):
        if not self.log_file.exists():
            logger.info(f"Creating new smart money log: {self.log_file}")
            return
        try:
            with open(self.log_file, "r") as f:
                self.entries = json.load(f)
            logger.info(f"Loaded {len(self.entries)} entries from {self.log_file}")
        except Exception as e:
            logger.error(f"Error loading smart money log: {e}")
            self.entries = {}

    def log_token_detection(self, token_mint: str, token_symbol: str, platform: str, curve_address: str) -> str:
        token_id = f"{token_mint}_{datetime.utcnow().isoformat()}"
        self.entries[token_id] = {
            "token_mint": token_mint,
            "token_symbol": token_symbol,
            "platform": platform,
            "curve_address": curve_address,
            "detection_timestamp": datetime.utcnow().isoformat(),
            "detected_smart_buys": [],
            "action_taken": None,
            "our_buy_amount": None,
            "our_tx_signature": None,
            "status": "monitoring",
        }
        logger.info(f"Detected new token: {token_symbol} ({token_mint}) on {platform}")
        self.save_log()
        return token_id

    def log_smart_buy(self, token_id: str, wallet: str, amount_sol: float, timestamp: str, label: str, tx_signature: str):
        if token_id not in self.entries:
            logger.warning(f"Token ID {token_id} not found in log")
            return
        self.entries[token_id]["detected_smart_buys"].append({
            "wallet": wallet,
            "amount_sol": amount_sol,
            "timestamp": timestamp,
            "label": label,
            "tx_signature": tx_signature,
        })
        logger.info(f"Logged smart buy for {self.entries[token_id]['token_symbol']}: {wallet[:8]}... bought {amount_sol} SOL")
        self.save_log()

    def save_log(self):
        try:
            with open(self.log_file, "w") as f:
                json.dump(self.entries, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving smart money log: {e}")

    def get_statistics(self) -> dict:
        total_entries = len(self.entries)
        completed = sum(1 for e in self.entries.values() if e.get("status") == "completed")
        with_smart_buys = sum(1 for e in self.entries.values() if len(e.get("detected_smart_buys", [])) > 0)
        copy_buys = sum(1 for e in self.entries.values() if e.get("action_taken") == "COPY_BUY")
        return {
            "total_tokens_tracked": total_entries,
            "completed_trades": completed,
            "tokens_with_smart_buys": with_smart_buys,
            "copy_trades_executed": copy_buys,
        }
