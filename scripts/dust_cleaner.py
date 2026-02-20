#!/usr/bin/env python3
"""
Dust cleaner — closes token accounts that are worth less than the rent refund.
Usage: 
  dust              — close ATAs where token value < rent refund (always profitable)
  dust 0.5          — close ATAs where token value < $0.5 USD
  dust --dry        — show what would be closed, don't send TX
  dust 0.5 --dry    — combine both
"""
import json
import sys
import os
import time
import urllib.request

sys.path.insert(0, '/opt/pumpfun-bonkfun-bot')

from solders.keypair import Keypair
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts, TxOpts
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import close_account, CloseAccountParams
from solana.transaction import Transaction
import redis

RENT_REFUND_SOL = 0.00203  # ~rent per ATA

def load_env():
    env_file = '/opt/pumpfun-bonkfun-bot/.env'
    pk = rpc = None
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith('SOLANA_PRIVATE_KEY='): pk = line.split('=',1)[1].strip('"').strip("'")
            if line.startswith('SOLANA_NODE_RPC_ENDPOINT='): rpc = line.split('=',1)[1].strip('"').strip("'")
    return pk, rpc

def get_sol_price():
    try:
        resp = urllib.request.urlopen('https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112', timeout=5)
        data = json.loads(resp.read())
        return float(data['data']['So11111111111111111111111111111111111111112']['price'])
    except Exception:
        return 80.0

def get_token_price_usd(mint):
    try:
        resp = urllib.request.urlopen(f'https://api.jup.ag/price/v2?ids={mint}', timeout=3)
        data = json.loads(resp.read())
        if mint in data.get('data', {}) and data['data'][mint].get('price'):
            return float(data['data'][mint]['price'])
    except Exception:
        pass
    return 0.0

def main():
    # Parse args
    dry_run = '--dry' in sys.argv
    args = [a for a in sys.argv[1:] if a != '--dry']
    threshold_usd = float(args[0]) if args else None  # None = auto (rent-based)
    
    pk, rpc = load_env()
    kp = Keypair.from_base58_string(pk)
    client = Client(rpc)
    
    sol_price = get_sol_price()
    rent_usd = RENT_REFUND_SOL * sol_price
    
    if threshold_usd is None:
        threshold_usd = rent_usd  # Only close if token value < rent refund
        mode = f"RENT-PROFITABLE (token value < rent ${rent_usd:.3f})"
    else:
        mode = f"MANUAL threshold ${threshold_usd:.2f}"
    
    print(f"[DUST] SOL: ${sol_price:.2f} | Rent refund: {RENT_REFUND_SOL} SOL (${rent_usd:.3f}) | Mode: {mode}")
    if dry_run:
        print("[DUST] DRY RUN — no transactions will be sent\n")
    
    opts = TokenAccountOpts(program_id=TOKEN_PROGRAM_ID)
    resp = client.get_token_accounts_by_owner_json_parsed(kp.pubkey(), opts)
    
    # Protect active Redis positions
    r = redis.Redis()
    redis_positions = set()
    for k in r.hgetall('whale:positions'):
        redis_positions.add(k.decode())
    
    dust_accounts = []
    protected = 0
    kept = 0
    total_value_dust = 0
    
    for acc in resp.value:
        info = json.loads(acc.account.data.to_json())
        parsed = info['parsed']['info']
        mint = parsed['mint']
        amount = int(parsed['tokenAmount']['amount'])
        decimals = int(parsed['tokenAmount']['decimals'])
        ui_amount = amount / (10 ** decimals) if decimals > 0 else amount
        ata_pubkey = acc.pubkey
        
        if mint in redis_positions:
            protected += 1
            continue
        
        if amount == 0:
            # Empty ATA — always close, pure rent profit
            dust_accounts.append({
                'mint': mint, 'ata': ata_pubkey,
                'amount': amount, 'ui_amount': 0, 'usd_value': 0,
            })
            continue
        
        token_price = get_token_price_usd(mint)
        usd_value = ui_amount * token_price
        
        if usd_value < threshold_usd:
            dust_accounts.append({
                'mint': mint, 'ata': ata_pubkey,
                'amount': amount, 'ui_amount': ui_amount, 'usd_value': usd_value,
            })
            total_value_dust += usd_value
        else:
            kept += 1
            print(f"  [KEEP] {mint[:16]}... value=${usd_value:.4f} > threshold")
    
    print(f"\n[DUST] Dust: {len(dust_accounts)} | Protected: {protected} | Kept: {kept}")
    
    if not dust_accounts:
        print("[DUST] Nothing to clean!")
        return
    
    empty = sum(1 for d in dust_accounts if d['amount'] == 0)
    with_tokens = len(dust_accounts) - empty
    
    for d in dust_accounts:
        if d['amount'] == 0:
            print(f"  [EMPTY ATA] {d['mint'][:16]}...")
        else:
            print(f"  [DUST]      {d['mint'][:16]}... tokens={d['ui_amount']:.2f} value=${d['usd_value']:.4f}")
    
    rent_total = len(dust_accounts) * RENT_REFUND_SOL
    net_profit_usd = (rent_total * sol_price) - total_value_dust
    print(f"\n  Rent reclaim: {rent_total:.5f} SOL (${rent_total * sol_price:.3f})")
    print(f"  Token value lost: ${total_value_dust:.4f}")
    print(f"  Net profit: ${net_profit_usd:.3f}")
    
    if dry_run:
        return
    
    # Close in batches of 5
    closed = 0
    batch_size = 5
    for i in range(0, len(dust_accounts), batch_size):
        batch = dust_accounts[i:i+batch_size]
        tx = Transaction()
        
        recent_blockhash = client.get_latest_blockhash().value.blockhash
        tx.recent_blockhash = recent_blockhash
        tx.fee_payer = kp.pubkey()
        
        for d in batch:
            ix = close_account(CloseAccountParams(
                program_id=TOKEN_PROGRAM_ID,
                account=d['ata'],
                dest=kp.pubkey(),
                owner=kp.pubkey(),
            ))
            tx.add(ix)
        
        try:
            result = client.send_transaction(tx, kp, opts=TxOpts(skip_preflight=True))
            sig = str(result.value)
            closed += len(batch)
            print(f"  [TX] Closed {len(batch)} accounts: {sig[:30]}...")
            time.sleep(1)
        except Exception as e:
            print(f"  [ERROR] Batch failed: {e}")
    
    rent_reclaimed = closed * RENT_REFUND_SOL
    print(f"\n[DUST] Done! Closed: {closed} | Rent: +{rent_reclaimed:.5f} SOL (${rent_reclaimed * sol_price:.3f})")

if __name__ == '__main__':
    main()
