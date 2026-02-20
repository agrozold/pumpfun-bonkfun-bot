import asyncio
"""Fallback trading methods for migrated tokens.

Provides Jupiter buy/sell functionality when bonding curve is unavailable.
"""

import os
import struct
from typing import TYPE_CHECKING

import aiohttp
import base58
from solana.rpc.commitment import Confirmed
from solana.rpc.types import MemcmpOpts, TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from spl.token.instructions import get_associated_token_address

from utils.logger import get_logger
from utils.retry import calculate_delay, classify_error, ErrorCategory

if TYPE_CHECKING:
    from core.client import SolanaClient
    from core.wallet import Wallet

logger = get_logger(__name__)

# Transaction verification (Fire & Forget with background check)
from core.tx_verifier import get_tx_verifier
from core.tx_callbacks import on_buy_success, on_buy_failure
from trading.jito_sender import get_jito_sender

# Constants
TOKEN_DECIMALS = 6  # Default, use get_token_decimals() for dynamic
LAMPORTS_PER_SOL = 1_000_000_000

# === DYNAMIC DECIMALS CACHE ===
_decimals_cache: dict[str, int] = {}

async def get_token_decimals(client, mint: "Pubkey") -> int:
    """
    Get token decimals from mint account.
    Uses cache to avoid repeated RPC calls.
    Falls back to TOKEN_DECIMALS (6) on error.
    """
    mint_str = str(mint)
    
    # Check cache
    if mint_str in _decimals_cache:
        return _decimals_cache[mint_str]
    
    # Fast path: ALL memecoins are 6 decimals - skip RPC entirely
    # Only WSOL is 9 decimals, everything else we trade = 6
    if mint_str == "So11111111111111111111111111111111111111112":
        _decimals_cache[mint_str] = 9
        return 9
    # Fast path for known platforms (no RPC needed)
    if mint_str.endswith("pump") or mint_str.endswith("bonk"):
        _decimals_cache[mint_str] = 6
        return 6

    # Fast path: Bags.fm / Meteora DBC tokens = 9 decimals (confirmed on-chain via Solscan)
    if mint_str.endswith("BAGS"):
        _decimals_cache[mint_str] = 9
        return 9
    
    try:
        # Get mint account info
        response = await asyncio.wait_for(client.get_account_info(mint, encoding="base64"), timeout=1.0)
        if response and response.value:
            data = response.value.data
            # Handle base64 encoded data
            if isinstance(data, tuple):
                import base64
                data = base64.b64decode(data[0])
            elif isinstance(data, str):
                import base64
                data = base64.b64decode(data)
            
            # Decimals is at offset 44 in SPL Token mint layout
            if len(data) >= 45:
                decimals = data[44]
                _decimals_cache[mint_str] = decimals
                if decimals != TOKEN_DECIMALS:
                    logger.info(f"[DECIMALS] Token {mint_str[:8]}... has {decimals} decimals (not default 6)")
                return decimals
    except Exception as e:
        logger.warning(f"[DECIMALS] Failed to get decimals for {mint_str[:8]}...: {e}, using default {TOKEN_DECIMALS}")
    
    # Fallback
    _decimals_cache[mint_str] = TOKEN_DECIMALS
    return TOKEN_DECIMALS
# === END DYNAMIC DECIMALS ===

# PumpSwap constants
SOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_SWAP_GLOBAL_CONFIG = Pubkey.from_string("ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw")
PUMP_SWAP_EVENT_AUTHORITY = Pubkey.from_string("GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR")
STANDARD_PUMPSWAP_FEE_RECIPIENT = Pubkey.from_string("7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ")
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SELL_DISCRIMINATOR = bytes.fromhex("33e685a4017f83ad")
BUY_DISCRIMINATOR = bytes.fromhex("c62e1552b4d9e870")  # buy_exact_quote_in

# System constants
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# Pool structure offsets
POOL_BASE_MINT_OFFSET = 43
POOL_MAYHEM_MODE_OFFSET = 243
POOL_MAYHEM_MODE_MIN_SIZE = 244
GLOBALCONFIG_RESERVED_FEE_OFFSET = 72


# ============================================================================
# Transaction Verification Helper
# ============================================================================
async def verify_transaction_success(rpc_client, signature: str, max_wait: float = 10.0) -> tuple[bool, str | None]:
    """
    Verify transaction was confirmed AND successful on-chain.
    Returns (success, error_message)
    """
    import asyncio
    from solders.signature import Signature
    
    sig = Signature.from_string(signature) if isinstance(signature, str) else signature
    start_time = asyncio.get_event_loop().time()
    
    while asyncio.get_event_loop().time() - start_time < max_wait:
        try:
            resp = await rpc_client.get_signature_statuses([sig])
            if resp.value and resp.value[0]:
                status = resp.value[0]
                if status.err:
                    # Transaction failed on-chain (6001, 6024, etc.)
                    return False, f"TX failed: {status.err}"
                if status.confirmation_status:
                    # Confirmed successfully
                    return True, None
        except Exception:
            pass
        await asyncio.sleep(0.3)
    
    return False, "Confirmation timeout"



# [edit:s12] post-buy verify — corrects Jupiter estimate if decimals mismatch
async def _post_buy_verify_balance(wallet_pubkey: str, mint_str: str, expected_tokens: float, 
                                     sol_spent: float, token_decimals_expected: int,
                                     rpc_url: str = None) -> tuple[float, float, int]:
    """Verify actual tokens received after buy. Returns (corrected_tokens, corrected_price, actual_decimals).
    
    Compares Jupiter estimate with actual on-chain balance.
    If >50% difference, recalculates using real balance (likely decimals mismatch).
    """
    import aiohttp, os
    if not rpc_url:
        rpc_url = os.getenv("DRPC_RPC_ENDPOINT") or os.getenv("SOLANA_NODE_RPC_ENDPOINT") or "https://api.mainnet-beta.solana.com"
    
    if not wallet_pubkey:
        logger.warning("[POST-BUY VERIFY] No wallet_pubkey, skipping")
        return expected_tokens, sol_spent / expected_tokens if expected_tokens > 0 else 0, token_decimals_expected

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [wallet_pubkey, {"mint": mint_str}, 
                          {"encoding": "jsonParsed", "commitment": "confirmed"}]
            }
            async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    accounts = data.get("result", {}).get("value", [])
                    if accounts:
                        token_info = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
                        actual_ui = float(token_info.get("uiAmount") or 0)
                        actual_decimals = int(token_info.get("decimals", 6))
                        actual_raw = int(token_info.get("amount", "0"))
                        
                        # Update decimals cache
                        _decimals_cache[mint_str] = actual_decimals
                        
                        if expected_tokens > 0 and actual_ui > 0:
                            ratio = actual_ui / expected_tokens
                            if ratio < 0.5 or ratio > 2.0:
                                corrected_price = sol_spent / actual_ui if actual_ui > 0 else 0
                                logger.warning(
                                    f"[POST-BUY VERIFY] CORRECTED! Expected {expected_tokens:,.2f} tokens "
                                    f"(decimals={token_decimals_expected}), actual {actual_ui:,.2f} "
                                    f"(decimals={actual_decimals}). Ratio={ratio:.4f}. "
                                    f"Price: {sol_spent/expected_tokens:.10f} -> {corrected_price:.10f}"
                                )
                                return actual_ui, corrected_price, actual_decimals
                            else:
                                logger.info(f"[POST-BUY VERIFY] OK: expected={expected_tokens:,.2f}, actual={actual_ui:,.2f}, ratio={ratio:.2f}")
                                return actual_ui, sol_spent / actual_ui if actual_ui > 0 else 0, actual_decimals
                        
                        return expected_tokens, sol_spent / expected_tokens if expected_tokens > 0 else 0, actual_decimals
    except Exception as e:
        logger.warning(f"[POST-BUY VERIFY] Failed: {e}")
    
    return expected_tokens, sol_spent / expected_tokens if expected_tokens > 0 else 0, token_decimals_expected


