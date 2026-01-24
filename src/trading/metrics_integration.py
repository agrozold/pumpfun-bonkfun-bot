"""
Metrics integration for UniversalTrader.
Патчит методы для записи метрик и трейсов.
Импортировать ПОСЛЕ universal_trader.
"""

import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Ленивый импорт чтобы избежать circular imports
_metrics_available = False
_trace_available = False

def _lazy_import_metrics():
    global _metrics_available
    if _metrics_available:
        return True
    try:
        global record_tx_sent, record_trade_failure, set_active_positions
        from analytics.metrics_server import (
            record_tx_sent,
            record_trade_failure,
            set_active_positions,
        )
        _metrics_available = True
        return True
    except ImportError as e:
        logger.warning(f"Metrics not available: {e}")
        return False

def _lazy_import_trace():
    global _trace_available
    if _trace_available:
        return True
    try:
        global TraceContext, get_trace_id
        from analytics.trace_context import TraceContext, get_trace_id
        _trace_available = True
        return True
    except ImportError as e:
        logger.warning(f"Tracing not available: {e}")
        return False


def patch_universal_trader(trader_class):
    """
    Патчит UniversalTrader для записи метрик.
    
    Использование:
        from trading.universal_trader import UniversalTrader
        from trading.metrics_integration import patch_universal_trader
        patch_universal_trader(UniversalTrader)
    """

    # Сохраняем оригинальные методы
    original_handle_successful_buy = trader_class._handle_successful_buy
    original_handle_failed_buy = trader_class._handle_failed_buy

    @functools.wraps(original_handle_successful_buy)
    async def patched_handle_successful_buy(self, token_info, buy_result):
        """Обёртка с метриками для успешной покупки"""
        # Вызываем оригинал
        result = await original_handle_successful_buy(self, token_info, buy_result)

        # Записываем метрики
        if _lazy_import_metrics():
            try:
                platform = getattr(token_info, 'platform', None)
                platform_str = platform.value if platform else 'unknown'
                record_tx_sent(platform=platform_str, tx_type='buy', success=True)

                # Обновляем счётчик активных позиций
                active_count = len(getattr(self, 'active_positions', []))
                set_active_positions(active_count)

                logger.debug(f"[METRICS] Recorded successful buy for {token_info.symbol}")
            except Exception as e:
                logger.warning(f"[METRICS] Failed to record buy metrics: {e}")

        return result

    @functools.wraps(original_handle_failed_buy)
    async def patched_handle_failed_buy(self, token_info, buy_result):
        """Обёртка с метриками для неудачной покупки"""
        # Записываем метрики ДО вызова оригинала
        if _lazy_import_metrics():
            try:
                error_msg = getattr(buy_result, 'error_message', 'unknown')
                # Обрезаем до 50 символов для label
                reason = str(error_msg)[:50] if error_msg else 'unknown'
                record_trade_failure(reason=reason)

                platform = getattr(token_info, 'platform', None)
                platform_str = platform.value if platform else 'unknown'
                record_tx_sent(platform=platform_str, tx_type='buy', success=False)

                logger.debug(f"[METRICS] Recorded failed buy for {token_info.symbol}: {reason}")
            except Exception as e:
                logger.warning(f"[METRICS] Failed to record failure metrics: {e}")

        # Вызываем оригинал
        return await original_handle_failed_buy(self, token_info, buy_result)

    # Применяем патчи
    trader_class._handle_successful_buy = patched_handle_successful_buy
    trader_class._handle_failed_buy = patched_handle_failed_buy

    logger.info("[METRICS] UniversalTrader patched with metrics integration")
    return trader_class


# Автопатч при импорте (опционально)
def auto_patch():
    """Автоматически патчит UniversalTrader если он уже импортирован"""
    try:
        from trading.universal_trader import UniversalTrader
        patch_universal_trader(UniversalTrader)
    except ImportError:
        pass
