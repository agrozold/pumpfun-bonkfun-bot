import logging
from src.analytics.trace_context import get_trace_id

class TraceIdFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = get_trace_id() or '-'
        return True

TRACE_LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(trace_id)s | %(name)s | %(message)s'

def patch_logger(logger: logging.Logger) -> None:
    logger.addFilter(TraceIdFilter())
    for handler in logger.handlers:
        handler.setFormatter(logging.Formatter(TRACE_LOG_FORMAT))
