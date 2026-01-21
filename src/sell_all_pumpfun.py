#!/usr/bin/env python3
"""
Sell all pump.fun/BAGS/BONK tokens via PumpPortal trade-local API.
Works with Token-2022 via DRPC/Helius DAS API.
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


def get_all_tokens_das(wallet_pubkey: str) -> list[dict]:
    """Get all tokens using DAS API (DRPC or Helius - supports Token-2022)."""
    
    # Try DRPC first, then Helius
    drpc_endpoint = os.getenv("DRPC_RPC_ENDPOINT")
    helius_key = os.getenv("HELIUS_API_KEY")
    
    # DRPC with DAS support
    if drpc_endpoint:
        api_url = drpc_endpoint
    elif helius_key:
        api_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
    else:
        print("⚠️  No DRPC or HELIUS API configured")
        return []
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAssetsByOwner",
        "params": {
            "ownerAddress": wallet_pubkey,
            "page": 1,
            "limit": 100,
            "displayOptions": {"showFungible": True}
        }
    }
    
    try:
        resp = requests.post(api_url, json=payload, timeout=30)
        data = resp.json()
    except Exception as e:
        print(f"⚠️  DAS API error: {e}")
        return []
    
    tokens = []
    if 'result' in data and 'items' in data['result']:
        for item in data['result']['items']:
            mint = item.get('id')
            if not mint or mint in SKIP_TOKENS:
                continue
            
            # Only pump.fun/BAGS/BONK tokens (ending with 'pump')
            if not mint.endswith('pump'):
                continue
            
            token_info = item.get('token_info', {})
            balance = token_info.get('balance', 0)
            decimals = token_info.get('decimals', 6)
            
            if balance and int(balance) > 0:
                content = item.get('content', {})
                meta = content.get('metadata', {})
                symbol = meta.get('symbol') or meta.get('name') or 'UNKNOWN'
                
                tokens.append({
                    "mint": mint,
                    "symbol": symbol,
                    "balance": int(balance) / (10 ** decimals)
                })
    elif 'error' in data:
        print(f"⚠️  DAS API returned error: {data['error']}")
        # Fallback to standard RPC
        return get_all_tokens_rpc(wallet_pubkey)
    
    return tokens


def get_all_tokens_rpc(wallet_pubkey: str) -> list[dict]:
    """Fallback: Get tokens using standard RPC (may miss Token-2022)."""
    rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    
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
            print(f"⚠️  RPC error: {e}")
            continue
        
        if "result" in data and "value" in data["result"]:
            for account in data["result"]["value"]:
                info = account["account"]["data"]["parsed"]["info"]
                mint = info["mint"]
                balance = float(info["tokenAmount"]["uiAmount"] or 0)
                
                if balance > 0 and mint not in SKIP_TOKENS and mint.endswith("pump"):
                    tokens.append({
                        "mint": mint,
                        "symbol": "UNKNOWN",
                        "balance": balance
                    })
    
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
    print("Fetching tokens (DRPC/Helius DAS API)...\n")
    
    # Use DAS API for Token-2022 support
    tokens = get_all_tokens_das(pubkey)
    
    if not tokens:
        print("No pump.fun/BAGS/BONK tokens found!")
        return
    
    print(f"Found {len(tokens)} tokens:\n")
    for i, t in enumerate(tokens, 1):
        print(f"  {i}. {t['symbol']:12} | {t['mint'][:20]}... ({t['balance']:.2f})")
    
    if "--dry-run" in sys.argv:
        print("\nDRY RUN - no sells executed")
        return
    
    confirm = input("\nType 'SELL' to confirm: ")
    if confirm != "SELL":
        print("Cancelled.")
        return
    
    print(f"\nSelling...\n")
    
    success = 0
    failed = 0
    
    for t in tokens:
        print(f"Selling {t['symbol']} ({t['mint'][:20]}...)...")
        
        try:
            ok, sig, error = sell_token(t["mint"], keypair, pubkey, rpc)
            
            if ok:
                print(f"  ✅ TX: {sig}")
                remove_from_positions(t["mint"], t["symbol"])
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
