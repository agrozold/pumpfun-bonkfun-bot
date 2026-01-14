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

import base58
from construct import Flag, Int64ul, Struct
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import MemcmpOpts, TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction
from spl.token.instructions import (
    SyncNativeParams,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    sync_native,
)

load_dotenv()

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
    """Determine if mint uses TokenProgram or Token2022Program."""
    mint_info = await client.get_account_info(mint)
    if not mint_info.value:
        raise ValueError(f"Could not fetch mint info for {mint}")
    owner = mint_info.value.owner
    if owner == SYSTEM_TOKEN_PROGRAM:
        return SYSTEM_TOKEN_PROGRAM
    elif owner == TOKEN_2022_PROGRAM:
        return TOKEN_2022_PROGRAM
    raise ValueError(f"Unknown token program: {owner}")


async def get_curve_state(client: AsyncClient, curve: Pubkey) -> BondingCurveState | None:
    """Get bonding curve state, returns None if not found."""
    response = await client.get_account_info(curve, encoding="base64")
    if not response.value or not response.value.data:
        return None
    try:
        return BondingCurveState(response.value.data)
    except Exception:
        return None


async def get_fee_recipient(client: AsyncClient, curve_state: BondingCurveState) -> Pubkey:
    if not curve_state.is_mayhem_mode:
        return PUMP_FEE
    response = await client.get_account_info(PUMP_GLOBAL, encoding="base64")
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
    """Find the AMM pool address for a token using get_program_accounts."""
    filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=bytes(base_mint))]
    response = await client.get_program_accounts(PUMP_AMM_PROGRAM_ID, encoding="base64", filters=filters)
    if response.value:
        return response.value[0].pubkey
    return None


async def get_market_data(client: AsyncClient, market_address: Pubkey) -> dict:
    """Parse pool account data."""
    response = await client.get_account_info(market_address, encoding="base64")
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
    """Get fee recipient based on pool's mayhem mode status."""
    response = await client.get_account_info(pool, encoding="base64")
    if not response.value or not response.value.data:
        fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
    else:
        pool_data = response.value.data
        is_mayhem = len(pool_data) >= POOL_MAYHEM_MODE_MIN_SIZE and bool(pool_data[POOL_MAYHEM_MODE_OFFSET])
        if is_mayhem:
            cfg_resp = await client.get_account_info(PUMP_SWAP_GLOBAL_CONFIG, encoding="base64")
            if cfg_resp.value and cfg_resp.value.data:
                fee_recipient = Pubkey.from_bytes(cfg_resp.value.data[GLOBALCONFIG_RESERVED_FEE_OFFSET:GLOBALCONFIG_RESERVED_FEE_OFFSET + 32])
            else:
                fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
        else:
            fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
    
    fee_recipient_ata = get_associated_token_address(fee_recipient, SOL, SYSTEM_TOKEN_PROGRAM)
    return (fee_recipient, fee_recipient_ata)


async def calculate_pool_price(client: AsyncClient, pool_base_ata: Pubkey, pool_quote_ata: Pubkey) -> float:
    """Calculate token price from AMM pool balances."""
    base_resp = await client.get_token_account_balance(pool_base_ata)
    quote_resp = await client.get_token_account_balance(pool_quote_ata)
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
    """Buy tokens via PumpSwap/Raydium AMM."""
    print("🔄 Token migrated to Raydium - using PumpSwap AMM...")
    
    # Find market
    market = await get_market_address_by_base_mint(client, mint)
    if not market:
        print("❌ PumpSwap market not found for this token")
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

    for attempt in range(max_retries):
        try:
            blockhash = await client.get_latest_blockhash()
            msg = Message.new_with_blockhash(
                [compute_limit_ix, compute_price_ix, create_wsol_ata_ix, transfer_sol_ix, sync_native_ix, create_token_ata_ix, buy_ix],
                payer.pubkey(),
                blockhash.value.blockhash,
            )
            tx = VersionedTransaction(message=msg, keypairs=[payer])
            
            print(f"🚀 Sending PumpSwap transaction (attempt {attempt + 1}/{max_retries})...")
            result = await client.send_transaction(tx, opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed))
            sig = result.value
            
            print(f"📤 Signature: {sig}")
            print(f"🔗 https://solscan.io/tx/{sig}")
            
            await client.confirm_transaction(sig, commitment="confirmed", sleep_seconds=0.5)
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
    
    # Send transaction
    msg = Message([set_compute_unit_price(priority_fee), idempotent_ata_ix, buy_ix], payer.pubkey())
    
    for attempt in range(max_retries):
        try:
            blockhash = await client.get_latest_blockhash()
            tx = Transaction([payer], msg, blockhash.value.blockhash)
            opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
            
            print(f"🚀 Sending Pump.fun transaction (attempt {attempt + 1}/{max_retries})...")
            result = await client.send_transaction(tx, opts=opts)
            sig = result.value
            
            print(f"📤 Signature: {sig}")
            print(f"🔗 https://solscan.io/tx/{sig}")
            
            await client.confirm_transaction(sig, commitment="confirmed", sleep_seconds=0.5)
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
    rpc_endpoint = os.environ.get("ALCHEMY_RPC_ENDPOINT") or os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    
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
        curve_state = await get_curve_state(client, bonding_curve)
        
        # Decide which method to use
        if curve_state is None or curve_state.complete:
            # Token migrated to Raydium - use PumpSwap
            return await buy_via_pumpswap(client, payer, mint, amount_sol, slippage, priority_fee, max_retries)
        else:
            # Token still on bonding curve - use Pump.fun
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
    
    success = asyncio.run(buy_token(mint, args.amount, args.slippage, args.priority_fee))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
