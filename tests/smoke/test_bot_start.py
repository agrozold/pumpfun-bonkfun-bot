"""Smoke tests for bot startup"""
import pytest
import subprocess
import sys


class TestImports:
    """Test that all modules can be imported"""
    
    def test_import_analytics(self):
        from src.analytics.trace_context import TraceContext
        from src.analytics.trace_recorder import TraceRecorder
        from src.analytics.metrics_server import MetricsServer
    
    def test_import_security(self):
        from src.security.file_guard import FileGuard
        from src.security.secrets_manager import SecretsManager
    
    def test_import_core(self):
        from src.core.sender import SendResult, SendStatus
        from src.core.sender_registry import SenderRegistry
    
    def test_import_trading(self):
        from src.trading.position_state import PositionState, StateMachine
    
    def test_import_monitoring(self):
        from src.monitoring.watchdog_mixin import WatchdogMixin


class TestConfigValidation:
    """Test bot configs are valid"""
    
    @pytest.mark.parametrize("config", [
        "bots/bot-sniper-0-pump.yaml",
        "bots/bot-sniper-0-bonkfun.yaml",
        "bots/bot-sniper-0-bags.yaml",
    ])
    def test_config_loads(self, config):
        import yaml
        from pathlib import Path
        
        config_path = Path(config)
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            
            assert 'platform' in cfg or 'mode' in cfg
