"""
Whale Tracker - отслеживает транзакции китов в РЕАЛЬНОМ ВРЕМЕНИ.
Когда кит покупает токен - отправляет сигнал на копирование.

ВАЖНО: Копируем ТОЛЬКО свежие покупки (в пределах time_window_minutes).
Старые/исторические покупки игнорируются!

Поддерживает ВСЕ платформы:
- pump.fun (6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P)
- letsbonk/Raydium LaunchLab (LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj)
- PumpSwap AMM (PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP) - migrated tokens
- Raydium AMM (675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8) - migrated tokens
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import aiohttp
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

# Program IDs for all supported platforms
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LETS_BONK_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
# Migrated tokens trade on these DEXes
PUMPSWAP_PROGRAM = "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP"
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# All programs to monitor (bonding curves + DEXes for migrated)
ALL_PROGRAMS = [PUMP_FUN_PROGRAM, LETS_BONK_PROGRAM, PUMPSWAP_PROGRAM, RAYDIUM_AMM_PROGRAM]

# Program ID to platform mapping
PROGRAM_TO_PLATFORM: dict[str, str] = {
    PUMP_FUN_PROGRAM: "pump_fun",
    LETS_BONK_PROGRAM: "lets_bonk",
    PUMPSWAP_PROGRAM: "pumpswap",
    RAYDIUM_AMM_PROGRAM: "raydium",
}

# Public RPC endpoints for fallback only (free, no rate limit)
PUBLIC_RPC_FALLBACK = [
    "https://rpc.ankr.com/solana",
    "https://api.mainnet-beta.solana.com",
]

# Helius RPC - PRIMARY endpoint (fast, reliable)
HELIUS_RPC_ENDPOINT = "https://mainnet.helius-rpc.com/?api-key=a53d15c7-d5f5-40fc-81fe-49942d03d4f3"

# Helius Enhanced API - парсит транзакции автоматически (лучше чем getTransaction!)
HELIUS_PARSE_TX_URL = "https://api-mainnet.helius-rpc.com/v0/transactions/?api-key=a53d15c7-d5f5-40fc-81fe-49942d03d4f3"


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
    ):
        self.wallets_file = wallets_file
        self.min_buy_amount = min_buy_amount
        self.helius_api_key = helius_api_key
        self.rpc_endpoint = rpc_endpoint
        self.wss_endpoint = wss_endpoint
        self.time_window_minutes = time_window_minutes
        self.time_window_seconds = time_window_minutes * 60
        self.target_platform = platform  # None = все платформы, иначе только указанная
        
        self.whale_wallets: dict[str, dict] = {}  # wallet -> info
        self.on_whale_buy: Callable | None = None
        self.running = False
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._processed_txs: set[str] = set()
        
        # RPC optimization: TX cache with extended TTL for quota saving
        self._tx_cache: dict[str, tuple[dict, float]] = {}  # sig -> (result, timestamp)
        self._cache_ttl = 60.0  # 60 seconds TTL (extended for quota saving)
        self._cache_max_size = 500  # LRU cache size
        
        # Helius rate limiting - STRICT for quota conservation
        # Budget: 380k credits / 14 days = 27k/day = 1125/hour
        # Whale tracker budget: ~30% = 337/hour = 5.6/min = 1 per 10 seconds
        self._last_helius_call = 0.0
        self._helius_rate_limit = 10.0  # 1 request per 10 seconds (strict!)
        
        # TX queue for rate-limited processing
        self._pending_txs: list[tuple[str, str]] = []  # (signature, platform)
        self._max_pending = 50  # Don't queue more than 50 TXs
        
        # Performance metrics
        self._metrics = {
            "helius_calls": 0,
            "helius_success": 0,
            "public_fallback_calls": 0,
            "cache_hits": 0,
            "timeouts": 0,
            "requests_today": 0,
        }
        
        self._load_wallets()
        
        platform_info = f"platform={platform}" if platform else "ALL platforms"
        logger.info(
            f"WhaleTracker initialized: {len(self.whale_wallets)} wallets, "
            f"min_buy={min_buy_amount} SOL, time_window={time_window_minutes} min, {platform_info}"
        )
        logger.info(f"[WHALE] RPC: Helius PRIMARY (hardcoded), fallback: Ankr, Solana mainnet")

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
            
            logger.warning(f"[WHALE] Loaded {len(self.whale_wallets)} whale wallets successfully")
            
        except json.JSONDecodeError as e:
            logger.error(f"[WHALE] JSON parse error in {self.wallets_file}: {e}")
        except Exception as e:
            logger.exception(f"[WHALE] Error loading wallets: {e}")
        except Exception as e:
            logger.exception(f"Failed to load wallets: {e}")

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
        
        ВАЖНО: Helius WSS даёт 429 rate limit на logsSubscribe!
        Используем публичный Solana WSS для подписок.
        Helius оставляем только для HTTP запросов (getTransaction и т.д.)
        """
        # Публичный Solana WSS - стабильный для logsSubscribe
        # НЕ используем Helius WSS - даёт 429!
        public_wss = "wss://api.mainnet-beta.solana.com"
        
        # Если передан wss_endpoint - используем его (может быть приватный RPC)
        if self.wss_endpoint and "helius" not in self.wss_endpoint.lower():
            logger.warning(f"[WHALE] WSS ENDPOINT: Using provided: {self.wss_endpoint[:50]}...")
            return self.wss_endpoint
        
        # Fallback на публичный Solana WSS
        logger.warning(f"[WHALE] WSS ENDPOINT: Using public Solana (Helius gives 429)")
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
        
        logger.warning(f"[WHALE] WHALE TRACKER STARTED - tracking {len(self.whale_wallets)} wallets")
        logger.warning(f"[WHALE] Min buy: {self.min_buy_amount} SOL, Time window: {self.time_window_minutes} min")
        logger.warning(f"[WHALE] Monitoring: {platform_names}")
        logger.info(f"[WHALE] WSS endpoint: {wss_url[:50]}...")
        
        # Подписываемся на выбранные программы
        await self._track_programs(wss_url, programs_to_track)

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
        """Отслеживание через подписку на логи указанных программ.
        
        Args:
            wss_url: WebSocket URL для подключения
            programs: Список program ID для подписки
        """
        consecutive_errors = 0
        max_consecutive_errors = 10
        
        while self.running:
            try:
                logger.info(f"[WHALE] Connecting to WSS for whale tracking...")
                async with self._session.ws_connect(
                    wss_url,
                    heartbeat=30,
                    timeout=aiohttp.ClientTimeout(total=60, sock_connect=30),
                    receive_timeout=120,  # 2 min timeout for receiving messages
                ) as ws:
                    self._ws = ws
                    consecutive_errors = 0  # Reset on successful connect
                    
                    # Подписываемся на каждую программу
                    for i, program in enumerate(programs):
                        subscribe_msg = {
                            "jsonrpc": "2.0",
                            "id": i + 1,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [program]},
                                {"commitment": "processed"}
                            ]
                        }
                        await ws.send_json(subscribe_msg)
                        platform_name = PROGRAM_TO_PLATFORM.get(program, program[:8])
                        logger.warning(f"[WHALE] SUBSCRIBED to {platform_name} logs")
                    
                    platform_info = self.target_platform or "ALL platforms"
                    logger.warning(f"[WHALE] Filtering {len(self.whale_wallets)} whale wallets on {platform_info}")
                    
                    # Message processing loop with timeout protection
                    last_message_time = time.time()
                    while self.running:
                        try:
                            # Wait for message with timeout
                            msg = await asyncio.wait_for(
                                ws.receive(),
                                timeout=120  # 2 min timeout - if no message, reconnect
                            )
                            last_message_time = time.time()
                            
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                    # Process with timeout to prevent hanging
                                    await asyncio.wait_for(
                                        self._handle_log(data),
                                        timeout=10  # 10s max for processing single message
                                    )
                                except asyncio.TimeoutError:
                                    logger.warning("[WHALE] Message processing timeout (10s) - skipping message")
                                    continue
                                except json.JSONDecodeError:
                                    pass
                            elif msg.type == aiohttp.WSMsgType.PING:
                                await ws.pong(msg.data)
                            elif msg.type == aiohttp.WSMsgType.PONG:
                                pass  # Heartbeat response
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                                logger.warning(f"[WHALE] WebSocket closed (type={msg.type}), reconnecting...")
                                break
                                
                        except asyncio.TimeoutError:
                            # No message for 2 minutes - connection might be dead
                            idle_time = time.time() - last_message_time
                            logger.warning(f"[WHALE] No messages for {idle_time:.0f}s - reconnecting...")
                            break
                        except asyncio.CancelledError:
                            logger.info("[WHALE] Whale tracker cancelled")
                            raise
                    
                    self._ws = None
                    
            except asyncio.CancelledError:
                logger.info("[WHALE] Whale tracker task cancelled")
                raise
            except asyncio.TimeoutError as e:
                consecutive_errors += 1
                logger.warning(f"[WHALE] WebSocket timeout: {e} (error {consecutive_errors}/{max_consecutive_errors})")
            except aiohttp.ClientError as e:
                consecutive_errors += 1
                logger.warning(f"[WHALE] WebSocket client error: {e} (error {consecutive_errors}/{max_consecutive_errors})")
            except Exception as e:
                consecutive_errors += 1
                logger.exception(f"[WHALE] Error in log subscription: {e} (error {consecutive_errors}/{max_consecutive_errors})")
            
            if self.running:
                # Exponential backoff with max 30s
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(f"[WHALE] Too many consecutive errors ({consecutive_errors}), waiting 30s...")
                    await asyncio.sleep(30)
                    consecutive_errors = 0  # Reset after long wait
                else:
                    backoff = min(3 * (2 ** consecutive_errors), 30)
                    logger.info(f"[WHALE] Reconnecting in {backoff}s...")
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
        if data.get("method") != "logsNotification":
            return
        
        try:
            params = data.get("params", {})
            result = params.get("result", {})
            value = result.get("value", {})
            
            signature = value.get("signature", "")
            logs = value.get("logs", [])
            err = value.get("err")
            
            if err or not signature:
                return
            
            if signature in self._processed_txs:
                return
            
            # Определяем платформу по логам
            platform = self._detect_platform_from_logs(logs)
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
                if PUMPSWAP_PROGRAM in log and ("swap" in log.lower() or "buy" in log.lower()):
                    is_buy = True
                    break
            
            if not is_buy:
                return
            
            # Логируем что нашли Buy транзакцию
            logger.info(f"[WHALE] BUY TX detected on {platform}: {signature[:16]}...")
            
            # Получаем детали транзакции и проверяем кошелёк
            await self._check_if_whale_tx(signature, platform)
            
        except Exception as e:
            logger.warning(f"[WHALE] Error handling log: {e}")

    async def _check_if_whale_tx(self, signature: str, platform: str = "pump_fun"):
        """Проверить, является ли транзакция покупкой кита.
        
        Использует Alchemy RPC (ALCHEMY_RPC_ENDPOINT) для getTransaction.
        Публичный Solana RPC как fallback.
        
        Args:
            signature: Сигнатура транзакции
            platform: Платформа ("pump_fun" или "lets_bonk")
        """
        import os
        
        # Mark as processed to avoid duplicates
        self._processed_txs.add(signature)
        if len(self._processed_txs) > 1000:
            # Keep only last 500
            self._processed_txs = set(list(self._processed_txs)[-500:])
        
        # Check cache first
        if signature in self._tx_cache:
            cached, ts = self._tx_cache[signature]
            if time.time() - ts < self._cache_ttl:
                self._metrics["cache_hits"] += 1
                if cached:
                    await self._process_rpc_tx(cached, signature, platform)
                return
        
        # Rate limit check - 1 request per 2 seconds to conserve quota
        now = time.time()
        if now - self._last_helius_call < 2.0:
            return
        self._last_helius_call = now
        
        # Try Alchemy RPC with retry (TX needs time to confirm)
        alchemy_endpoint = os.getenv("ALCHEMY_RPC_ENDPOINT")
        tx = None
        
        if alchemy_endpoint:
            # Retry up to 3 times with 1s delay - TX needs to confirm
            for attempt in range(3):
                if attempt > 0:
                    await asyncio.sleep(1.0)
                tx = await self._get_tx_from_endpoint(signature, alchemy_endpoint, timeout=5.0)
                if tx:
                    logger.info(f"[WHALE] Got TX from Alchemy (attempt {attempt + 1})")
                    self._cache_tx(signature, tx)
                    await self._process_rpc_tx(tx, signature, platform)
                    return
        else:
            logger.warning(f"[WHALE] ALCHEMY_RPC_ENDPOINT not set!")
        
        # TX not confirmed yet - this is normal for very fresh TXs
        logger.debug(f"[WHALE] TX {signature[:16]}... not confirmed yet")

    def _cache_tx(self, signature: str, tx: dict):
        """Cache TX result with LRU eviction."""
        self._tx_cache[signature] = (tx, time.time())
        
        # LRU eviction if over size limit
        if len(self._tx_cache) > self._cache_max_size:
            oldest = min(self._tx_cache.keys(), key=lambda k: self._tx_cache[k][1])
            del self._tx_cache[oldest]

    async def _get_tx_helius(self, signature: str) -> dict | None:
        """Получить транзакцию через Helius Enhanced API (парсит автоматически!)."""
        try:
            async with self._session.post(
                HELIUS_PARSE_TX_URL,
                json={"transactions": [signature]},
                timeout=aiohttp.ClientTimeout(total=5),
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return data[0]
                    return None
                elif resp.status == 429:
                    logger.warning("[WHALE] Helius Enhanced API rate limited (429)")
                    return None
                else:
                    logger.debug(f"[WHALE] Helius Enhanced API HTTP {resp.status}")
                    return None
        except asyncio.TimeoutError:
            logger.warning("[WHALE] Helius Enhanced API timeout")
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
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        }
        
        endpoint_name = "Helius" if "helius" in endpoint.lower() else endpoint[:30]
        
        try:
            async with self._session.post(
                endpoint, json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"Content-Type": "application/json"}
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
        except asyncio.TimeoutError:
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

    async def _process_rpc_tx(self, tx: dict, signature: str, platform: str = "pump_fun"):
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
            fee_payer = first_key.get("pubkey", "") if isinstance(first_key, dict) else str(first_key)
            
            logger.info(f"[WHALE] TX {signature[:16]}... fee_payer: {fee_payer[:8]}...")
            
            if fee_payer not in self.whale_wallets:
                logger.info(f"[WHALE] Fee payer {fee_payer} NOT in whale list")
                return
            
            # [WHALE] НАШЛИ КИТА!
            whale_info = self.whale_wallets[fee_payer]
            logger.warning(f"[WHALE] WHALE TX DETECTED: {whale_info.get('label', 'whale')} ({fee_payer[:8]}...) on {platform}")
            
            meta = tx.get("meta", {})
            
            # Получаем block_time для проверки свежести
            block_time = tx.get("blockTime")
            
            # Считаем SOL
            pre = meta.get("preBalances", [])
            post = meta.get("postBalances", [])
            sol_spent = (pre[0] - post[0]) / 1e9 if pre and post else 0
            
            logger.warning(f"[WHALE] Whale spent: {sol_spent:.4f} SOL (min: {self.min_buy_amount})")
            
            # Ищем токен
            token_mint = None
            for bal in meta.get("postTokenBalances", []):
                if bal.get("owner") == fee_payer:
                    token_mint = bal.get("mint")
                    break
            
            if token_mint:
                logger.warning(f"[WHALE] Token mint: {token_mint[:16]}...")
            else:
                logger.warning(f"[WHALE] No token mint found in postTokenBalances")
            
            if sol_spent >= self.min_buy_amount and token_mint:
                logger.warning(f"[WHALE] WHALE BUY QUALIFIES: {sol_spent:.2f} SOL >= {self.min_buy_amount} SOL on {platform}")
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
                if sol_spent < self.min_buy_amount:
                    logger.info(f"[WHALE] Amount too small: {sol_spent:.4f} < {self.min_buy_amount}")
                if not token_mint:
                    logger.info(f"[WHALE] No token mint found")
                
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
        
        Args:
            wallet: Кошелёк кита
            token_mint: Адрес токена
            sol_spent: Сколько SOL потрачено
            signature: Сигнатура транзакции
            whale_label: Метка кита
            block_time: Unix timestamp транзакции
            platform: Платформа ("pump_fun" или "lets_bonk")
        """
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
            logger.info(f"[WHALE] REAL-TIME BUY: {whale_label} (no block_time, assuming fresh)")
        
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
        
        logger.warning(
            f"[WHALE] WHALE BUY: {whale_label} ({wallet[:8]}...) "
            f"bought {token_mint[:8]}... for {sol_spent:.2f} SOL "
            f"on {platform} ({age_seconds:.1f}s ago)"
        )
        
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
                                buys.append(WhaleBuy(
                                    whale_wallet=wallet,
                                    token_mint=tx.get("tokenTransfers", [{}])[0].get("mint", ""),
                                    token_symbol="UNKNOWN",
                                    amount_sol=0,
                                    timestamp=datetime.utcnow(),
                                    tx_signature=tx.get("signature", ""),
                                ))
            except Exception as e:
                logger.exception(f"Error checking wallet: {e}")
        
        return buys

    def get_tracked_wallets(self) -> list[str]:
        """Получить список отслеживаемых кошельков."""
        return list(self.whale_wallets.keys())
