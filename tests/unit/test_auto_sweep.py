"""Unit tests for AutoSweep"""
import pytest
from security.auto_sweep import SweepConfig, TradingLimiter, DailyStats


class TestSweepConfig:
    def test_default_values(self):
        config = SweepConfig()
        assert config.enabled is False
        assert config.sweep_threshold_sol == 1.0
        assert config.keep_balance_sol == 0.1
    
    def test_from_dict(self):
        data = {
            "enabled": True,
            "cold_wallet": "ColdWallet123",
            "sweep_threshold_sol": 5.0,
        }
        config = SweepConfig.from_dict(data)
        assert config.enabled is True
        assert config.cold_wallet == "ColdWallet123"
        assert config.sweep_threshold_sol == 5.0


class TestTradingLimiter:
    def test_can_trade_initially(self):
        limiter = TradingLimiter(max_daily_loss_sol=1.0)
        assert limiter.can_trade() is True
    
    def test_record_buy(self):
        limiter = TradingLimiter()
        limiter.record_buy(0.5)
        stats = limiter.get_stats()
        assert stats["bought_sol"] == 0.5
    
    def test_record_sell_with_profit(self):
        limiter = TradingLimiter()
        limiter.record_buy(0.5)
        limiter.record_sell(0.6, 0.1)  # Profit
        stats = limiter.get_stats()
        assert stats["realized_pnl"] == 0.1
    
    def test_daily_loss_limit(self):
        limiter = TradingLimiter(max_daily_loss_sol=0.5)
        
        # Record losses
        limiter.record_sell(0.3, -0.3)
        assert limiter.can_trade() is True
        
        limiter.record_sell(0.3, -0.3)  # Total loss = 0.6 > 0.5 limit
        assert limiter.can_trade() is False
    
    def test_can_buy_check(self):
        limiter = TradingLimiter(max_position_size_sol=1.0)
        
        allowed, reason = limiter.can_buy(0.5)
        assert allowed is True
        
        allowed, reason = limiter.can_buy(1.5)
        assert allowed is False
        assert "Position size" in reason
