#!/usr/bin/env python3
"""
Sell all pump.fun/BAGS/BONK tokens via PumpPortal trade-local API.
Uses standard RPC getTokenAccountsByOwner (works with all providers).
Removes sold positions from positions.json.
"""

import asyncio
import requests
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.commitment_config import CommitmentLevel
from solders.rpc.requests import SendVersionedTransaction
from solders.rpc.config import RpcSendTransactionConfig

load_dotenv()

# Position management
try:
    from trading.position import load_positions, save_positions, remove_position
    from solders.pubkey import Pubkey
    POSITIONS_AVAILABLE = True
except ImportError:
    POSITIONS_AVAILABLE = False

SKIP_TOKENS = {
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def get_all_tokens(wallet_pubkey: str) -> list[dict]:
    """Get all tokens using standard RPC (works with any provider)."""
    rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    
    if not rpc_endpoint:
        print("⚠️  SOLANA_NODE_RPC_ENDPOINT not configured")
        return []

    TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

    tokens = []

    for program_id in [TOKEN_PROGRAM, TOKEN_2022_PROGRAM]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet_pubkey,
                {"programId": program_id},
                {"encoding": "jsonParsed"}
            ]
        }

        try:
            resp = requests.post(rpc_endpoint, json=payload, timeout=30)
            data = resp.json()
        except Exception as e:
            print(f"⚠️  RPC error ({program_id[:8]}...): {e}")
            continue

        if "result" in data and "value" in data["result"]:
            for account in data["result"]["value"]:
                try:
                    info = account["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    balance = float(info["tokenAmount"]["uiAmount"] or 0)

                    if balance > 0 and mint not in SKIP_TOKENS:
                        # Check if pump.fun token (ends with 'pump')
                        is_pump = mint.endswith("pump")
                        
                        tokens.append({
                            "mint": mint,
                            "symbol": "PUMP" if is_pump else "TOKEN",
                            "balance": balance,
                            "is_pump": is_pump
                        })
                except (KeyError, TypeError) as e:
                    continue

    # Sort: pump tokens first, then by balance
    tokens.sort(key=lambda x: (not x.get("is_pump", False), -x["balance"]))
    
    return tokens


def sell_token(mint: str, keypair: Keypair, pubkey: str, rpc: str) -> tuple[bool, str, str]:
    """Sell 100% of token via PumpPortal."""

    response = requests.post(
        url="https://pumpportal.fun/api/trade-local",
        data={
            "publicKey": pubkey,
            "action": "sell",
            "mint": mint,
            "amount": "100%",
            "denominatedInSol": "false",
            "slippage": 30,  # 30% slippage for memecoins
            "priorityFee": 0.0005,
            "pool": "auto"
        }
    )

    if response.status_code != 200:
        return False, None, f"PumpPortal error: {response.text}"

    # Sign TX
    tx = VersionedTransaction(
        VersionedTransaction.from_bytes(response.content).message,
        [keypair]
    )

    # Send via RPC
    commitment = CommitmentLevel.Confirmed
    config = RpcSendTransactionConfig(preflight_commitment=commitment)

    send_response = requests.post(
        url=rpc,
        headers={"Content-Type": "application/json"},
        data=SendVersionedTransaction(tx, config).to_json()
    )

    result = send_response.json()

    if "result" in result:
        return True, result["result"], None
    elif "error" in result:
        return False, None, str(result["error"])
    else:
        return False, None, str(result)


def remove_from_positions(mint: str, symbol: str):
    """Remove token from positions.json after successful sell."""
    if not POSITIONS_AVAILABLE:
        return

    try:
        positions = load_positions()
        for p in positions:
            if str(p.mint) == mint:
                remove_position(Pubkey.from_string(mint))
                print(f"  📝 Removed {symbol} from positions.json")
                return
    except Exception as e:
        print(f"  ⚠️ Could not update positions.json: {e}")


async def main():
    private_key = os.getenv("SOLANA_PRIVATE_KEY")
    rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT")

    if not private_key:
        print("❌ SOLANA_PRIVATE_KEY not set")
        return

    keypair = Keypair.from_base58_string(private_key)
    pubkey = str(keypair.pubkey())

    print(f"\nWallet: {pubkey}")
    print("Fetching tokens...\n")

    # Use standard RPC (works with all providers)
    tokens = get_all_tokens(pubkey)

    # Filter only pump.fun tokens for selling
    pump_tokens = [t for t in tokens if t.get("is_pump", False)]

    if not pump_tokens:
        print("No pump.fun tokens found!")
        
        # Show other tokens if any
        other_tokens = [t for t in tokens if not t.get("is_pump", False)]
        if other_tokens:
            print(f"\nOther tokens in wallet ({len(other_tokens)}):")
            for t in other_tokens[:10]:  # Show first 10
                print(f"  • {t['mint'][:20]}... ({t['balance']:.4f})")
        return

    print(f"Found {len(pump_tokens)} pump.fun tokens:\n")
    for i, t in enumerate(pump_tokens, 1):
        print(f"  {i}. {t['mint'][:32]}... ({t['balance']:.2f})")

    if "--dry-run" in sys.argv:
        print("\nDRY RUN - no sells executed")
        return

    confirm = input("\nType 'SELL' to confirm: ")
    if confirm != "SELL":
        print("Cancelled.")
        return

    print("\nSelling...\n")

    success = 0
    failed = 0

    for t in pump_tokens:
        print(f"Selling {t['mint'][:32]}...")

        try:
            ok, sig, error = sell_token(t["mint"], keypair, pubkey, rpc)

            if ok:
                print(f"  ✅ TX: {sig}")
                remove_from_positions(t["mint"], t.get("symbol", "PUMP"))
                success += 1
            else:
                print(f"  ❌ {error}")
                failed += 1
        except Exception as e:
            print(f"  ❌ {e}")
            failed += 1

        time.sleep(2)  # Delay between sells

    print(f"\nDONE: {success} sold, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
