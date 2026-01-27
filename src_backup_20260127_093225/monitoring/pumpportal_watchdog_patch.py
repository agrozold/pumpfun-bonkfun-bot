"""
Пример интеграции WatchdogMixin в PumpPortal listener.
Добавить в существующий класс.
"""

# В начале файла добавить импорт:
# from src.monitoring.watchdog_mixin import WatchdogMixin

# Изменить объявление класса:
# class UniversalPumpPortalListener(WatchdogMixin):

# В __init__ добавить:
#     self.watchdog_timeout = 60.0  # Секунд
#     self.watchdog_check_interval = 10.0

# В методе обработки сообщений добавить:
#     self._update_last_message_time()

# В методе connect/run добавить после подключения:
#     await self._start_watchdog()

# В методе close/stop добавить:
#     await self._stop_watchdog()

# Реализовать метод reconnect:
#     async def _trigger_reconnect(self) -> None:
#         """Переподключение WebSocket"""
#         if self._websocket:
#             await self._websocket.close()
#         await self._connect()
