"""
Unified logging system for pump.fun/bonk.fun trading bot.
Session 12: Standardized logging with rotation, JSON events, and alerts.
"""

import logging
import logging.handlers
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

# ============================================================================
# CONFIGURATION
# ============================================================================

LOG_DIR = Path("logs")
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Rotation settings
MAX_LOG_SIZE_MB = 10  # Rotate when file exceeds 10MB
BACKUP_COUNT = 5      # Keep 5 rotated files per bot

# Global loggers cache
_loggers: Dict[str, logging.Logger] = {}
_initialized = False


# ============================================================================
# JSON FORMATTER FOR CRITICAL EVENTS
# ============================================================================

class JSONFormatter(logging.Formatter):
    """Formatter that outputs JSON for structured logging of critical events."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields if present
        if hasattr(record, "event_type"):
            log_data["event_type"] = record.event_type
        if hasattr(record, "token_mint"):
            log_data["token_mint"] = record.token_mint
        if hasattr(record, "amount_sol"):
            log_data["amount_sol"] = record.amount_sol
        if hasattr(record, "tx_signature"):
            log_data["tx_signature"] = record.tx_signature
        if hasattr(record, "platform"):
            log_data["platform"] = record.platform
        if hasattr(record, "error_code"):
            log_data["error_code"] = record.error_code

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


# ============================================================================
# CORE FUNCTIONS
# ============================================================================

def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Get or create a logger with the given name.
    
    Args:
        name: Logger name, typically __name__
        level: Logging level
        
    Returns:
        Configured logger
    """
    global _loggers

    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(level)

    _loggers[name] = logger
    return logger


def setup_file_logging(
    filename: str = "pump_trading.log",
    level: int = logging.INFO,
    use_rotation: bool = True
) -> None:
    """Set up file logging with optional rotation.
    
    Args:
        filename: Log file path
        level: Logging level for file handler
        use_rotation: Whether to use rotating file handler
    """
    # Ensure logs directory exists
    LOG_DIR.mkdir(exist_ok=True)

    # Ensure filename is in logs directory
    log_path = Path(filename)
    if not str(log_path).startswith("logs"):
        log_path = LOG_DIR / log_path.name

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Check if handler with same filename already exists
    for handler in root_logger.handlers:
        if isinstance(handler, (logging.FileHandler, logging.handlers.RotatingFileHandler)):
            if hasattr(handler, 'baseFilename') and handler.baseFilename.endswith(log_path.name):
                return  # Handler already exists

    # Create formatter
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # Create file handler (with or without rotation)
    if use_rotation:
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=MAX_LOG_SIZE_MB * 1024 * 1024,
            backupCount=BACKUP_COUNT,
            encoding='utf-8'
        )
    else:
        file_handler = logging.FileHandler(str(log_path), encoding='utf-8')

    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def setup_console_logging(level: int = logging.INFO) -> None:
    """Set up console logging with colored output indicator.
    
    Args:
        level: Logging level for console
    """
    root_logger = logging.getLogger()

    # Check if console handler already exists
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream == sys.stdout:
            return

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def setup_json_logging(filename: str = "critical_events.jsonl") -> logging.Logger:
    """Set up JSON logging for critical trading events.
    
    Args:
        filename: JSON log file path
        
    Returns:
        Logger configured for JSON output
    """
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / filename

    json_logger = logging.getLogger("trading.events")
    json_logger.setLevel(logging.INFO)
    json_logger.propagate = False  # Don't propagate to root logger

    # Check if handler already exists
    for handler in json_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            return json_logger

    # Rotating handler for JSON events
    json_handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=MAX_LOG_SIZE_MB * 1024 * 1024,
        backupCount=BACKUP_COUNT,
        encoding='utf-8'
    )
    json_handler.setFormatter(JSONFormatter())
    json_logger.addHandler(json_handler)

    return json_logger


# ============================================================================
# STRUCTURED EVENT LOGGING
# ============================================================================

