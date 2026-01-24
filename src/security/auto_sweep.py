"""
Auto-Sweep - автоматический перевод средств на cold wallet.
Защита от потерь при взломе hot wallet.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Callable, Awaitable

from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction
from solders.message import Message

logger = logging.getLogger(__name__)


@dataclass
class SweepConfig:
    """Конфигурация Auto-Sweep"""
    enabled: bool = False
    cold_wallet: Optional[str] = None          # Адрес cold wallet
    sweep_threshold_sol: float = 1.0           # Переводить если баланс > X SOL
    keep_balance_sol: float = 0.1              # Оставлять X SOL на hot wallet
    check_interval_sec: int = 300              # Проверять каждые N секунд
    max_daily_loss_sol: float = 0.0            # 0 = без лимита
    
    @classmethod
    def from_dict(cls, data: dict) -> "SweepConfig":
        return cls(
            enabled=data.get("enabled", False),
            cold_wallet=data.get("cold_wallet"),
            sweep_threshold_sol=data.get("sweep_threshold_sol", 1.0),
            keep_balance_sol=data.get("keep_balance_sol", 0.1),
            check_interval_sec=data.get("check_interval_sec", 300),
            max_daily_loss_sol=data.get("max_daily_loss_sol", 0.0),
        )


@dataclass
class DailyStats:
    """Статистика за день"""
    date: str
    total_bought_sol: float = 0.0
    total_sold_sol: float = 0.0
    realized_pnl_sol: float = 0.0
    sweep_count: int = 0
    sweep_total_sol: float = 0.0
    
    @property
    def net_loss(self) -> float:
        """Чистый убыток (отрицательное значение = убыток)"""
        return self.realized_pnl_sol


class TradingLimiter:
    """
    Контроль торговых лимитов и daily loss.
    """
    
    def __init__(
        self,
        max_daily_loss_sol: float = 0.0,
        max_position_size_sol: float = 0.0,
        on_limit_reached: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self.max_daily_loss = max_daily_loss_sol
        self.max_position_size = max_position_size_sol
        self.on_limit_reached = on_limit_reached
        self._stats = DailyStats(date=datetime.utcnow().strftime("%Y-%m-%d"))
        self._trading_halted = False
    
    def _check_date(self) -> None:
        """Сброс статистики при смене дня"""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self._stats.date != today:
            logger.info(f"[LIMITS] New day {today}, resetting stats")
            self._stats = DailyStats(date=today)
            self._trading_halted = False
    
    def record_buy(self, sol_amount: float) -> None:
        """Записать покупку"""
        self._check_date()
        self._stats.total_bought_sol += sol_amount
        logger.debug(f"[LIMITS] Buy recorded: {sol_amount} SOL")
    
    def record_sell(self, sol_amount: float, pnl: float) -> None:
        """Записать продажу с PnL"""
        self._check_date()
        self._stats.total_sold_sol += sol_amount
        self._stats.realized_pnl_sol += pnl
        
        logger.debug(f"[LIMITS] Sell recorded: {sol_amount} SOL, PnL: {pnl} SOL")
        
        # Проверяем лимит убытков
        if self.max_daily_loss > 0 and self._stats.net_loss < -self.max_daily_loss:
            self._trigger_halt()
    
    def _trigger_halt(self) -> None:
        """Остановить торговлю"""
        if not self._trading_halted:
            self._trading_halted = True
            logger.error(
                f"[LIMITS] DAILY LOSS LIMIT REACHED! "
                f"Loss: {abs(self._stats.net_loss):.4f} SOL > Max: {self.max_daily_loss} SOL"
            )
            if self.on_limit_reached:
                asyncio.create_task(self.on_limit_reached())
    
    def can_trade(self) -> bool:
        """Можно ли торговать"""
        self._check_date()
        return not self._trading_halted
    
    def can_buy(self, sol_amount: float) -> tuple[bool, str]:
        """
        Проверить можно ли совершить покупку.
        
        Returns:
            (allowed, reason)
        """
        self._check_date()
        
        if self._trading_halted:
            return False, "Trading halted: daily loss limit reached"
        
        if self.max_position_size > 0 and sol_amount > self.max_position_size:
            return False, f"Position size {sol_amount} > max {self.max_position_size}"
        
        return True, "OK"
    
    def get_stats(self) -> dict:
        """Получить статистику"""
        self._check_date()
        return {
            "date": self._stats.date,
            "bought_sol": self._stats.total_bought_sol,
            "sold_sol": self._stats.total_sold_sol,
            "realized_pnl": self._stats.realized_pnl_sol,
            "trading_halted": self._trading_halted,
            "sweep_count": self._stats.sweep_count,
            "sweep_total": self._stats.sweep_total_sol,
        }


class AutoSweeper:
    """
    Автоматический перевод средств на cold wallet.
    """
    
    def __init__(
        self,
        config: SweepConfig,
        client,  # SolanaClient
        wallet,  # Wallet
        on_sweep: Optional[Callable[[float, str], Awaitable[None]]] = None,
    ):
        self.config = config
        self.client = client
        self.wallet = wallet
        self.on_sweep = on_sweep  # Callback(amount, signature)
        
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_sweep: Optional[datetime] = None
        self._total_swept: float = 0.0
    
    async def start(self) -> None:
        """Запустить фоновый sweep worker"""
        if not self.config.enabled:
            logger.info("[SWEEP] Auto-sweep disabled")
            return
        
        if not self.config.cold_wallet:
            logger.warning("[SWEEP] No cold wallet configured, sweep disabled")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())
        logger.info(
            f"[SWEEP] Started: threshold={self.config.sweep_threshold_sol} SOL, "
            f"keep={self.config.keep_balance_sol} SOL, "
            f"cold={self.config.cold_wallet[:8]}..."
        )
    
    async def stop(self) -> None:
        """Остановить sweep worker"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"[SWEEP] Stopped. Total swept: {self._total_swept:.4f} SOL")
    
    async def _sweep_loop(self) -> None:
        """Основной цикл проверки и sweep"""
        while self._running:
            try:
                await asyncio.sleep(self.config.check_interval_sec)
                
                if not self._running:
                    break
                
                await self._check_and_sweep()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SWEEP] Loop error: {e}")
                await asyncio.sleep(60)  # Пауза при ошибке
    
    async def _check_and_sweep(self) -> None:
        """Проверить баланс и выполнить sweep если нужно"""
        try:
            # Получаем баланс
            balance_lamports = await self.client.get_balance(self.wallet.pubkey)
            balance_sol = balance_lamports / 1_000_000_000
            
            logger.debug(f"[SWEEP] Current balance: {balance_sol:.4f} SOL")
            
            # Проверяем порог
            if balance_sol <= self.config.sweep_threshold_sol:
                return
            
            # Вычисляем сумму для sweep
            sweep_amount = balance_sol - self.config.keep_balance_sol
            
            # Учитываем комиссию (~0.000005 SOL)
            sweep_amount -= 0.00001
            
            if sweep_amount <= 0:
                return
            
            logger.info(
                f"[SWEEP] Balance {balance_sol:.4f} SOL > threshold {self.config.sweep_threshold_sol} SOL. "
                f"Sweeping {sweep_amount:.4f} SOL to cold wallet..."
            )
            
            # Выполняем sweep
            success, signature = await self._execute_sweep(sweep_amount)
            
            if success:
                self._last_sweep = datetime.utcnow()
                self._total_swept += sweep_amount
                logger.warning(
                    f"[SWEEP] ✓ Swept {sweep_amount:.4f} SOL to {self.config.cold_wallet[:8]}... "
                    f"TX: {signature}"
                )
                
                if self.on_sweep:
                    await self.on_sweep(sweep_amount, signature)
            else:
                logger.error(f"[SWEEP] Failed to sweep: {signature}")
                
        except Exception as e:
            logger.error(f"[SWEEP] Check error: {e}")
    
    async def _execute_sweep(self, amount_sol: float) -> tuple[bool, str]:
        """
        Выполнить transfer на cold wallet.
        
        Returns:
            (success, signature_or_error)
        """
        try:
            cold_pubkey = Pubkey.from_string(self.config.cold_wallet)
            lamports = int(amount_sol * 1_000_000_000)
            
            # Создаём transfer instruction
            transfer_ix = transfer(
                TransferParams(
                    from_pubkey=self.wallet.pubkey,
                    to_pubkey=cold_pubkey,
                    lamports=lamports
                )
            )
            
            # Получаем blockhash
            blockhash_resp = await self.client.get_latest_blockhash()
            blockhash = blockhash_resp.value.blockhash
            
            # Собираем и подписываем транзакцию
            msg = Message.new_with_blockhash(
                [transfer_ix],
                self.wallet.pubkey,
                blockhash
            )
            tx = Transaction.new_unsigned(msg)
            tx.sign([self.wallet.keypair], blockhash)
            
            # Отправляем
            result = await self.client.send_transaction(tx)
            signature = str(result.value)
            
            # Ждём подтверждения
            from solders.signature import Signature
            await self.client.confirm_transaction(
                Signature.from_string(signature),
                commitment="confirmed"
            )
            
            return True, signature
            
        except Exception as e:
            return False, str(e)
    
    async def force_sweep(self) -> tuple[bool, str]:
        """Принудительный sweep (игнорирует threshold)"""
        if not self.config.cold_wallet:
            return False, "No cold wallet configured"
        
        try:
            balance_lamports = await self.client.get_balance(self.wallet.pubkey)
            balance_sol = balance_lamports / 1_000_000_000
            
            sweep_amount = balance_sol - self.config.keep_balance_sol - 0.00001
            
            if sweep_amount <= 0:
                return False, f"Balance too low: {balance_sol:.4f} SOL"
            
            return await self._execute_sweep(sweep_amount)
            
        except Exception as e:
            return False, str(e)
    
    def get_stats(self) -> dict:
        """Получить статистику sweep"""
        return {
            "enabled": self.config.enabled,
            "cold_wallet": self.config.cold_wallet,
            "threshold_sol": self.config.sweep_threshold_sol,
            "keep_sol": self.config.keep_balance_sol,
            "total_swept_sol": self._total_swept,
            "last_sweep": self._last_sweep.isoformat() if self._last_sweep else None,
        }