class FallbackSeller:
    """Handles selling tokens via PumpSwap or Jupiter when bonding curve unavailable."""

    def __init__(
        self,
        client: "SolanaClient",
        wallet: "Wallet",
        slippage: float = 0.30,  # 30% buy, 10% sell from config
        priority_fee: int = 10000,
        max_retries: int = 3,
        alt_rpc_endpoint: str | None = None,  # Alternative RPC to avoid rate limits
        jupiter_api_key: str | None = None,  # Jupiter Ultra API key
    ):
        self.client = client
        self.wallet = wallet
        self.slippage = slippage
        self.priority_fee = priority_fee
        self.max_retries = max_retries
        self.alt_rpc_endpoint = alt_rpc_endpoint
        self.jupiter_api_key = jupiter_api_key or os.getenv("JUPITER_TRADE_API_KEY")  # NO fallback to monitor key!
        self._alt_client = None

    async def _get_rpc_client(self):
        """Get RPC client - uses dRPC/Chainstack/Alchemy.
        
        Priority:
        1. DRPC_RPC_ENDPOINT
        2. SOLANA_NODE_RPC_ENDPOINT  
        3. CHAINSTACK_RPC_ENDPOINT
        4. ALCHEMY_RPC_ENDPOINT
        5. Public Solana (last resort)
        """
        import os

        if self._alt_client is not None:
            return self._alt_client

        # S12: Chainstack PRIMARY (74ms avg), Alchemy fallback (45ms avg)
        # dRPC has cold-start timeouts — moved to last resort
        rpc_url = (
            os.getenv("CHAINSTACK_RPC_ENDPOINT") or
            os.getenv("ALCHEMY_RPC_ENDPOINT") or
            os.getenv("DRPC_RPC_ENDPOINT") or
            os.getenv("SOLANA_NODE_RPC_ENDPOINT") or
            "https://api.mainnet-beta.solana.com"
        )
        
        from solana.rpc.async_api import AsyncClient
        self._alt_client = AsyncClient(rpc_url)
        logger.info(f"[FALLBACK] Using RPC: {rpc_url[:60]}...")
        return self._alt_client

    async def _send_tx_parallel(self, signed_tx, rpc_client):
        """Send TX via Jito + RPC in parallel. Returns first successful signature."""
        jito = get_jito_sender()

        async def _jito_send():
            if not jito.enabled:
                return None
            return await jito.send_transaction(signed_tx)

        async def _rpc_send():
            result = await rpc_client.send_transaction(
                signed_tx,
                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
            )
            return str(result.value)

        tasks = [asyncio.create_task(_rpc_send())]
        if jito.enabled:
            tasks.insert(0, asyncio.create_task(_jito_send()))

        sig = None
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
                if result:
                    sig = result
                    break
            except Exception as e:
                logger.warning(f'[TX] Parallel send error: {e}')
                continue

        # Cancel remaining tasks
        for t in tasks:
            if not t.done():
                t.cancel()

        if not sig:
            raise RuntimeError('Both Jito and RPC send failed')
        return sig



    async def buy_via_pumpswap(
        self,
        mint: Pubkey,
        sol_amount: float,
        symbol: str = "TOKEN",
        market_address: Pubkey | None = None,  # Optional - skip lookup if provided
        position_config: dict | None = None,  # TSL/TP/SL parameters for callback
    ) -> tuple[bool, str | None, str | None, float, float]:
        """Buy token via PumpSwap AMM - for migrated tokens.

        Args:
            mint: Token mint address
            sol_amount: Amount of SOL to spend
            symbol: Token symbol for logging
            market_address: Optional pool address (skip lookup if provided)

        Returns:
            Tuple of (success, tx_signature, error_message, token_amount, price)
        """
        from solders.system_program import TransferParams, transfer
        from spl.token.instructions import (
            SyncNativeParams,
            create_idempotent_associated_token_account,
            sync_native,
        )

        logger.info(f"[PUMPSWAP] PumpSwap BUY starting for {symbol} ({mint})")
        logger.info(f"[PUMPSWAP] Amount: {sol_amount} SOL, market_address provided: {market_address is not None}")

        try:
            rpc_client = await self._get_rpc_client()
            
            # Get dynamic decimals for this token
            token_decimals = await get_token_decimals(rpc_client, mint)
            logger.info(f"[DECIMALS] Using {token_decimals} decimals for {symbol}")

            # Use provided market or find it
            if market_address:
                market = market_address
                logger.info(f"[MARKET] Using provided PumpSwap market: {market}")
            else:
                # Find market via get_program_accounts (expensive!)
                logger.info("[MARKET] Looking up PumpSwap market via get_program_accounts...")
                filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=bytes(mint))]
                try:
                    response = await rpc_client.get_program_accounts(
                        PUMP_AMM_PROGRAM_ID, encoding="base64", filters=filters
                    )
                except Exception as e:
                    logger.error(f"[MARKET] get_program_accounts failed: {e}")
                    return False, None, f"RPC error looking up market: {e}", 0.0, 0.0

                if not response.value:
                    logger.warning(f"[MARKET] No PumpSwap market found for {symbol}")
                    return False, None, f"PumpSwap market not found for {mint}", 0.0, 0.0

                market = response.value[0].pubkey
                logger.info(f"[MARKET] Found PumpSwap market: {market}")

            # Get market data
            logger.info("[MARKET] Fetching market account data...")
            market_response = None
            for retry in range(3):
                try:
                    market_response = await rpc_client.get_account_info(market, encoding="base64")
                    break
                except Exception as e:
                    error_str = str(e)
                    if "429" in error_str or not error_str:
                        logger.warning(f"[MARKET] RPC rate limited, retry {retry + 1}/3...")
                        import asyncio
                        await asyncio.sleep(calculate_delay(retry, base_delay=0.5, max_delay=10.0))
                        continue
                    logger.error(f"[MARKET] get_account_info failed for market {market}: {e}")
                    return False, None, f"Failed to fetch market data: {e}", 0.0, 0.0

            if not market_response or not market_response.value:
                logger.error(f"[MARKET] Market account {market} not found on chain")
                return False, None, f"Market account {market} not found on chain", 0.0, 0.0

            data = market_response.value.data
            # Handle both bytes and tuple (base64 encoded)
            if isinstance(data, tuple):
                import base64
                data = base64.b64decode(data[0])
            elif isinstance(data, str):
                import base64
                data = base64.b64decode(data)

            logger.info(f"[MARKET] Parsing market data ({len(data)} bytes)...")
            try:
                market_data = self._parse_market_data(data)
                logger.info(f"[MARKET] Market data parsed: base_mint={market_data.get('base_mint', 'N/A')[:8]}...")
            except Exception as e:
                logger.error(f"[MARKET] Failed to parse market data: {e}")
                return False, None, f"Failed to parse market data: {e}", 0.0, 0.0

            try:
                token_program_id = await self._get_token_program_id(mint)
                logger.info(f"[TOKEN] Token program: {token_program_id}")
                logger.info(f"[TOKEN] Is Token2022: {token_program_id == TOKEN_2022_PROGRAM}")
            except Exception as e:
                # Retry once after delay
                import asyncio
                await asyncio.sleep(0.5)
                try:
                    token_program_id = await self._get_token_program_id(mint)
                    logger.info(f"[TOKEN] Token program (retry): {token_program_id}")
                except Exception as e2:
                    logger.error(f"[TOKEN] Failed to get token program: {e2}")
                    return False, None, f"Failed to get token program: {e2}", 0.0, 0.0

            # Get user token accounts
            user_base_ata = get_associated_token_address(
                self.wallet.pubkey, mint, token_program_id
            )
            user_quote_ata = get_associated_token_address(
                self.wallet.pubkey, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )

            # Get pool accounts
            pool_base_ata = Pubkey.from_string(market_data["pool_base_token_account"])
            pool_quote_ata = Pubkey.from_string(market_data["pool_quote_token_account"])

            # Get pool balances - use batch call to save RPC requests
            import asyncio
            for balance_retry in range(3):
                try:
                    # Single batch call for both accounts - use rpc_client (alt RPC) not self.client!
                    response = await rpc_client.get_multiple_accounts([pool_base_ata, pool_quote_ata], encoding="base64")
                    accounts = response.value if response.value else []

                    if len(accounts) < 2 or not accounts[0] or not accounts[1]:
                        raise ValueError("Pool vault accounts not found")

                    # Parse token account data (offset 64 for amount in SPL token account)
                    base_data = accounts[0].data
                    quote_data = accounts[1].data

                    # Handle base64 encoded data
                    if isinstance(base_data, tuple):
                        import base64 as b64
                        base_data = b64.b64decode(base_data[0])
                    if isinstance(quote_data, tuple):
                        import base64 as b64
                        quote_data = b64.b64decode(quote_data[0])

                    # Token account layout: amount is at offset 64, 8 bytes little-endian
                    base_amount_raw = struct.unpack("<Q", base_data[64:72])[0]
                    quote_amount_raw = struct.unpack("<Q", quote_data[64:72])[0]

                    base_amount = base_amount_raw / (10 ** token_decimals)
                    quote_amount = quote_amount_raw / (10 ** 9)  # SOL has 9 decimals

                    if base_amount == 0:
                        raise ValueError("Pool has zero base tokens")

                    price = quote_amount / base_amount
                    logger.info(f"[POOL] Pool reserves: {base_amount:,.2f} tokens, {quote_amount:.4f} SOL")
                    logger.info(f"[POOL] Pool price: {price:.10f} SOL per token")
                    break
                except Exception as e:
                    if balance_retry < 2:
                        logger.warning(f"[POOL] Pool balance fetch failed, retry {balance_retry + 1}/3: {e}")
                        await asyncio.sleep(calculate_delay(balance_retry, base_delay=0.5, max_delay=10.0))
                    else:
                        logger.error(f"[POOL] Failed to get pool balances: {e}")
                        return False, None, f"Failed to get pool balances: {e}", 0.0, 0.0

            # Calculate expected tokens
            expected_tokens = sol_amount / price
            min_tokens_output = int(expected_tokens * (1 - self.slippage) * 10**token_decimals)
            buy_amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)

            logger.info(f"[BUY] PumpSwap BUY: {sol_amount} SOL ({buy_amount_lamports} lamports) -> ~{expected_tokens:,.2f} {symbol}")
            logger.info(f"[BUY] Min tokens out: {min_tokens_output} (with {self.slippage*100}% slippage)")

            # Get fee recipients
            fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
            fee_recipient_ata = get_associated_token_address(
                fee_recipient, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )

            # Get creator vault
            coin_creator = Pubkey.from_string(market_data["coin_creator"])
            coin_creator_vault, _ = Pubkey.find_program_address(
                [b"creator_vault", bytes(coin_creator)], PUMP_AMM_PROGRAM_ID
            )
            coin_creator_vault_ata = get_associated_token_address(
                coin_creator_vault, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )

            # Fee config PDA
            fee_config, _ = Pubkey.find_program_address(
                [b"fee_config", bytes(PUMP_AMM_PROGRAM_ID)], PUMP_FEE_PROGRAM
            )

            # Volume accumulator PDAs (required by IDL)
            global_volume_accumulator, _ = Pubkey.find_program_address(
                [b"global_volume_accumulator"], PUMP_AMM_PROGRAM_ID
            )
            user_volume_accumulator, _ = Pubkey.find_program_address(
                [b"user_volume_accumulator", bytes(self.wallet.pubkey)], PUMP_AMM_PROGRAM_ID
            )

            # Build accounts for BUY (SOL -> Token) - ORDER MUST MATCH IDL!
            accounts = [
                AccountMeta(pubkey=market, is_signer=False, is_writable=True),  # 0: pool
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),  # 1: user
                AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),  # 2: global_config
                AccountMeta(pubkey=mint, is_signer=False, is_writable=False),  # 3: base_mint (token)
                AccountMeta(pubkey=SOL_MINT, is_signer=False, is_writable=False),  # 4: quote_mint (SOL)
                AccountMeta(pubkey=user_base_ata, is_signer=False, is_writable=True),  # 5: user_base_token_account
                AccountMeta(pubkey=user_quote_ata, is_signer=False, is_writable=True),  # 6: user_quote_token_account
                AccountMeta(pubkey=pool_base_ata, is_signer=False, is_writable=True),  # 7: pool_base_token_account
                AccountMeta(pubkey=pool_quote_ata, is_signer=False, is_writable=True),  # 8: pool_quote_token_account
                AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=False),  # 9: protocol_fee_recipient
                AccountMeta(pubkey=fee_recipient_ata, is_signer=False, is_writable=True),  # 10: protocol_fee_recipient_token_account
                AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),  # 11: base_token_program (Token2022!)
                AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),  # 12: quote_token_program (SOL)
                AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),  # 13: system_program
                AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False),  # 14: associated_token_program
                AccountMeta(pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False),  # 15: event_authority
                AccountMeta(pubkey=PUMP_AMM_PROGRAM_ID, is_signer=False, is_writable=False),  # 16: program
                AccountMeta(pubkey=coin_creator_vault_ata, is_signer=False, is_writable=True),  # 17: coin_creator_vault_ata
                AccountMeta(pubkey=coin_creator_vault, is_signer=False, is_writable=False),  # 18: coin_creator_vault_authority
                AccountMeta(pubkey=global_volume_accumulator, is_signer=False, is_writable=False),  # 19: global_volume_accumulator
                AccountMeta(pubkey=user_volume_accumulator, is_signer=False, is_writable=True),  # 20: user_volume_accumulator
                AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),  # 21: fee_config
                AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),  # 22: fee_program
            ]

            # Log accounts list for debugging
            logger.info("=== PumpSwap BUY Accounts ===")
            for i, acc in enumerate(accounts):
                logger.info(f"  #{i}: {acc.pubkey} (signer={acc.is_signer}, writable={acc.is_writable})")
            logger.info(f"  Quote token program (SOL): {SYSTEM_TOKEN_PROGRAM}")
            logger.info(f"  Base token program: {token_program_id}")

            # Phase 4: Store vault addresses for real-time price stream
            _vault_context = {
                "pool_base_vault": str(pool_base_ata),
                "pool_quote_vault": str(pool_quote_ata),
                "pool_address": str(market),
                "token_decimals": token_decimals,
            }

            # Build instruction data: discriminator + spendable_quote_in + min_base_amount_out
            # Using buy_exact_quote_in: spend X SOL, get at least Y tokens
            ix_data = BUY_DISCRIMINATOR + struct.pack("<Q", buy_amount_lamports) + struct.pack("<Q", min_tokens_output) + bytes([0])  # track_volume = false

            # Instructions - use idempotent ATA creation like buy.py (always include, won't fail if exists)
            compute_limit_ix = set_compute_unit_limit(200_000)
            compute_price_ix = set_compute_unit_price(self.priority_fee)

            # Create WSOL ATA (idempotent - won't fail if exists)
            create_wsol_ata_ix = create_idempotent_associated_token_account(
                self.wallet.pubkey, self.wallet.pubkey, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )

            # Wrap SOL (transfer + sync) - add 10% buffer for fees like buy.py
            wrap_amount = int(sol_amount * 1.1 * LAMPORTS_PER_SOL)
            transfer_ix = transfer(
                TransferParams(
                    from_pubkey=self.wallet.pubkey,
                    to_pubkey=user_quote_ata,
                    lamports=wrap_amount,
                )
            )

            # Sync native (update wrapped SOL balance) - MUST use SYSTEM_TOKEN_PROGRAM
            sync_ix = sync_native(SyncNativeParams(SYSTEM_TOKEN_PROGRAM, user_quote_ata))

            # Create token ATA (idempotent - won't fail if exists)
            create_token_ata_ix = create_idempotent_associated_token_account(
                self.wallet.pubkey, self.wallet.pubkey, mint, token_program_id
            )

            # Buy instruction
            buy_ix = Instruction(PUMP_AMM_PROGRAM_ID, ix_data, accounts)

            # Order matches buy.py: wsol_ata, transfer, sync, token_ata, buy
            instructions = [
                compute_limit_ix,
                compute_price_ix,
                create_wsol_ata_ix,
                transfer_ix,
                sync_ix,
                create_token_ata_ix,
                buy_ix,
            ]

            logger.info(f"[TX] Total instructions: {len(instructions)} (using idempotent ATA creation)")

            # Send transaction ONCE, then retry confirmation
            try:
                blockhash = await self.client.get_cached_blockhash()
            except RuntimeError:
                blockhash_resp = await rpc_client.get_latest_blockhash()
                blockhash = blockhash_resp.value.blockhash

            msg = Message.new_with_blockhash(
                instructions,
                self.wallet.pubkey,
                blockhash,
            )
            tx = VersionedTransaction(message=msg, keypairs=[self.wallet.keypair])

            logger.info("[TX] Sending PumpSwap BUY transaction...")
            sig = await self._send_tx_parallel(tx, rpc_client)
            logger.info(f"PumpSwap BUY signature: {sig}")
            logger.info(f"https://solscan.io/tx/{sig}")

            # Quick status check - don't block too long on rate limits
            import asyncio
            for attempt in range(min(self.max_retries, 3)):  # Max 3 attempts for status
                try:
                    backoff = 2.0 * (attempt + 1)  # 2, 4, 6 seconds
                    logger.info(f"Checking tx status (attempt {attempt + 1}/3, wait {backoff}s)...")
                    await asyncio.sleep(backoff)

                    tx_response = await rpc_client.get_transaction(
                        Signature.from_string(sig),
                        encoding="json",
                        max_supported_transaction_version=0,
                    )

                    if tx_response.value is None:
                        logger.warning("Transaction not found yet...")
                        continue

                    meta = tx_response.value.transaction.meta
                    if meta and meta.err is not None:
                        error_msg = f"Transaction FAILED on-chain: {meta.err}"
                        logger.error(f"FAILED: {error_msg}")
                        return False, sig, error_msg, 0.0, 0.0

                    logger.info(f"PumpSwap BUY SUCCESS! Got ~{expected_tokens:,.2f} {symbol}")
                    # Schedule callback for position management (TX already verified above)
                    verifier = await get_tx_verifier()
                    await verifier.schedule_verification(
                        signature=sig, mint=str(mint), symbol=symbol, action="buy",
                        token_amount=expected_tokens, price=price,
                        on_success=on_buy_success, on_failure=on_buy_failure,
                        context={"platform": "pumpswap", "bot_name": "fallback_seller", "pre_verified": True, **_vault_context, **(position_config or {})},
                    )
                    return True, sig, None, expected_tokens, price

                except Exception as e:
                    error_str = str(e).lower()
                    # If rate limited, return signature - user can check on solscan
                    if "429" in error_str or "rate" in error_str or "too many" in error_str:
                        logger.warning(f"RPC rate limited - scheduling background verification: {sig}")
                        # Schedule background verification instead of assuming success
                        verifier = await get_tx_verifier()
                        await verifier.schedule_verification(
                            signature=sig, mint=str(mint), symbol=symbol, action="buy",
                            token_amount=expected_tokens, price=price,
                            on_success=on_buy_success, on_failure=on_buy_failure,
                            context={"platform": "pumpswap", "bot_name": "fallback_seller", **_vault_context, **(position_config or {})},
                        )
                        return True, sig, None, expected_tokens, price  # TX sent, verification pending

                    error_msg = str(e) if str(e) else f"{type(e).__name__}"
                    logger.warning(f"Status check failed: {error_msg}")
                    if attempt == 2:  # Last attempt
                        logger.warning(f"Could not verify - scheduling background verification: {sig}")
                        verifier = await get_tx_verifier()
                        await verifier.schedule_verification(
                            signature=sig, mint=str(mint), symbol=symbol, action="buy",
                            token_amount=expected_tokens, price=price,
                            on_success=on_buy_success, on_failure=on_buy_failure,
                            context={"platform": "pumpswap", "bot_name": "fallback_seller", **_vault_context, **(position_config or {})},
                        )
                        return True, sig, None, expected_tokens, price  # TX sent, verification pending

            # If we get here, tx was sent but status unknown - schedule background verification
            logger.warning(f"Status unknown - scheduling background verification: {sig}")
            verifier = await get_tx_verifier()
            await verifier.schedule_verification(
                signature=sig, mint=str(mint), symbol=symbol, action="buy",
                token_amount=expected_tokens, price=price,
                on_success=on_buy_success, on_failure=on_buy_failure,
                context={"platform": "pumpswap", "bot_name": "fallback_seller", **_vault_context, **(position_config or {})},
            )
            return True, sig, None, expected_tokens, price  # TX sent, verification pending

        except Exception as e:
            logger.exception(f"PumpSwap BUY error for {symbol}: {e}")
            return False, None, str(e), 0.0, 0.0

    async def buy_via_pumpfun_direct(
        self,
        mint: Pubkey,
        sol_amount: float,
        symbol: str = "TOKEN",
        position_config: dict | None = None,
        virtual_sol_reserves: int = 0,
        virtual_token_reserves: int = 0,
        whale_token_program: str = "",
        whale_creator_vault: str = "",
        whale_fee_recipient: str = "",
        whale_assoc_bonding_curve: str = "",
    ) -> tuple[bool, str | None, str | None, float, float]:
        """Buy token directly via pump.fun bonding curve program.
        
        Bypasses Jupiter — builds TX from on-chain bonding curve state.
        Only works for tokens still on bonding curve (not migrated).
        ONE RPC call: getAccountInfo for bonding curve (~20ms).
        All accounts derived via PDA (0ms).
        
        Returns:
            Tuple of (success, tx_signature, error_message, token_amount, price)
        """
        import time as _time
        t_start = _time.monotonic()
        
        try:
            from platforms.pumpfun.address_provider import (
                PumpFunAddresses, PumpFunAddressProvider,
            )
            from solders.compute_budget import (
                set_compute_unit_limit, set_compute_unit_price,
            )
            from spl.token.instructions import (
                create_idempotent_associated_token_account,
            )
            import base64 as b64mod
            
            logger.info(
                f"[PUMPFUN-DIRECT] BUY {symbol} with {sol_amount} SOL"
            )
            
            rpc_client = await self._get_rpc_client()
            address_provider = PumpFunAddressProvider()
            
            # --- PDA derivation (0ms, no RPC) ---
            bonding_curve = address_provider.derive_pool_address(mint)
            
            # --- Get bonding curve reserves ---
            # ZERO-RPC path: use reserves from whale TX TradeEvent
            # RPC fallback: getAccountInfo if reserves not available
            vt_reserves = virtual_token_reserves
            vs_reserves = virtual_sol_reserves
            
            if vt_reserves > 0 and vs_reserves > 0:
                logger.info(
                    f"[PUMPFUN-DIRECT] ZERO-RPC: using reserves from TX "
                    f"(vsr={vs_reserves}, vtr={vt_reserves})"
                )
                # MEDIUM #6: Check complete flag — curve may have migrated
                # between whale TX and our TX. Fast check (~15ms), skip on timeout.
                try:
                    _cbc_resp = await asyncio.wait_for(
                        rpc_client.get_account_info(bonding_curve, encoding="base64"),
                        timeout=0.10,
                    )
                    if _cbc_resp and _cbc_resp.value and _cbc_resp.value.data:
                        import base64 as _b64
                        _cbc_raw = _cbc_resp.value.data
                        _cbc_bytes = _b64.b64decode(_cbc_raw[0]) if isinstance(_cbc_raw, list) else bytes(_cbc_raw)
                        if len(_cbc_bytes) > 48 and _cbc_bytes[48] != 0:
                            logger.info("[PUMPFUN-DIRECT] ZERO-RPC: complete=True (migrated since whale TX) — falling through")
                            return False, None, "BC complete", 0.0, 0.0
                except asyncio.TimeoutError:
                    logger.info("[PUMPFUN-DIRECT] ZERO-RPC: complete check timed out — proceeding optimistically")
                except Exception as _cc_err:
                    logger.info(f"[PUMPFUN-DIRECT] ZERO-RPC: complete check error: {_cc_err} — proceeding")
            else:
                # Fallback: fetch from RPC (~20ms)
                logger.info("[PUMPFUN-DIRECT] No TX reserves, fetching via RPC...")
                try:
                    bc_resp = await asyncio.wait_for(
                        rpc_client.get_account_info(
                            bonding_curve, encoding="base64"
                        ),
                        timeout=0.15,  # S12: was 0.5s
                    )
                except asyncio.TimeoutError:
                    elapsed = (_time.monotonic() - t_start) * 1000
                    logger.warning(
                        f"[PUMPFUN-DIRECT] RPC timeout ({elapsed:.0f}ms)"
                    )
                    return False, None, "BC RPC timeout", 0.0, 0.0
                except Exception as e:
                    return False, None, f"BC RPC error: {e}", 0.0, 0.0
                
                if not bc_resp or not bc_resp.value:
                    elapsed = (_time.monotonic() - t_start) * 1000
                    logger.info(
                        f"[PUMPFUN-DIRECT] No bonding curve ({elapsed:.0f}ms)"
                    )
                    return False, None, "BC not found", 0.0, 0.0
                
                bc_data = bc_resp.value.data
                if isinstance(bc_data, (tuple, list)):
                    bc_data = b64mod.b64decode(bc_data[0])
                elif isinstance(bc_data, str):
                    bc_data = b64mod.b64decode(bc_data)
                
                t_rpc = _time.monotonic()
                logger.info(
                    f"[PUMPFUN-DIRECT] BC fetched {(t_rpc-t_start)*1000:.0f}ms "
                    f"({len(bc_data)} bytes)"
                )
                
                if len(bc_data) < 49:
                    return (
                        False, None,
                        f"BC data too short: {len(bc_data)}",
                        0.0, 0.0,
                    )
                
                vt_reserves = struct.unpack('<Q', bc_data[8:16])[0]
                vs_reserves = struct.unpack('<Q', bc_data[16:24])[0]
                complete = bc_data[48] != 0
                
                if complete:
                    logger.info("[PUMPFUN-DIRECT] BC complete (migrated)")
                    return False, None, "BC complete", 0.0, 0.0
                
                if vt_reserves <= 0 or vs_reserves <= 0:
                    return (
                        False, None,
                        f"Bad reserves vt={vt_reserves} vs={vs_reserves}",
                        0.0, 0.0,
                    )
            
            # --- Calculate buy amounts ---
            sol_lamports = int(sol_amount * LAMPORTS_PER_SOL)
            
            # xy=k formula: tokens_out = (sol_in * vt) / (vs + sol_in)
            tokens_out_raw = (
                (sol_lamports * vt_reserves) // (vs_reserves + sol_lamports)
            )
            if tokens_out_raw <= 0:
                return False, None, "Zero tokens output", 0.0, 0.0
            
            # Slippage: max_sol_cost = sol_lamports * (1 + slippage)
            max_sol_cost = int(sol_lamports * (1.0 + self.slippage))
            # min tokens = tokens * (1 - slippage) — for the instruction
            min_tokens = int(tokens_out_raw * (1.0 - self.slippage))
            
            _actual_decimals = await get_token_decimals(rpc_client, mint)
            tokens_decimal = tokens_out_raw / (10 ** _actual_decimals)
            price = sol_amount / tokens_decimal if tokens_decimal > 0 else 0
            
            logger.info(
                f"[PUMPFUN-DIRECT] Expected: {tokens_decimal:,.2f} {symbol} "
                f"@ {price:.10f} SOL (slippage {self.slippage*100:.0f}%)"
            )
            
            mint_str = str(mint)

            # --- S14: Detect token program from whale TX accounts ---
            # Priority: whale TX account[8] > bc_data owner > default Token-2022
            if whale_token_program:
                # Best: use exact token program from whale TX
                token_program_id = Pubkey.from_string(whale_token_program)
                logger.info(f"[PUMPFUN-DIRECT] S14 token_program from whale TX: {whale_token_program[:16]}...")
            else:
                # Default: Token-2022 (all new pump.fun tokens since Nov 2025)
                token_program_id = TOKEN_2022_PROGRAM
                logger.info(f"[PUMPFUN-DIRECT] S14 token_program DEFAULT: Token-2022")
            
            # --- S14: Creator vault from whale TX, bc_data, or RPC fallback ---
            if whale_creator_vault:
                creator_vault = Pubkey.from_string(whale_creator_vault)
                logger.info(f"[PUMPFUN-DIRECT] S14 creator_vault from whale TX: {whale_creator_vault[:16]}...")
            else:
                # Try bc_data first (available in RPC path)
                creator_pubkey = None
                _bc_data_local = locals().get("bc_data", b"")
                if isinstance(_bc_data_local, bytes) and len(_bc_data_local) >= 81:
                    try:
                        creator_bytes = _bc_data_local[49:81]
                        creator_pubkey = Pubkey.from_bytes(creator_bytes)
                    except Exception:
                        pass
                # RPC fallback: fetch BC to get creator (adds ~45ms)
                if not creator_pubkey:
                    try:
                        _bc_resp = await asyncio.wait_for(
                            rpc_client.get_account_info(bonding_curve, encoding="base64"),
                            timeout=0.2,
                        )
                        if _bc_resp and _bc_resp.value:
                            _bc_raw = _bc_resp.value.data
                            if isinstance(_bc_raw, (tuple, list)):
                                _bc_raw = b64mod.b64decode(_bc_raw[0])
                            elif isinstance(_bc_raw, str):
                                _bc_raw = b64mod.b64decode(_bc_raw)
                            if len(_bc_raw) >= 81:
                                creator_pubkey = Pubkey.from_bytes(_bc_raw[49:81])
                                logger.info(f"[PUMPFUN-DIRECT] S14 creator from RPC: {str(creator_pubkey)[:16]}...")
                    except Exception as _e:
                        logger.warning(f"[PUMPFUN-DIRECT] S14 creator RPC failed: {_e}")
                if creator_pubkey:
                    creator_vault = address_provider.derive_creator_vault(creator_pubkey)
                    logger.info(f"[PUMPFUN-DIRECT] S14 creator_vault derived: {str(creator_vault)[:16]}...")
                else:
                    creator_vault = PumpFunAddresses.FEE
                    logger.warning(f"[PUMPFUN-DIRECT] S14 creator_vault FALLBACK to FEE (will likely fail)")
            # --- S14: Fee recipient from whale TX or default ---
            if whale_fee_recipient:
                fee_recipient = Pubkey.from_string(whale_fee_recipient)
                logger.info(f"[PUMPFUN-DIRECT] S14 fee_recipient from whale TX: {whale_fee_recipient[:16]}...")
            else:
                fee_recipient = PumpFunAddresses.FEE
            
            # --- S14: Associated bonding curve from whale TX or derive ---
            if whale_assoc_bonding_curve:
                assoc_bc = Pubkey.from_string(whale_assoc_bonding_curve)
                logger.info(f"[PUMPFUN-DIRECT] S14 assoc_bc from whale TX: {whale_assoc_bonding_curve[:16]}...")
            else:
                assoc_bc = address_provider.derive_associated_bonding_curve(
                    mint, bonding_curve, token_program_id
                )
            
            # --- Derive user ATA with correct token program ---
            user_ata = address_provider.derive_user_token_account(
                self.wallet.pubkey, mint, token_program_id
            )
            
            # Volume accumulators + fee config
            global_vol_acc = PumpFunAddresses.find_global_volume_accumulator()
            user_vol_acc = PumpFunAddresses.find_user_volume_accumulator(
                self.wallet.pubkey
            )
            fee_config = PumpFunAddresses.find_fee_config()
            
            # --- Build instruction ---
            # Buy discriminator from IDL: sha256("global:buy")[:8]
            buy_discriminator = bytes([102, 6, 61, 18, 1, 218, 235, 234])
            
            # Data: discriminator + amount(tokens) + max_sol_cost
            # + track_volume OptionBool [1,1] = Some(true)
            ix_data = (
                buy_discriminator
                + struct.pack("<Q", min_tokens)
                + struct.pack("<Q", max_sol_cost)
                + bytes([1, 1])  # track_volume = Some(true)
            )
            
            buy_accounts = [
                AccountMeta(PumpFunAddresses.GLOBAL, False, False),
                AccountMeta(fee_recipient, False, True),
                AccountMeta(mint, False, False),
                AccountMeta(bonding_curve, False, True),
                AccountMeta(assoc_bc, False, True),
                AccountMeta(user_ata, False, True),
                AccountMeta(self.wallet.pubkey, True, True),
                AccountMeta(SYSTEM_PROGRAM, False, False),
                AccountMeta(token_program_id, False, False),
                AccountMeta(creator_vault, False, True),
                AccountMeta(PumpFunAddresses.EVENT_AUTHORITY, False, False),
                AccountMeta(PumpFunAddresses.PROGRAM, False, False),
                AccountMeta(global_vol_acc, False, False),
                AccountMeta(user_vol_acc, False, True),
                AccountMeta(fee_config, False, False),
                AccountMeta(PumpFunAddresses.FEE_PROGRAM, False, False),
            ]
            
            buy_ix = Instruction(
                PumpFunAddresses.PROGRAM, ix_data, buy_accounts
            )
            
            # ATA creation (idempotent — safe if already exists)
            create_ata_ix = create_idempotent_associated_token_account(
                self.wallet.pubkey,
                self.wallet.pubkey,
                mint,
                token_program_id,
            )
            
            # Compute budget
            cu_limit_ix = set_compute_unit_limit(150_000)
            cu_price_ix = set_compute_unit_price(self.priority_fee)
            
            instructions = [
                cu_limit_ix, cu_price_ix, create_ata_ix, buy_ix
            ]
            
            # --- Build and sign TX ---
            from solders.message import Message
            
            try:
                blockhash = await self.client.get_cached_blockhash()
            except (RuntimeError, AttributeError):
                bh_resp = await rpc_client.get_latest_blockhash()
                blockhash = bh_resp.value.blockhash
            
            msg = Message.new_with_blockhash(
                instructions, self.wallet.pubkey, blockhash
            )
            tx = VersionedTransaction(msg, [self.wallet.keypair])
            
            t_build = _time.monotonic()
            logger.info(
                f"[PUMPFUN-DIRECT] TX built {(t_build-t_start)*1000:.0f}ms"
            )
            
            # --- Send TX ---
            sig = await self._send_tx_parallel(tx, rpc_client)
            
            t_send = _time.monotonic()
            total_ms = (t_send - t_start) * 1000
            logger.warning(
                f"[PUMPFUN-DIRECT] TX SENT in {total_ms:.0f}ms: {sig}"
            )
            logger.warning(f"[PUMPFUN-DIRECT] https://solscan.io/tx/{sig}")
            
            # --- Schedule verification (fire & forget) ---
            from core.tx_verifier import get_tx_verifier
            from core.tx_callbacks import on_buy_success, on_buy_failure
            
            verifier = await get_tx_verifier()
            await verifier.schedule_verification(
                signature=sig,
                mint=mint_str,
                symbol=symbol,
                action="buy",
                token_amount=tokens_decimal,
                price=price,
                on_success=on_buy_success,
                on_failure=on_buy_failure,
                context={
                    "platform": "pump_fun_direct",
                    "bot_name": "fallback_seller",
                    "buy_amount": sol_amount,
                    "wallet_pubkey": str(self.wallet.pubkey),
                    "bonding_curve": str(bonding_curve),
                    **(position_config or {}),
                },
            )
            
            return True, sig, None, tokens_decimal, price
        
        except Exception as e:
            logger.warning(f"[PUMPFUN-DIRECT] Error: {e}")
            return False, None, str(e), 0.0, 0.0


    async def buy_via_jupiter(
        self,
        mint: Pubkey,
        sol_amount: float,
        symbol: str = "TOKEN",
        position_config: dict | None = None,  # TSL/TP/SL parameters for callback
        prefetched_quote: dict | None = None,  # Phase 3.3: Pre-fetched quote
    ) -> tuple[bool, str | None, str | None, float, float]:
        """Buy token via Jupiter aggregator - works for any token with liquidity.

        Args:
            mint: Token mint address
            sol_amount: Amount of SOL to spend
            symbol: Token symbol for logging

        Returns:
            Tuple of (success, tx_signature, error_message)
        """
        import base64

        try:
            logger.info(f"[JUPITER] Jupiter BUY for {symbol} with {sol_amount} SOL...")

            buy_amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)
            slippage_bps = int(self.slippage * 10000)


            # Get dynamic decimals for this token (CRITICAL for non-standard tokens!)
            rpc_client = await self._get_rpc_client()
            token_decimals = await get_token_decimals(rpc_client, mint)
            logger.info(f"[JUPITER] Token {symbol} has {token_decimals} decimals")
            # Use Swap API directly (Ultra disabled)
            if False:  # Ultra disabled
                jupiter_url = "https://api.jup.ag/ultra/v1/order"
                headers = {"x-api-key": self.jupiter_api_key}
                logger.info("[JUPITER] Using Jupiter Ultra API")
            else:
                jupiter_quote_url = "https://api.jup.ag/swap/v1/quote"
                jupiter_swap_url = "https://api.jup.ag/swap/v1/swap"
                headers = {"x-api-key": self.jupiter_api_key} if self.jupiter_api_key else {}
                logger.info(f"[JUPITER] Using Jupiter Swap API (key: {bool(self.jupiter_api_key)})")

            async with aiohttp.ClientSession() as session:
                rpc_client = await self._get_rpc_client()

                if False:  # Ultra disabled
                    # Jupiter Ultra API - GET /order (not POST!)
                    # Docs: https://dev.jup.ag/docs/ultra/get-order
                    order_params = {
                        "inputMint": str(SOL_MINT),
                        "outputMint": str(mint),
                        "amount": str(buy_amount_lamports),
                        "taker": str(self.wallet.pubkey),
                    }

                    for attempt in range(self.max_retries):
                        try:
                            async with session.get(
                                jupiter_url,
                                params=order_params,
                                headers=headers
                            ) as resp:
                                if resp.status != 200:
                                    error_text = await resp.text()
                                    logger.warning(f"Jupiter Ultra order failed: {error_text}")
                                    continue
                                order_data = await resp.json()

                            # Ultra returns transaction directly
                            tx_base64 = order_data.get("transaction")
                            if not tx_base64:
                                logger.warning("No transaction in Jupiter Ultra response")
                                continue

                            out_amount = int(order_data.get("outAmount", 0))
                            out_amount_tokens = out_amount / (10 ** token_decimals)
                            logger.info(f"[JUPITER] Jupiter Ultra expected: ~{out_amount_tokens:,.2f} {symbol}")

                            tx_bytes = base64.b64decode(tx_base64)
                            tx = VersionedTransaction.from_bytes(tx_bytes)
                            signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])

                            logger.info(f"[TX] Jupiter Ultra BUY attempt {attempt + 1}/{self.max_retries}...")
                            sig = await self._send_tx_parallel(signed_tx, rpc_client)

                            logger.info(f"[SIG] Jupiter Ultra BUY signature: {sig}")
                            real_price = sol_amount / out_amount_tokens if out_amount_tokens > 0 else 0
                            
                            # CRITICAL: Verify TX actually succeeded on chain (not just sent!)
                            tx_confirmed = False
                            for verify_try in range(10):
                                await asyncio.sleep(2)
                                try:
                                    verify_resp = await rpc_client.get_transaction(
                                        Signature.from_string(sig),
                                        encoding="jsonParsed",
                                        max_supported_transaction_version=0
                                    )
                                    if verify_resp.value:
                                        tx_meta = verify_resp.value.transaction.meta if verify_resp.value.transaction else None
                                        if tx_meta and tx_meta.err:
                                            logger.error(f"[FAIL] Jupiter Ultra BUY TX FAILED ON CHAIN: {sig} - {tx_meta.err}")
                                            break  # TX failed, try next attempt
                                        elif tx_meta and not tx_meta.err:
                                            tx_confirmed = True
                                            break  # TX confirmed successful
                                except Exception as ve:
                                    logger.debug(f"Verify attempt {verify_try+1}: {ve}")
                                    continue
                            
                            if not tx_confirmed:
                                logger.error(f"[FAIL] Jupiter Ultra BUY TX NOT CONFIRMED: {sig}")
                                continue  # Try next attempt
                            
                            logger.info(f"[OK] Jupiter Ultra BUY SUCCESS (VERIFIED): {out_amount_tokens:,.2f} {symbol} @ {real_price:.10f} SOL")
                            # Schedule callback for position management
                            verifier = await get_tx_verifier()
                            await verifier.schedule_verification(
                                signature=sig, mint=str(mint), symbol=symbol, action="buy",
                                token_amount=out_amount_tokens, price=real_price,
                                on_success=on_buy_success, on_failure=on_buy_failure,
                                context={"platform": "jupiter_ultra", "bot_name": "fallback_seller", "pre_verified": True, "buy_amount": sol_amount, "wallet_pubkey": str(self.wallet.pubkey), **(position_config or {})},
                            )
                            return True, sig, None, out_amount_tokens, real_price

                        except Exception as e:
                            error_msg = str(e) if str(e) else f"{type(e).__name__} (no message)"
                            logger.warning(f"Jupiter Ultra BUY attempt {attempt + 1} failed: {error_msg}")
                            if attempt == self.max_retries - 1:
                                return False, None, error_msg, 0.0, 0.0

                    # FALLBACK TO LITE API when Ultra fails (404 for Meteora/BAGS tokens)
                    logger.warning("[JUPITER] Ultra API failed, trying Lite API fallback...")
                    
                    # Switch to Lite API
                    jupiter_quote_url = "https://api.jup.ag/swap/v1/quote"
                    jupiter_swap_url = "https://api.jup.ag/swap/v1/swap"
                    
                    quote_params = {
                        "inputMint": str(SOL_MINT),
                        "outputMint": str(mint),
                        "amount": str(buy_amount_lamports),
                        "restrictIntermediateTokens": "true",  # Safer routes
                        "maxAccounts": "64",  # Limit accounts to avoid complex routes
                        "slippageBps": str(slippage_bps),  # From config
                    }
                    
                    try:
                        async with session.get(jupiter_quote_url, params=quote_params, headers=headers) as resp:
                            if resp.status != 200:
                                error_text = await resp.text()
                                return False, None, f"Jupiter Lite quote also failed: {error_text}", 0.0, 0.0
                            quote = await resp.json()
                        
                        out_amount = int(quote.get("outAmount", 0))
                        out_amount_tokens = out_amount / (10 ** token_decimals)
                        logger.info(f"[JUPITER] Lite API expected: ~{out_amount_tokens:,.2f} {symbol}")
                        
                        swap_body = {
                            "quoteResponse": quote,
                            "userPublicKey": str(self.wallet.pubkey),
                            "wrapAndUnwrapSol": True,
                            "prioritizationFeeLamports": self.priority_fee,
                            "dynamicComputeUnitLimit": True,  # Better CU estimation
                            # "dynamicSlippage": True,  # DISABLED - use fixed slippage  # Let Jupiter calculate optimal slippage
                            "asLegacyTransaction": False,  # Use versioned TX for Token2022
                        }
                        
                        for lite_attempt in range(self.max_retries):
                            try:
                                async with session.post(jupiter_swap_url, json=swap_body, headers=headers) as resp:
                                    if resp.status != 200:
                                        error_text = await resp.text()
                                        logger.warning(f"Jupiter Lite swap failed: {error_text}")
                                        continue
                                    swap_data = await resp.json()
                                
                                swap_tx_base64 = swap_data.get("swapTransaction")
                                if not swap_tx_base64:
                                    continue
                                
                                tx_bytes = base64.b64decode(swap_tx_base64)
                                tx = VersionedTransaction.from_bytes(tx_bytes)
                                signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])
                                
                                logger.info(f"[TX] Jupiter Lite BUY attempt {lite_attempt + 1}/{self.max_retries}...")
                                sig = await self._send_tx_parallel(signed_tx, rpc_client)
                                
                                real_price = sol_amount / out_amount_tokens if out_amount_tokens > 0 else 0
                                
                                # CRITICAL: Verify transaction actually succeeded (not just sent)
                                await asyncio.sleep(5)  # Wait longer for confirmation
                                try:
                                    from solders.signature import Signature
                                    tx_resp = await rpc_client.get_transaction(
                                        Signature.from_string(sig),
                                        encoding="jsonParsed",
                                        max_supported_transaction_version=0
                                    )
                                    if tx_resp.value and tx_resp.value.transaction:
                                        meta = tx_resp.value.transaction.meta
                                        if meta and meta.err:
                                            logger.error(f"[FAIL] Jupiter Lite BUY TX FAILED: {sig} - error: {meta.err}")
                                            continue  # Retry next attempt
                                except Exception as verify_err:
                                    logger.warning(f"[WARN] Could not verify tx {sig[:20]}...: {verify_err}")
                                    continue  # CRITICAL: retry if verify failed
                                
                                # VERIFY TX ON CHAIN BEFORE DECLARING SUCCESS
                                tx_confirmed = False
                                for verify_try in range(10):
                                    await asyncio.sleep(2)
                                    try:
                                        from solders.signature import Signature
                                        verify_resp = await rpc_client.get_transaction(
                                            Signature.from_string(sig),
                                            encoding="jsonParsed",
                                            max_supported_transaction_version=0
                                        )
                                        if verify_resp.value:
                                            tx_meta = verify_resp.value.transaction.meta if verify_resp.value.transaction else None
                                            if tx_meta and tx_meta.err:
                                                logger.error(f"[FAIL] Jupiter BUY TX FAILED ON CHAIN: {sig} - {tx_meta.err}")
                                                break  # TX failed, exit verify loop
                                            elif tx_meta and not tx_meta.err:
                                                tx_confirmed = True
                                                break  # TX confirmed successful
                                    except Exception as ve:
                                        logger.debug(f"Verify attempt {verify_try+1}: {ve}")
                                        continue
                                
                                if not tx_confirmed:
                                    logger.error(f"[FAIL] Jupiter BUY TX NOT CONFIRMED: {sig}")
                                    continue  # Try next attempt
                                
                                logger.info(f"[OK] Jupiter Lite BUY SUCCESS (VERIFIED): {sig} - {out_amount_tokens:,.2f} tokens @ {real_price:.10f} SOL")
                                # Schedule callback for position management
                                verifier = await get_tx_verifier()
                                await verifier.schedule_verification(
                                    signature=sig, mint=str(mint), symbol=symbol, action="buy",
                                    token_amount=out_amount_tokens, price=real_price,
                                    on_success=on_buy_success, on_failure=on_buy_failure,
                                    context={"platform": "jupiter_lite", "bot_name": "fallback_seller", "pre_verified": True, "buy_amount": sol_amount, "wallet_pubkey": str(self.wallet.pubkey), **(position_config or {})},
                                )
                                return True, sig, None, out_amount_tokens, real_price
                                
                            except Exception as e:
                                logger.warning(f"Jupiter Lite attempt {lite_attempt + 1} failed: {e}")
                        
                        return False, None, "All Jupiter Lite BUY attempts also failed", 0.0, 0.0
                        
                    except Exception as e:
                        return False, None, f"Jupiter Lite fallback failed: {e}", 0.0, 0.0

                else:
                    # Fallback to v6 API
                    quote_params = {
                        "inputMint": str(SOL_MINT),
                        "outputMint": str(mint),
                        "amount": str(buy_amount_lamports),
                        "restrictIntermediateTokens": "true",  # Safer routes
                        "maxAccounts": "64",  # Limit accounts to avoid complex routes
                        "slippageBps": str(slippage_bps),  # From config
                    }

                    # Phase 3.3: Use prefetched quote if available and fresh
                    _pf_quote = (position_config or {}).get("prefetched_quote")
                    if _pf_quote and _pf_quote.get("outAmount"):
                        import time as _time
                        _pf_age = _time.monotonic() - _pf_quote.get("_prefetch_time", 0)
                        if _pf_age < 5.0:
                            quote = _pf_quote
                            logger.info(f"[JUPITER] Using pre-fetched quote ({_pf_age:.1f}s old)")
                        else:
                            logger.info(f"[JUPITER] Pre-fetched quote too old ({_pf_age:.1f}s), fetching new")
                            _pf_quote = None

                    if not _pf_quote or not _pf_quote.get("outAmount"):
                        async with session.get(jupiter_quote_url, params=quote_params, headers=headers) as resp:
                            if resp.status != 200:
                                error_text = await resp.text()
                                return False, None, f"Jupiter quote failed: {error_text}", 0.0, 0.0
                            quote = await resp.json()

                    out_amount = int(quote.get("outAmount", 0))
                    out_amount_tokens = out_amount / (10 ** token_decimals)
                    logger.info(f"[JUPITER] Jupiter expected: ~{out_amount_tokens:,.2f} {symbol}")

                    swap_body = {
                        "quoteResponse": quote,
                        "userPublicKey": str(self.wallet.pubkey),
                        "wrapAndUnwrapSol": True,
                        "prioritizationFeeLamports": self.priority_fee,
                        "dynamicComputeUnitLimit": True,  # Better CU estimation
                        # "dynamicSlippage": True,  # DISABLED - use fixed slippage  # Let Jupiter calculate optimal slippage
                        "asLegacyTransaction": False,  # Use versioned TX for Token2022
                    }

                    for attempt in range(self.max_retries):
                        try:
                            async with session.post(jupiter_swap_url, json=swap_body, headers=headers) as resp:
                                if resp.status != 200:
                                    error_text = await resp.text()
                                    logger.warning(f"Jupiter swap request failed: {error_text}")
                                    continue
                                swap_data = await resp.json()

                            swap_tx_base64 = swap_data.get("swapTransaction")
                            if not swap_tx_base64:
                                return False, None, "No swap transaction in Jupiter response", 0.0, 0.0

                            tx_bytes = base64.b64decode(swap_tx_base64)
                            tx = VersionedTransaction.from_bytes(tx_bytes)
                            signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])

                            logger.info(f"[TX] Jupiter BUY attempt {attempt + 1}/{self.max_retries}...")
                            sig = await self._send_tx_parallel(signed_tx, rpc_client)

                            logger.info(f"[SIG] Jupiter BUY signature: {sig}")

                            # FIRE & FORGET: Schedule background verification (non-blocking)
                            real_price = sol_amount / out_amount_tokens if out_amount_tokens > 0 else 0
                            logger.info(f"[TX SENT] Jupiter BUY: {sig}")
                            
                            # Schedule async verification
                            verifier = await get_tx_verifier()
                            await verifier.schedule_verification(
                                signature=sig,
                                mint=str(mint),
                                symbol=symbol,
                                action="buy",
                                token_amount=out_amount_tokens,
                                price=real_price,
                                on_success=on_buy_success,
                                on_failure=on_buy_failure,
                                context={"platform": "jupiter", "bot_name": "fallback_seller", "buy_amount": sol_amount, "wallet_pubkey": str(self.wallet.pubkey), **(position_config or {})},
                            )
                            
                            # Return immediately - position added by callback on success
                            return True, sig, None, out_amount_tokens, real_price

                        except Exception as e:
                            error_msg = str(e) if str(e) else f"{type(e).__name__} (no message)"
                            logger.warning(f"Jupiter BUY attempt {attempt + 1} failed: {error_msg}")
                            if attempt == self.max_retries - 1:
                                return False, None, error_msg, 0.0, 0.0

                    return False, None, "All Jupiter BUY attempts failed", 0.0, 0.0

        except Exception as e:
            return False, None, str(e), 0.0, 0.0

    async def sell(
        self,
        mint: Pubkey,
        token_amount: float,
        symbol: str = "TOKEN",
    ) -> tuple[bool, str | None, str | None]:
        """Try to sell via PumpPortal FIRST (fastest for pump.fun), then PumpSwap, then Jupiter."""
        logger.info(f"[FALLBACK] Attempting fallback sell for {symbol} ({mint})")

        # Check if pump.fun token (ends with 'pump')
        mint_str = str(mint)
        is_pumpfun = mint_str.endswith("pump")

        if is_pumpfun:
            # PumpPortal FIRST for pump.fun tokens (fastest, most reliable)
            logger.info(f"[FALLBACK] Pump.fun token detected, trying PumpPortal first...")
            success, sig, error = await self._sell_via_pumpportal(mint, token_amount, symbol)
            if success:
                return success, sig, None
            logger.info(f"PumpPortal failed: {error}, trying PumpSwap...")

            # PumpSwap second (for migrated tokens)
            success, sig, error = await self._sell_via_pumpswap(mint, token_amount, symbol)
            if success:
                return success, sig, None
            logger.info(f"PumpSwap failed: {error}, trying Jupiter...")
        else:
                        # [edit:s12] Jupiter-first sell routing for non-pump tokens
