#!/usr/bin/env python3
"""Quick buy script - покупка токена по адресу контракта.

Usage:
    buy <TOKEN_ADDRESS> <AMOUNT_SOL>
    
Examples:
    buy 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU 0.01
    buy 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU 0.01 --slippage 0.3
    
Автоматически определяет:
- Pump.fun bonding curve (если токен ещё не мигрировал)
- PumpSwap/Raydium AMM (если токен мигрировал)
"""

import argparse
import asyncio
import os
import struct
import sys

import aiohttp
import base58
from construct import Flag, Int64ul, Struct
from dotenv import load_dotenv
from core.blockhash_cache import get_blockhash_cache, init_blockhash_cache
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import MemcmpOpts, TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction
from spl.token.instructions import (
    SyncNativeParams,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    sync_native,
)

# API Keys
from dotenv import load_dotenv
load_dotenv()
JUPITER_API_KEY = os.environ.get("JUPITER_TRADE_API_KEY")  # Trade only!

load_dotenv()

# JITO integration for faster transaction landing
from src.trading.jito_sender import get_jito_sender

# Constants
EXPECTED_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)
TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000

# Pump.fun constants
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

# PumpSwap/Raydium AMM constants
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_SWAP_GLOBAL_CONFIG = Pubkey.from_string("ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw")
PUMP_SWAP_EVENT_AUTHORITY = Pubkey.from_string("GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR")
STANDARD_PUMPSWAP_FEE_RECIPIENT = Pubkey.from_string("7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ")
BUY_DISCRIMINATOR = bytes.fromhex("66063d1201daebea")

# System constants
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)

# Pool structure offsets
POOL_BASE_MINT_OFFSET = 43
POOL_MAYHEM_MODE_OFFSET = 243
POOL_MAYHEM_MODE_MIN_SIZE = 244
GLOBALCONFIG_RESERVED_FEE_OFFSET = 72


