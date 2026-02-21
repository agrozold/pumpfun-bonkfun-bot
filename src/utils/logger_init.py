"""
Logger initialization with trace_id support
Import this at application startup
"""
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

from .logger_trace_patch import patch_all_loggers, TRACE_LOG_FORMAT


def setup_logging(
    log_dir: str = 'logs',
    log_level: str = 'INFO',
    max_bytes: int = 10_000_000,  # 10MB
    backup_count: int = 5
) -> logging.Logger:
    """
    Setup logging with trace_id support and rotation.

    Returns:
        Root logger configured with trace support
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(TRACE_LOG_FORMAT))
    root_logger.addHandler(console_handler)

    # File handler with rotation — FIX S19-4: daily rotation, append on restart
    file_handler = TimedRotatingFileHandler(
        log_path / 'bot.log',
        when='midnight',
        interval=1,
        backupCount=14,
        encoding='utf-8',
        utc=True,
    )
    file_handler.suffix = '%Y-%m-%d' 
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(TRACE_LOG_FORMAT))
    root_logger.addHandler(file_handler)

    # Error file handler — FIX S19-4: daily rotation
    error_handler = TimedRotatingFileHandler(
        log_path / 'error.log',
        when='midnight',
        interval=1,
        backupCount=14,
        encoding='utf-8',
        utc=True,
    )
    error_handler.suffix = '%Y-%m-%d' 
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(TRACE_LOG_FORMAT))
    root_logger.addHandler(error_handler)

    # Apply trace_id patch
    patch_all_loggers()

    root_logger.info("Logging initialized with trace_id support")

    return root_logger