# Non pump.fun tokens - Jupiter FIRST (universal, handles any DEX/decimals)
            logger.info(f"[FALLBACK] Non-pump token {symbol}, using Jupiter directly")

        # Jupiter as PRIMARY sell (works for ALL tokens)
        success, sig, error = await self._sell_via_jupiter(mint, token_amount, symbol)
        if success:
            return success, sig, None

        # PumpSwap as last resort fallback
        if not is_pumpfun:
            logger.info(f"Jupiter sell failed: {error}, trying PumpSwap fallback...")
            success, sig, error = await self._sell_via_pumpswap(mint, token_amount, symbol)
        return success, sig, error

    async def _get_token_program_id(self, mint: Pubkey) -> Pubkey:
        """Determine if mint uses TokenProgram or Token2022Program."""
        rpc_client = await self._get_rpc_client()
        mint_info = await rpc_client.get_account_info(mint)
        if not mint_info.value:
            raise ValueError(f"Could not fetch mint info for {mint}")
        owner = mint_info.value.owner
        if owner == SYSTEM_TOKEN_PROGRAM:
            return SYSTEM_TOKEN_PROGRAM
        elif owner == TOKEN_2022_PROGRAM:
            return TOKEN_2022_PROGRAM
        raise ValueError(f"Unknown token program: {owner}")

    async def _get_token_balance(self, ata: Pubkey) -> int:
        """Get token balance in raw units - works with both TokenProgram and Token2022Program.
        
        Token2022 accounts may not appear in getTokenAccountsByOwner with TokenProgram filter.
        This function queries the ATA directly, which works for both program types.
        """
        try:
            rpc_client = await self._get_rpc_client()
            response = await rpc_client.get_token_account_balance(ata)
            return int(response.value.amount) if response.value else 0
        except Exception as e:
            # If ATA doesn't exist or is empty, return 0
            logger.warning(f"[BALANCE] Could not get balance for {ata}: {e}")
            return 0

    async def _sell_via_pumpswap(
        self,
        mint: Pubkey,
        token_amount: float,
        symbol: str,
    ) -> tuple[bool, str | None, str | None]:
        """Sell via PumpSwap AMM."""
        try:
            rpc_client = await self._get_rpc_client()
            
            # Get dynamic decimals for this token
            token_decimals = await get_token_decimals(rpc_client, mint)

            # Find market
            filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=bytes(mint))]
            response = await rpc_client.get_program_accounts(
                PUMP_AMM_PROGRAM_ID, encoding="base64", filters=filters
            )

            if not response.value:
                return False, None, "PumpSwap market not found"

            market = response.value[0].pubkey
            logger.info(f"[MARKET] Found PumpSwap market: {market}")

            # Get market data
            market_response = await rpc_client.get_account_info(market, encoding="base64")
            data = market_response.value.data
            market_data = self._parse_market_data(data)

            token_program_id = await self._get_token_program_id(mint)

            # Get user token accounts
            user_base_ata = get_associated_token_address(
                self.wallet.pubkey, mint, token_program_id
            )
            user_quote_ata = get_associated_token_address(
                self.wallet.pubkey, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )

            # Calculate sell amount
            # Get token decimals
            rpc_client = await self._get_rpc_client()
            token_decimals = await get_token_decimals(rpc_client, mint)
            sell_amount = int(token_amount * 10**token_decimals)

            # Get pool accounts
            pool_base_ata = Pubkey.from_string(market_data["pool_base_token_account"])
            pool_quote_ata = Pubkey.from_string(market_data["pool_quote_token_account"])

            # Get pool balances - use batch call to save RPC requests (use rpc_client not self.client!)
            response = await rpc_client.get_multiple_accounts([pool_base_ata, pool_quote_ata], encoding="base64")
            accounts = response.value if response.value else []

            if len(accounts) < 2 or not accounts[0] or not accounts[1]:
                return False, None, "Pool vault accounts not found"

            # Parse token account data
            base_data = accounts[0].data
            quote_data = accounts[1].data

            if isinstance(base_data, tuple):
                import base64 as b64
                base_data = b64.b64decode(base_data[0])
            if isinstance(quote_data, tuple):
                import base64 as b64
                quote_data = b64.b64decode(quote_data[0])

            base_amount_raw = struct.unpack("<Q", base_data[64:72])[0]
            quote_amount_raw = struct.unpack("<Q", quote_data[64:72])[0]

            base_amount = base_amount_raw / (10 ** token_decimals)
            quote_amount = quote_amount_raw / (10 ** 9)
            price = quote_amount / base_amount

            sol_value = token_amount * price
            min_sol_output = int(sol_value * (1 - self.slippage) * LAMPORTS_PER_SOL)

            logger.info(f"[SELL] PumpSwap price: {price:.10f} SOL, expected: ~{sol_value:.6f} SOL")

            # Get fee recipients
            fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
            fee_recipient_ata = get_associated_token_address(
                fee_recipient, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )

            # Get creator vault
            coin_creator = Pubkey.from_string(market_data["coin_creator"])
            coin_creator_vault, _ = Pubkey.find_program_address(
                [b"creator_vault", bytes(coin_creator)], PUMP_AMM_PROGRAM_ID
            )
            coin_creator_vault_ata = get_associated_token_address(
                coin_creator_vault, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )

            # Fee config PDA
            fee_config, _ = Pubkey.find_program_address(
                [b"fee_config", bytes(PUMP_AMM_PROGRAM_ID)], PUMP_FEE_PROGRAM
            )

            # Build accounts
            accounts = [
                AccountMeta(pubkey=market, is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),
                AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SOL_MINT, is_signer=False, is_writable=False),
                AccountMeta(pubkey=user_base_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_quote_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=pool_base_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=pool_quote_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=False),
                AccountMeta(pubkey=fee_recipient_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_AMM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=coin_creator_vault_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=coin_creator_vault, is_signer=False, is_writable=False),
                AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
            ]

            # Build instruction
            ix_data = SELL_DISCRIMINATOR + struct.pack("<Q", sell_amount) + struct.pack("<Q", min_sol_output)

            # Create ATA instruction (idempotent)
            create_ata_accounts = [
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=user_quote_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=SOL_MINT, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
            ]
            create_ata_ix = Instruction(ASSOCIATED_TOKEN_PROGRAM, bytes([1]), create_ata_accounts)

            compute_limit_ix = set_compute_unit_limit(150_000)
            compute_price_ix = set_compute_unit_price(self.priority_fee)
            sell_ix = Instruction(PUMP_AMM_PROGRAM_ID, ix_data, accounts)

            # Send transaction ONCE, then retry confirmation
            try:
                blockhash = await self.client.get_cached_blockhash()
            except RuntimeError:
                blockhash_resp = await rpc_client.get_latest_blockhash()
                blockhash = blockhash_resp.value.blockhash

            msg = Message.new_with_blockhash(
                [compute_limit_ix, compute_price_ix, create_ata_ix, sell_ix],
                self.wallet.pubkey,
                blockhash,
            )
            tx = VersionedTransaction(message=msg, keypairs=[self.wallet.keypair])

            logger.info("Sending PumpSwap SELL transaction...")
            sig = await self._send_tx_parallel(tx, rpc_client)
            logger.info(f"PumpSwap SELL signature: {sig}")
            logger.info(f"https://solscan.io/tx/{sig}")

            # Quick status check - don't block too long on rate limits
            import asyncio
            for attempt in range(min(self.max_retries, 3)):
                try:
                    backoff = 2.0 * (attempt + 1)
                    logger.info(f"Checking tx status (attempt {attempt + 1}/3, wait {backoff}s)...")
                    await asyncio.sleep(backoff)

                    tx_response = await rpc_client.get_transaction(
                        Signature.from_string(sig),
                        encoding="json",
                        max_supported_transaction_version=0,
                    )

                    if tx_response.value is None:
                        logger.warning("Transaction not found yet...")
                        continue

                    meta = tx_response.value.transaction.meta
                    if meta and meta.err is not None:
                        error_msg = f"Transaction FAILED on-chain: {meta.err}"
                        logger.error(f"FAILED: {error_msg}")
                        return False, sig, error_msg

                    logger.info("PumpSwap SELL SUCCESS!")
                    return True, sig, None

                except Exception as e:
                    error_str = str(e).lower()
                    if "429" in error_str or "rate" in error_str or "too many" in error_str:
                        logger.warning(f"RPC rate limited - check tx on solscan: {sig}")
                        return False, sig, "Rate limited, TX unconfirmed"

                    error_msg = str(e) if str(e) else f"{type(e).__name__}"
                    logger.warning(f"Status check failed: {error_msg}")
                    if attempt == 2:
                        logger.warning(f"Could not verify - check solscan: {sig}")
                        return False, sig, "Could not verify TX"

            logger.warning(f"Status unknown - check solscan: {sig}")
            return False, sig, "Status unknown"

        except Exception as e:
            return False, None, str(e)

    def _parse_market_data(self, data: bytes) -> dict:
        """Parse PumpSwap pool account data."""
        parsed_data = {}
        offset = 8  # Skip discriminator

        fields = [
            ("pool_bump", "u8"), ("index", "u16"), ("creator", "pubkey"),
            ("base_mint", "pubkey"), ("quote_mint", "pubkey"), ("lp_mint", "pubkey"),
            ("pool_base_token_account", "pubkey"), ("pool_quote_token_account", "pubkey"),
            ("lp_supply", "u64"), ("coin_creator", "pubkey"),
        ]

        for field_name, field_type in fields:
            if field_type == "pubkey":
                parsed_data[field_name] = base58.b58encode(data[offset:offset + 32]).decode("utf-8")
                offset += 32
            elif field_type in {"u64", "i64"}:
                parsed_data[field_name] = struct.unpack("<Q" if field_type == "u64" else "<q", data[offset:offset + 8])[0]
                offset += 8
            elif field_type == "u16":
                parsed_data[field_name] = struct.unpack("<H", data[offset:offset + 2])[0]
                offset += 2
            elif field_type == "u8":
                parsed_data[field_name] = data[offset]
                offset += 1

        return parsed_data

    async def _sell_via_jupiter(
        self,
        mint: Pubkey,
        token_amount: float,
        symbol: str,
    ) -> tuple[bool, str | None, str | None]:
        """Sell via Jupiter aggregator. Ultra API first, fallback to Lite on 404."""
        import base64

        try:
            logger.info(f"[JUPITER] Jupiter SELL for {symbol}...")

                        # [edit:s12] sell uses on-chain raw balance instead of unreliable get_token_decimals