async def rpc_call_with_retry(
    coro_func,
    max_retries: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
):
    """Execute RPC call with exponential backoff on rate limit errors.
    
    Args:
        coro_func: Async function that returns a coroutine (will be called each retry)
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        
    Returns:
        Result of the RPC call
        
    Raises:
        Last exception if all retries fail
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            return await coro_func()
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            # Check if it's a rate limit error
            if "429" in error_str or "too many" in error_str or "rate" in error_str:
                delay = min(base_delay * (2 ** attempt), max_delay)
                print(f"⚠️ RPC rate limited, retry {attempt + 1}/{max_retries} in {delay:.1f}s...")
                await asyncio.sleep(delay)
            else:
                # Not a rate limit error, re-raise immediately
                raise
    
    # All retries exhausted
    raise last_error


class BondingCurveState:
    _BASE_STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
    )

    def __init__(self, data: bytes) -> None:
        if data[:8] != EXPECTED_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")
        parsed = self._BASE_STRUCT.parse(data[8:])
        self.__dict__.update(parsed)
        offset = 8 + self._BASE_STRUCT.sizeof()
        self.creator = Pubkey.from_bytes(data[offset:offset + 32]) if len(data) >= offset + 32 else None
        self.is_mayhem_mode = bool(data[offset + 32]) if len(data) >= offset + 33 else False


# Pump.fun PDA functions
def get_bonding_curve_address(mint: Pubkey) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)


def find_associated_bonding_curve(mint: Pubkey, bonding_curve: Pubkey, token_program_id: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [bytes(bonding_curve), bytes(token_program_id), bytes(mint)],
        SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
    )
    return derived_address


def find_creator_vault(creator: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address([b"creator-vault", bytes(creator)], PUMP_PROGRAM)
    return derived_address


def _find_global_volume_accumulator() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address([b"global_volume_accumulator"], PUMP_PROGRAM)
    return derived_address


def _find_user_volume_accumulator(user: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address([b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM)
    return derived_address


def _find_fee_config() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address([b"fee_config", bytes(PUMP_PROGRAM)], PUMP_FEE_PROGRAM)
    return derived_address


# PumpSwap PDA functions
def find_pumpswap_fee_config() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address([b"fee_config", bytes(PUMP_AMM_PROGRAM_ID)], PUMP_FEE_PROGRAM)
    return derived_address


def find_coin_creator_vault(coin_creator: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address([b"creator_vault", bytes(coin_creator)], PUMP_AMM_PROGRAM_ID)
    return derived_address


def find_pumpswap_global_volume() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address([b"global_volume_accumulator"], PUMP_AMM_PROGRAM_ID)
    return derived_address


def find_pumpswap_user_volume(user: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address([b"user_volume_accumulator", bytes(user)], PUMP_AMM_PROGRAM_ID)
    return derived_address


async def get_token_program_id(client: AsyncClient, mint: Pubkey) -> Pubkey:
    """Determine if mint uses TokenProgram or Token2022Program with retry."""
    async def _call():
        return await client.get_account_info(mint)
    
    mint_info = await rpc_call_with_retry(_call, max_retries=5, base_delay=0.5)
    if not mint_info.value:
        raise ValueError(f"Could not fetch mint info for {mint}")
    owner = mint_info.value.owner
    if owner == SYSTEM_TOKEN_PROGRAM:
        return SYSTEM_TOKEN_PROGRAM
    elif owner == TOKEN_2022_PROGRAM:
        return TOKEN_2022_PROGRAM
    raise ValueError(f"Unknown token program: {owner}")


async def get_curve_state(client: AsyncClient, curve: Pubkey) -> BondingCurveState | None:
    """Get bonding curve state, returns None if not found with retry.
    
    Raises exception on RPC errors to distinguish from "not found".
    """
    async def _call():
        return await client.get_account_info(curve, encoding="base64")
    
    # Let RPC errors propagate - don't swallow them
    response = await rpc_call_with_retry(_call, max_retries=5, base_delay=0.5)
    
    if not response.value or not response.value.data:
        return None
    try:
        return BondingCurveState(response.value.data)
    except Exception:
        return None


async def get_fee_recipient(client: AsyncClient, curve_state: BondingCurveState) -> Pubkey:
    """Get fee recipient with retry."""
    if not curve_state.is_mayhem_mode:
        return PUMP_FEE
    
    async def _call():
        return await client.get_account_info(PUMP_GLOBAL, encoding="base64")
    
    try:
        response = await rpc_call_with_retry(_call, max_retries=3, base_delay=0.3)
    except Exception:
        return PUMP_FEE
    
    if not response.value or not response.value.data:
        return PUMP_FEE
    data = response.value.data
    RESERVED_FEE_OFFSET = 483
    if len(data) < RESERVED_FEE_OFFSET + 32:
        return PUMP_FEE
    return Pubkey.from_bytes(data[RESERVED_FEE_OFFSET:RESERVED_FEE_OFFSET + 32])


def calculate_price(curve_state: BondingCurveState) -> float:
    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


# ============================================================================
# PumpSwap/Raydium AMM Functions
# ============================================================================

async def get_market_address_by_base_mint(client: AsyncClient, base_mint: Pubkey) -> Pubkey | None:
    """Find the AMM pool address for a token using get_program_accounts with retry."""
    filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=bytes(base_mint))]
    
    async def _call():
        return await client.get_program_accounts(PUMP_AMM_PROGRAM_ID, encoding="base64", filters=filters)
    
    try:
        response = await rpc_call_with_retry(_call, max_retries=5, base_delay=0.5)
        if response.value:
            return response.value[0].pubkey
    except Exception as e:
        print(f"❌ Failed to get market address after retries: {e}")
    return None


async def get_pool_from_dexscreener(mint: str) -> tuple[str | None, str | None]:
    """Find best swap pool using DexScreener API.
    
    Returns:
        Tuple of (pair_address, dex_id) or (None, None) if not found
    """
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()
        
        pairs = data.get("pairs", [])
        if not pairs:
            return None, None
        
        # Filter for Solana pairs only
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana_pairs:
            return None, None
        
        # Prefer pumpswap, then raydium
        pumpswap_pairs = [p for p in solana_pairs if "pumpswap" in p.get("dexId", "").lower()]
        raydium_pairs = [p for p in solana_pairs if "raydium" in p.get("dexId", "").lower()]
        
        if pumpswap_pairs:
            # Sort by liquidity
            best = max(pumpswap_pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0))
            print(f"📍 DexScreener found PumpSwap pool: {best.get('pairAddress')}")
            return best.get("pairAddress"), "pumpswap"
        elif raydium_pairs:
            best = max(raydium_pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0))
            print(f"📍 DexScreener found Raydium pool: {best.get('pairAddress')}")
            return best.get("pairAddress"), "raydium"
        else:
            # Any other DEX
            best = max(solana_pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0))
            print(f"📍 DexScreener found {best.get('dexId')} pool: {best.get('pairAddress')}")
            return best.get("pairAddress"), best.get("dexId")
            
    except Exception as e:
        print(f"⚠️ DexScreener lookup failed: {e}")
        return None, None


async def buy_via_jupiter(
    payer: Keypair,
    mint: Pubkey,
    amount_sol: float,
    slippage: float,
    priority_fee: int,
    rpc_endpoint: str,
    max_retries: int = 3,
) -> bool:
    """Buy tokens via Jupiter aggregator with API key (bypasses Cloudflare)."""
    import base64
    
    print("🪐 Using Jupiter aggregator...")
    
    buy_amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
    slippage_bps = int(slippage * 10000)
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if JUPITER_API_KEY:
        headers["x-api-key"] = JUPITER_API_KEY
    
    try:
        async with aiohttp.ClientSession() as session:
            # Get quote: SOL -> Token (with retry)
            quote_url = "https://api.jup.ag/swap/v1/quote"
            quote_params = {
                "inputMint": str(SOL),
                "outputMint": str(mint),
                "amount": str(buy_amount_lamports),
                "slippageBps": slippage_bps,
            }
            
            quote = None
            for attempt in range(max_retries):
                try:
                    async with session.get(quote_url, params=quote_params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 429:
                            delay = 1.0 * (attempt + 1)
                            print(f"⚠️ Jupiter rate limited, retry {attempt + 1}/{max_retries} in {delay}s...")
                            await asyncio.sleep(delay)
                            continue
                        if resp.status != 200:
                            error_text = await resp.text()
                            print(f"❌ Jupiter quote failed: {error_text}")
                            return False
                        quote = await resp.json()
                        break
                except asyncio.TimeoutError:
                    print(f"⚠️ Jupiter quote timeout, retry {attempt + 1}/{max_retries}...")
                    await asyncio.sleep(1.0)
            
            if not quote:
                print("❌ Failed to get Jupiter quote after retries")
                return False
            
            out_amount = int(quote.get("outAmount", 0))
            out_amount_tokens = out_amount / (10 ** TOKEN_DECIMALS)
            print(f"💵 Jupiter expected: ~{out_amount_tokens:,.2f} tokens")
            
            # Get swap transaction
            swap_url = "https://api.jup.ag/swap/v1/swap"
            swap_body = {
                "quoteResponse": quote,
                "userPublicKey": str(payer.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": priority_fee,
            }
            
            swap_data = None
            for attempt in range(max_retries):
                try:
                    async with session.post(swap_url, json=swap_body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 429:
                            delay = 1.0 * (attempt + 1)
                            print(f"⚠️ Jupiter swap rate limited, retry {attempt + 1}/{max_retries}...")
                            await asyncio.sleep(delay)
                            continue
                        if resp.status != 200:
                            error_text = await resp.text()
                            print(f"❌ Jupiter swap request failed: {error_text}")
                            return False
                        swap_data = await resp.json()
                        break
                except asyncio.TimeoutError:
                    print(f"⚠️ Jupiter swap timeout, retry {attempt + 1}/{max_retries}...")
                    await asyncio.sleep(1.0)
            
            if not swap_data:
                print("❌ Failed to get Jupiter swap after retries")
                return False
            
            swap_tx_base64 = swap_data.get("swapTransaction")
            if not swap_tx_base64:
                print("❌ No swap transaction in Jupiter response")
                return False
            
            # Sign and send transaction
            tx_bytes = base64.b64decode(swap_tx_base64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [payer])

            # JITO support (note: can't add tip to Jupiter pre-built tx)
            jito = get_jito_sender()

            async with AsyncClient(rpc_endpoint) as client:
                for attempt in range(max_retries):
                    try:
                        print(f"🚀 Sending Jupiter transaction (attempt {attempt + 1}/{max_retries})...")
                        
                        # Try JITO first if enabled
                        sig = None
                        if jito.enabled:
                            try:
                                jito_sig = await jito.send_transaction(signed_tx)
                                if jito_sig:
                                    sig = jito_sig
                                    print(f"⚡ [JITO] TX sent: {sig}")
                            except Exception as jito_err:
                                print(f"⚠️ [JITO] Failed: {jito_err}, using regular RPC...")
                        
                        # Fallback to regular RPC
                        if not sig:
                            result = await client.send_transaction(
                                signed_tx,
                                opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                            )
                            sig = str(result.value)
                            print(f"📤 [RPC] Signature: {sig}")

                        print(f"🔗 https://solscan.io/tx/{sig}")

                        await client.confirm_transaction(
                            Signature.from_string(sig) if isinstance(sig, str) else sig,
                            commitment="confirmed"
                        )
                        # Verify TX actually succeeded (not just landed)
                        sig_obj = Signature.from_string(sig) if isinstance(sig, str) else sig
                        tx_status = await client.get_signature_statuses([sig_obj])
                        if tx_status.value and tx_status.value[0]:
                            err = tx_status.value[0].err
                            if err:
                                print(f"❌ Jupiter BUY FAILED! TX error: {err}")
                                return False
                        print(f"✅ Jupiter BUY confirmed! Got ~{out_amount_tokens:,.2f} tokens")
                        return out_amount_tokens  # Return actual tokens bought
                    except Exception as e:
                        print(f"⚠️ Jupiter attempt {attempt + 1} failed: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(1.0)

                print("❌ All Jupiter attempts failed")
                return False

                
    except Exception as e:
        print(f"❌ Jupiter error: {e}")
        return False


async def get_market_data(client: AsyncClient, market_address: Pubkey) -> dict:
    """Parse pool account data with retry."""
    async def _call():
        return await client.get_account_info(market_address, encoding="base64")
    
    response = await rpc_call_with_retry(_call, max_retries=5, base_delay=0.5)
    data = response.value.data
    parsed_data: dict = {}
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


async def get_pumpswap_fee_recipients(client: AsyncClient, pool: Pubkey) -> tuple[Pubkey, Pubkey]:
    """Get fee recipient based on pool's mayhem mode status with retry."""
    async def _get_pool():
        return await client.get_account_info(pool, encoding="base64")
    
    try:
        response = await rpc_call_with_retry(_get_pool, max_retries=3, base_delay=0.3)
    except Exception:
        response = None
    
    if not response or not response.value or not response.value.data:
        fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
    else:
        pool_data = response.value.data
        is_mayhem = len(pool_data) >= POOL_MAYHEM_MODE_MIN_SIZE and bool(pool_data[POOL_MAYHEM_MODE_OFFSET])
        if is_mayhem:
            async def _get_config():
                return await client.get_account_info(PUMP_SWAP_GLOBAL_CONFIG, encoding="base64")
            
            try:
                cfg_resp = await rpc_call_with_retry(_get_config, max_retries=3, base_delay=0.3)
            except Exception:
                cfg_resp = None
            
            if cfg_resp and cfg_resp.value and cfg_resp.value.data:
                fee_recipient = Pubkey.from_bytes(cfg_resp.value.data[GLOBALCONFIG_RESERVED_FEE_OFFSET:GLOBALCONFIG_RESERVED_FEE_OFFSET + 32])
            else:
                fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
        else:
            fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
    
    fee_recipient_ata = get_associated_token_address(fee_recipient, SOL, SYSTEM_TOKEN_PROGRAM)
    return (fee_recipient, fee_recipient_ata)


