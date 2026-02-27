"""
SpyDefi + KOLscope Telegram Listener — integrated into UniversalTrader.
Listens to multiple Telegram channels, catches multiplier posts (x2+),
extracts contract from entity URL, emits WhaleBuy signal.

Supported channels:
  - @spydefi: "Achievement Unlocked: x2!" format
  - @KOLscope: "MULTIPLIER DETECTED: 2x+" and "DIP MODE: 2x+" format
"""
import asyncio
import re
import os
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)

BOT_ROOT = Path("/opt/pumpfun-bonkfun-bot")

# === Contract extraction from entity URLs ===
# SpyDefi: t.me/spydefi_bot?start=<ADDRESS>
# KOLscope: t.me/KOLscopeBot?start=<ADDRESS>  (WARNING: address may be lowercased!)
BOT_URL_RE = re.compile(
    r"t\.me/(?:[Ss]py[Dd]efi_bot|KOLscope[Bb]ot)\?start=([1-9A-HJ-NP-Za-km-z]{32,50})"
)

# Solana address in message text (for case-sensitive recovery)
SOL_ADDR_TEXT_RE = re.compile(r"[\U0001f4c4]\s*([1-9A-HJ-NP-Za-km-z]{32,50})")
# Generic: any base58 32-50 chars ending with "pump"
PUMP_ADDR_RE = re.compile(r"([1-9A-HJ-NP-Za-km-z]{32,50}pump)")

# === CallAnalyserSol patterns ===
# Format: "☘️(First Call) Score: 32/100 | $TICKER got first call at"
# Format: "☘️(Update) Score: 62/100 | $TICKER +1 call at"
CALLANALYSER_TYPE_RE = re.compile(r'\((?:First Call|Update|Total Call)\)')
CALLANALYSER_SCORE_RE = re.compile(r'Score:\s*(\d+)/100')
CALLANALYSER_TICKER_RE = re.compile(r'\$(\w[\w.]*)')
CALLANALYSER_CPW_RE = re.compile(r'CPW:\s*(\d+(?:\.\d+)?)/1000')
CALLANALYSER_TOTAL_CALLS_RE = re.compile(r'Total calls:\s*(\d+)')
CALLANALYSER_MCAP_RE = re.compile(r'MCap:\s*\$(\d+(?:\.\d+)?)([KMB]?)')
CALLANALYSER_LIQUID_RE = re.compile(r'Liquid:\s*\$(\d+(?:\.\d+)?)([KMB]?)')



