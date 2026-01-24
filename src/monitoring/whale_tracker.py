"""
Whale Tracker - отслеживает транзакции китов в РЕАЛЬНОМ ВРЕМЕНИ.
Когда кит покупает токен - отправляет сигнал на копирование.

ВАЖНО: Копируем ТОЛЬКО свежие покупки (в пределах time_window_minutes).
Старые/исторические покупки игнорируются!

Поддерживает ВСЕ платформы:
- pump.fun (6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P)
- letsbonk/Raydium LaunchLab (LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj)
- BAGS (HWPsB1A5biibMngZB8XXb7FnFT4ohm1DMY6y1JdLBAGS)
- PumpSwap AMM (PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP) - migrated tokens
- Raydium AMM (675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8) - migrated tokens

OPTIMIZED: Uses global RPC Manager for rate limiting and provider rotation.
"""

import asyncio
import json
import logging
import time
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

# Import RPC Manager for optimized requests
try:
    from core.rpc_manager import RPCManager, get_rpc_manager

    RPC_MANAGER_AVAILABLE = True
except ImportError:
    RPC_MANAGER_AVAILABLE = False
    logger.warning("[WHALE] RPC Manager not available, using legacy mode")

# Program IDs for all supported platforms
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LETS_BONK_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
# BAGS uses Meteora DBC (Dynamic Bonding Curve) program
BAGS_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
# Migrated tokens trade on these DEXes
PUMPSWAP_PROGRAM = "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP"
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
# Jupiter Aggregator v6
JUPITER_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
# Jupiter Limit Order
JUPITER_LIMIT_PROGRAM = "jupoNjAxXgZ4rjzxzPMP4oxduvQsQtZzyknqvzYNrNu"
# Orca Whirlpool
ORCA_WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
# Meteora DLMM
METEORA_DLMM_PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
# Raydium CLMM (Concentrated Liquidity)
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"

# All programs to monitor (bonding curves + DEXes for migrated)
ALL_PROGRAMS = [
    PUMP_FUN_PROGRAM,
    LETS_BONK_PROGRAM,
    BAGS_PROGRAM,
    PUMPSWAP_PROGRAM,
    RAYDIUM_AMM_PROGRAM,
    JUPITER_PROGRAM,
    JUPITER_LIMIT_PROGRAM,
    ORCA_WHIRLPOOL_PROGRAM,
    METEORA_DLMM_PROGRAM,
    RAYDIUM_CLMM_PROGRAM,
]

# Program ID to platform mapping
PROGRAM_TO_PLATFORM: dict[str, str] = {
    PUMP_FUN_PROGRAM: "pump_fun",
    LETS_BONK_PROGRAM: "lets_bonk",
    BAGS_PROGRAM: "bags",
    PUMPSWAP_PROGRAM: "pumpswap",
    RAYDIUM_AMM_PROGRAM: "raydium",
    JUPITER_PROGRAM: "jupiter",
    JUPITER_LIMIT_PROGRAM: "jupiter",
    ORCA_WHIRLPOOL_PROGRAM: "orca",
    METEORA_DLMM_PROGRAM: "meteora",
    RAYDIUM_CLMM_PROGRAM: "raydium",
}

# ============================================
# RATE LIMITING NOW HANDLED BY RPC MANAGER
# ============================================
# All RPC requests go through src/core/rpc_manager.py which:
# - Rotates between Helius, Alchemy, and public Solana
# - Applies per-provider rate limits
# - Automatic fallback on 429 errors
# - Request caching to reduce API calls
#
# Legacy constants kept for backwards compatibility
HELIUS_RATE_LIMIT_SECONDS = 1.0
HELIUS_RATE_LIMIT_JITTER = 0.5


@dataclass
class WhaleBuy:
    """Информация о покупке кита."""

    whale_wallet: str
    token_mint: str
    token_symbol: str
    amount_sol: float
    timestamp: datetime
    tx_signature: str
    whale_label: str = "whale"
    block_time: int | None = None  # Unix timestamp транзакции
    age_seconds: float = 0  # Сколько секунд назад была покупка
    platform: str = "pump_fun"  # Платформа: pump_fun или lets_bonk


