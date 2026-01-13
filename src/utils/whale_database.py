"""
Whale Database Manager
Управляет локальной БД известных whale'ов и smart money кошельков
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class WhaleDatabase:
    """
    Локальная БД whale'ов с их статистикой
    """

    def __init__(self, wallets_file: str = "smart_money_wallets.json"):
        self.wallets_file = Path(wallets_file)
        self.whales: Dict[str, dict] = {}
        self.min_buy_amount = 0.5
        self.min_win_rate = 0.65

        self.load_wallets()

    def load_wallets(self):
        """Загрузить whale'ов из JSON"""
        if not self.wallets_file.exists():
            logger.warning(f"Whale file not found: {self.wallets_file}")
            return

        try:
            with open(self.wallets_file, "r") as f:
                data = json.load(f)

            self.min_buy_amount = data.get("min_buy_amount_sol", 0.5)
            self.min_win_rate = data.get("min_win_rate_threshold", 0.65)

            for whale in data.get("whales", []):
                wallet = whale["wallet"]
                self.whales[wallet] = {
                    "win_rate": whale.get("win_rate", 0.0),
                    "trades_count": whale.get("trades_count", 0),
                    "label": whale.get("label", "whale"),
                    "source": whale.get("source", "manual"),
                    "added_date": whale.get("added_date", datetime.utcnow().isoformat()),
                }

            logger.info(
                f"Loaded {len(self.whales)} whales from {self.wallets_file}"
            )

        except Exception as e:
            logger.error(f"Error loading whale database: {e}")

    def is_whale(self, wallet_address: str) -> bool:
        """Проверить есть ли wallet в БД"""
        return wallet_address in self.whales

    def get_whale_info(self, wallet_address: str) -> Optional[dict]:
        """Получить инфо о whale'е"""
        return self.whales.get(wallet_address)

    def get_all_whales(self) -> List[str]:
        """Получить список всех whale'ов"""
        return list(self.whales.keys())

    def update_whale_stats(
        self,
        wallet_address: str,
        trades_count: int,
        win_rate: float,
    ):
        """Обновить статистику whale'а (если отслеживаем локально)"""
        if wallet_address in self.whales:
            self.whales[wallet_address]["trades_count"] = trades_count
            self.whales[wallet_address]["win_rate"] = win_rate

    def add_whale(
        self,
        wallet_address: str,
        win_rate: float = 0.65,
        label: str = "whale",
    ):
        """Добавить нового whale'а"""
        if wallet_address not in self.whales:
            self.whales[wallet_address] = {
                "win_rate": win_rate,
                "trades_count": 0,
                "label": label,
                "source": "manual",
                "added_date": datetime.utcnow().isoformat(),
            }
            logger.info(f"Added new whale: {wallet_address}")
            self.save_to_file()

    def save_to_file(self):
        """Сохранить БД в файл"""
        try:
            data = {
                "whales": [
                    {
                        "wallet": wallet,
                        "win_rate": info["win_rate"],
                        "trades_count": info["trades_count"],
                        "label": info["label"],
                        "source": info["source"],
                        "added_date": info["added_date"],
                    }
                    for wallet, info in self.whales.items()
                ],
                "min_buy_amount_sol": self.min_buy_amount,
                "min_win_rate_threshold": self.min_win_rate,
                "tracking_enabled": True,
                "last_updated": datetime.utcnow().isoformat(),
            }

            with open(self.wallets_file, "w") as f:
                json.dump(data, f, indent=2)

            logger.info(f"Saved {len(self.whales)} whales to {self.wallets_file}")

        except Exception as e:
            logger.error(f"Error saving whale database: {e}")
