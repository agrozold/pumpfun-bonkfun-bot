"""
FileGuard - технический запрет AI-агентам на доступ к секретам
Активируется при AI_AGENT_MODE=1 в environment
"""

import os
import re
import builtins
import logging
from pathlib import Path
from typing import Set, Optional
from functools import wraps

logger = logging.getLogger(__name__)


class SecurityViolationError(Exception):
    """Исключение при попытке доступа к запрещённому файлу"""
    pass


class FileGuard:
    """
    Защита от чтения секретов AI-агентами.
    
    Использование:
        guard = FileGuard()
        guard.install()  # Патчит builtins.open
        
    При попытке открыть запрещённый файл:
        - Логируется security warning
        - Выбрасывается SecurityViolationError
    """
    
    # Запрещённые паттерны (регулярные выражения)
    FORBIDDEN_PATTERNS = [
        r'\.env($|\.)',           # .env, .env.local, .env.production
        r'\.key$',                 # *.key
        r'\.pem$',                 # *.pem
        r'private',                # *private*
        r'secret',                 # *secret*
        r'seed',                   # *seed*
        r'\.ssh/',                 # ~/.ssh/
        r'keys\.json$',            # keys.json
        r'wallet\.json$',          # wallet.json
        r'config/.*credentials',   # credentials в config
    ]
    
    # Разрешённые пути (whitelist)
    ALLOWED_PATHS = {
        '.env.example',
        '.env.example.safe',
        'README.md',
        'AGENTS.md',
    }
    
    def __init__(self):
        self._original_open = None
        self._is_installed = False
        self._patterns = [re.compile(p, re.IGNORECASE) for p in self.FORBIDDEN_PATTERNS]
    
    def is_agent_mode(self) -> bool:
        """Проверка режима агента"""
        return os.environ.get('AI_AGENT_MODE', '').lower() in ('1', 'true', 'yes')
    
    def is_forbidden(self, path: str) -> bool:
        """Проверка, запрещён ли путь"""
        path_str = str(path)
        path_lower = path_str.lower()
        
        # Проверка whitelist
        for allowed in self.ALLOWED_PATHS:
            if path_str.endswith(allowed):
                return False
        
        # Проверка forbidden patterns
        for pattern in self._patterns:
            if pattern.search(path_lower):
                return True
        
        return False
    
    def check_path(self, path: str) -> None:
        """Проверить путь и выбросить исключение если запрещён"""
        if self.is_agent_mode() and self.is_forbidden(path):
            logger.warning(f"SECURITY: Blocked access to sensitive file: {path}")
            raise SecurityViolationError(
                f"Access denied: '{path}' is restricted in AI agent mode"
            )
    
    def install(self) -> None:
        """Установить патч на builtins.open"""
        if self._is_installed:
            return
        
        self._original_open = builtins.open
        guard = self
        
        @wraps(builtins.open)
        def guarded_open(file, mode='r', *args, **kwargs):
            # Проверяем только операции чтения
            if 'r' in mode or mode == '':
                guard.check_path(str(file))
            return guard._original_open(file, mode, *args, **kwargs)
        
        builtins.open = guarded_open
        self._is_installed = True
        logger.info("FileGuard installed - sensitive files protected")
    
    def uninstall(self) -> None:
        """Удалить патч"""
        if self._is_installed and self._original_open:
            builtins.open = self._original_open
            self._is_installed = False
            logger.info("FileGuard uninstalled")


# Автоматическая установка при импорте (если в agent mode)
_guard = FileGuard()
if _guard.is_agent_mode():
    _guard.install()
