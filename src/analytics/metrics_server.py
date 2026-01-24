"""
Prometheus Metrics Server
HTTP endpoint /metrics для сбора метрик
"""

import asyncio
import logging
from typing import Optional
from aiohttp import web
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST,
    CollectorRegistry, REGISTRY
)

logger = logging.getLogger(__name__)

# ========== Метрики ==========

# Counters
TX_SENT_TOTAL = Counter(
    'bot_tx_sent_total',
    'Total transactions sent',
    ['platform', 'type', 'status']
)

TOKENS_DETECTED_TOTAL = Counter(
    'bot_tokens_detected_total',
    'Tokens detected by listeners',
    ['platform']
)

TRADE_FAIL_TOTAL = Counter(
    'bot_trade_fail_total',
    'Failed trades by reason',
    ['reason']
)

LISTENER_RECONNECT_TOTAL = Counter(
    'bot_listener_reconnect_total',
    'Listener reconnection count',
    ['listener']
)

# Histograms
TX_LATENCY = Histogram(
    'bot_tx_latency_seconds',
    'Transaction latency by stage',
    ['stage'],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
)

BUILD_DURATION = Histogram(
    'bot_build_duration_seconds',
    'Transaction build duration',
    ['stage'],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5]
)

SEND_LATENCY = Histogram(
    'bot_send_latency_seconds',
    'Send latency by provider',
    ['provider'],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
)

DISTANCE_TO_TARGET_SLOTS = Histogram(
    'bot_distance_to_target_slots',
    'Slots between signal and landing',
    buckets=[1, 2, 3, 5, 10, 20, 50]
)

# Gauges
ACTIVE_POSITIONS = Gauge(
    'bot_active_positions',
    'Current active positions'
)

WALLET_BALANCE_SOL = Gauge(
    'bot_wallet_balance_sol',
    'Wallet balance in SOL'
)

LISTENER_LAST_MSG_AGE = Gauge(
    'bot_listener_last_msg_age_seconds',
    'Seconds since last message',
    ['listener']
)


# ========== Вспомогательные функции ==========

def record_tx_sent(platform: str, tx_type: str, success: bool) -> None:
    """Записать отправку транзакции"""
    status = 'ok' if success else 'err'
    TX_SENT_TOTAL.labels(platform=platform, type=tx_type, status=status).inc()


def record_token_detected(platform: str) -> None:
    """Записать обнаружение токена"""
    TOKENS_DETECTED_TOTAL.labels(platform=platform).inc()


def record_trade_failure(reason: str) -> None:
    """Записать провал сделки"""
    TRADE_FAIL_TOTAL.labels(reason=reason).inc()


def record_latency(stage: str, latency_sec: float) -> None:
    """Записать latency"""
    TX_LATENCY.labels(stage=stage).observe(latency_sec)


def record_send_latency(provider: str, latency_sec: float) -> None:
    """Записать send latency по провайдеру"""
    SEND_LATENCY.labels(provider=provider).observe(latency_sec)


def set_active_positions(count: int) -> None:
    """Установить количество активных позиций"""
    ACTIVE_POSITIONS.set(count)


def set_wallet_balance(sol: float) -> None:
    """Установить баланс кошелька"""
    WALLET_BALANCE_SOL.set(sol)


# ========== HTTP Server ==========

async def metrics_handler(request: web.Request) -> web.Response:
    """Handler для /metrics endpoint"""
    metrics_output = generate_latest(REGISTRY)
    return web.Response(
        body=metrics_output,
        headers={'Content-Type': CONTENT_TYPE_LATEST}
    )


async def health_handler(request: web.Request) -> web.Response:
    """Handler для /health endpoint"""
    return web.Response(text='OK')


class MetricsServer:
    """HTTP сервер для метрик"""
    
    def __init__(self, host: str = '0.0.0.0', port: int = 9090):
        self.host = host
        self.port = port
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
    
    async def start(self) -> None:
        """Запустить сервер"""
        self._app = web.Application()
        self._app.router.add_get('/metrics', metrics_handler)
        self._app.router.add_get('/health', health_handler)
        
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        
        logger.info(f"Metrics server started on http://{self.host}:{self.port}/metrics")
    
    async def stop(self) -> None:
        """Остановить сервер"""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Metrics server stopped")


# Глобальный сервер
_server: Optional[MetricsServer] = None


async def start_metrics_server(host: str = '0.0.0.0', port: int = 9090) -> MetricsServer:
    """Запустить глобальный сервер метрик"""
    global _server
    _server = MetricsServer(host, port)
    await _server.start()
    return _server


async def stop_metrics_server() -> None:
    """Остановить глобальный сервер"""
    if _server:
        await _server.stop()