def log_trade_event(
    event_type: str,
    token_mint: str,
    platform: str,
    amount_sol: Optional[float] = None,
    tx_signature: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None
) -> None:
    """Log a structured trading event to JSON log.
    
    Args:
        event_type: Type of event (BUY, SELL, DETECTED, ERROR, etc.)
        token_mint: Token mint address
        platform: Trading platform (pump_fun, bonk_fun, bags)
        amount_sol: Amount in SOL (optional)
        tx_signature: Transaction signature (optional)
        extra: Additional data to include (optional)
    """
    json_logger = setup_json_logging()

    record = json_logger.makeRecord(
        name="trading.events",
        level=logging.INFO,
        fn="",
        lno=0,
        msg=f"{event_type}: {token_mint[:8]}... on {platform}",
        args=(),
        exc_info=None
    )

    record.event_type = event_type
    record.token_mint = token_mint
    record.platform = platform

    if amount_sol is not None:
        record.amount_sol = amount_sol
    if tx_signature is not None:
        record.tx_signature = tx_signature
    if extra:
        for key, value in extra.items():
            setattr(record, key, value)

    json_logger.handle(record)


def log_critical_error(
    error_code: str,
    message: str,
    module: str,
    exception: Optional[Exception] = None,
    extra: Optional[Dict[str, Any]] = None
) -> None:
    """Log a critical error that may need immediate attention.
    
    Args:
        error_code: Error code for categorization (RPC_FAIL, TX_FAIL, etc.)
        message: Error message
        module: Module where error occurred
        exception: Exception object (optional)
        extra: Additional context (optional)
    """
    json_logger = setup_json_logging("critical_errors.jsonl")

    record = json_logger.makeRecord(
        name="trading.errors",
        level=logging.ERROR,
        fn="",
        lno=0,
        msg=message,
        args=(),
        exc_info=(type(exception), exception, exception.__traceback__) if exception else None
    )

    record.event_type = "CRITICAL_ERROR"
    record.error_code = error_code
    record.module = module

    if extra:
        for key, value in extra.items():
            setattr(record, key, value)

    json_logger.handle(record)

    # Also log to regular logger
    logger = get_logger(module)
    logger.error(f"[{error_code}] {message}")


# ============================================================================
# CLEANUP UTILITIES
# ============================================================================

def cleanup_old_logs(days: int = 7) -> int:
    """Remove log files older than specified days.
    
    Args:
        days: Number of days to keep logs
        
    Returns:
        Number of files deleted
    """
    import time

    if not LOG_DIR.exists():
        return 0

    cutoff_time = time.time() - (days * 24 * 60 * 60)
    deleted = 0

    for log_file in LOG_DIR.glob("*.log*"):
        if log_file.stat().st_mtime < cutoff_time:
            try:
                log_file.unlink()
                deleted += 1
            except OSError:
                pass

    return deleted


def get_log_stats() -> Dict[str, Any]:
    """Get statistics about current log files.
    
    Returns:
        Dictionary with log statistics
    """
    if not LOG_DIR.exists():
        return {"total_files": 0, "total_size_mb": 0}

    files = list(LOG_DIR.glob("*.log*"))
    total_size = sum(f.stat().st_size for f in files)

    return {
        "total_files": len(files),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "log_dir": str(LOG_DIR.absolute())
    }



# ============================================================================
# TRACE ID INTEGRATION
# ============================================================================

class TraceIdFilter(logging.Filter):
    """Filter that adds trace_id to log records for request correlation."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Always set trace_id, even if empty
        if not hasattr(record, 'trace_id'):
            try:
                from analytics.trace_context import get_trace_id
                record.trace_id = get_trace_id() or '-'
            except ImportError:
                record.trace_id = '-'
        return True


# Format with trace_id support
LOG_FORMAT_WITH_TRACE = "%(asctime)s | %(levelname)-8s | %(trace_id)s | %(name)s | %(message)s"

# Global filter instance
_trace_filter = TraceIdFilter()


def setup_trace_logging() -> None:
    """Add trace_id support to all existing and future handlers."""
    root_logger = logging.getLogger()

    # Add filter to root logger (applies to all child loggers)
    if _trace_filter not in root_logger.filters:
        root_logger.addFilter(_trace_filter)

    # Update all existing handlers with new format
    formatter = logging.Formatter(LOG_FORMAT_WITH_TRACE, datefmt=LOG_DATE_FORMAT)
    for handler in root_logger.handlers:
        # Add filter to handler too (belt and suspenders)
        if _trace_filter not in handler.filters:
            handler.addFilter(_trace_filter)
        handler.setFormatter(formatter)


def enable_global_trace_logging() -> None:
    """Alias for setup_trace_logging for backward compatibility."""
    setup_trace_logging()
