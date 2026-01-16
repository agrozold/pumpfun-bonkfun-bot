"""
EMERGENCY SELL SCRIPT
Экстренная продажа токена когда SL не сработал!

Usage:
    uv run emergency_sell.py <MINT_ADDRESS>
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv
load_dotenv()

from solders.pubkey import Pubkey
from core.client import SolanaClient
from core.wallet import Wallet
from trading.fallback_seller import FallbackSeller


async def emergency_sell(mint_str: str):
    """Emergency sell a token via Jupiter/PumpSwap."""
    print("=" * 60)
    print("[EMERGENCY SELL] Starting emergency sell")
    print(f"Token: {mint_str}")
    print("=" * 60)
    
    # Load environment
    rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT") or os.getenv("SOLANA_RPC_HTTP")
    private_key = os.getenv("SOLANA_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
    jupiter_api_key = os.getenv("JUPITER_API_KEY")
    
    if not rpc_endpoint:
        print("ERROR: SOLANA_NODE_RPC_ENDPOINT or SOLANA_RPC_HTTP not set!")
        return False
    
    if not private_key:
        print("ERROR: SOLANA_PRIVATE_KEY or PRIVATE_KEY not set!")
        return False
    
    print(f"RPC: {rpc_endpoint[:50]}...")
    
    # Initialize
    client = SolanaClient(rpc_endpoint)
    wallet = Wallet(private_key)
    mint = Pubkey.from_string(mint_str)
    
    print(f"Wallet: {wallet.pubkey}")
    
    # Get token balance
    try:
        from spl.token.instructions import get_associated_token_address
        from solders.pubkey import Pubkey as SoldersPubkey
        
        # Try Token-2022 first, then regular SPL
        TOKEN_PROGRAM_ID = SoldersPubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
        TOKEN_2022_PROGRAM_ID = SoldersPubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
        
        # Check both token programs
        token_amount = 0
        token_program_id = TOKEN_PROGRAM_ID
        
        for program_id in [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]:
            try:
                ata = get_associated_token_address(wallet.pubkey, mint, program_id)
                account_info = await client.client.get_account_info(ata)
                if account_info.value:
                    # Parse token account data
                    data = account_info.value.data
                    if len(data) >= 72:
                        # Token amount is at offset 64, 8 bytes little-endian
                        token_amount = int.from_bytes(data[64:72], "little")
                        token_program_id = program_id
                        print(f"Found {token_amount} tokens in ATA (program: {program_id})")
                        break
            except Exception as e:
                continue
        
        if token_amount == 0:
            print("ERROR: No tokens found in wallet!")
            return False
            
    except Exception as e:
        print(f"ERROR getting token balance: {e}")
        return False
    
    # Create fallback seller
    seller = FallbackSeller(
        client=client,
        wallet=wallet,
        slippage=0.5,  # 50% slippage for emergency!
        priority_fee=500_000,  # High priority
        max_retries=5,
        jupiter_api_key=jupiter_api_key,
    )
    
    print(f"\nSelling {token_amount} tokens with 50% slippage...")
    print("This may take a moment...\n")
    
    # Execute sell
    success, tx_sig, error = await seller.sell(
        mint=mint,
        token_amount=token_amount,
        symbol="EMERGENCY",
    )
    
    if success:
        print("=" * 60)
        print("[SUCCESS] EMERGENCY SELL COMPLETED!")
        print(f"TX: {tx_sig}")
        print(f"Explorer: https://solscan.io/tx/{tx_sig}")
        print("=" * 60)
        return True
    else:
        print("=" * 60)
        print("[FAILED] EMERGENCY SELL FAILED!")
        print(f"Error: {error}")
        print("=" * 60)
        return False


async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run emergency_sell.py <MINT_ADDRESS>")
        print("Example: uv run emergency_sell.py 2zno8ULdYzzTaSwREHBGH2vM8avLykYMBC2Vydg8pump")
        sys.exit(1)
    
    mint = sys.argv[1]
    success = await emergency_sell(mint)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