# === Уведомления ===

class WebhookNotifier:
    """Отправка уведомлений через webhook (Telegram, Slack, Discord)"""
    
    def __init__(
        self,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        slack_webhook_url: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
    ):
        self.telegram_token = telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.slack_url = slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL")
        self.discord_url = discord_webhook_url or os.getenv("DISCORD_WEBHOOK_URL")
    
    async def send_alert(self, title: str, message: str, level: str = "warning") -> None:
        """
        Отправить алерт во все настроенные каналы.
        
        Args:
            title: Заголовок
            message: Текст сообщения
            level: "info", "warning", "error", "critical"
        """
        emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}.get(level, "📢")
        
        tasks = []
        
        if self.telegram_token and self.telegram_chat:
            tasks.append(self._send_telegram(f"{emoji} *{title}*\n\n{message}"))
        
        if self.slack_url:
            tasks.append(self._send_slack(title, message, level))
        
        if self.discord_url:
            tasks.append(self._send_discord(title, message, emoji))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _send_telegram(self, text: str) -> None:
        """Отправить в Telegram"""
        import aiohttp
        
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat,
            "text": text,
            "parse_mode": "Markdown"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        logger.warning(f"[NOTIFY] Telegram failed: {await resp.text()}")
        except Exception as e:
            logger.warning(f"[NOTIFY] Telegram error: {e}")
    
    async def _send_slack(self, title: str, message: str, level: str) -> None:
        """Отправить в Slack"""
        import aiohttp
        
        color = {"info": "#36a64f", "warning": "#ffcc00", "error": "#ff0000", "critical": "#8b0000"}.get(level, "#808080")
        
        payload = {
            "attachments": [{
                "color": color,
                "title": title,
                "text": message,
                "ts": datetime.utcnow().timestamp()
            }]
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.slack_url, json=payload) as resp:
                    if resp.status != 200:
                        logger.warning(f"[NOTIFY] Slack failed: {await resp.text()}")
        except Exception as e:
            logger.warning(f"[NOTIFY] Slack error: {e}")
    
    async def _send_discord(self, title: str, message: str, emoji: str) -> None:
        """Отправить в Discord"""
        import aiohttp
        
        payload = {
            "content": f"{emoji} **{title}**\n{message}"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.discord_url, json=payload) as resp:
                    if resp.status not in (200, 204):
                        logger.warning(f"[NOTIFY] Discord failed: {await resp.text()}")
        except Exception as e:
            logger.warning(f"[NOTIFY] Discord error: {e}")
