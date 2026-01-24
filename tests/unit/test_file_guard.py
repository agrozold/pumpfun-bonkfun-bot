"""Unit tests for FileGuard security module"""
import pytest
import os
from src.security.file_guard import FileGuard, SecurityViolationError


class TestFileGuard:
    """Tests for FileGuard"""
    
    @pytest.fixture
    def guard(self):
        return FileGuard()
    
    def test_forbidden_env_files(self, guard):
        assert guard.is_forbidden('.env') is True
        assert guard.is_forbidden('.env.local') is True
        assert guard.is_forbidden('.env.production') is True
        assert guard.is_forbidden('config/.env') is True
        assert guard.is_forbidden('/path/to/.env') is True
    
    def test_forbidden_key_files(self, guard):
        assert guard.is_forbidden('wallet.key') is True
        assert guard.is_forbidden('private.pem') is True
        assert guard.is_forbidden('server.key') is True
    
    def test_forbidden_sensitive_patterns(self, guard):
        assert guard.is_forbidden('private_key.txt') is True
        assert guard.is_forbidden('my_secret_file') is True
        assert guard.is_forbidden('seed_phrase.txt') is True
        assert guard.is_forbidden('keys.json') is True
        assert guard.is_forbidden('wallet.json') is True
    
    def test_allowed_example_files(self, guard):
        assert guard.is_forbidden('.env.example') is False
        assert guard.is_forbidden('.env.example.safe') is False
    
    def test_allowed_normal_files(self, guard):
        assert guard.is_forbidden('README.md') is False
        assert guard.is_forbidden('src/trading/position.py') is False
        assert guard.is_forbidden('logs/bot.log') is False
        assert guard.is_forbidden('config/bot.yaml') is False
    
    def test_check_path_in_agent_mode(self, guard, monkeypatch):
        monkeypatch.setenv('AI_AGENT_MODE', '1')
        
        with pytest.raises(SecurityViolationError) as exc_info:
            guard.check_path('.env')
        
        assert 'Access denied' in str(exc_info.value)
    
    def test_check_path_not_in_agent_mode(self, guard, monkeypatch):
        monkeypatch.setenv('AI_AGENT_MODE', '0')
        
        # Should not raise
        guard.check_path('.env')
    
    def test_agent_mode_detection(self, guard, monkeypatch):
        monkeypatch.setenv('AI_AGENT_MODE', '1')
        assert guard.is_agent_mode() is True
        
        monkeypatch.setenv('AI_AGENT_MODE', 'true')
        assert guard.is_agent_mode() is True
        
        monkeypatch.setenv('AI_AGENT_MODE', 'yes')
        assert guard.is_agent_mode() is True
        
        monkeypatch.setenv('AI_AGENT_MODE', '0')
        assert guard.is_agent_mode() is False
        
        monkeypatch.setenv('AI_AGENT_MODE', '')
        assert guard.is_agent_mode() is False
