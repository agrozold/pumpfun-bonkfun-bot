"""
Local Transaction Parser - Parse swap transactions from gRPC protobuf data.
Eliminates ~650ms Helius API call by parsing pre/post token balances locally.

Drop-in replacement for Helius Enhanced API parsing in whale_geyser.py.
"""

import logging
import struct
from dataclasses import dataclass, field
from typing import Optional

import base58

logger = logging.getLogger(__name__)


# =============================================================================
# COMPREHENSIVE TOKEN BLACKLIST
# Tokens we NEVER want to copy-trade (stables, wrapped, LSTs, infrastructure)
# =============================================================================

# --- Stablecoins (USD-pegged) ---
_STABLECOINS = {
    # USDC (Circle) - native on Solana
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    # USDT (Tether) - native on Solana
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    # PYUSD (PayPal USD)
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
    # USDH (Hubble Protocol)
    "USDH1SM1ojwWUga67PGrgFWUHibbjqMvuMaDkRJTgkX",
    # USDS (Sky/Maker, ex-DAI successor)
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",
    # USD* (Perena / USD Star)
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
    # DAI (Wormhole bridged from Ethereum)
    "EjmyN6qEC1Tf1JxiG1ae7UTJhUxSwk1TCWNWqxWV4J6o",
    # USDT (Wormhole bridged)
    "8qJSyQprMC57TWKaYEmetUR3UUiTP2M3hXdcvFhkZdmv",
    # USDC (Wormhole bridged)
    "A9mUU4qviSctJVPJdBGMTd5mKb5aE1bcRoFV6ic1gFiV",
    # UXD (UXD Protocol stablecoin)
    "7kbnvuGBxxj8AG9qp8Scn56muWGaRaFqxg1FsRp3PaFT",
    # EURC (Circle EUR stablecoin)
    "HzwqbKZw8HxMN6bF2yFZNrht3c2iXXzpKcFu7uBEDKtr",
    # ISC (International Stable Currency)
    "J9BcrQfX4p9D1bvLzRNCbMDv8f44a9LFdeqNE9Ip3KL",
    # FDUSD (First Digital USD - if bridged)
    "Dn4noZ5jgGfkntzcQSUZ8czCreg32FeNj4VFpjjMxoYi",
    # ZUSD (Z.com USD)
    "AhhdRu5YZdjVkKR3wbnUDaymVQL2ucjMQ63sZ3LFHsch",
    # jUSD (Jupiter Perps USD)
    "JuprjznTrTSp2UFa3ZBUFgwdAmtZCq4MQCwysN55USD",
    # pyUSD/stablecoin (from stablecoin_filter config)
    "F3hW1kkYVXhMz9FRV8t3mEfwmLQygF7PtPSsofPCdmXR",
}