# Get token decimals — use on-chain parsed data (NOT get_token_decimals which can fallback to wrong value!)
            rpc_client = await self._get_rpc_client()
            sell_amount = None
            token_decimals = 6  # default fallback

            # FIX S12-1: Get decimals from on-chain, then compute sell_amount from
            # the REQUESTED token_amount (not from wallet balance).
            # Previous code re-derived sell_pct from wallet balance, which could
            # round to 100% and sell entire position instead of partial amount.
            try:
                import aiohttp as _aiohttp
                _helius_key = os.getenv("HELIUS_API_KEY", "")
                _rpc_chain = [
                    os.getenv("DRPC_RPC_ENDPOINT", ""),
                    os.getenv("ALCHEMY_RPC_ENDPOINT", ""),
                    f"https://mainnet.helius-rpc.com/?api-key={_helius_key}" if _helius_key else "",
                    os.getenv("SOLANA_PUBLIC_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com"),
                ]
                _rpc_chain = [u for u in _rpc_chain if u]
                _payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [str(self.wallet.pubkey), {"mint": str(mint)},
                              {"encoding": "jsonParsed", "commitment": "confirmed"}]
                }
                async with _aiohttp.ClientSession() as _sess:
                    for _rpc_url in _rpc_chain:
                        try:
                            async with _sess.post(_rpc_url, json=_payload, timeout=_aiohttp.ClientTimeout(total=3)) as _resp:
                                if _resp.status == 200:
                                    _data = await _resp.json()
                                    if "error" in _data:
                                        continue
                                    _accounts = _data.get("result", {}).get("value", [])
                                    if _accounts:
                                        _ti = _accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
                                        _ui = float(_ti.get("uiAmount") or 0)
                                        token_decimals = int(_ti.get("decimals", 6))
                                        _raw = int(_ti.get("amount", "0"))
                                        _decimals_cache[str(mint)] = token_decimals
                                        # FIX S12-1: Use requested token_amount directly
                                        # Convert to raw using on-chain decimals
                                        sell_amount = int(token_amount * 10**token_decimals)
                                        # Safety: never sell more than wallet balance
                                        if sell_amount > _raw and _raw > 0:
                                            logger.warning(f"[SELL] Capped: requested {sell_amount} raw > wallet {_raw} raw, using wallet balance")
                                            sell_amount = _raw
                                        if _ui > 0:
                                            _sell_pct_log = (sell_amount / _raw * 100) if _raw > 0 else 0
                                            logger.info(f"[SELL] On-chain: {_ui:,.2f} tokens (decimals={token_decimals}, raw={_raw}), selling {_sell_pct_log:.1f}% = {sell_amount} raw")
                                    break  # Success — stop trying RPCs
                        except Exception:
                            continue  # Try next RPC
            except Exception as _e:
                logger.warning(f"[SELL] On-chain balance check failed: {_e}, falling back to get_token_decimals")

            if sell_amount is None:
                # Fallback to old method
                token_decimals = await get_token_decimals(rpc_client, mint)
                sell_amount = int(token_amount * 10**token_decimals)
                logger.info(f"[SELL] Fallback: decimals={token_decimals}, sell_amount={sell_amount}")

            slippage_bps = int(self.slippage * 10000)

            async with aiohttp.ClientSession() as session:
                # Ultra disabled - go directly to Swap API
                if False:  # Ultra disabled
                    logger.info("[JUPITER] Trying Ultra API first...")
                    success, sig, error = await self._jupiter_ultra_sell(
                        session, rpc_client, mint, sell_amount, slippage_bps, symbol
                    )
                    if success:
                        return True, sig, None
                    
                    # If 404, fallback to Lite API
                    if error and "404" in str(error):
                        logger.warning(f"[JUPITER] Ultra returned 404, trying Lite API...")
                    else:
                        # Other error - still try Lite as last resort
                        logger.warning(f"[JUPITER] Ultra failed: {error}, trying Lite API...")
                
                # Lite API (fallback or no key)
                logger.info("[JUPITER] Using Lite API for SELL")
                return await self._jupiter_lite_sell(
                    session, rpc_client, mint, sell_amount, slippage_bps, symbol
                )

        except Exception as e:
            logger.error(f"[JUPITER] SELL error: {e}")
            return False, None, str(e)

    async def _jupiter_ultra_sell(
        self, session, rpc_client, mint, sell_amount, slippage_bps, symbol
    ) -> tuple[bool, str | None, str | None]:
        """Jupiter Ultra API sell."""
        import base64
        
        jupiter_url = "https://api.jup.ag/ultra/v1/order"
        headers = {"x-api-key": self.jupiter_api_key}

        order_params = {
            "inputMint": str(mint),
            "outputMint": str(SOL_MINT),
            "amount": str(sell_amount),
            "taker": str(self.wallet.pubkey),
            # Note: Jupiter Ultra has built-in RTSE (Real-Time Slippage Estimator)
        }

        for attempt in range(self.max_retries):
            try:
                async with session.get(jupiter_url, params=order_params, headers=headers) as resp:
                    if resp.status == 404:
                        return False, None, "404 Not Found"
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning(f"Jupiter Ultra SELL failed ({resp.status}): {error_text}")
                        continue
                    order_data = await resp.json()

                tx_base64 = order_data.get("transaction")
                if not tx_base64:
                    logger.warning("No transaction in Jupiter Ultra response")
                    continue

                out_amount = int(order_data.get("outAmount", 0))
                out_amount_sol = out_amount / LAMPORTS_PER_SOL
                logger.info(f"[JUPITER] Ultra expected: ~{out_amount_sol:.6f} SOL")

                tx_bytes = base64.b64decode(tx_base64)
                tx = VersionedTransaction.from_bytes(tx_bytes)
                signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])

                sig = await self._send_tx_parallel(signed_tx, rpc_client)
                logger.info(f"[OK] Jupiter Ultra SELL sent: {sig}")
                return True, sig, None

            except Exception as e:
                error_msg = str(e) if str(e) else f"{type(e).__name__}"
                logger.warning(f"Jupiter Ultra attempt {attempt + 1} failed: {error_msg}")
                if attempt == self.max_retries - 1:
                    return False, None, error_msg

        return False, None, "All Jupiter Ultra attempts failed"


    async def _confirm_transaction(self, sig: str, rpc_client, timeout: int = 15) -> bool:
        """Poll getSignatureStatuses until confirmed or timeout."""
        from solders.signature import Signature
        import time
        
        start = time.time()
        signature = Signature.from_string(sig)
        
        while time.time() - start < timeout:
            try:
                resp = await rpc_client.get_signature_statuses([signature])
                if resp.value and resp.value[0]:
                    status = resp.value[0]
                    if status.err:
                        logger.warning(f"[TX CONFIRM] TX {sig[:16]}... failed on-chain: {status.err}")
                        return False
                    if status.confirmation_status is not None:
                        conf_str = str(status.confirmation_status)
                        if "confirmed" in conf_str.lower() or "finalized" in conf_str.lower():
                            logger.info(f"[TX CONFIRM] TX {sig[:16]}... confirmed on-chain")
                            return True
            except Exception as e:
                logger.warning(f"[TX CONFIRM] Status check error: {e}")
            
            await asyncio.sleep(1)
        
        logger.warning(f"[TX CONFIRM] TX {sig[:16]}... not confirmed after {timeout}s")
        return False

    async def _jupiter_lite_sell(
        self, session, rpc_client, mint, sell_amount, slippage_bps, symbol
    ) -> tuple[bool, str | None, str | None]:
        """Jupiter Lite API sell."""
        import base64
        
        jupiter_quote_url = "https://api.jup.ag/swap/v1/quote"
        jupiter_swap_url = "https://api.jup.ag/swap/v1/swap"
        headers = {"x-api-key": self.jupiter_api_key} if self.jupiter_api_key else {}

        quote_params = {
            "inputMint": str(mint),
            "outputMint": str(SOL_MINT),
            "amount": str(sell_amount),
            "restrictIntermediateTokens": "true",  # Safer routes
            "slippageBps": str(slippage_bps),  # From config (10%)
        }

        try:
            async with session.get(jupiter_quote_url, params=quote_params, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return False, None, f"Jupiter Lite quote failed: {error_text}"
                quote = await resp.json()
        except Exception as e:
            return False, None, f"Jupiter Lite quote error: {e}"

        out_amount = int(quote.get("outAmount", 0))
        out_amount_sol = out_amount / LAMPORTS_PER_SOL
        logger.info(f"[JUPITER] Lite expected: ~{out_amount_sol:.6f} SOL")

        swap_body = {
            "quoteResponse": quote,
            "userPublicKey": str(self.wallet.pubkey),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": self.priority_fee,
            "dynamicComputeUnitLimit": True,  # Better CU estimation
            # "dynamicSlippage": True,  # DISABLED - use fixed slippage  # Let Jupiter calculate optimal slippage
            "asLegacyTransaction": False,  # Use versioned TX for Token2022
        }

        for attempt in range(self.max_retries):
            try:
                async with session.post(jupiter_swap_url, json=swap_body, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning(f"Jupiter Lite swap failed: {error_text}")
                        continue
                    swap_data = await resp.json()

                swap_tx_base64 = swap_data.get("swapTransaction")
                if not swap_tx_base64:
                    return False, None, "No swap transaction in response"

                tx_bytes = base64.b64decode(swap_tx_base64)
                tx = VersionedTransaction.from_bytes(tx_bytes)
                signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])

                sig = await self._send_tx_parallel(signed_tx, rpc_client)
                if not sig:
                    logger.warning(f"Jupiter Lite attempt {attempt + 1}: _send_tx_parallel returned no signature")
                    if attempt == self.max_retries - 1:
                        return False, None, "No signature from send"
                    continue
                
                logger.info(f"[OK] Jupiter Lite SELL sent: {sig} — confirming on-chain...")
                confirmed = await self._confirm_transaction(sig, rpc_client, timeout=15)
                if confirmed:
                    logger.info(f"[OK] Jupiter Lite SELL CONFIRMED: {sig}")
                    return True, sig, None
                else:
                    logger.warning(f"Jupiter Lite attempt {attempt + 1}: TX {sig[:16]}... NOT confirmed")
                    if attempt == self.max_retries - 1:
                        return False, sig, "TX sent but not confirmed on-chain"
                    continue

            except Exception as e:
                error_msg = str(e) if str(e) else f"{type(e).__name__}"
                logger.warning(f"Jupiter Lite attempt {attempt + 1} failed: {error_msg}")
                if attempt == self.max_retries - 1:
                    return False, None, error_msg

        return False, None, "All Jupiter Lite attempts failed"


    async def _sell_via_pumpportal(
        self,
        mint: Pubkey,
        token_amount: float,
        symbol: str = "TOKEN",
    ) -> tuple[bool, str | None, str | None]:
        """Sell via PumpPortal trade-local API (works for Token-2022 pump.fun tokens)."""
        import requests
        from solders.keypair import Keypair
        from solders.commitment_config import CommitmentLevel
        from solders.rpc.requests import SendVersionedTransaction
        from solders.rpc.config import RpcSendTransactionConfig

        logger.info(f"[PUMPPORTAL] Attempting PumpPortal sell for {symbol} ({mint})")

        try:
            # Get unsigned TX from PumpPortal
            response = requests.post(
                url="https://pumpportal.fun/api/trade-local",
                data={
                    "publicKey": str(self.wallet.pubkey),
                    "action": "sell",
                    "mint": str(mint),
                    "amount": str(int(token_amount)),  # Exact amount, not 100%,
                    "denominatedInSol": "false",
                    "slippage": int(self.slippage * 100),  # From config (sell_slippage)
                    "priorityFee": 0.0005,
                    "pool": "auto"
                },
                timeout=30
            )

            if response.status_code != 200:
                return False, None, f"PumpPortal error: {response.text}"

            # Sign TX
            tx = VersionedTransaction(
                VersionedTransaction.from_bytes(response.content).message,
                [self.wallet.keypair]
            )

            # Send via RPC
            rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
            commitment = CommitmentLevel.Confirmed
            config = RpcSendTransactionConfig(preflight_commitment=commitment)

            send_response = requests.post(
                url=rpc_endpoint,
                headers={"Content-Type": "application/json"},
                data=SendVersionedTransaction(tx, config).to_json(),
                timeout=30
            )

            result = send_response.json()

            if "result" in result:
                sig = result["result"]
                logger.info(f"[PUMPPORTAL] Sell TX: {sig} — confirming on-chain...")

                # Confirm on-chain before reporting success
                try:
                    from solana.rpc.async_api import AsyncClient
                    rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT") or os.getenv("DRPC_RPC_ENDPOINT")
                    async_client = AsyncClient(rpc_endpoint)
                    try:
                        confirmed = await self._confirm_transaction(sig, async_client, timeout=15)
                    finally:
                        await async_client.close()

                    if confirmed:
                        logger.info(f"[PUMPPORTAL] SELL CONFIRMED on-chain: {sig}")
                        return True, sig, None
                    else:
                        logger.warning(f"[PUMPPORTAL] SELL NOT CONFIRMED on-chain: {sig}")
                        return False, sig, "TX sent but not confirmed on-chain"
                except Exception as confirm_err:
                    logger.warning(f"[PUMPPORTAL] Confirm error: {confirm_err}, treating as unconfirmed")
                    return False, sig, f"Confirm failed: {confirm_err}"
            elif "error" in result:
                return False, None, str(result["error"])
            else:
                return False, None, str(result)

        except Exception as e:
            return False, None, str(e)
