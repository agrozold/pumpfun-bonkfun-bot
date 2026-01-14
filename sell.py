#!/usr/bin/env python3
"""Quick sell script - продажа токена по адресу контракта.

Usage:
    sell <TOKEN_ADDRESS> <PERCENT>
    
    PERCENT: 100 = продать всё, 50 = половину, 10 = 10%
    
Examples:
    sell 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU 100      # продать всё
    sell 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU 50       # продать половину
    sell 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU 10       # продать 10%
    sell 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU 100 --slippage 0.5
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
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import get_associated_token_address

load_dotenv()

# Constants
EXPECTED_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)
TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)


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


def get_bonding_curve_address(mint: Pubkey) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)


def find_associated_bonding_curve(
    mint: Pubkey, bonding_curve: Pubkey, token_program_id: Pubkey
) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [bytes(bonding_curve), bytes(token_program_id), bytes(mint)],
        SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
    )
    return derived_address


def find_creator_vault(creator: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)], PUMP_PROGRAM
    )
    return derived_address


def _find_fee_config() -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PUMP_PROGRAM)], PUMP_FEE_PROGRAM
    )
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


async def get_curve_state(client: AsyncClient, curve: Pubkey) -> BondingCurveState:
    response = await client.get_account_info(curve, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError("Bonding curve not found - token may have migrated to Raydium")
    return BondingCurveState(response.value.data)


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


async def get_token_balance(client: AsyncClient, ata: Pubkey) -> int:
    response = await client.get_token_account_balance(ata)
    return int(response.value.amount) if response.value else 0


def calculate_price(curve_state: BondingCurveState) -> float:
    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


async def sell_token(
    mint: Pubkey,
    percent: float = 100.0,
    slippage: float = 0.25,
    priority_fee: int = 1000,
    max_retries: int = 3,
) -> bool:
    """Sell tokens for given mint address.
    
    Args:
        mint: Token mint address
        percent: Percentage to sell (100 = all, 50 = half, etc.)
        slippage: Slippage tolerance
        priority_fee: Priority fee in microlamports
        max_retries: Max retry attempts
    """
    private_key = os.environ.get("SOLANA_PRIVATE_KEY")
    rpc_endpoint = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    
    if not private_key:
        print("❌ SOLANA_PRIVATE_KEY not set in .env")
        return False
    if not rpc_endpoint:
        print("❌ SOLANA_NODE_RPC_ENDPOINT not set in .env")
        return False

    payer = Keypair.from_bytes(base58.b58decode(private_key))
    
    async with AsyncClient(rpc_endpoint) as client:
        # Get token program
        token_program_id = await get_token_program_id(client, mint)
        
        # Get bonding curve
        bonding_curve, _ = get_bonding_curve_address(mint)
        curve_state = await get_curve_state(client, bonding_curve)
        
        if curve_state.complete:
            print("⚠️  Token has migrated to Raydium - use Raydium sell instead")
            return False
        
        # Get token balance
        ata = get_associated_token_address(payer.pubkey(), mint, token_program_id)
        total_balance = await get_token_balance(client, ata)
        total_balance_decimal = total_balance / 10**TOKEN_DECIMALS
        
        if total_balance == 0:
            print("❌ No tokens to sell")
            return False
        
        # Calculate amount to sell based on percentage
        sell_amount = int(total_balance * (percent / 100.0))
        sell_amount_decimal = sell_amount / 10**TOKEN_DECIMALS
        
        if sell_amount == 0:
            print("❌ Sell amount too small")
            return False
        
        # Calculate price and min output
        price = calculate_price(curve_state)
        sol_value = sell_amount_decimal * price
        min_sol_output = int(sol_value * (1 - slippage) * LAMPORTS_PER_SOL)
        
        print(f"💰 Total balance: {total_balance_decimal:,.2f} tokens")
        print(f"📊 Selling: {percent:.0f}% = {sell_amount_decimal:,.2f} tokens")
        print(f"💵 Price: {price:.10f} SOL per token")
        print(f"💵 Value: ~{sol_value:.6f} SOL")
        print(f"📉 Min output (with {slippage*100:.0f}% slippage): {min_sol_output/LAMPORTS_PER_SOL:.6f} SOL")
        print(f"⚡ Priority fee: {priority_fee} microlamports")
        print()
        
        # Build accounts
        associated_bonding_curve = find_associated_bonding_curve(mint, bonding_curve, token_program_id)
        creator_vault = find_creator_vault(curve_state.creator)
        fee_recipient = await get_fee_recipient(client, curve_state)
        
        accounts = [
            AccountMeta(pubkey=PUMP_GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=associated_bonding_curve, is_signer=False, is_writable=True),
            AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=payer.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True),
            AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=_find_fee_config(), is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
        ]
        
        # Build instruction
        discriminator = struct.pack("<Q", 12502976635542562355)
        track_volume = bytes([1, 1])
        data = discriminator + struct.pack("<Q", sell_amount) + struct.pack("<Q", min_sol_output) + track_volume
        sell_ix = Instruction(PUMP_PROGRAM, data, accounts)
        
        # Send transaction
        msg = Message([set_compute_unit_price(priority_fee), sell_ix], payer.pubkey())
        
        for attempt in range(max_retries):
            try:
                blockhash = await client.get_latest_blockhash()
                tx = Transaction([payer], msg, blockhash.value.blockhash)
                opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                
                print(f"🚀 Sending transaction (attempt {attempt + 1}/{max_retries})...")
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
        
        print("❌ All attempts failed")
        return False


def main():
    parser = argparse.ArgumentParser(description="Quick sell token by contract address")
    parser.add_argument("token", help="Token mint address")
    parser.add_argument("percent", type=float, help="Percentage to sell (100=all, 50=half, 10=10%%)")
    parser.add_argument("--slippage", type=float, default=0.5, help="Slippage tolerance (default: 0.5 = 50%%)")
    parser.add_argument("--priority-fee", type=int, default=100000, help="Priority fee in microlamports (default: 100000)")
    args = parser.parse_args()
    
    try:
        mint = Pubkey.from_string(args.token)
    except Exception:
        print(f"❌ Invalid token address: {args.token}")
        sys.exit(1)
    
    if args.percent <= 0 or args.percent > 100:
        print(f"❌ Invalid percent: {args.percent}. Must be between 1 and 100")
        sys.exit(1)
    
    print(f"🎯 Selling {args.percent:.0f}% of token: {mint}")
    print(f"=" * 50)
    
    success = asyncio.run(sell_token(mint, args.percent, args.slippage, args.priority_fee))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