# --- SOL variants (native, wrapped, staked) ---
_SOL_VARIANTS = {
    # Native SOL (Wrapped SOL SPL token)
    "So11111111111111111111111111111111111111112",
    # mSOL (Marinade staked SOL)
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    # stSOL (Lido staked SOL)
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",
    # jitoSOL (Jito staked SOL)
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    # bSOL (BlazeStake staked SOL)
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    # JupSOL (Jupiter staked SOL)
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",
    # LST (Liquid Staking Token by Marginfi)
    "LSTxxxnJzKDFSLr4dUkPcmCf5VyryEqzPLz5j4bpxFp",
    # INF (Sanctum Infinity SOL)
    "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm",
    # compassSOL (Solana Compass)
    "Comp4ssDzXcLeu2MnLuGNNFC4cmLPMng8qWHPvzAMU1h",
    # bonkSOL (BonkStake)
    "BonK1YhkXEGLZzwtcvRTip3gAL9nCeQD7ppZBLXhtTs",
    # dSOL (Drift staked SOL)
    "Dso1bDeDjCQxTrWHqUUi63oBvV7Mdm6WaobLbQ7gnPQ",
    # hSOL (Helius staked SOL)
    "he1iusmfkpAdwvxLNGV8Y1iSbj4rUy6yMhEA3fotn9A",
    # vSOL (Valo staked SOL)
    "vSoLxydx6akxyMD9XEcPvGYNGq6Nn66oqVb3UkGkei7",
    # JSOL (JPool staked SOL)
    "7Q2afV64in6N6SeZsAAB81TJzwpeLmGEsZ9T91dzbqTo",
    # edgeSOL (Edgevana staked SOL)
    "edge86g9cVz87xcpKpy3J77vbp4wYd9idEV562CCntt",
    # laineSOL (Laine staked SOL)
    "LAinEtNLgpmCP9Rvsf5Hn8W6EhNiKLZQv1oXJsXkOlQ",
    # pathSOL (Pathfinders staked SOL)
    "pathdXw4He1Xk3eX84pDdDcoFhWd3XkSBJsEJpsSdSo",
    # hubSOL (SolanaHub staked SOL)
    "HUBsveNpjo5pWqNkH57QzxjQASdTVXcSK7bVKTSZtcSX",
    # pumpkinSOL (Pumpkin staked SOL)
    "pumpkinsEq8xENVZE6QgTS93EN4r9iKvNxNALS1ooyp",
    # picoSOL (Picasso staked SOL)
    "picobAEvs6w7QEknPce34wAE4gknZA9v5tTonnmHYdX",
    # phaseSOL
    "phaseQLbhsFR4NHBZbcxr5qvLo7MLbv8a8AypEQUFPt",
    # powerSOL
    "PoWERanXGwKk4FzBFB7jxGaKRZq7WPJ1vcWE7SsbbA1",
    # cgntSOL (Cogent staked SOL)
    "CgnTSoL3DgY9SFHxcLj6CgCgKKoTBr6tp4CPAEWy25DE",
    # strongSOL
    "strng7mqqc1MBJJV6vMzYbEqnwVGvKKGKedeCvtktWA",
    # lanternSOL
    "LnTRntk2kTfWEY6cVB8K9649pgJbt6dJLS1Ns1GZCWg",
}

# --- Wrapped BTC variants ---
_WRAPPED_BTC = {
    # WBTC (Wormhole Portal bridged)
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
    # Wrapped Bitcoin (Sollet - legacy)
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",
    # cbBTC (Coinbase Wrapped BTC)
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    # tBTC (Threshold Network BTC)
    "6DNSN2BJsaPFdBAy8hxQqCQDSYzNfemWW5v3CXLkm4Rj",
}

# --- Wrapped ETH variants ---
_WRAPPED_ETH = {
    # WETH (Wormhole Portal bridged)
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
    # Wrapped Ether (Sollet - legacy)
    "2FPyTwcZLUg1MDrwsyoP4D6s1tM6hAkTTpEhCqW5FCLR",
    # cbETH (Coinbase staked ETH - if bridged)
    "BRjpCHtyQLeSRW8rkz2P1zXW4bAixbkKbfAi9Mrp6beN",
}

# --- Infrastructure / Governance tokens we don't want to snipe ---
_INFRASTRUCTURE = {
    # JUP (Jupiter governance)
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    # JupSOL already in SOL variants
    # RAY (Raydium)
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    # SRM (Serum - legacy)
    "SRMuApVNdxXokk5GT7XD5cUUgXMBCoAz2LHeuAoKWRt",
    # MNDE (Marinade governance)
    "MNDEFzGvMt87ueuHvVU9VcTqsAP5b3fTGPsHuuPA5ey",
    # ORCA
    "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    # JTO (Jito governance)
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
}

# --- Combine all into one master blacklist ---
COMPREHENSIVE_TOKEN_BLACKLIST: set[str] = (
    _STABLECOINS
    | _SOL_VARIANTS
    | _WRAPPED_BTC
    | _WRAPPED_ETH
    | _INFRASTRUCTURE
)


# =============================================================================
# DEX Program IDs
# =============================================================================
DEX_PROGRAM_IDS: dict[str, str] = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump_fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter",
    "JUP2jxvXaqu7NQY1GmNF4m1vodw12LVXYxbFL2uN9oQp": "jupiter",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB":  "jupiter",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc":  "orca",
    "MERLuDFBMmsHnsBPZw2sDQZHvXFMwp8EdjudcU2HKky":  "mercurial",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo":  "meteora",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UG":  "meteora_dlmm",
}

