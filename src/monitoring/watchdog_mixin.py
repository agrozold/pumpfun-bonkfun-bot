"""
WatchdogMixin - автоматическое переподключение при отсутствии сообщений
Добавляется к WebSocket/gRPC listeners через наследование
"""

import asyncio
import time
import logging
from typing import Optional, Callable, Awaitable
from abc import abstractmethod


class WatchdogMixin:
    """
    Mixin для добавления watchdog функциональности к listeners.
    
    Требует от класса:
    - метод _reconnect() -> None
    - атрибут logger: logging.Logger
    """
    
    # Конфигурация (переопределить в наследнике)
    watchdog_timeout: float = 60.0      # секунд без сообщений
    watchdog_check_interval: float = 10.0  # интервал проверки
    
    # Внутреннее состояние
    _last_message_time: float = 0.0
    _watchdog_task: Optional[asyncio.Task] = None
    _reconnect_count: int = 0
    _is_shutting_down: bool = False
    
    def _update_last_message_time(self) -> None:
        """Вызывать при получении каждого сообщения"""
        self._last_message_time = time.monotonic()
    
    async def _start_watchdog(self) -> None:
        """Запустить watchdog loop"""
        self._last_message_time = time.monotonic()
        self._is_shutting_down = False
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
    
    async def _stop_watchdog(self) -> None:
        """Остановить watchdog"""
        self._is_shutting_down = True
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
    
    async def _watchdog_loop(self) -> None:
        """Основной цикл watchdog"""
        while not self._is_shutting_down:
            try:
                await asyncio.sleep(self.watchdog_check_interval)
                
                if self._is_shutting_down:
                    break
                
                elapsed = time.monotonic() - self._last_message_time
                
                if elapsed > self.watchdog_timeout:
                    self._log_watchdog_timeout(elapsed)
                    self._reconnect_count += 1
                    
                    try:
                        await self._trigger_reconnect()
                    except Exception as e:
                        self._log_reconnect_error(e)
                    
                    # Сбросить таймер после попытки реконнекта
                    self._last_message_time = time.monotonic()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log_watchdog_error(e)
                await asyncio.sleep(5)  # Пауза при ошибке
    
    @abstractmethod
    async def _trigger_reconnect(self) -> None:
        """Переопределить: логика переподключения"""
        raise NotImplementedError
    
    def _log_watchdog_timeout(self, elapsed: float) -> None:
        """Логирование таймаута"""
        if hasattr(self, 'logger'):
            self.logger.warning(
                f"Watchdog timeout: no messages for {elapsed:.1f}s "
                f"(threshold: {self.watchdog_timeout}s). Reconnecting..."
            )
    
    def _log_reconnect_error(self, error: Exception) -> None:
        """Логирование ошибки реконнекта"""
        if hasattr(self, 'logger'):
            self.logger.error(f"Reconnect failed: {error}")
    
    def _log_watchdog_error(self, error: Exception) -> None:
        """Логирование ошибки watchdog"""
        if hasattr(self, 'logger'):
            self.logger.error(f"Watchdog error: {error}")
    
    def get_watchdog_metrics(self) -> dict:
        """Метрики для мониторинга"""
        return {
            'reconnect_count': self._reconnect_count,
            'last_message_age_sec': time.monotonic() - self._last_message_time,
            'watchdog_timeout': self.watchdog_timeout
        }
