#!/usr/bin/env python3
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

from solders.pubkey import Pubkey
from solders.keypair import Keypair
import base58

def get_wallet():
    pk = os.environ.get('SOLANA_PRIVATE_KEY')
    if not pk:
        raise ValueError("SOLANA_PRIVATE_KEY not found")
    pk = pk.strip()
    if pk.startswith('['):
        key_bytes = bytes(json.loads(pk))
    else:
        key_bytes = base58.b58decode(pk)
    return Keypair.from_bytes(key_bytes)

async def sell_all():
    from trading.fallback_seller import FallbackSeller
    from core.client import SolanaClient
    
    positions_path = os.path.join(os.path.dirname(__file__), '..', 'positions.json')
    
    with open(positions_path, 'r') as f:
        positions = json.load(f)
    
    if not positions:
        print("Нет позиций")
        return
    
    keypair = get_wallet()
    
    class WalletWrapper:
        def __init__(self, kp):
            self.keypair = kp
            self.pubkey = kp.pubkey()
    
    wallet = WalletWrapper(keypair)
    
    rpc_url = os.environ.get('CHAINSTACK_RPC_ENDPOINT') or os.environ.get('ALCHEMY_RPC_ENDPOINT')
    client = SolanaClient(rpc_url)
    
    print("=" * 50)
    print(f"Кошелек: {wallet.pubkey}")
    print(f"Позиций: {len(positions)}")
    print("=" * 50)
    
    seller = FallbackSeller(
        client=client,
        wallet=wallet,
        slippage=0.25,
        priority_fee=100000,
        max_retries=3,
        jupiter_api_key=os.environ.get('JUPITER_API_KEY'),
    )
    
    results = []
    for i, pos in enumerate(positions, 1):
        mint = pos['mint']
        symbol = pos.get('symbol') or mint[:8]
        quantity = pos['quantity']
        
        print(f"[{i}/{len(positions)}] {symbol}: {quantity:.2f}...")
        
        try:
            success, sig, error = await seller.sell(
                mint=Pubkey.from_string(mint),
                token_amount=quantity,
                symbol=symbol
            )
            
            if success:
                print(f"  OK: {sig[:40]}...")
                results.append((symbol, mint, True))
            else:
                print(f"  FAIL: {error}")
                results.append((symbol, mint, False))
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append((symbol, mint, False))
        
        await asyncio.sleep(1)
    
    print()
    success_count = sum(1 for r in results if r[2])
    print(f"Продано: {success_count}/{len(results)}")
    
    if success_count > 0:
        sold_mints = {r[1] for r in results if r[2]}
        remaining = [p for p in positions if p['mint'] not in sold_mints]
        with open(positions_path, 'w') as f:
            json.dump(remaining, f, indent=2)
        print(f"Осталось: {len(remaining)}")

if __name__ == '__main__':
    asyncio.run(sell_all())
