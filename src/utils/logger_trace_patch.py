"""
Патч для интеграции trace_id в логгер
Добавить в logger.py после создания formatter
"""

import logging
from src.analytics.trace_context import get_trace_id


class TraceIdFilter(logging.Filter):
    """Фильтр для добавления trace_id в записи лога"""
    
    def filter(self, record):
        trace_id = get_trace_id()
        record.trace_id = trace_id if trace_id else '-'
        return True


# Обновлённый формат с trace_id
TRACE_LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(trace_id)s | %(name)s | %(message)s'


def add_trace_filter_to_logger(logger: logging.Logger) -> None:
    """Добавить TraceIdFilter к логгеру"""
    logger.addFilter(TraceIdFilter())
    
    # Обновить формат для всех хендлеров
    for handler in logger.handlers:
        handler.setFormatter(logging.Formatter(TRACE_LOG_FORMAT))
