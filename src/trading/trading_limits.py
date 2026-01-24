"""
Trading Limits & Auto-Sweep.
Контроль лимитов на торговлю и автоматический вывод прибыли.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from decimal import Decimal
from pathlib import Path
import json

logger = logging.getLogger(__name__)


@dataclass
class TradingLimits:
    """Лимиты на торговлю"""
    # Per-trade limits
    max_buy_amount_sol: Decimal = Decimal('0.1')
    min_buy_amount_sol: Decimal = Decimal('0.01')
    max_position_size_sol: Decimal = Decimal('0.5')
    
    # Time-based limits
    max_trades_per_hour: int = 10
    max_trades_per_day: int = 50
    max_sol_per_hour: Decimal = Decimal('1.0')
    max_sol_per_day: Decimal = Decimal('5.0')
    
    # Loss limits
    max_loss_per_trade_pct: Decimal = Decimal('0.30')  # 30% stop-loss
    max_daily_loss_sol: Decimal = Decimal('0.5')
    
    # Concurrent limits
    max_concurrent_positions: int = 5
    max_positions_per_token: int = 1


@dataclass
class AutoSweepConfig:
    """Конфигурация автоматического вывода"""
    enabled: bool = False
    target_wallet: Optional[str] = None
    
    # Triggers
    sweep_threshold_sol: Decimal = Decimal('1.0')  # Вывод при достижении
    sweep_percentage: Decimal = Decimal('0.5')     # Выводить 50% сверх порога
    
    # Timing
    sweep_interval_hours: int = 24
    min_balance_keep_sol: Decimal = Decimal('0.1')  # Оставлять минимум
    
    # Safety
    require_confirmation: bool = True
    max_sweep_amount_sol: Decimal = Decimal('10.0')


@dataclass
class TradeRecord:
    """Запись о сделке для учёта лимитов"""
    timestamp: datetime
    trade_type: str  # 'buy' | 'sell'
    mint: str
    amount_sol: Decimal
    success: bool
    pnl_sol: Optional[Decimal] = None


class LimitsTracker:
    """
    Трекер лимитов торговли.
    
    Использование:
        tracker = LimitsTracker(limits)
        can_trade, reason = await tracker.can_execute_trade('buy', 0.05, mint)
        if can_trade:
            await tracker.record_trade(...)
    """
    
    def __init__(
        self,
        limits: TradingLimits = None,
        persistence_file: str = 'data/trading_limits.json'
    ):
        self.limits = limits or TradingLimits()
        self.persistence_file = Path(persistence_file)
        self._trades: List[TradeRecord] = []
        self._active_positions: Dict[str, int] = {}  # mint -> count
        self._daily_loss: Decimal = Decimal('0')
        self._last_reset: datetime = datetime.utcnow()
        self._lock = asyncio.Lock()
        
        # Загружаем состояние
        self._load_state()
    
    def _load_state(self) -> None:
        """Загрузить состояние из файла"""
        if self.persistence_file.exists():
            try:
                with open(self.persistence_file, 'r') as f:
                    data = json.load(f)
                    self._daily_loss = Decimal(str(data.get('daily_loss', 0)))
                    self._last_reset = datetime.fromisoformat(data.get('last_reset', datetime.utcnow().isoformat()))
                    
                    # Загружаем историю сделок за последние 24 часа
                    cutoff = datetime.utcnow() - timedelta(hours=24)
                    for trade_data in data.get('trades', []):
                        ts = datetime.fromisoformat(trade_data['timestamp'])
                        if ts > cutoff:
                            self._trades.append(TradeRecord(
                                timestamp=ts,
                                trade_type=trade_data['trade_type'],
                                mint=trade_data['mint'],
                                amount_sol=Decimal(str(trade_data['amount_sol'])),
                                success=trade_data['success'],
                                pnl_sol=Decimal(str(trade_data['pnl_sol'])) if trade_data.get('pnl_sol') else None
                            ))
            except Exception as e:
                logger.error(f"Failed to load limits state: {e}")
    
    def _save_state(self) -> None:
        """Сохранить состояние в файл"""
        try:
            self.persistence_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Сохраняем только сделки за последние 24 часа
            cutoff = datetime.utcnow() - timedelta(hours=24)
            recent_trades = [t for t in self._trades if t.timestamp > cutoff]
            
            data = {
                'daily_loss': str(self._daily_loss),
                'last_reset': self._last_reset.isoformat(),
                'trades': [
                    {
                        'timestamp': t.timestamp.isoformat(),
                        'trade_type': t.trade_type,
                        'mint': t.mint,
                        'amount_sol': str(t.amount_sol),
                        'success': t.success,
                        'pnl_sol': str(t.pnl_sol) if t.pnl_sol else None
                    }
                    for t in recent_trades
                ]
            }
            
            with open(self.persistence_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save limits state: {e}")
    
    def _maybe_reset_daily(self) -> None:
        """Сбросить дневные лимиты если прошло 24 часа"""
        now = datetime.utcnow()
        if now - self._last_reset > timedelta(hours=24):
            self._daily_loss = Decimal('0')
            self._last_reset = now
            # Очищаем старые сделки
            cutoff = now - timedelta(hours=24)
            self._trades = [t for t in self._trades if t.timestamp > cutoff]
            logger.info("Daily limits reset")
    
    async def can_execute_trade(
        self,
        trade_type: str,
        amount_sol: Decimal,
        mint: str,
        current_positions: int = 0
    ) -> tuple[bool, str]:
        """
        Проверить, можно ли выполнить сделку.
        
        Returns:
            (can_trade, reason)
        """
        async with self._lock:
            self._maybe_reset_daily()
            
            # Проверка суммы
            if trade_type == 'buy':
                if amount_sol < self.limits.min_buy_amount_sol:
                    return False, f"Amount {amount_sol} SOL below minimum {self.limits.min_buy_amount_sol}"
                
                if amount_sol > self.limits.max_buy_amount_sol:
                    return False, f"Amount {amount_sol} SOL exceeds maximum {self.limits.max_buy_amount_sol}"
            
            # Проверка concurrent positions
            if trade_type == 'buy' and current_positions >= self.limits.max_concurrent_positions:
                return False, f"Max concurrent positions ({self.limits.max_concurrent_positions}) reached"
            
            # Проверка positions per token
            token_positions = self._active_positions.get(mint, 0)
            if trade_type == 'buy' and token_positions >= self.limits.max_positions_per_token:
                return False, f"Max positions for this token ({self.limits.max_positions_per_token}) reached"
            
            # Проверка hourly limits
            hour_ago = datetime.utcnow() - timedelta(hours=1)
            hourly_trades = [t for t in self._trades if t.timestamp > hour_ago]
            
            if len(hourly_trades) >= self.limits.max_trades_per_hour:
                return False, f"Hourly trade limit ({self.limits.max_trades_per_hour}) reached"
            
            hourly_volume = sum(t.amount_sol for t in hourly_trades if t.trade_type == 'buy')
            if trade_type == 'buy' and hourly_volume + amount_sol > self.limits.max_sol_per_hour:
                return False, f"Hourly volume limit ({self.limits.max_sol_per_hour} SOL) would be exceeded"
            
            # Проверка daily limits
            day_ago = datetime.utcnow() - timedelta(hours=24)
            daily_trades = [t for t in self._trades if t.timestamp > day_ago]
            
            if len(daily_trades) >= self.limits.max_trades_per_day:
                return False, f"Daily trade limit ({self.limits.max_trades_per_day}) reached"
            
            daily_volume = sum(t.amount_sol for t in daily_trades if t.trade_type == 'buy')
            if trade_type == 'buy' and daily_volume + amount_sol > self.limits.max_sol_per_day:
                return False, f"Daily volume limit ({self.limits.max_sol_per_day} SOL) would be exceeded"
            
            # Проверка daily loss
            if self._daily_loss >= self.limits.max_daily_loss_sol:
                return False, f"Daily loss limit ({self.limits.max_daily_loss_sol} SOL) reached"
            
            return True, "OK"
    
    async def record_trade(
        self,
        trade_type: str,
        mint: str,
        amount_sol: Decimal,
        success: bool,
        pnl_sol: Decimal = None
    ) -> None:
        """Записать выполненную сделку"""
        async with self._lock:
            record = TradeRecord(
                timestamp=datetime.utcnow(),
                trade_type=trade_type,
                mint=mint,
                amount_sol=amount_sol,
                success=success,
                pnl_sol=pnl_sol
            )
            self._trades.append(record)
            
            # Обновляем позиции
            if trade_type == 'buy' and success:
                self._active_positions[mint] = self._active_positions.get(mint, 0) + 1
            elif trade_type == 'sell' and success:
                if mint in self._active_positions:
                    self._active_positions[mint] = max(0, self._active_positions[mint] - 1)
            
            # Обновляем daily loss
            if pnl_sol and pnl_sol < 0:
                self._daily_loss += abs(pnl_sol)
            
            self._save_state()
            
            logger.debug(f"Trade recorded: {trade_type} {amount_sol} SOL, success={success}")
    
    def get_stats(self) -> Dict:
        """Получить статистику"""
        self._maybe_reset_daily()
        
        hour_ago = datetime.utcnow() - timedelta(hours=1)
        day_ago = datetime.utcnow() - timedelta(hours=24)
        
        hourly_trades = [t for t in self._trades if t.timestamp > hour_ago]
        daily_trades = [t for t in self._trades if t.timestamp > day_ago]
        
        return {
            'hourly_trades': len(hourly_trades),
            'hourly_limit': self.limits.max_trades_per_hour,
            'hourly_volume_sol': float(sum(t.amount_sol for t in hourly_trades if t.trade_type == 'buy')),
            'hourly_volume_limit': float(self.limits.max_sol_per_hour),
            'daily_trades': len(daily_trades),
            'daily_limit': self.limits.max_trades_per_day,
            'daily_volume_sol': float(sum(t.amount_sol for t in daily_trades if t.trade_type == 'buy')),
            'daily_volume_limit': float(self.limits.max_sol_per_day),
            'daily_loss_sol': float(self._daily_loss),
            'daily_loss_limit': float(self.limits.max_daily_loss_sol),
            'active_positions': dict(self._active_positions)
        }


class AutoSweeper:
    """
    Автоматический вывод прибыли.
    
    Использование:
        sweeper = AutoSweeper(config, rpc_client, keypair)
        await sweeper.check_and_sweep(current_balance)
    """
    
    def __init__(
        self,
        config: AutoSweepConfig,
        rpc_client=None,
        keypair=None
    ):
        self.config = config
        self.rpc_client = rpc_client
        self.keypair = keypair
        self._last_sweep: Optional[datetime] = None
        self._pending_confirmation: bool = False
    
    async def check_and_sweep(self, current_balance_sol: Decimal) -> Optional[Dict]:
        """
        Проверить и выполнить sweep при необходимости.
        
        Returns:
            Dict с информацией о sweep или None
        """
        if not self.config.enabled:
            return None
        
        if not self.config.target_wallet:
            logger.warning("Auto-sweep enabled but no target wallet configured")
            return None
        
        # Проверяем интервал
        if self._last_sweep:
            hours_since_sweep = (datetime.utcnow() - self._last_sweep).total_seconds() / 3600
            if hours_since_sweep < self.config.sweep_interval_hours:
                return None
        
        # Проверяем порог
        if current_balance_sol < self.config.sweep_threshold_sol:
            return None
        
        # Вычисляем сумму для вывода
        excess = current_balance_sol - self.config.sweep_threshold_sol
        sweep_amount = min(
            excess * self.config.sweep_percentage,
            self.config.max_sweep_amount_sol
        )
        
        # Проверяем что останется минимум
        remaining = current_balance_sol - sweep_amount
        if remaining < self.config.min_balance_keep_sol:
            sweep_amount = current_balance_sol - self.config.min_balance_keep_sol
        
        if sweep_amount <= 0:
            return None
        
        # Требуется подтверждение?
        if self.config.require_confirmation and not self._pending_confirmation:
            self._pending_confirmation = True
            logger.info(f"Auto-sweep pending confirmation: {sweep_amount} SOL to {self.config.target_wallet[:16]}...")
            return {
                'status': 'pending_confirmation',
                'amount_sol': float(sweep_amount),
                'target': self.config.target_wallet
            }
        
        # Выполняем sweep
        try:
            signature = await self._execute_sweep(sweep_amount)
            self._last_sweep = datetime.utcnow()
            self._pending_confirmation = False
            
            logger.info(f"Auto-sweep executed: {sweep_amount} SOL, signature: {signature}")
            
            return {
                'status': 'success',
                'amount_sol': float(sweep_amount),
                'target': self.config.target_wallet,
                'signature': signature
            }
        except Exception as e:
            logger.error(f"Auto-sweep failed: {e}")
            return {
                'status': 'failed',
                'error': str(e)
            }
    
    async def _execute_sweep(self, amount_sol: Decimal) -> str:
        """Выполнить перевод SOL"""
        if not self.rpc_client or not self.keypair:
            raise ValueError("RPC client and keypair required for sweep")
        
        from solders.pubkey import Pubkey
        from solders.system_program import transfer, TransferParams
        from solders.transaction import Transaction
        from solders.message import Message
        
        target_pubkey = Pubkey.from_string(self.config.target_wallet)
        lamports = int(amount_sol * 10**9)
        
        # Создаём transfer instruction
        transfer_ix = transfer(TransferParams(
            from_pubkey=self.keypair.pubkey(),
            to_pubkey=target_pubkey,
            lamports=lamports
        ))
        
        # Получаем blockhash
        blockhash_resp = await self.rpc_client.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash
        
        # Создаём и подписываем транзакцию
        msg = Message.new_with_blockhash([transfer_ix], self.keypair.pubkey(), blockhash)
        tx = Transaction.new_unsigned(msg)
        tx.sign([self.keypair], blockhash)
        
        # Отправляем
        result = await self.rpc_client.send_transaction(tx)
        
        return str(result.value)
    
    def confirm_sweep(self) -> None:
        """Подтвердить pending sweep"""
        self._pending_confirmation = False
    
    def cancel_sweep(self) -> None:
        """Отменить pending sweep"""
        self._pending_confirmation = False


# Глобальные экземпляры
_limits_tracker: Optional[LimitsTracker] = None
_auto_sweeper: Optional[AutoSweeper] = None


def get_limits_tracker(limits: TradingLimits = None) -> LimitsTracker:
    """Получить глобальный трекер лимитов"""
    global _limits_tracker
    if _limits_tracker is None:
        _limits_tracker = LimitsTracker(limits)
    return _limits_tracker


def get_auto_sweeper(config: AutoSweepConfig = None, rpc_client=None, keypair=None) -> AutoSweeper:
    """Получить глобальный sweeper"""
    global _auto_sweeper
    if _auto_sweeper is None:
        _auto_sweeper = AutoSweeper(config or AutoSweepConfig(), rpc_client, keypair)
    return _auto_sweeper
