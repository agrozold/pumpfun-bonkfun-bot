"""
Патч для интеграции trace_id в логгер
Импортировать и вызвать patch_logger() при старте приложения
"""
import logging
from src.analytics.trace_context import get_trace_id


class TraceIdFilter(logging.Filter):
    """Фильтр для добавления trace_id в записи лога"""

    def filter(self, record):
        trace_id = get_trace_id()
        record.trace_id = trace_id if trace_id else '-'
        return True


TRACE_LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(trace_id)s | %(name)s | %(message)s'


def patch_logger(logger_name: str = None) -> None:
    """
    Добавить TraceIdFilter к логгеру.
    
    Args:
        logger_name: Имя логгера (None = root logger)
    """
    logger = logging.getLogger(logger_name)

    # Добавляем фильтр
    trace_filter = TraceIdFilter()
    logger.addFilter(trace_filter)

    # Обновляем форматтер для всех хендлеров
    formatter = logging.Formatter(TRACE_LOG_FORMAT)
    for handler in logger.handlers:
        handler.setFormatter(formatter)

    # Если нет хендлеров, добавляем StreamHandler
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(trace_filter)
        logger.addHandler(handler)


def patch_all_loggers() -> None:
    """Патчим все существующие логгеры проекта"""
    loggers_to_patch = [
        None,  # root
        'bot',
        'trading',
        'monitoring',
        'core',
        'security',
        'analytics'
    ]

    for name in loggers_to_patch:
        try:
            patch_logger(name)
        except Exception:
            pass