# Pump.fun BUY discriminator (first 8 bytes of instruction data)
PUMP_FUN_BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
PUMP_FUN_SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])

SOL_MINT = "So11111111111111111111111111111111111111112"


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class ParsedSwap:
    """Result of local transaction parsing."""
    signature: str
    fee_payer: str
    is_buy: bool
    token_mint: str
    sol_amount: float          # SOL spent (buy) or received (sell)
    token_amount: float        # token quantity (UI amount)
    platform: str              # pump_fun, pumpswap, jupiter, raydium, unknown
    token_symbol: str = ""     # filled later via DexScreener
    virtual_sol_reserves: int = 0    # from TradeEvent (lamports)
    virtual_token_reserves: int = 0  # from TradeEvent (raw units)
    whale_token_program: str = ""       # S14: from whale TX account[8]
    whale_creator_vault: str = ""       # S14: from whale TX account[9]
    whale_fee_recipient: str = ""       # S14: from whale TX account[1]
    whale_assoc_bonding_curve: str = "" # S14: from whale TX account[4]


@dataclass
class ParserStats:
    """Statistics for monitoring parser performance."""
    total_parsed: int = 0
    successful: int = 0
    failed: int = 0
    buys_detected: int = 0
    sells_detected: int = 0
    blacklisted_skipped: int = 0
    no_swap_detected: int = 0
    pump_discriminator_used: int = 0
    balance_method_used: int = 0


# =============================================================================
# Main parser class
# =============================================================================