class SpyDefiListener:
    """Listens to SpyDefi + KOLscope Telegram channels and emits buy signals."""

    def __init__(
        self,
        min_multiplier: int = 2,
        max_multiplier: int = 2,
        max_mcap: float = 0,
        min_mcap: float = 0,
        channels: list[str] | None = None,
        api_id: int = 0,
        api_hash: str = "",
        session_file: str = "",
        # Legacy single-channel param (backwards compat)
        channel: str = "",
        kolscope_skip_dip: bool = False,
        callanalyser_min_cpw: float = 490,
        callanalyser_mcap_max: float = 3_000_000,
        callanalyser_min_calls: int = 2,
        callanalyser_max_calls: int = 10,
    ):
        self.min_multiplier = min_multiplier
        self.max_multiplier = max_multiplier
        self.max_mcap = max_mcap
        self.min_mcap = min_mcap
        
        # Multi-channel support
        if channels:
            self.channels = channels
        elif channel:
            self.channels = [channel]
        else:
            self.channels = ["spydefi", "KOLscope"]

        self.kolscope_skip_dip = kolscope_skip_dip
        self.callanalyser_min_cpw = callanalyser_min_cpw
        self.callanalyser_mcap_max = callanalyser_mcap_max
        self.callanalyser_min_calls = callanalyser_min_calls
        self.callanalyser_max_calls = callanalyser_max_calls
        
        # FIX S47: Read from .env if not provided in YAML
        self.api_id = api_id or int(os.environ.get("TELEGRAM_API_ID", 0))
        self.api_hash = api_hash or os.environ.get("TELEGRAM_API_HASH", "")
        self.session_file = session_file or str(BOT_ROOT / "data" / "tg_session")

        self._callback: Callable | None = None
        self._client = None
        self._seen: set[str] = set()
        self._seen_file = BOT_ROOT / "data" / "spydefi_seen.json"
        self._running = False

    def set_callback(self, callback: Callable):
        self._callback = callback

    def _load_seen(self):
        try:
            if self._seen_file.exists():
                data = json.loads(self._seen_file.read_text())
                # FIX S47-7: Support timestamped format + migrate old format
                if "contracts_ts" in data and data["contracts_ts"]:
                    self._seen_ts = data["contracts_ts"]
                    # Cleanup entries older than 1h on load
                    _now = time.time()
                    _old_len = len(self._seen_ts)
                    self._seen_ts = {k: v for k, v in self._seen_ts.items() if _now - v < 3600}
                    if _old_len != len(self._seen_ts):
                        logger.info(f"[SPYDEFI] Seen cleanup on load: {_old_len} -> {len(self._seen_ts)} (removed {_old_len - len(self._seen_ts)} stale)")
                else:
                    # Old format: list — migrate with current timestamp
                    contracts = data.get("contracts", [])
                    self._seen_ts = {c: time.time() for c in contracts}
                    logger.warning(f"[SPYDEFI] Migrated {len(self._seen_ts)} seen contracts to timestamped format")
                    self._save_seen()  # FIX S47-7: persist migration immediately
                self._seen = set(self._seen_ts.keys())
                logger.info(f"[SPYDEFI] Loaded {len(self._seen)} seen contracts")
        except Exception as e:
            logger.error(f"[SPYDEFI] Failed to load seen: {e}")

    def _save_seen(self):
        try:
            # FIX S47-7: Cleanup stale entries before save (max age 1h)
            _now = time.time()
            _old = len(self._seen_ts)
            self._seen_ts = {k: v for k, v in self._seen_ts.items() if _now - v < 3600}
            self._seen = set(self._seen_ts.keys())
            if _old != len(self._seen_ts):
                logger.info(f"[SPYDEFI] Seen cleanup: {_old} -> {len(self._seen_ts)}")
            self._seen_file.parent.mkdir(exist_ok=True)
            tmp = str(self._seen_file) + ".tmp"
            Path(tmp).write_text(json.dumps({
                "contracts_ts": dict(list(self._seen_ts.items())[-500:]),
                "contracts": list(self._seen)[-500:],
                "updated": datetime.now().isoformat()
            }))
            os.replace(tmp, str(self._seen_file))
        except Exception as e:
            logger.error(f"[SPYDEFI] Failed to save seen: {e}")

    @staticmethod
    def _extract_contract(message) -> str | None:
        """Extract Solana contract from message entity URLs + text.
        
        KOLscope lowercases addresses in URLs, so we prefer the address
        from message text (📄 line) if available, falling back to URL.
        """
        text = message.raw_text or ""
        contract_from_url = None
        contract_from_text = None
        
        # 1. Try entity URLs (works for both SpyDefi and KOLscope)
        if message.entities:
            for entity in message.entities:
                url = getattr(entity, "url", None)
                if not url:
                    continue
                # Match both spydefi_bot and KOLscopeBot
                if "?start=" in url and ("spy" in url.lower() or "kolscope" in url.lower()):
                    # Extract address after ?start=
                    start_idx = url.index("?start=") + 7
                    addr = url[start_idx:]
                    # Remove any trailing params
                    if "&" in addr:
                        addr = addr[:addr.index("&")]
                    if len(addr) >= 32:
                        contract_from_url = addr
                        break
        
        # 2. Try 📄 line in text (KOLscope CALL ALERT has it, case-sensitive!)
        paper_match = re.search(r"\U0001f4c4\s*([1-9A-HJ-NP-Za-km-z]{32,50})", text)
        if paper_match:
            contract_from_text = paper_match.group(1)
        
        # 3. Try any pump address in text
        if not contract_from_text:
            pump_match = PUMP_ADDR_RE.search(text)
            if pump_match:
                contract_from_text = pump_match.group(1)
        
        # Prefer text (case-sensitive) over URL (may be lowercased)
        if contract_from_text:
            return contract_from_text
        
        # URL address: KOLscope lowercases it, but pump.fun addresses 
        # ending in "pump" are case-insensitive in the suffix.
        # For non-CALL posts (MULTIPLIER/DIP), text doesn't have the address,
        # so URL is our only source. Use it as-is — the bot will handle it.
        if contract_from_url:
            return contract_from_url
        
        return None

    @staticmethod
    def _extract_contract_solearlytrending(message) -> str | None:
        """Extract contract from solearlytrending buttons/entities.
        
        Button: 🔓 CA -> soul_sniper_bot?start=15_<ADDRESS>
        Entity: geckoterminal.com/solana/tokens/<ADDRESS>?...
        """
        # 1. Try buttons first (most reliable, case-sensitive)
        if message.buttons:
            for row in message.buttons:
                for btn in row:
                    url = getattr(btn.button, "url", None)
                    if url and "soul_sniper_bot?start=15_" in url:
                        # Extract: start=15_<ADDRESS>
                        idx = url.index("start=15_") + 9
                        addr = url[idx:]
                        if len(addr) >= 32:
                            return addr
        
        # 2. Try entity URLs — geckoterminal
        if message.entities:
            for entity in message.entities:
                url = getattr(entity, "url", None)
                if not url:
                    continue
                if "geckoterminal.com/solana/tokens/" in url:
                    # Extract: /tokens/<ADDRESS>?
                    import re as _re
                    m = _re.search(r"/tokens/([1-9A-HJ-NP-Za-km-z]{32,50})", url)
                    if m:
                        return m.group(1)
        
        return None


    @staticmethod
    def _extract_contract_callanalyser(message) -> str | None:
        """Extract contract from CallAnalyserSol message.

        Contract appears as a base58 address (32-50 chars) in message text.
        Entity offsets can be corrupted by invisible Unicode chars (U+200E etc),
        so we extract directly from cleaned text via regex.
        """
        text = message.raw_text or ""
        # Clean invisible unicode (LRM, RLM, ZWS, ZWNJ, etc)
        clean = re.sub(r'[\u200e\u200f\u200b\u200c\u200d\ufeff]', '', text)

        # 1. Best: standalone base58 on its own line (most posts have this)
        for line in clean.split('\n'):
            line = line.strip()
            if re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,50}$', line):
                return line

        # 2. Fallback: any base58 32-50 chars surrounded by non-alnum
        addr_match = re.search(r'(?:^|[^1-9A-HJ-NP-Za-km-z])([1-9A-HJ-NP-Za-km-z]{32,50})(?:[^1-9A-HJ-NP-Za-km-z]|$)', clean)
        if addr_match:
            return addr_match.group(1)

        # 3. Last resort: pump address pattern
        pump_match = PUMP_ADDR_RE.search(clean)
        if pump_match:
            return pump_match.group(1)

        return None

    @staticmethod
    def _parse_mcap_value(amount_str: str, suffix: str) -> float:
        """Parse MCap value like '330.2K' or '1.2M' to float."""
        val = float(amount_str)
        multipliers = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}
        return val * multipliers.get(suffix, 1)

    def _parse_signal(self, text: str) -> dict | None:
        """Parse multiplier signal from SpyDefi or KOLscope.
        
        SpyDefi format:
            Achievement Unlocked: x2! ... call on TokenName...
        
        KOLscope formats:
            MULTIPLIER DETECTED: 2x+ ... made 2x+ on TokenName.
            DIP MODE: 2x+ ... made 2x+ on TokenName from dip.
        """
        multiplier = 0
        caller = "unknown"
        token_name = "UNKNOWN"
        signal_type = ""
        channel_source = ""
        
        # --- SpyDefi ---
        if "Achievement" in text:
            channel_source = "spydefi"
            signal_type = "achievement"
            mult_m = re.search(r"x(\d+)\+?", text)
            multiplier = int(mult_m.group(1)) if mult_m else 0
            caller_m = re.search(r"@(\w+)", text)
            caller = caller_m.group(1) if caller_m else "unknown"
            name_m = re.search(r"call on\s+(.+?)(?:\.{2,}|\.\s|\n)", text)
            token_name = name_m.group(1).strip().rstrip(".") if name_m else "UNKNOWN"
        
        # --- KOLscope MULTIPLIER ---
        elif "MULTIPLIER DETECTED" in text:
            channel_source = "kolscope"
            signal_type = "multiplier"
            mult_m = re.search(r"(\d+)x\+", text)
            multiplier = int(mult_m.group(1)) if mult_m else 0
            caller_m = re.search(r"@(\w+)", text)
            caller = caller_m.group(1) if caller_m else "unknown"
            name_m = re.search(r"on\s+([A-Z][A-Za-z0-9 ]+?)(?:\.|\n|\s+from)", text)
            token_name = name_m.group(1).strip().rstrip(".") if name_m else "UNKNOWN"
        
        # --- KOLscope DIP MODE ---
        elif "DIP MODE" in text:
            if self.kolscope_skip_dip:
                logger.debug("[KOLSCOPE] DIP MODE skipped (kolscope_skip_dip=True)")
                return None
            channel_source = "kolscope"
            signal_type = "dip"
            mult_m = re.search(r"(\d+)x\+", text)
            multiplier = int(mult_m.group(1)) if mult_m else 0
            caller_m = re.search(r"@(\w+)", text)
            caller = caller_m.group(1) if caller_m else "unknown"
            name_m = re.search(r"on\s+([A-Z][A-Za-z0-9 ]+?)\s+from\s+dip", text)
            token_name = name_m.group(1).strip().rstrip(".") if name_m else "UNKNOWN"
        
        # --- SolEarlyTrending: "TOKEN is up 50%" ---
        elif "is up" in text and "%" in text:
            channel_source = "solearlytrending"
            signal_type = "pump_pct"
            # FIX S47: Simplified regex — strip all non-ASCII first, then parse
            _clean = re.sub(r"[^\x20-\x7E]", " ", text)  # strip emoji/unicode to spaces
            pct_m = re.search(r"is up (\d+)\s*%", _clean)
            if not pct_m:
                logger.info(f"[SOLEARLYTRENDING] SKIP: 'is up' found but no pct match in: {_clean[:80]}")
                return None
            pct = int(pct_m.group(1))
            # Only 44-68% range
            if pct < 44 or pct > 68:
                logger.info(f"[SOLEARLYTRENDING] SKIP: pct {pct}% outside 44-68 range")
                return None
            multiplier = pct  # Store raw percentage as "multiplier"
            # Token name: word(s) before "is up" (no emoji dependency)
            name_m = re.search(r"([A-Za-z0-9_]{2,20})\s+is up", _clean)
            token_name = name_m.group(1).strip() if name_m else "UNKNOWN"
            caller = "solearlytrending"
        
        # --- SolHouse Signal: "Nx ACHIEVED!" + Contract Address ---
        elif "ACHIEVED" in text and "Contract Address" in text:
            channel_source = "solhousesignal"
            signal_type = "achieved"
            _clean_sh = re.sub(r"[^ -~]", " ", text)
            # Parse multiplier: "2x ACHIEVED" or "3x ACHIEVED"
            mult_m = re.search(r"(\d+)x\s*ACHIEVED", _clean_sh, re.IGNORECASE)
            multiplier = int(mult_m.group(1)) if mult_m else 0
            if multiplier < 2:
                return None
            # Token name from "Token Name: XXX" or "Ticker: XXX"
            ticker_m = re.search(r"Ticker:\s*([A-Za-z0-9_ ]+)", _clean_sh)
            name_m = re.search(r"Token Name:\s*([A-Za-z0-9_ ]+)", _clean_sh)
            token_name = (ticker_m or name_m).group(1).strip() if (ticker_m or name_m) else "UNKNOWN"
            caller = "solhousesignal"
            logger.warning(
                f"[SOLHOUSE] PARSED: {multiplier}x ACHIEVED | {token_name}"
            )

        # --- CallAnalyserSol ---
        elif "Score:" in text and ("First Call" in text or "Update" in text or "Total Call" in text):
            channel_source = "callanalyser"
            # Parse type
            type_m = CALLANALYSER_TYPE_RE.search(text)
            signal_type = type_m.group(0).strip("()") if type_m else "unknown"
            # Parse Score
            score_m = CALLANALYSER_SCORE_RE.search(text)
            score = int(score_m.group(1)) if score_m else 0
            # Parse ticker
            ticker_m = CALLANALYSER_TICKER_RE.search(text)
            token_name = ticker_m.group(1) if ticker_m else "UNKNOWN"
            # Parse CPW (the key filter!)
            cpw_m = CALLANALYSER_CPW_RE.search(text)
            cpw = float(cpw_m.group(1)) if cpw_m else 0
            # Parse total calls
            calls_m = CALLANALYSER_TOTAL_CALLS_RE.search(text)
            total_calls = int(calls_m.group(1)) if calls_m else 1
            # Parse MCap
            mcap_m = CALLANALYSER_MCAP_RE.search(text)
            mcap_from_post = self._parse_mcap_value(mcap_m.group(1), mcap_m.group(2)) if mcap_m else 0
            # Parse Liquid
            liquid_m = CALLANALYSER_LIQUID_RE.search(text)
            liquid = self._parse_mcap_value(liquid_m.group(1), liquid_m.group(2)) if liquid_m else 0
            # Parse caller name (between "Caller: " and " | CPW")
            caller_m = re.search(r'Caller:\s*(.+?)\s*\|', text)
            caller = caller_m.group(1).strip() if caller_m else "unknown"

            # === FILTERS ===
            if cpw < self.callanalyser_min_cpw:
                logger.info(f"[CALLANALYSER] SKIP {token_name}: CPW {cpw:.0f} < {self.callanalyser_min_cpw:.0f}")
                return None
            if mcap_from_post > 0 and mcap_from_post > self.callanalyser_mcap_max:
                logger.info(f"[CALLANALYSER] SKIP {token_name}: MCap ${mcap_from_post:,.0f} > ${self.callanalyser_mcap_max:,.0f}")
                return None
            if total_calls < self.callanalyser_min_calls:
                logger.info(f"[CALLANALYSER] SKIP {token_name}: calls {total_calls} < {self.callanalyser_min_calls}")
                return None
            if total_calls > self.callanalyser_max_calls:
                logger.info(f"[CALLANALYSER] SKIP {token_name}: calls {total_calls} > {self.callanalyser_max_calls}")
                return None

            logger.warning(
                f"[CALLANALYSER] PARSED: {signal_type} | Score {score}/100 | "
                f"${token_name} | CPW {cpw:.0f}/1000 | calls={total_calls} | "
                f"MCap=${mcap_from_post:,.0f} | Liquid=${liquid:,.0f} | by {caller}"
            )

            multiplier = int(cpw)  # Store CPW as "multiplier" for WhaleBuy compat
            # signal_type already set above (e.g. "First Call", "Update")

        else:
            return None
        
        if multiplier == 0:
            return None
        
        return {
            "multiplier": multiplier,
            "caller": caller,
            "token_name": token_name,
            "signal_type": signal_type,
            "channel": channel_source,
        }

    async def _get_token_info_dex(self, contract: str):
        """Get token info from DexScreener. Returns (symbol, mcap, correct_address)."""
        try:
            url = f"https://api.dexscreener.com/tokens/v1/solana/{contract}"
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return "", 0, ""
                    pairs = await resp.json()
                    if pairs and isinstance(pairs, list):
                        for p in pairs:
                            if p.get("chainId") == "solana":
                                mcap = p.get("marketCap") or p.get("fdv") or 0
                                symbol = p.get("baseToken", {}).get("symbol", "")
                                # DexScreener returns correct case-sensitive address
                                correct_addr = p.get("baseToken", {}).get("address", "")
                                return symbol, mcap, correct_addr
        except Exception:
            pass
        return "", 0, ""

    async def _on_message(self, event):
        """Handle new message from any tracked channel."""
        text = event.raw_text or ""

        # === DEBUG S47: Log ALL incoming messages from tracked channels ===
        _ch_name = ""
        try:
            _ch_name = getattr(event.chat, "username", "") or getattr(event.chat, "title", "") or ""
        except Exception:
            pass
        if text and len(text) > 5:
            logger.info(f"[SPYDEFI RAW] ch={_ch_name} | {text[:120].replace(chr(10), ' ')}")
        # === END DEBUG S47 ===

        signal = self._parse_signal(text)
        if not signal:
            return

        mult = signal["multiplier"]
        src = signal["channel"].upper()

        # --- Filter: multiplier ---
        # solearlytrending uses raw percentage (50-59), not x multiplier
        # callanalyser uses CPW as multiplier — already filtered in _parse_signal
        if signal["channel"] in ("solearlytrending", "callanalyser", "solhousesignal"):
            # Already filtered in _parse_signal
            pass
        elif mult < self.min_multiplier or mult > self.max_multiplier:
            logger.debug(
                f"[{src}] Skip x{mult} (want x{self.min_multiplier}-x{self.max_multiplier})"
            )
            return

        # --- Extract contract ---
        if signal["channel"] == "solearlytrending":
            contract = self._extract_contract_solearlytrending(event.message)
        elif signal["channel"] == "callanalyser":
            contract = self._extract_contract_callanalyser(event.message)
        elif signal["channel"] == "solhousesignal":
            # Contract is in plain text after "Contract Address:"
            _ca_m = re.search(r"([1-9A-HJ-NP-Za-km-z]{32,50})", text.split("Contract Address")[-1] if "Contract Address" in text else "")
            contract = _ca_m.group(1) if _ca_m else None
        else:
            contract = self._extract_contract(event.message)
        if not contract:
            logger.info(f"[{src}] No contract in x{mult} {signal['token_name']}")
            return

        # --- Dedup ---
        if contract in self._seen:
            # Also check lowercase variant (KOLscope may lowercase)
            logger.debug(f"[{src}] Dedup: {contract[:16]}...")
            return
        
        # Check lowercase dedup (KOLscope sends lowercase URLs)
        contract_lower = contract.lower()
        for seen in self._seen:
            if seen.lower() == contract_lower:
                logger.debug(f"[{src}] Dedup (case-insensitive): {contract[:16]}...")
                return
        
        self._seen.add(contract)
        self._seen_ts[contract] = time.time()  # FIX S47-7
        self._save_seen()

        logger.warning(
            f"[{src}] {signal['signal_type'].upper()} x{mult} | "
            f"{signal['token_name']} | {contract} | by @{signal['caller']}"
        )

        # --- Get symbol + fix lowercase address (KOLscope sends lowercase URLs) ---
        symbol = signal["token_name"][:10].replace(" ", "")
        mcap = 0
        try:
            _sym, mcap, correct_addr = await self._get_token_info_dex(contract)
            if _sym:
                symbol = _sym
            # Fix lowercase address from KOLscope URL
            if correct_addr and correct_addr != contract:
                logger.warning(
                    f"[{src}] Address fix: {contract[:20]}... -> {correct_addr[:20]}..."
                )
                contract = correct_addr
        except Exception:
            pass

        # --- Mcap: log only ---
        if mcap > 0:
            logger.info(f"[{src}] {symbol} mcap=${mcap:,.0f}")

        # --- Emit WhaleBuy signal ---
        if self._callback:
            from monitoring.whale_geyser import WhaleBuy

            whale_buy = WhaleBuy(
                whale_wallet=f"{signal['channel']}_telegram",
                token_mint=contract,
                amount_sol=0.0,
                timestamp=datetime.now(),
                tx_signature=f"{signal['channel']}_{contract[:16]}_{int(time.time())}",
                whale_label=f"{src}-CPW{mult}" if signal["channel"] == "callanalyser" else f"{src}-x{mult}",
                platform=signal["channel"],
                token_symbol=symbol,
            )

            logger.warning(
                f"[{src}] === SIGNAL === x{mult} | {symbol} | {contract} | "
                f"mcap=${mcap:,.0f} | -> _on_whale_buy()"
            )

            try:
                await self._callback(whale_buy)
            except Exception as e:
                logger.error(f"[{src}] Callback error: {e}", exc_info=True)

    async def start(self):
        """Start listening to all configured channels. Runs forever."""
        if not self.api_id or not self.api_hash:
            logger.error("[SPYDEFI] Missing api_id/api_hash")
            return

        self._load_seen()

        try:
            from telethon import TelegramClient, events

            self._client = TelegramClient(
                self.session_file,
                self.api_id,
                self.api_hash,
                connection_retries=10,
                retry_delay=5,
                auto_reconnect=True,
                flood_sleep_threshold=60,
            )

            # Register handler for ALL channels
            self._client.add_event_handler(
                self._on_message,
                events.NewMessage(chats=self.channels)
            )

            await self._client.start()
            me = await self._client.get_me()
            self._running = True

            channels_str = ", ".join(self.channels)
            logger.warning(f"[SPYDEFI] Started | user={me.first_name} | channels=[{channels_str}]")
            logger.warning(
                f"[SPYDEFI] Filter: x{self.min_multiplier}-x{self.max_multiplier} | "
                f"seen={len(self._seen)}"
            )

            await self._client.run_until_disconnected()

        except Exception as e:
            logger.error(f"[SPYDEFI] Fatal error: {e}", exc_info=True)
            self._running = False

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            self._running = False
