#!/usr/bin/env python3
"""
Sell all pump.fun tokens via PumpPortal trade-local API.
"""

import asyncio
import aiohttp
import requests
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.commitment_config import CommitmentLevel
from solders.rpc.requests import SendVersionedTransaction
from solders.rpc.config import RpcSendTransactionConfig

load_dotenv()

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

SKIP_TOKENS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}


async def get_all_tokens(wallet_pubkey: str) -> list[dict]:
    """Get all pump.fun tokens."""
    rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    tokens = []
    
    async with aiohttp.ClientSession() as session:
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
            
            async with session.post(rpc_endpoint, json=payload) as resp:
                data = await resp.json()
            
            if "result" in data and "value" in data["result"]:
                for account in data["result"]["value"]:
                    info = account["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    balance = float(info["tokenAmount"]["uiAmount"] or 0)
                    
                    if balance > 0 and mint not in SKIP_TOKENS and mint.endswith("pump"):
                        tokens.append({"mint": mint, "balance": balance})
    
    return tokens


def sell_token(mint: str, keypair: Keypair, pubkey: str, rpc: str) -> tuple[bool, str, str]:
    """Sell 100% of token."""
    
    # Get unsigned TX from PumpPortal
    response = requests.post(
        url="https://pumpportal.fun/api/trade-local",
        data={
            "publicKey": pubkey,
            "action": "sell",
            "mint": mint,
            "amount": "100%",
            "denominatedInSol": "false",
            "slippage": 25,
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


async def main():
    private_key = os.getenv("SOLANA_PRIVATE_KEY")
    rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    
    keypair = Keypair.from_base58_string(private_key)
    pubkey = str(keypair.pubkey())
    
    print(f"\nWallet: {pubkey}")
    print("Fetching tokens...\n")
    
    tokens = await get_all_tokens(pubkey)
    
    if not tokens:
        print("No pump.fun tokens!")
        return
    
    print(f"Found {len(tokens)} tokens:\n")
    for i, t in enumerate(tokens, 1):
        print(f"  {i}. {t['mint'][:20]}... ({t['balance']:.2f})")
    
    if "--dry-run" in sys.argv:
        print("\nDRY RUN")
        return
    
    confirm = input("\nType 'SELL' to confirm: ")
    if confirm != "SELL":
        print("Cancelled.")
        return
    
    print(f"\nSelling...\n")
    
    success = 0
    failed = 0
    
    for t in tokens:
        print(f"Selling {t['mint'][:20]}...")
        
        try:
            ok, sig, error = sell_token(t["mint"], keypair, pubkey, rpc)
            
            if ok:
                print(f"  ✅ TX: {sig}")
                success += 1
            else:
                print(f"  ❌ {error}")
                failed += 1
        except Exception as e:
            print(f"  ❌ {e}")
            failed += 1
        
        import time
        time.sleep(2)  # Delay between sells
    
    print(f"\nDONE: {success} sold, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