async def calculate_pool_price(client: AsyncClient, pool_base_ata: Pubkey, pool_quote_ata: Pubkey) -> float:
    """Calculate token price from AMM pool balances with retry."""
    async def _get_base():
        return await client.get_token_account_balance(pool_base_ata)
    
    async def _get_quote():
        return await client.get_token_account_balance(pool_quote_ata)
    
    base_resp = await rpc_call_with_retry(_get_base, max_retries=3, base_delay=0.3)
    quote_resp = await rpc_call_with_retry(_get_quote, max_retries=3, base_delay=0.3)
    base_amount = float(base_resp.value.ui_amount)
    quote_amount = float(quote_resp.value.ui_amount)
    return quote_amount / base_amount


async def buy_via_pumpswap(
    client: AsyncClient,
    payer: Keypair,
    mint: Pubkey,
    amount_sol: float,
    slippage: float,
    priority_fee: int,
    max_retries: int,
) -> bool:
    """Buy tokens via PumpSwap/Raydium AMM, fallback to Jupiter for other DEXes."""
    
    # Get RPC endpoint for Jupiter fallback
    rpc_endpoint = os.environ.get("DRPC_RPC_ENDPOINT") or os.environ.get("ALCHEMY_RPC_ENDPOINT") or os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    
    # Find market via RPC first
    market = await get_market_address_by_base_mint(client, mint)
    
    # Fallback to DexScreener if not found
    if not market:
        print("📍 PumpSwap market not found via RPC, trying DexScreener...")
        pair_address, dex_id = await get_pool_from_dexscreener(str(mint))
        
        if pair_address and dex_id == "pumpswap":
            market = Pubkey.from_string(pair_address)
            print(f"📍 Using DexScreener PumpSwap pool: {market}")
        elif pair_address and dex_id in ("raydium", "orca", "meteora", "raydium_cp"):
            # Use Jupiter for Raydium/Orca/Meteora
            print(f"📍 Token trades on {dex_id} - using Jupiter aggregator...")
            return await buy_via_jupiter(payer, mint, amount_sol, slippage, priority_fee, rpc_endpoint, max_retries)
        elif pair_address:
            # Unknown DEX - try Jupiter anyway
            print(f"📍 Token trades on {dex_id} - trying Jupiter aggregator...")
            return await buy_via_jupiter(payer, mint, amount_sol, slippage, priority_fee, rpc_endpoint, max_retries)
        else:
            print("❌ No swap pool found for this token on any DEX")
            return False
    
    print(f"📍 Found PumpSwap market: {market}")
    
    # Get market data
    market_data = await get_market_data(client, market)
    token_program_id = await get_token_program_id(client, mint)
    
    # Get pool accounts
    pool_base_ata = Pubkey.from_string(market_data["pool_base_token_account"])
    pool_quote_ata = Pubkey.from_string(market_data["pool_quote_token_account"])
    
    # Calculate price
    price = await calculate_pool_price(client, pool_base_ata, pool_quote_ata)
    token_amount = amount_sol / price
    base_amount_out = int(token_amount * 10**TOKEN_DECIMALS)
    max_sol_input = int(amount_sol * (1 + slippage) * LAMPORTS_PER_SOL)
    
    print(f"💰 Buying for: {amount_sol} SOL")
    print(f"📊 Price: {price:.10f} SOL per token")
    print(f"🎯 Expected tokens: ~{token_amount:,.2f}")
    print(f"📈 Max spend (with {slippage*100:.0f}% slippage): {max_sol_input/LAMPORTS_PER_SOL:.6f} SOL")
    print()
    
    # Get user token accounts
    user_base_ata = get_associated_token_address(payer.pubkey(), mint, token_program_id)
    user_quote_ata = get_associated_token_address(payer.pubkey(), SOL, SYSTEM_TOKEN_PROGRAM)
    
    # Get fee recipients
    fee_recipient, fee_recipient_ata = await get_pumpswap_fee_recipients(client, market)
    
    # Get creator vault
    coin_creator = Pubkey.from_string(market_data["coin_creator"])
    coin_creator_vault = find_coin_creator_vault(coin_creator)
    coin_creator_vault_ata = get_associated_token_address(coin_creator_vault, SOL, SYSTEM_TOKEN_PROGRAM)
    
    # Build accounts
    accounts = [
        AccountMeta(pubkey=market, is_signer=False, is_writable=True),
        AccountMeta(pubkey=payer.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SOL, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user_base_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_quote_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_base_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_quote_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=False),
        AccountMeta(pubkey=fee_recipient_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_AMM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(pubkey=coin_creator_vault_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=coin_creator_vault, is_signer=False, is_writable=False),
        AccountMeta(pubkey=find_pumpswap_global_volume(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=find_pumpswap_user_volume(payer.pubkey()), is_signer=False, is_writable=True),
        AccountMeta(pubkey=find_pumpswap_fee_config(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
    ]
    
    # Build instruction data
    data = BUY_DISCRIMINATOR + struct.pack("<Q", base_amount_out) + struct.pack("<Q", max_sol_input) + struct.pack("<B", 1)
    
    # Build instructions
    compute_limit_ix = set_compute_unit_limit(200_000)
    compute_price_ix = set_compute_unit_price(priority_fee)
    
    # Create WSOL ATA
    create_wsol_ata_ix = create_idempotent_associated_token_account(payer.pubkey(), payer.pubkey(), SOL, SYSTEM_TOKEN_PROGRAM)
    
    # Wrap SOL (transfer + sync)
    wrap_amount = int(amount_sol * 1.1 * LAMPORTS_PER_SOL)  # 10% buffer for fees
    transfer_sol_ix = transfer(TransferParams(from_pubkey=payer.pubkey(), to_pubkey=user_quote_ata, lamports=wrap_amount))
    sync_native_ix = sync_native(SyncNativeParams(SYSTEM_TOKEN_PROGRAM, user_quote_ata))
    
    # Create token ATA
    create_token_ata_ix = create_idempotent_associated_token_account(payer.pubkey(), payer.pubkey(), mint, token_program_id)
    
    buy_ix = Instruction(PUMP_AMM_PROGRAM_ID, data, accounts)

    # JITO support
    jito = get_jito_sender()

    for attempt in range(max_retries):
        try:
            # Use cached blockhash for speed
            cache = await get_blockhash_cache(rpc_endpoint)
            blockhash = await cache.get_blockhash()
            
            # Build instructions list - add JITO tip if enabled
            instructions = [compute_limit_ix, compute_price_ix, create_wsol_ata_ix, transfer_sol_ix, sync_native_ix, create_token_ata_ix, buy_ix]
            if jito.enabled:
                tip_ix = jito.create_tip_instruction(payer.pubkey())
                instructions.append(tip_ix)
                print(f"💰 JITO tip: {jito.tip_lamports} lamports")
            
            msg = Message.new_with_blockhash(
                instructions,
                payer.pubkey(),
                blockhash,
            )
            tx = VersionedTransaction(message=msg, keypairs=[payer])

            print(f"🚀 Sending PumpSwap transaction (attempt {attempt + 1}/{max_retries})...")
            
            # Try JITO first if enabled
            sig = None
            if jito.enabled:
                try:
                    jito_sig = await jito.send_transaction(tx)
                    if jito_sig:
                        sig = jito_sig
                        print(f"⚡ [JITO] TX sent: {sig}")
                except Exception as jito_err:
                    print(f"⚠️ [JITO] Failed: {jito_err}, using regular RPC...")
            
            # Fallback to regular RPC
            if not sig:
                result = await client.send_transaction(tx, opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed))
                sig = result.value
                print(f"📤 [RPC] Signature: {sig}")

            print(f"🔗 https://solscan.io/tx/{sig}")

            await client.confirm_transaction(Signature.from_string(sig) if isinstance(sig, str) else sig, commitment="confirmed", sleep_seconds=0.5)
            print("✅ Transaction confirmed!")
            return True

        except Exception as e:
            print(f"❌ Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)

    print("❌ All PumpSwap attempts failed")
    return False



# ============================================================================
# Pump.fun Bonding Curve Buy
# ============================================================================

async def buy_via_pumpfun(
    client: AsyncClient,
    payer: Keypair,
    mint: Pubkey,
    curve_state: BondingCurveState,
    amount_sol: float,
    slippage: float,
    priority_fee: int,
    max_retries: int,
) -> bool:
    """Buy tokens via Pump.fun bonding curve."""
    # Get RPC endpoint for blockhash cache
    rpc_endpoint = os.environ.get("DRPC_RPC_ENDPOINT") or os.environ.get("ALCHEMY_RPC_ENDPOINT") or os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    token_program_id = await get_token_program_id(client, mint)
    bonding_curve, _ = get_bonding_curve_address(mint)
    
    # Calculate price and token amount
    price = calculate_price(curve_state)
    token_amount = amount_sol / price
    amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)
    max_amount_lamports = int(amount_lamports * (1 + slippage))
    
    print(f"💰 Buying for: {amount_sol} SOL")
    print(f"📊 Price: {price:.10f} SOL per token")
    print(f"🎯 Expected tokens: ~{token_amount:,.2f}")
    print(f"📈 Max spend (with {slippage*100:.0f}% slippage): {max_amount_lamports/LAMPORTS_PER_SOL:.6f} SOL")
    print()
    
    # Build accounts
    associated_bonding_curve = find_associated_bonding_curve(mint, bonding_curve, token_program_id)
    creator_vault = find_creator_vault(curve_state.creator)
    fee_recipient = await get_fee_recipient(client, curve_state)
    ata = get_associated_token_address(payer.pubkey(), mint, token_program_id)
    
    accounts = [
        AccountMeta(pubkey=PUMP_GLOBAL, is_signer=False, is_writable=False),
        AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=True),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=payer.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),
        AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),
        AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=_find_global_volume_accumulator(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=_find_user_volume_accumulator(payer.pubkey()), is_signer=False, is_writable=True),
        AccountMeta(pubkey=_find_fee_config(), is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
    ]
    
    # Build instruction
    discriminator = struct.pack("<Q", 16927863322537952870)
    track_volume = bytes([1, 1])
    data = discriminator + struct.pack("<Q", int(token_amount * 10**TOKEN_DECIMALS)) + struct.pack("<Q", max_amount_lamports) + track_volume
    buy_ix = Instruction(PUMP_PROGRAM, data, accounts)
    
    # Create ATA if needed
    idempotent_ata_ix = create_idempotent_associated_token_account(payer.pubkey(), payer.pubkey(), mint, token_program_id=token_program_id)
    
    # Send transaction with JITO support
    jito = get_jito_sender()
    
    # Build instructions list - add JITO tip if enabled
    instructions = [set_compute_unit_price(priority_fee), idempotent_ata_ix, buy_ix]
    if jito.enabled:
        tip_ix = jito.create_tip_instruction(payer.pubkey())
        instructions.append(tip_ix)
        print(f"💰 JITO tip: {jito.tip_lamports} lamports")
    
    msg = Message(instructions, payer.pubkey())

    for attempt in range(max_retries):
        try:
            # Use cached blockhash for speed
            cache = await get_blockhash_cache(rpc_endpoint)
            blockhash = await cache.get_blockhash()
            tx = Transaction([payer], msg, blockhash)
            opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)

            print(f"🚀 Sending Pump.fun transaction (attempt {attempt + 1}/{max_retries})...")
            
            # Try JITO first if enabled
            sig = None
            if jito.enabled:
                try:
                    jito_sig = await jito.send_transaction(tx)
                    if jito_sig:
                        sig = jito_sig
                        print(f"⚡ [JITO] TX sent: {sig}")
                except Exception as jito_err:
                    print(f"⚠️ [JITO] Failed: {jito_err}, using regular RPC...")
            
            # Fallback to regular RPC
            if not sig:
                result = await client.send_transaction(tx, opts=opts)
                sig = result.value
                print(f"📤 [RPC] Signature: {sig}")

            print(f"🔗 https://solscan.io/tx/{sig}")

            await client.confirm_transaction(Signature.from_string(sig) if isinstance(sig, str) else sig, commitment="confirmed", sleep_seconds=0.5)
            print("✅ Transaction confirmed!")
            return True

        except Exception as e:
            print(f"❌ Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)

    print("❌ All Pump.fun attempts failed")
    return False


# ============================================================================
# Main Buy Function
# ============================================================================

async def buy_token(
    mint: Pubkey,
    amount_sol: float,
    slippage: float = 0.3,
    priority_fee: int = 100000,
    max_retries: int = 3,
) -> bool:
    """Buy tokens - автоматически выбирает Pump.fun или PumpSwap."""
    private_key = os.environ.get("SOLANA_PRIVATE_KEY")
    # Use Alchemy for manual scripts to avoid rate limits from bot
    rpc_endpoint = os.environ.get("DRPC_RPC_ENDPOINT") or os.environ.get("ALCHEMY_RPC_ENDPOINT") or os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    
    if not private_key:
        print("❌ SOLANA_PRIVATE_KEY not set in .env")
        return False
    if not rpc_endpoint:
        print("❌ SOLANA_NODE_RPC_ENDPOINT not set in .env")
        return False

    payer = Keypair.from_bytes(base58.b58decode(private_key))
    
    async with AsyncClient(rpc_endpoint) as client:
        # Check bonding curve state
        bonding_curve, _ = get_bonding_curve_address(mint)
        print(f"📍 Checking bonding curve: {bonding_curve}")
        
        try:
            # Таймаут 5 сек на проверку bonding curve
            curve_state = await asyncio.wait_for(
                get_curve_state(client, bonding_curve),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            print(f"⚠️ Bonding curve check timeout - using Jupiter...")
            return await buy_via_jupiter(payer, mint, amount_sol, slippage, priority_fee, rpc_endpoint, max_retries)
        except Exception as e:
            print(f"⚠️ Bonding curve check failed: {e}")
            print(f"🪐 Falling back to Jupiter...")
            return await buy_via_jupiter(payer, mint, amount_sol, slippage, priority_fee, rpc_endpoint, max_retries)
        
        # Decide which method to use
        if curve_state is None:
            print("🔄 Bonding curve not found - token migrated to Raydium, using PumpSwap AMM...")
            return await buy_via_jupiter(payer, mint, amount_sol, slippage, priority_fee, rpc_endpoint, max_retries)
        elif curve_state.complete:
            print(f"🔄 Bonding curve COMPLETE (complete={curve_state.complete}) - using PumpSwap AMM...")
            print(f"   Virtual SOL: {curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL:.4f}")
            print(f"   Real SOL: {curve_state.real_sol_reserves / LAMPORTS_PER_SOL:.4f}")
            return await buy_via_jupiter(payer, mint, amount_sol, slippage, priority_fee, rpc_endpoint, max_retries)
        else:
            print(f"📈 Token on bonding curve (complete={curve_state.complete}) - using Pump.fun...")
            print(f"   Virtual SOL: {curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL:.4f}")
            print(f"   Real SOL: {curve_state.real_sol_reserves / LAMPORTS_PER_SOL:.4f}")
            return await buy_via_pumpfun(client, payer, mint, curve_state, amount_sol, slippage, priority_fee, max_retries)


def main():
    parser = argparse.ArgumentParser(description="Quick buy token by contract address")
    parser.add_argument("token", help="Token mint address")
    parser.add_argument("amount", type=float, help="Amount of SOL to spend")
    parser.add_argument("--slippage", type=float, default=0.3, help="Slippage tolerance (default: 0.3 = 30%%)")
    parser.add_argument("--priority-fee", type=int, default=100000, help="Priority fee in microlamports (default: 100000)")
    args = parser.parse_args()
    
    try:
        mint = Pubkey.from_string(args.token)
    except Exception:
        print(f"❌ Invalid token address: {args.token}")
        sys.exit(1)
    
    if args.amount <= 0:
        print("❌ Amount must be positive")
        sys.exit(1)
    
    print(f"🎯 Buying token: {mint}")
    print(f"=" * 50)
    
    bought_tokens_result = asyncio.run(buy_token(mint, args.amount, args.slippage, args.priority_fee))
    if isinstance(bought_tokens_result, (int, float)) and bought_tokens_result > 0:
        bought_tokens = bought_tokens_result
        success = True
    elif bought_tokens_result == True:
        bought_tokens = 0
        success = True
    else:
        bought_tokens = 0
        success = False
    
    # После успешной покупки - синхронизируем через wsync (берёт реальный баланс с кошелька)
    if success:
        print("")
        print("🔄 Синхронизация позиции...")
        import time
        import subprocess, json, requests, base58, os
        from solders.keypair import Keypair
        
        try:
            pk = os.environ.get("SOLANA_PRIVATE_KEY")
            # Используем DRPC как основной - он быстрее обновляется
            rpc = os.environ.get("DRPC_RPC_ENDPOINT") or os.environ.get("ALCHEMY_RPC_ENDPOINT") or os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
            wallet = str(Keypair.from_bytes(base58.b58decode(pk)).pubkey())
            mint_addr = str(mint)
            
            # Получаем старый баланс из Redis
            old_balance = 0
            result = subprocess.run(["redis-cli", "HGET", "whale:positions", mint_addr], capture_output=True, text=True)
            if result.stdout.strip():
                pos = json.loads(result.stdout.strip())
                old_balance = pos.get("quantity", 0)
            
            # Ждём 10 сек и делаем 3 попытки с разными RPC
            rpcs = [
                os.environ.get("DRPC_RPC_ENDPOINT"),
                os.environ.get("ALCHEMY_RPC_ENDPOINT"),
                os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
            ]
            rpcs = [r for r in rpcs if r]  # Убираем None
            
            real_balance = old_balance
            # Retry до 3 раз с увеличивающейся задержкой
            for sync_attempt in range(3):
                delay = 10 + sync_attempt * 5  # 10, 15, 20 сек
                print(f"   Ожидание {delay} сек... (попытка {sync_attempt+1}/3)")
                time.sleep(delay)
            
            for attempt, rpc_url in enumerate(rpcs):
                if not rpc_url:
                    continue
                try:
                    resp = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner", 
                        "params": [wallet, {"mint": mint_addr}, {"encoding": "jsonParsed"}]}, timeout=30)
                    accounts = resp.json().get("result", {}).get("value", [])
                    
                    if accounts:
                        real_balance = float(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"])
                        if abs(real_balance - old_balance) > 1:  # Изменение больше 1 токена
                            print(f"📊 Баланс: {old_balance:.2f} -> {real_balance:.2f}")
                            break
                except:
                    pass
                    
                if attempt < len(rpcs) - 1:
                    print(f"   Пробуем другой RPC...")
                    time.sleep(3)
            
            # Обновляем Redis, positions.json и history
            if abs(real_balance - old_balance) > 1:
                if result.stdout.strip():
                    # Получаем текущую цену через DexScreener (Jupiter decimals ненадёжны)
                    current_price = 0
                    try:
                        price_resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint_addr}", timeout=10)
                        pairs = price_resp.json().get("pairs", [])
                        if pairs:
                            current_price = float(pairs[0].get("priceNative", 0) or 0)
                    except:
                        pass
                    
                    pos["quantity"] = real_balance
                    
                    # Обновляем entry_price, SL, TSL
                    if current_price > 0:
                        old_entry = pos.get("entry_price", 0)
                        pos["entry_price"] = current_price
                        pos["stop_loss_price"] = current_price * 0.7
                        pos["high_water_mark"] = current_price
                        pos["tsl_active"] = False
                        pos["tsl_trigger_price"] = 0
                        print(f"📊 Entry: {old_entry:.10f} -> {current_price:.10f}")
                        print(f"📊 SL: {current_price * 0.7:.10f} (-30%)")
                    
                    subprocess.run(["redis-cli", "HSET", "whale:positions", mint_addr, json.dumps(pos)], capture_output=True)

                    # positions.json
                    with open("/opt/pumpfun-bonkfun-bot/positions.json", "r") as f:
                        positions = json.load(f)
                    for p in positions:
                        if p.get("mint") == mint_addr:
                            p["quantity"] = real_balance
                            if current_price > 0:
                                p["entry_price"] = current_price
                                p["stop_loss_price"] = current_price * 0.7
                                p["high_water_mark"] = current_price
                                p["tsl_active"] = False
                                p["tsl_trigger_price"] = 0
                            break
                    with open("/opt/pumpfun-bonkfun-bot/positions.json", "w") as f:
                        json.dump(positions, f, indent=2)

                    # purchased_tokens_history.json
                    try:
                        with open("/opt/pumpfun-bonkfun-bot/data/purchased_tokens_history.json", "r") as f:
                            history = json.load(f)
                        if mint_addr in history.get("purchased_tokens", {}):
                            history["purchased_tokens"][mint_addr]["amount"] = real_balance
                            if current_price > 0:
                                history["purchased_tokens"][mint_addr]["price"] = current_price
                            with open("/opt/pumpfun-bonkfun-bot/data/purchased_tokens_history.json", "w") as f:
                                json.dump(history, f, indent=2)
                    except:
                        pass

                    print(f"✅ Синхронизировано: {real_balance:,.2f}")
                else:
                    # НОВЫЙ ТОКЕН - создаём позицию!
                    print("📝 Создаю новую позицию...")
                    
                    # Получаем цену
                    current_price = 0
                    try:
                        price_resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint_addr}", timeout=10)
                        pairs = price_resp.json().get("pairs", [])
                        if pairs:
                            current_price = float(pairs[0].get("priceNative", 0) or 0)
                            symbol = pairs[0].get("baseToken", {}).get("symbol", "UNKNOWN")
                        else:
                            symbol = "UNKNOWN"
                    except:
                        symbol = "UNKNOWN"
                    
                    if current_price <= 0:
                        current_price = args.amount / max(real_balance, 1)
                    
                    # Создаём новую позицию
                    from datetime import datetime
                    new_pos = {
                        "mint": mint_addr,
                        "symbol": symbol,
                        "entry_price": current_price,
                        "quantity": real_balance,
                        "entry_time": datetime.utcnow().isoformat(),
                        "platform": "jupiter",
                        "take_profit_price": current_price * 100,
                        "stop_loss_price": current_price * 0.7,
                        "max_hold_time": 0,
                        "tsl_enabled": True,
                        "tsl_activation_pct": 0.3,
                        "tsl_trail_pct": 0.3,
                        "tsl_sell_pct": 0.7,
                        "tsl_active": False,
                        "tsl_trigger_price": 0,
                        "high_water_mark": current_price,
                        "is_active": True,
                        "dca_enabled": True,
                        "dca_pending": True,
                        "dca_bought": False,
                        "dca_trigger_pct": 0.25,
                        "dca_first_buy_pct": 0.5,
                        "original_entry_price": current_price,
                    }
                    
                    # Сохраняем в Redis
                    subprocess.run(["redis-cli", "HSET", "whale:positions", mint_addr, json.dumps(new_pos)], capture_output=True)
                    
                    # Сохраняем в positions.json
                    with open("/opt/pumpfun-bonkfun-bot/positions.json", "r") as f:
                        positions = json.load(f)
                    positions.append(new_pos)
                    with open("/opt/pumpfun-bonkfun-bot/positions.json", "w") as f:
                        json.dump(positions, f, indent=2)
                    
                    # Сохраняем в history
                    try:
                        with open("/opt/pumpfun-bonkfun-bot/data/purchased_tokens_history.json", "r") as f:
                            history = json.load(f)
                        if "purchased_tokens" not in history:
                            history["purchased_tokens"] = {}
                        history["purchased_tokens"][mint_addr] = {
                            "symbol": symbol,
                            "bot_name": "manual_buy",
                            "platform": "jupiter",
                            "price": current_price,
                            "amount": real_balance,
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                        with open("/opt/pumpfun-bonkfun-bot/data/purchased_tokens_history.json", "w") as f:
                            json.dump(history, f, indent=2)
                    except:
                        pass
                    
                    print(f"✅ Новая позиция создана:")
                    print(f"   Symbol: {symbol}")
                    print(f"   Qty: {real_balance:,.2f}")
                    print(f"   Entry: {current_price:.10f}")
                    print(f"   SL: {current_price * 0.7:.10f} (-30%)")
            else:
                print(f"⚠️ RPC не обновился. Текущий баланс: {real_balance:,.2f}")
                print(f"   Запусти: wsync && bot-restart")
        except Exception as e:
            print(f"⚠️ Sync error: {e}")

        print("")
        print("💡 Перезапусти бота: bot-restart")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