class WhaleTracker:
    """Отслеживает покупки китов через WebSocket соединения.

    REAL-TIME копирование: только свежие покупки (< time_window_minutes).
    Поддерживает: pump.fun, letsbonk

    ВАЖНО: Каждый бот должен создавать свой WhaleTracker с указанием platform,
    чтобы избежать конфликтов WebSocket подписок между процессами.

    OPTIMIZED: Uses global RPC Manager for all HTTP requests to avoid 429 errors.
    """

    def __init__(
        self,
        wallets_file: str = "smart_money_wallets.json",
        min_buy_amount: float = 0.5,
        helius_api_key: str | None = None,
        rpc_endpoint: str | None = None,
        wss_endpoint: str | None = None,
        time_window_minutes: float = 5.0,  # Копируем только покупки за последние N минут
        platform: str | None = None,  # Если указано - слушаем только эту платформу
        stablecoin_filter: list | None = None,  # Список mint адресов стейблкоинов для игнорирования
    ):
        self.wallets_file = wallets_file
        self.min_buy_amount = min_buy_amount
        self.helius_api_key = helius_api_key or os.getenv("HELIUS_API_KEY")
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        self.time_window_minutes = time_window_minutes
        self.time_window_seconds = time_window_minutes * 60
        self.target_platform = platform  # None = все платформы, иначе только указанная

        # Стейблкоины для игнорирования (не копируем сделки с ними)
        self.stablecoin_filter = set(stablecoin_filter or [])
        if self.stablecoin_filter:
            logger.info(f"[WHALE] Stablecoin filter: {len(self.stablecoin_filter)} tokens will be ignored")

        self.whale_wallets: dict[str, dict] = {}  # wallet -> info
        self.on_whale_buy: Callable | None = None
        self.running = False
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._processed_txs: set[str] = set()
        self._emitted_tokens: set[str] = (
            set()
        )  # Tokens already emitted to prevent duplicates

        # WSS fallback tracking
        self._fast_closes = 0
        self._use_fallback_wss = False
        self._connect_time = 0.0
        # RPC Manager for optimized requests (initialized lazily)
        self._rpc_manager: RPCManager | None = None

        # RPC optimization: TX cache with extended TTL for quota saving
        self._tx_cache: dict[str, tuple[dict, float]] = {}  # sig -> (result, timestamp)
        self._cache_ttl = 180.0  # 180 seconds TTL (3 min - extended for quota saving)
        self._cache_max_size = 1500  # Larger LRU cache to reduce API calls

        # Helius rate limiting - now handled by RPC Manager
        self._last_helius_call = 0.0
        self._helius_rate_limit = HELIUS_RATE_LIMIT_SECONDS

        # TX queue for rate-limited processing
        self._pending_txs: list[tuple[str, str]] = []  # (signature, platform)
        self._max_pending = 100  # Queue up to 100 TXs for processing

        # Performance metrics for quota monitoring
        self._metrics = {
            "helius_calls": 0,
            "helius_success": 0,
            "public_fallback_calls": 0,
            "cache_hits": 0,
            "timeouts": 0,
            "requests_today": 0,
            "day_start": time.time(),
            "rpc_manager_calls": 0,
        }

        # СТАТИСТИКА КОПИРОВАНИЯ
        self._copy_stats = {"signals": 0, "success": 0, "failed": 0, "skipped": 0}

        self._load_wallets()

        platform_info = f"platform={platform}" if platform else "ALL platforms"
        logger.info(
            f"WhaleTracker initialized: {len(self.whale_wallets)} wallets, "
            f"min_buy={min_buy_amount} SOL, time_window={time_window_minutes} min, {platform_info}"
        )

        if RPC_MANAGER_AVAILABLE:
            logger.info("[WHALE] Using RPC Manager for optimized requests")
        else:
            logger.info("[WHALE] RPC Manager not available, using legacy mode")

    def _load_wallets(self):
        """Загрузить список кошельков китов."""
        path = Path(self.wallets_file)
        logger.warning(f"[WHALE] Loading wallets from: {path.absolute()}")

        if not path.exists():
            logger.error(f"[WHALE] Wallets file NOT FOUND: {path.absolute()}")
            return

        try:
            with open(path) as f:
                data = json.load(f)

            whales_list = data.get("whales", [])
            logger.warning(f"[WHALE] Found {len(whales_list)} entries in whales list")

            for whale in whales_list:
                wallet = whale.get("wallet", "")
                if wallet:
                    self.whale_wallets[wallet] = {
                        "label": whale.get("label", "whale"),
                        "win_rate": whale.get("win_rate", 0.5),
                        "source": whale.get("source", "manual"),
                    }

            logger.warning(
                f"[WHALE] Loaded {len(self.whale_wallets)} whale wallets successfully"
            )

            # ВЫВОД ПЕРВЫХ 10 КОШЕЛЬКОВ
            logger.warning("=" * 70)
            logger.warning("[WHALE] MY SMART MONEY WALLETS (first 10):")
            for i, (w, info) in enumerate(list(self.whale_wallets.items())[:10], 1):
                logger.warning(f"  {i}. {w} | {info.get('label', 'whale')} | wr={info.get('win_rate', 0):.0%}")
            if len(self.whale_wallets) > 10:
                logger.warning(f"  ... and {len(self.whale_wallets) - 10} more wallets")
            logger.warning("=" * 70)

        except json.JSONDecodeError as e:
            logger.error(f"[WHALE] JSON parse error in {self.wallets_file}: {e}")
        except Exception as e:
            logger.exception(f"[WHALE] Error loading wallets: {e}")

    def add_wallet(self, wallet: str, label: str = "whale", win_rate: float = 0.5):
        """Добавить кошелёк для отслеживания."""
        self.whale_wallets[wallet] = {
            "label": label,
            "win_rate": win_rate,
            "source": "runtime",
        }
        logger.info(f"Added whale wallet: {wallet[:8]}... ({label})")

    def set_callback(self, callback: Callable):
        """Установить callback для сигналов о покупках китов."""
        self.on_whale_buy = callback

    def _get_wss_endpoint(self) -> str | None:
        """Получить WSS endpoint для logsSubscribe.

        FALLBACK LOGIC:
        1. Сначала пробуем Chainstack (если указан в wss_endpoint)
        2. Если слишком много быстрых закрытий (< 90 сек) - переключаемся на публичный Solana
        3. Helius WSS не используем - даёт 429 rate limit
        """
        public_wss = "wss://api.mainnet-beta.solana.com"

        # Если уже переключились на fallback - используем публичный Solana
        if self._use_fallback_wss:
            logger.warning("[WHALE] WSS ENDPOINT: Using PUBLIC SOLANA (fallback mode)")
            return public_wss

        # Если передан wss_endpoint (не Helius) - пробуем его
        if self.wss_endpoint and "helius" not in self.wss_endpoint.lower():
            logger.warning(
                f"[WHALE] WSS ENDPOINT: Using provided: {self.wss_endpoint[:50]}..."
            )
            return self.wss_endpoint

        # По умолчанию публичный Solana WSS
        logger.warning("[WHALE] WSS ENDPOINT: Using public Solana (default)")
        return public_wss

    async def start(self):
        """Запустить отслеживание платформ.

        Если target_platform указан - слушаем только её.
        Иначе слушаем все платформы.
        """
        if not self.whale_wallets:
            logger.warning("[WHALE] No whale wallets to track")
            return

        wss_url = self._get_wss_endpoint()
        if not wss_url:
            logger.error("[WHALE] Cannot start whale tracker without WSS endpoint")
            return

        self.running = True
        self._session = aiohttp.ClientSession()

        # Определяем какие программы слушать
        if self.target_platform:
            # Слушаем только указанную платформу
            programs_to_track = []
            for program_id, platform in PROGRAM_TO_PLATFORM.items():
                if platform == self.target_platform:
                    programs_to_track.append(program_id)
            platform_names = self.target_platform
        else:
            # Слушаем все платформы
            programs_to_track = ALL_PROGRAMS
            platform_names = "pump.fun, letsbonk"

        logger.warning(
            f"[WHALE] WHALE TRACKER STARTED - tracking {len(self.whale_wallets)} wallets"
        )
        logger.warning(
            f"[WHALE] Min buy: {self.min_buy_amount} SOL, Time window: {self.time_window_minutes} min"
        )
        logger.warning(f"[WHALE] Monitoring: {platform_names}")
        logger.info(f"[WHALE] WSS endpoint: {wss_url[:50]}...")

        # Start queue processor in background
        queue_task = asyncio.create_task(self._process_pending_queue())

        try:
            # Подписываемся на выбранные программы
            await self._track_programs(wss_url, programs_to_track)
        finally:
            queue_task.cancel()
            try:
                await queue_task
            except asyncio.CancelledError:
                pass

    async def _process_pending_queue(self):
        """Background task to process queued transactions."""
        stats_interval = 300  # Log stats every 5 minutes
        last_stats_log = time.time()

        while self.running:
            try:
                if self._pending_txs:
                    # Process one TX from queue
                    signature, platform = self._pending_txs.pop(0)
                    # Remove from processed set to allow re-check
                    self._processed_txs.discard(signature)
                    await self._check_if_whale_tx(signature, platform)

                # Periodic stats logging
                now = time.time()
                if now - last_stats_log >= stats_interval:
                    self._log_quota_stats()
                    last_stats_log = now

                await asyncio.sleep(0.6)  # Slightly longer than rate limit
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[WHALE] Queue processor error: {e}")
                await asyncio.sleep(1.0)

    def _log_quota_stats(self):
        """Log Helius quota usage statistics with daily budget tracking."""
        m = self._metrics
        total_calls = (
            m["helius_calls"]
            + m["public_fallback_calls"]
            + m.get("rpc_manager_calls", 0)
        )
        cache_rate = (
            (m["cache_hits"] / (m["cache_hits"] + total_calls) * 100)
            if (m["cache_hits"] + total_calls) > 0
            else 0
        )

        # Calculate daily usage
        now = time.time()
        hours_elapsed = (now - m["day_start"]) / 3600
        if hours_elapsed > 0:
            hourly_rate = total_calls / hours_elapsed
            daily_projection = hourly_rate * 24
        else:
            hourly_rate = 0
            daily_projection = 0

        # Log RPC Manager metrics if available
        if self._rpc_manager:
            rpc_metrics = self._rpc_manager.get_metrics()
            logger.info(
                f"[WHALE STATS] RPC Manager: {rpc_metrics['total_requests']} total, "
                f"{rpc_metrics['successful_requests']} success, "
                f"{rpc_metrics['rate_limited']} rate limited, "
                f"{rpc_metrics['cache_hits']} cache hits"
            )

        logger.info(
            f"[WHALE STATS] Local: Helius {m['helius_calls']} ({m['helius_success']} ok), "
            f"Fallback: {m['public_fallback_calls']}, Cache: {m['cache_hits']} ({cache_rate:.1f}%)"
        )
        logger.info(
            f"[WHALE QUOTA] Rate: {hourly_rate:.0f}/hr, Projection: {daily_projection:.0f}/day, "
            f"Queue: {len(self._pending_txs)}"
        )

    async def stop(self):
        """Остановить отслеживание."""
        self.running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("Whale tracker stopped")

    async def _track_programs(self, wss_url: str, programs: list[str]):
        """Распределённое отслеживание whale адресов через несколько WSS соединений.
        
        Args:
            wss_url: Primary WebSocket URL
            programs: Список program ID (не используется - подписываемся на whale адреса)
        """
        whale_addresses = list(self.whale_wallets.keys())
        
        if not whale_addresses:
            logger.error("[WHALE] No whale wallets loaded! Cannot subscribe.")
            return
        
        # Получаем все доступные WSS endpoints
        wss_endpoints = self._get_all_wss_endpoints(wss_url)
        
        # Разбиваем адреса на группы (макс 40 на соединение для надёжности)
        CHUNK_SIZE = 40
        address_chunks = [
            whale_addresses[i:i + CHUNK_SIZE] 
            for i in range(0, len(whale_addresses), CHUNK_SIZE)
        ]
        
        logger.warning(f"[WHALE] Distributed monitoring: {len(whale_addresses)} wallets -> {len(address_chunks)} WSS connections")
        
        # Создаём задачи для каждой группы
        tasks = []
        for i, chunk in enumerate(address_chunks):
            # Распределяем по разным WSS endpoints (round-robin)
            endpoint = wss_endpoints[i % len(wss_endpoints)]
            task = asyncio.create_task(
                self._track_whale_group(endpoint, chunk, group_id=i+1)
            )
            tasks.append(task)
        
        # Ждём все задачи (они работают бесконечно пока self.running)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"[WHALE] Distributed tracking error: {e}")
    
    def _get_all_wss_endpoints(self, primary_wss: str) -> list[str]:
        """Получить все доступные WSS endpoints для распределения нагрузки."""
        import os
        endpoints = []
        
        # Primary (Chainstack)
        if primary_wss:
            endpoints.append(primary_wss)
        
        # Syndica
        syndica_wss = os.getenv("SYNDICA_WSS_ENDPOINT")
        if syndica_wss:
            endpoints.append(syndica_wss)
        
        # dRPC  
        drpc_wss = os.getenv("DRPC_WSS_ENDPOINT")
        if drpc_wss:
            endpoints.append(drpc_wss)
        
        # QuickNode
        quicknode_wss = os.getenv("QUICKNODE_WSS_ENDPOINT")
        if quicknode_wss:
            endpoints.append(quicknode_wss)
        
        # Public Solana as fallback
        endpoints.append("wss://api.mainnet-beta.solana.com")
        
        logger.info(f"[WHALE] Available WSS endpoints: {len(endpoints)}")
        return endpoints
    
    async def _track_whale_group(self, wss_url: str, whale_addresses: list[str], group_id: int):
        """Отслеживание группы whale адресов через одно WSS соединение."""
        consecutive_errors = 0
        max_consecutive_errors = 10
        
        while self.running:
            try:
                logger.info(f"[WHALE-G{group_id}] Connecting to WSS ({len(whale_addresses)} wallets)...")
                async with self._session.ws_connect(
                    wss_url,
                    heartbeat=30,
                    timeout=aiohttp.ClientTimeout(total=60, sock_connect=30),
                    receive_timeout=180,
                ) as ws:
                    consecutive_errors = 0
                    
                    # Подписываемся на whale адреса этой группы
                    for i, whale_addr in enumerate(whale_addresses):
                        subscribe_msg = {
                            "jsonrpc": "2.0",
                            "id": group_id * 1000 + i,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [whale_addr]},
                                {"commitment": "processed"},
                            ],
                        }
                        await ws.send_json(subscribe_msg)
                    
                    logger.warning(f"[WHALE-G{group_id}] SUBSCRIBED to {len(whale_addresses)} wallets")
                    
                    # Message processing loop
                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=180)
                            
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                    await self._handle_log(data)
                                except json.JSONDecodeError:
                                    pass
                                except Exception as e:
                                    logger.debug(f"[WHALE-G{group_id}] Error processing: {e}")
                            elif msg.type == aiohttp.WSMsgType.PING:
                                await ws.pong(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                                logger.warning(f"[WHALE-G{group_id}] WSS closed, reconnecting...")
                                break
                        except asyncio.TimeoutError:
                            logger.warning(f"[WHALE-G{group_id}] No messages for 3 min, reconnecting...")
                            break
                            
            except asyncio.CancelledError:
                logger.info(f"[WHALE-G{group_id}] Cancelled")
                break
            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"[WHALE-G{group_id}] Error: {e}")
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(f"[WHALE-G{group_id}] Too many errors, stopping")
                    break
                backoff = min(2 ** consecutive_errors, 60)
                await asyncio.sleep(backoff)


    def _detect_platform_from_logs(self, logs: list[str]) -> str | None:
        """Определить платформу по логам транзакции.

        Args:
            logs: Список строк логов транзакции

        Returns:
            Строка платформы ("pump_fun" или "lets_bonk") или None
        """
        for log in logs:
            for program_id, platform in PROGRAM_TO_PLATFORM.items():
                if program_id in log:
                    return platform
        return None

    async def _handle_log(self, data: dict):
        """Роутинг логов на соответствующий обработчик платформы.

        Args:
            data: Сырые данные лог-нотификации от WebSocket
        """
        logger.debug(f"[WHALE-DBG] _handle_log called, method={data.get('method')}")
        if data.get("method") != "logsNotification":
            return

        try:
            params = data.get("params", {})
            result = params.get("result", {})
            value = result.get("value", {})

            signature = value.get("signature", "")
            logs = value.get("logs", [])
            err = value.get("err")

            logger.debug(f"[WHALE-DBG] sig={signature[:16]}... err={err}, logs_count={len(logs)}")
            if err or not signature:
                return

            if signature in self._processed_txs:
                return

            # Определяем платформу по логам
            platform = self._detect_platform_from_logs(logs)
            logger.debug(f"[WHALE-DBG] Platform: {platform}, target: {self.target_platform}")
            if not platform:
                return

            # ФИЛЬТР: Если указана target_platform - игнорируем другие платформы
            # Это критично для multi-bot setup где каждый бот слушает свою платформу
            if self.target_platform and platform != self.target_platform:
                return

            # Проверяем что это Buy/Swap инструкция
            is_buy = False
            for log in logs:
                # pump.fun и letsbonk используют "Instruction: Buy"
                # Raydium/PumpSwap используют "Instruction: swap" или transfer patterns
                if "Instruction: Buy" in log or "Instruction: buy" in log.lower():
                    is_buy = True
                    break
                # Raydium AMM swap detection
                if "Instruction: swap" in log.lower() or "ray_log" in log.lower():
                    is_buy = True
                    break
                # PumpSwap detection
                if PUMPSWAP_PROGRAM in log and (
                    "swap" in log.lower() or "buy" in log.lower()
                ):
                    is_buy = True
                    break
                # Jupiter Aggregator detection
                # Orca Whirlpool detection
                if "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc" in log or "Program whir" in log:
                    is_buy = True
                    break
                # Meteora DLMM detection
                if "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo" in log or "Program LBU" in log:
                    is_buy = True
                    break
                if "Program JUP" in log or "Instruction: route" in log.lower() or "Instruction: sharedAccountsRoute" in log.lower():
                    is_buy = True
                    break

            logger.debug(f"[WHALE-DBG] is_buy={is_buy} for {signature[:16]}")
            if not is_buy:
                return

            # Получаем детали транзакции и проверяем кошелёк
            await self._check_if_whale_tx(signature, platform)

        except Exception as e:
            logger.warning(f"[WHALE] Error handling log: {e}")

    async def _check_if_whale_tx(self, signature: str, platform: str = "pump_fun"):
        """Проверить, является ли транзакция покупкой кита.

        OPTIMIZED: Uses RPC Manager for automatic provider rotation and rate limiting.
        Falls back to legacy mode if RPC Manager is not available.

        Args:
            signature: Сигнатура транзакции
            platform: Платформа ("pump_fun" или "lets_bonk")
        """
        logger.debug(f"[WHALE-DBG] _check_if_whale_tx CALLED: {signature[:16]}...")

        # Mark as processed to avoid duplicates
        self._processed_txs.add(signature)
        if len(self._processed_txs) > 1000:
            # Keep only last 500
            self._processed_txs = set(list(self._processed_txs)[-500:])

        # Check local cache first
        if signature in self._tx_cache:
            cached, ts = self._tx_cache[signature]
            if time.time() - ts < self._cache_ttl:
                self._metrics["cache_hits"] += 1
                if cached:
                    if "feePayer" in cached:
                        await self._process_helius_tx(cached, platform)
                    else:
                        await self._process_rpc_tx(cached, signature, platform)
                return

        # Use RPC Manager if available (OPTIMIZED PATH)
        if RPC_MANAGER_AVAILABLE:
            await self._check_whale_tx_with_manager(signature, platform)
            return

        # Legacy fallback (if RPC Manager not available)
        await self._check_whale_tx_legacy(signature, platform)

    async def _check_whale_tx_with_manager(self, signature: str, platform: str):
        """Check whale TX using RPC Manager (optimized path)."""
        logger.debug(f"[WHALE-DBG] _check_whale_tx_with_manager START: {signature[:16]}...")
        # Initialize RPC Manager lazily
        if self._rpc_manager is None:
            self._rpc_manager = await get_rpc_manager()

        self._metrics["rpc_manager_calls"] += 1

        # Try Helius Enhanced API first (best for parsed transactions)
        logger.debug(f"[WHALE-DBG] Calling Helius Enhanced for {signature[:16]}...")
        tx = await self._rpc_manager.get_transaction(signature)
        logger.debug(f"[WHALE-DBG] Helius result: {bool(tx)} for {signature[:16]}...")
        if tx:
            self._metrics["helius_success"] += 1
            self._cache_tx(signature, tx)
            await self._process_helius_tx(tx, platform)
            return

        # Fallback to regular RPC (RPC Manager handles provider rotation)
        tx = await self._rpc_manager.get_transaction(signature)
        if tx:
            self._cache_tx(signature, tx)
            await self._process_rpc_tx(tx, signature, platform)
            return

        # TX not confirmed yet - this is normal for very fresh TXs
        logger.debug(f"[WHALE] TX {signature[:16]}... not confirmed yet")

    async def _check_whale_tx_legacy(self, signature: str, platform: str):
        """Legacy whale TX check (fallback when RPC Manager not available)."""
        import os
        import random

        # Rate limit check with jitter
        now = time.time()
        effective_rate_limit = HELIUS_RATE_LIMIT_SECONDS + random.uniform(
            0, HELIUS_RATE_LIMIT_JITTER
        )
        if now - self._last_helius_call < effective_rate_limit:
            if len(self._pending_txs) < self._max_pending:
                self._pending_txs.append((signature, platform))
            return
        self._last_helius_call = now

        # Try Helius Enhanced API first
        helius_key = self.helius_api_key or os.getenv("HELIUS_API_KEY")
        if helius_key:
            tx = await self._get_tx_helius(signature)
            if tx:
                self._metrics["helius_calls"] += 1
                self._metrics["helius_success"] += 1
                self._cache_tx(signature, tx)
                await self._process_helius_tx(tx, platform)
                return

        # Try Alchemy RPC
        alchemy_endpoint = os.getenv("ALCHEMY_RPC_ENDPOINT")
        if alchemy_endpoint:
            tx = await self._get_tx_from_endpoint(
                signature, alchemy_endpoint, timeout=5.0
            )
            if tx:
                self._cache_tx(signature, tx)
                await self._process_rpc_tx(tx, signature, platform)
                return

        # Try main RPC endpoint
        if self.rpc_endpoint:
            tx = await self._get_tx_from_endpoint(
                signature, self.rpc_endpoint, timeout=5.0
            )
            if tx:
                self._cache_tx(signature, tx)
                await self._process_rpc_tx(tx, signature, platform)
                return

        # Try public RPC as last resort
        public_rpc = "https://api.mainnet-beta.solana.com"
        tx = await self._get_tx_from_endpoint(signature, public_rpc, timeout=5.0)
        if tx:
            self._metrics["public_fallback_calls"] += 1
            self._cache_tx(signature, tx)
            await self._process_rpc_tx(tx, signature, platform)
            return

        logger.debug(f"[WHALE] TX {signature[:16]}... not confirmed yet on any RPC")

    def _cache_tx(self, signature: str, tx: dict):
        """Cache TX result with LRU eviction."""
        self._tx_cache[signature] = (tx, time.time())

        # LRU eviction if over size limit
        if len(self._tx_cache) > self._cache_max_size:
            oldest = min(self._tx_cache.keys(), key=lambda k: self._tx_cache[k][1])
            del self._tx_cache[oldest]

    async def _get_tx_helius(self, signature: str) -> dict | None:
        """Получить транзакцию через Helius Enhanced API (парсит автоматически!).

        HARDCODED Helius API key для надёжности.
        """
        # HARDCODED правильный ключ!
        helius_parse_url = (
            f"https://api.helius.xyz/v0/transactions"
            f"?api-key={os.getenv('HELIUS_API_KEY')}"
        )

        try:
            async with self._session.post(
                helius_parse_url,
                json={"transactions": [signature]},
                timeout=aiohttp.ClientTimeout(total=5),
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return data[0]
                    return None
                elif resp.status == 429:
                    logger.debug("[WHALE] Helius Enhanced API rate limited (429)")
                    return None
                else:
                    logger.debug(f"[WHALE] Helius Enhanced API HTTP {resp.status}")
                    return None
        except TimeoutError:
            logger.debug("[WHALE] Helius Enhanced API timeout")
            return None
        except Exception as e:
            logger.debug(f"[WHALE] Helius Enhanced API error: {e}")
        return None

    async def _get_tx_from_endpoint(
        self, signature: str, endpoint: str, timeout: float = 3.0
    ) -> dict | None:
        """Получить транзакцию через указанный RPC endpoint.

        Args:
            signature: Сигнатура транзакции
            endpoint: URL RPC endpoint
            timeout: Таймаут запроса в секундах

        Returns:
            Данные транзакции или None
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ],
        }

        endpoint_name = "Helius" if "helius" in endpoint.lower() else endpoint[:30]

        try:
            async with self._session.post(
                endpoint,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get("result")
                    if result:
                        logger.debug(f"[WHALE] {endpoint_name} returned TX data")
                        return result
                    # result is None - TX not found yet
                    error = data.get("error")
                    if error:
                        logger.warning(f"[WHALE] {endpoint_name} error: {error}")
                    return None
                elif resp.status == 429:
                    logger.warning(f"[WHALE] {endpoint_name} rate limited (429)")
                    return None
                else:
                    logger.warning(f"[WHALE] {endpoint_name} HTTP {resp.status}")
                    return None
        except TimeoutError:
            logger.warning(f"[WHALE] {endpoint_name} TIMEOUT ({timeout}s)")
            return None
        except Exception as e:
            logger.warning(f"[WHALE] {endpoint_name} error: {e}")
            return None

    async def _get_tx_rpc(self, signature: str) -> dict | None:
        """Получить транзакцию через RPC (legacy, использует self.rpc_endpoint)."""
        if not self.rpc_endpoint:
            return None
        return await self._get_tx_from_endpoint(signature, self.rpc_endpoint)

    async def _process_helius_tx(self, tx: dict, platform: str = "pump_fun"):
        """Обработать транзакцию от Helius.

        Args:
            tx: Данные транзакции от Helius
            platform: Платформа ("pump_fun" или "lets_bonk")
        """
        try:
            fee_payer = tx.get("feePayer", "")

            # Проверяем, является ли fee_payer китом
            if fee_payer not in self.whale_wallets:
                return

            whale_info = self.whale_wallets[fee_payer]
            signature = tx.get("signature", "")

            # Получаем block_time для проверки свежести
            block_time = tx.get("timestamp")

            # Считаем SOL
            sol_spent = 0
            token_mint = None

            for transfer in tx.get("nativeTransfers", []):
                if transfer.get("fromUserAccount") == fee_payer:
                    sol_spent += transfer.get("amount", 0) / 1e9

            for transfer in tx.get("tokenTransfers", []):
                if transfer.get("toUserAccount") == fee_payer:
                    token_mint = transfer.get("mint")
                    break

            if sol_spent >= self.min_buy_amount and token_mint:
                await self._emit_whale_buy(
                    wallet=fee_payer,
                    token_mint=token_mint,
                    sol_spent=sol_spent,
                    signature=signature,
                    whale_label=whale_info.get("label", "whale"),
                    block_time=block_time,
                    platform=platform,
                )

        except Exception as e:
            logger.debug(f"Error processing Helius tx: {e}")

    async def _process_rpc_tx(
        self, tx: dict, signature: str, platform: str = "pump_fun"
    ):
        """Обработать транзакцию от RPC.

        Args:
            tx: Данные транзакции от RPC
            signature: Сигнатура транзакции
            platform: Платформа ("pump_fun" или "lets_bonk")
        """
        try:
            message = tx.get("transaction", {}).get("message", {})
            account_keys = message.get("accountKeys", [])

            if not account_keys:
                logger.info(f"[WHALE] No account keys in TX {signature[:16]}...")
                return

            # fee_payer - первый аккаунт
            first_key = account_keys[0]
            fee_payer = (
                first_key.get("pubkey", "")
                if isinstance(first_key, dict)
                else str(first_key)
            )

            if fee_payer not in self.whale_wallets:
                # Не логируем - это засоряет логи и не несёт пользы
                return

            # Found whale transaction
            whale_info = self.whale_wallets[fee_payer]

            meta = tx.get("meta", {})

            # Получаем block_time для проверки свежести
            block_time = tx.get("blockTime")

            # Считаем SOL
            pre = meta.get("preBalances", [])
            post = meta.get("postBalances", [])
            sol_spent = (pre[0] - post[0]) / 1e9 if pre and post else 0

            # Ищем токен
            token_mint = None
            for bal in meta.get("postTokenBalances", []):
                if bal.get("owner") == fee_payer:
                    token_mint = bal.get("mint")
                    break

            # ФИЛЬТР: Только логируем если сумма >= min_buy_amount
            if sol_spent >= self.min_buy_amount and token_mint:
                logger.warning(
                    f"[WHALE] TX detected: {whale_info.get('label', 'whale')} | "
                    f"wallet: {fee_payer} | platform: {platform}"
                )
                logger.warning(
                    f"[WHALE] Whale spent: {sol_spent:.4f} SOL (min: {self.min_buy_amount})"
                )
                logger.warning(f"[WHALE] Token mint: {token_mint[:16]}...")
                logger.warning(
                    f"[WHALE] Buy qualifies: {sol_spent:.4f} SOL >= {self.min_buy_amount} SOL | "
                    f"token: {token_mint} | platform: {platform}"
                )
                await self._emit_whale_buy(
                    wallet=fee_payer,
                    token_mint=token_mint,
                    sol_spent=sol_spent,
                    signature=signature,
                    whale_label=whale_info.get("label", "whale"),
                    block_time=block_time,
                    platform=platform,
                )
            else:
                # Тихо пропускаем микро-транзакции (только DEBUG лог)
                if sol_spent < self.min_buy_amount:
                    logger.debug(
                        f"[WHALE] Skip small TX: {sol_spent:.4f} < {self.min_buy_amount} SOL"
                    )
                if not token_mint:
                    logger.debug("[WHALE] Skip TX without token mint")

        except Exception as e:
            logger.warning(f"[WHALE] Error processing RPC tx: {e}")

    async def _emit_whale_buy(
        self,
        wallet: str,
        token_mint: str,
        sol_spent: float,
        signature: str,
        whale_label: str,
        block_time: int | None = None,
        platform: str = "pump_fun",
    ):
        """Отправить сигнал о покупке кита.

        ВАЖНО: Проверяем что покупка СВЕЖАЯ (в пределах time_window).
        Старые покупки игнорируются!

        ANTI-DUPLICATE: Каждый токен эмитится только один раз.
        Если кит купил токен несколько раз или несколько китов купили
        один токен - отправляем сигнал только для первой покупки.

        Args:
            wallet: Кошелёк кита
            token_mint: Адрес токена
            sol_spent: Сколько SOL потрачено
            signature: Сигнатура транзакции
            whale_label: Метка кита
            block_time: Unix timestamp транзакции
            platform: Платформа ("pump_fun" или "lets_bonk")
        """
        # STABLECOIN FILTER: Skip stablecoins (USDC, USDT, etc.)
        if token_mint in self.stablecoin_filter:
            logger.info(
                f"[WHALE] SKIP STABLECOIN: {whale_label} bought {token_mint[:8]}... "
                f"but token is in stablecoin filter"
            )
            return

        # ANTI-DUPLICATE: Check if token already emitted
        if token_mint in self._emitted_tokens:
            logger.info(
                f"[WHALE] SKIP DUPLICATE: {whale_label} bought {token_mint[:8]}... "
                f"but signal already emitted for this token"
            )
            return

        now = time.time()
        age_seconds = 0.0

        # Проверяем время покупки
        if block_time:
            age_seconds = now - block_time

            # ГЛАВНЫЙ ФИЛЬТР: Пропускаем старые покупки!
            if age_seconds > self.time_window_seconds:
                logger.info(
                    f"[WHALE] SKIP OLD: {whale_label} ({wallet[:8]}...) "
                    f"bought {token_mint[:8]}... {age_seconds:.0f}s ago "
                    f"(outside {self.time_window_minutes} min window)"
                )
                return

            logger.info(
                f"[WHALE] FRESH BUY: {whale_label} bought {age_seconds:.1f}s ago "
                f"(within {self.time_window_minutes} min window OK)"
            )
        else:
            # Если нет block_time - это real-time событие, копируем
            logger.info(
                f"[WHALE] REAL-TIME BUY: {whale_label} (no block_time, assuming fresh)"
            )

        # Mark token as emitted BEFORE sending signal
        self._emitted_tokens.add(token_mint)

        # Cleanup old emitted tokens (keep last 500)
        if len(self._emitted_tokens) > 500:
            # Convert to list, keep last 400
            tokens_list = list(self._emitted_tokens)
            self._emitted_tokens = set(tokens_list[-400:])

        whale_buy = WhaleBuy(
            whale_wallet=wallet,
            token_mint=token_mint,
            token_symbol="TOKEN",
            amount_sol=sol_spent,
            timestamp=datetime.utcnow(),
            tx_signature=signature,
            whale_label=whale_label,
            block_time=block_time,
            age_seconds=age_seconds,
            platform=platform,
        )

        # Clean readable log format without emoji
        logger.warning("=" * 70)
        logger.warning("[WHALE BUY DETECTED]")
        logger.warning(f"  WHALE:     {whale_label}")
        logger.warning(f"  WALLET:    {wallet}")
        logger.warning(f"  TOKEN:     {token_mint}")
        logger.warning(f"  AMOUNT:    {sol_spent:.4f} SOL")
        logger.warning(f"  PLATFORM:  {platform}")
        logger.warning(f"  AGE:       {age_seconds:.1f}s ago")
        logger.warning(f"  TX:        {signature}")
        logger.warning("=" * 70)

        self._copy_stats["signals"] += 1
        if self.on_whale_buy:
            await self.on_whale_buy(whale_buy)

    async def check_wallet_activity(self, wallet: str) -> list[WhaleBuy]:
        """Проверить активность кошелька (для ручной проверки)."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        buys = []
        if self.helius_api_key:
            url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
            params = {"api-key": self.helius_api_key, "limit": 10, "type": "SWAP"}

            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 200:
                        for tx in await resp.json():
                            if tx.get("type") == "SWAP":
                                buys.append(
                                    WhaleBuy(
                                        whale_wallet=wallet,
                                        token_mint=tx.get("tokenTransfers", [{}])[
                                            0
                                        ].get("mint", ""),
                                        token_symbol="UNKNOWN",
                                        amount_sol=0,
                                        timestamp=datetime.utcnow(),
                                        tx_signature=tx.get("signature", ""),
                                    )
                                )
            except Exception as e:
                logger.exception(f"Error checking wallet: {e}")

        return buys

    def get_tracked_wallets(self) -> list[str]:
        """Получить список отслеживаемых кошельков."""
        return list(self.whale_wallets.keys())