class LocalTxParser:
    """
    Parse swap transactions locally from gRPC protobuf data.
    
    Extracts buy/sell info using pre/post token balances from TransactionStatusMeta.
    No HTTP calls needed — ~0-5ms instead of ~650ms via Helius.
    
    Filtering is applied at parse time:
    - Tokens in COMPREHENSIVE_TOKEN_BLACKLIST are rejected
    - Additional blacklist can be passed via constructor (stablecoin_filter from YAML)
    """

    def __init__(self, extra_blacklist: set[str] | None = None):
        """
        Args:
            extra_blacklist: Additional token mints to blacklist
                             (e.g. from stablecoin_filter in bot config)
        """
        self.blacklist: set[str] = COMPREHENSIVE_TOKEN_BLACKLIST.copy()
        if extra_blacklist:
            self.blacklist.update(extra_blacklist)

        self.stats = ParserStats()

        logger.info(
            f"[LOCAL_PARSER] Initialized with {len(self.blacklist)} blacklisted tokens "
            f"({len(COMPREHENSIVE_TOKEN_BLACKLIST)} built-in + "
            f"{len(extra_blacklist) if extra_blacklist else 0} extra)"
        )

    def parse(self, tx_update, fee_payer: str) -> Optional[ParsedSwap]:
        """
        Parse a gRPC SubscribeUpdateTransactionInfo into a ParsedSwap.
        
        Args:
            tx_update: geyser_pb2.SubscribeUpdateTransactionInfo
                       (has .signature, .transaction, .meta)
            fee_payer: Already-decoded base58 fee payer address
            
        Returns:
            ParsedSwap if a valid swap was detected, None otherwise
        """
        self.stats.total_parsed += 1

        try:
            # Extract signature
            sig_bytes = bytes(tx_update.signature)
            signature = base58.b58encode(sig_bytes).decode()

            meta = tx_update.meta
            msg = tx_update.transaction.message

            if not meta or not msg:
                self.stats.failed += 1
                return None

            # ------------------------------------------------------------------
            # Step 1: Build account_keys list (static + loaded from ALT)
            # ------------------------------------------------------------------
            account_keys: list[str] = []
            for key_bytes in msg.account_keys:
                account_keys.append(base58.b58encode(bytes(key_bytes)).decode())

            # Add loaded addresses from Address Lookup Tables (ALTs)
            # These are appended AFTER the static keys, in order:
            # first loaded_writable, then loaded_readonly
            if meta.loaded_writable_addresses:
                for addr_bytes in meta.loaded_writable_addresses:
                    account_keys.append(base58.b58encode(bytes(addr_bytes)).decode())
            if meta.loaded_readonly_addresses:
                for addr_bytes in meta.loaded_readonly_addresses:
                    account_keys.append(base58.b58encode(bytes(addr_bytes)).decode())

            # ------------------------------------------------------------------
            # Step 2: Detect DEX platform from account keys
            # ------------------------------------------------------------------
            platform = "unknown"
            for key in account_keys:
                if key in DEX_PROGRAM_IDS:
                    platform = DEX_PROGRAM_IDS[key]
                    break

            # ------------------------------------------------------------------
            # Step 3: Try pump.fun discriminator first (most precise)
            # ------------------------------------------------------------------
            if platform == "pump_fun":
                result = self._try_pump_discriminator(
                    msg, meta, account_keys, signature, fee_payer
                )
                if result is not None:
                    self.stats.pump_discriminator_used += 1
                    if result.is_buy:
                        self.stats.buys_detected += 1
                    else:
                        self.stats.sells_detected += 1
                    self.stats.successful += 1
                    return result

            # ------------------------------------------------------------------
            # Step 4: Universal method — pre/post token balance diff
            # Works for ALL DEXes without decoding instruction data
            # ------------------------------------------------------------------
            result = self._parse_from_balances(
                meta, account_keys, signature, fee_payer, platform
            )
            if result is not None:
                self.stats.balance_method_used += 1
                if result.is_buy:
                    self.stats.buys_detected += 1
                else:
                    self.stats.sells_detected += 1
                self.stats.successful += 1
                return result

            self.stats.no_swap_detected += 1
            return None

        except Exception as e:
            self.stats.failed += 1
            logger.error(f"[LOCAL_PARSER] Parse error: {e}")
            return None

    def _try_pump_discriminator(
        self, msg, meta, account_keys: list[str],
        signature: str, fee_payer: str
    ) -> Optional[ParsedSwap]:
        """
        Try to parse pump.fun swap via instruction discriminator.
        
        Pump.fun trade event layout (after 8-byte discriminator):
            mint:                 Pubkey  (32 bytes)
            solAmount:            u64     (8 bytes)
            tokenAmount:          u64     (8 bytes)
            isBuy:                bool    (1 byte)
            user:                 Pubkey  (32 bytes)
            timestamp:            i64     (8 bytes)
            virtualSolReserves:   u64     (8 bytes)
            virtualTokenReserves: u64     (8 bytes)
        """
        try:
            # Check outer instructions first
            for ix in msg.instructions:
                result = self._check_pump_instruction(
                    ix.data, ix.program_id_index, account_keys,
                    signature, fee_payer
                )
                if result:
                    return result

            # Check inner instructions (CPI calls)
            if meta.inner_instructions:
                for inner_group in meta.inner_instructions:
                    for ix in inner_group.instructions:
                        result = self._check_pump_instruction(
                            ix.data, ix.program_id_index, account_keys,
                            signature, fee_payer
                        )
                        if result:
                            return result

            # S12: Check inner instructions for Anchor CPI TradeEvent
            # pump.fun emits TradeEvent via emit_cpi! which wraps in Anchor event envelope
            # Layout: [8B anchor:event tag][8B event discriminator][event data...]
            ANCHOR_EVENT_TAG = bytes.fromhex("e445a52e51cb9a1d")
            TRADE_EVENT_DISC = bytes.fromhex("bddb7fd34ee661ee")
            if meta.inner_instructions:
                for inner_group in meta.inner_instructions:
                    for ix in inner_group.instructions:
                        _ixdata = bytes(ix.data)
                        if len(_ixdata) >= 137 and _ixdata[:8] == ANCHOR_EVENT_TAG:
                            # S13: Check event discriminator — ONLY parse TradeEvent
                            _evt_disc = _ixdata[8:16]
                            if _evt_disc != TRADE_EVENT_DISC:
                                logger.debug(
                                    f"[LOCAL_PARSER] CPI event skip: disc={_evt_disc.hex()} "
                                    f"len={len(_ixdata)} (not TradeEvent)"
                                )
                                continue
                            # Skip 16 bytes (8 anchor tag + 8 event discriminator)
                            _off = 16
                            _mint_bytes = _ixdata[_off:_off+32]
                            _mint = base58.b58encode(_mint_bytes).decode()
                            _off += 32
                            _sol_raw = struct.unpack("<Q", _ixdata[_off:_off+8])[0]
                            _sol_amount = _sol_raw / 1e9
                            _off += 8
                            _tok_raw = struct.unpack("<Q", _ixdata[_off:_off+8])[0]
                            _tok_amount = _tok_raw / 1e6
                            _off += 8
                            _is_buy = _ixdata[_off] != 0
                            _off += 1
                            _off += 32  # skip user pubkey
                            _off += 8   # skip timestamp
                            _vsr = struct.unpack("<Q", _ixdata[_off:_off+8])[0]
                            _off += 8
                            _vtr = struct.unpack("<Q", _ixdata[_off:_off+8])[0]

                            if _mint in self.blacklist:
                                self.stats.blacklisted_skipped += 1
                                return None

                            # S12: Validate reserves — pump.fun BC max is ~85 SOL (~85B lamports)
                            # and max tokens ~1.07T (1073000000000000)
                            _reserves_valid = (
                                0 < _vsr < 200_000_000_000 and  # < 200 SOL
                                0 < _vtr < 2_000_000_000_000_000  # < 2 quadrillion
                            )

                            if _reserves_valid:
                                logger.warning(
                                    f"[LOCAL_PARSER] S12 CPI TradeEvent: "
                                    f"mint={_mint[:16]}... sol={_sol_amount:.4f} "
                                    f"tok={_tok_amount:.0f} buy={_is_buy} "
                                    f"vsr={_vsr} vtr={_vtr}"
                                )
                            else:
                                logger.warning(
                                    f"[LOCAL_PARSER] S12 CPI BAD RESERVES: "
                                    f"mint={_mint[:16]}... vsr={_vsr} vtr={_vtr} "
                                    f"— setting to 0 (will use RPC fallback)"
                                )
                                _vsr = 0
                                _vtr = 0

                            # S14: Extract whale accounts from outer instruction
                            # Whale may use direct pump.fun OR router (term9, etc.)
                            # Both pass same 16 accounts in same order
                            # Detect by: 16+ accounts with GLOBAL at [0] and PUMP at [11]
                            _w_token_program = ""
                            _w_creator_vault = ""
                            _w_fee_recipient = ""
                            _w_assoc_bc = ""
                            _PUMP_PROG = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
                            _GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
                            try:
                                for _oix in msg.instructions:
                                    _oix_accs = list(_oix.accounts)
                                    if len(_oix_accs) >= 16:
                                        # Check signature: GLOBAL at [0], PUMP at [11]
                                        _a0 = account_keys[_oix_accs[0]] if _oix_accs[0] < len(account_keys) else ""
                                        _a11 = account_keys[_oix_accs[11]] if _oix_accs[11] < len(account_keys) else ""
                                        if _a0 == _GLOBAL and _a11 == _PUMP_PROG:
                                            if _oix_accs[1] < len(account_keys):
                                                _w_fee_recipient = account_keys[_oix_accs[1]]
                                            if _oix_accs[4] < len(account_keys):
                                                _w_assoc_bc = account_keys[_oix_accs[4]]
                                            if _oix_accs[8] < len(account_keys):
                                                _w_token_program = account_keys[_oix_accs[8]]
                                            if _oix_accs[9] < len(account_keys):
                                                _w_creator_vault = account_keys[_oix_accs[9]]
                                            logger.info(
                                                f"[LOCAL_PARSER] S14 whale accounts: "
                                                f"tp={_w_token_program[:8]}... "
                                                f"cv={_w_creator_vault[:8]}... "
                                                f"fee={_w_fee_recipient[:8]}..."
                                            )
                                            break
                            except Exception as _e:
                                logger.debug(f"[LOCAL_PARSER] S14 whale account extract: {_e}")

                            return ParsedSwap(
                                signature=signature,
                                fee_payer=fee_payer,
                                is_buy=_is_buy,
                                token_mint=_mint,
                                sol_amount=_sol_amount,
                                token_amount=_tok_amount,
                                platform="pump_fun",
                                virtual_sol_reserves=_vsr,
                                virtual_token_reserves=_vtr,
                                whale_token_program=_w_token_program,
                                whale_creator_vault=_w_creator_vault,
                                whale_fee_recipient=_w_fee_recipient,
                                whale_assoc_bonding_curve=_w_assoc_bc,
                            )

        except Exception as e:
            logger.debug(f"[LOCAL_PARSER] Pump discriminator failed: {e}")

        return None

    def _check_pump_instruction(
        self, data: bytes, program_id_index: int,
        account_keys: list[str], signature: str, fee_payer: str
    ) -> Optional[ParsedSwap]:
        """Check single instruction for pump.fun discriminator."""
        data = bytes(data)

        if len(data) < 8:
            return None
        
        # S13: DIAG removed (was S12, caused confusing logs)

        # Verify program is pump.fun
        if program_id_index < len(account_keys):
            prog = account_keys[program_id_index]
            if prog != "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P":
                return None
        else:
            return None

        discriminator = data[:8]

        if discriminator == PUMP_FUN_BUY_DISCRIMINATOR:
            is_buy = True
        elif discriminator == PUMP_FUN_SELL_DISCRIMINATOR:
            is_buy = False
        else:
            return None

        # Parse event data: need at least 8 + 32 + 8 + 8 + 1 = 57 bytes
        if len(data) < 57:
            return None

        offset = 8
        mint_bytes = data[offset:offset + 32]
        mint = base58.b58encode(mint_bytes).decode()
        offset += 32

        sol_amount_raw = struct.unpack("<Q", data[offset:offset + 8])[0]
        sol_amount = sol_amount_raw / 1e9  # lamports -> SOL
        offset += 8

        token_amount_raw = struct.unpack("<Q", data[offset:offset + 8])[0]
        token_amount = token_amount_raw / 1e6  # pump.fun uses 6 decimals
        offset += 8

        is_buy_flag = data[offset] != 0
        offset += 1

        # Extract reserves from TradeEvent (ZERO-RPC optimization)
        # Layout: user(32) + timestamp(8) + virtualSolReserves(8) + virtualTokenReserves(8)
        _vsr = 0
        _vtr = 0
        if len(data) >= 113:  # Full TradeEvent: 8+32+8+8+1+32+8+8+8=113
            offset += 32  # skip user pubkey
            offset += 8   # skip timestamp
            _vsr = struct.unpack("<Q", data[offset:offset + 8])[0]
            offset += 8
            _vtr = struct.unpack("<Q", data[offset:offset + 8])[0]

        # Blacklist check
        if mint in self.blacklist:
            self.stats.blacklisted_skipped += 1
            logger.debug(f"[LOCAL_PARSER] Blacklisted token via discriminator: {mint[:16]}...")
            return None

        return ParsedSwap(
            signature=signature,
            fee_payer=fee_payer,
            is_buy=is_buy_flag,
            token_mint=mint,
            sol_amount=sol_amount,
            token_amount=token_amount,
            platform="pump_fun",
            virtual_sol_reserves=_vsr,
            virtual_token_reserves=_vtr,
        )

    def _parse_from_balances(
        self, meta, account_keys: list[str],
        signature: str, fee_payer: str, platform: str
    ) -> Optional[ParsedSwap]:
        """
        Universal swap detection via pre/post balance diffs.
        Works for ANY DEX without decoding instruction data.
        
        Logic:
        1. Find token where fee_payer's balance INCREASED (buy) or DECREASED (sell)
        2. Calculate SOL change on fee_payer (index 0) minus tx fee
        3. If fee_payer SOL decreased AND token increased -> BUY
        4. If fee_payer SOL increased AND token decreased -> SELL
        """
        try:
            pre_balances = list(meta.pre_balances)
            post_balances = list(meta.post_balances)

            if not pre_balances or not post_balances:
                return None

            # SOL change for fee_payer (index 0), accounting for tx fee
            fee = meta.fee
            sol_change_lamports = (post_balances[0] - pre_balances[0]) + fee
            # Positive = SOL received, Negative = SOL spent

            # Build token balance diffs for fee_payer
            pre_tokens: dict[str, int] = {}   # mint -> raw amount
            post_tokens: dict[str, int] = {}  # mint -> raw amount
            token_decimals: dict[str, int] = {}

            for tb in meta.pre_token_balances:
                if tb.owner == fee_payer:
                    raw = int(tb.ui_token_amount.amount) if tb.ui_token_amount.amount else 0
                    pre_tokens[tb.mint] = raw
                    token_decimals[tb.mint] = tb.ui_token_amount.decimals

            for tb in meta.post_token_balances:
                if tb.owner == fee_payer:
                    raw = int(tb.ui_token_amount.amount) if tb.ui_token_amount.amount else 0
                    post_tokens[tb.mint] = raw
                    token_decimals[tb.mint] = tb.ui_token_amount.decimals

            # Find all mints involved with fee_payer
            all_mints = set(pre_tokens.keys()) | set(post_tokens.keys())

            # Find the non-SOL token with the largest absolute balance change
            best_mint = None
            best_diff = 0
            best_decimals = 6

            for mint in all_mints:
                # Skip SOL and blacklisted tokens
                if mint == SOL_MINT:
                    continue
                if mint in self.blacklist:
                    self.stats.blacklisted_skipped += 1
                    logger.debug(
                        f"[LOCAL_PARSER] Blacklisted via balances: {mint[:16]}..."
                    )
                    continue

                pre_raw = pre_tokens.get(mint, 0)
                post_raw = post_tokens.get(mint, 0)
                diff = post_raw - pre_raw

                if abs(diff) > abs(best_diff):
                    best_diff = diff
                    best_mint = mint
                    best_decimals = token_decimals.get(mint, 6)

            if best_mint is None or best_diff == 0:
                return None

            # Determine buy/sell:
            # Token balance increased -> BUY (whale received tokens)
            # Token balance decreased -> SELL (whale sent tokens)
            is_buy = best_diff > 0

            # Cross-validate with SOL flow
            # BUY: SOL should decrease (negative sol_change)
            # SELL: SOL should increase (positive sol_change)
            if is_buy and sol_change_lamports > 0:
                # Token increased but SOL also increased? Not a standard swap
                logger.debug(
                    f"[LOCAL_PARSER] Ambiguous: token+ but SOL+, skipping {signature[:16]}..."
                )
                return None

            if not is_buy and sol_change_lamports < 0:
                # Token decreased but SOL also decreased? Not a standard swap
                logger.debug(
                    f"[LOCAL_PARSER] Ambiguous: token- but SOL-, skipping {signature[:16]}..."
                )
                return None

            sol_amount = abs(sol_change_lamports) / 1e9
            token_amount = abs(best_diff) / (10 ** best_decimals)

            return ParsedSwap(
                signature=signature,
                fee_payer=fee_payer,
                is_buy=is_buy,
                token_mint=best_mint,
                sol_amount=sol_amount,
                token_amount=token_amount,
                platform=platform,
            )

        except Exception as e:
            logger.debug(f"[LOCAL_PARSER] Balance parse failed: {e}")
            return None

    def is_blacklisted(self, mint: str) -> bool:
        """Check if a token mint is in the blacklist."""
        return mint in self.blacklist

    def get_stats(self) -> dict:
        """Return parser statistics as dict."""
        return {
            "total_parsed": self.stats.total_parsed,
            "successful": self.stats.successful,
            "failed": self.stats.failed,
            "buys_detected": self.stats.buys_detected,
            "sells_detected": self.stats.sells_detected,
            "blacklisted_skipped": self.stats.blacklisted_skipped,
            "no_swap_detected": self.stats.no_swap_detected,
            "pump_discriminator_used": self.stats.pump_discriminator_used,
            "balance_method_used": self.stats.balance_method_used,
            "blacklist_size": len(self.blacklist),
        }
