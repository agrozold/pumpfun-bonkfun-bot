#!/usr/bin/env python3
"""
Zombie position cleaner — removes Redis positions with no on-chain balance.
Supports both TOKEN_PROGRAM_ID and Token2022.
Also syncs positions.json.
"""
import redis
import json
import sys
import os

sys.path.insert(0, '/opt/pumpfun-bonkfun-bot')

from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts

# Both token programs
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")

CHAINSTACK_RPC = "https://solana-mainnet.core.chainstack.com/28c858a6ec92aafc2569516da978dfb8"

def load_env():
    env_file = '/opt/pumpfun-bonkfun-bot/.env'
    pk = rpc = None
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith('SOLANA_PRIVATE_KEY='): pk = line.split('=',1)[1].strip('"').strip("'")
            if line.startswith('SOLANA_NODE_RPC_ENDPOINT='): rpc = line.split('=',1)[1].strip('"').strip("'")
    return pk, rpc

def get_wallet_balances(wallet_pubkey, rpc_url):
    """Get all token balances using both TOKEN_PROGRAM and TOKEN_2022."""
    client = Client(rpc_url)
    balances = {}

    for program_id, program_name in [(TOKEN_PROGRAM_ID, "SPL"), (TOKEN_2022_PROGRAM_ID, "Token2022")]:
        try:
            opts = TokenAccountOpts(program_id=program_id)
            resp = client.get_token_accounts_by_owner_json_parsed(wallet_pubkey, opts)
            for acc in resp.value:
                info = json.loads(acc.account.data.to_json())
                parsed = info['parsed']['info']
                mint = parsed['mint']
                amount = int(parsed['tokenAmount']['amount'])
                ui_amount = float(parsed['tokenAmount']['uiAmount'] or 0)
                if amount > 0:
                    balances[mint] = {'raw': amount, 'ui': ui_amount, 'program': program_name}
        except Exception as e:
            print(f"  [WARN] {program_name} scan error: {e}")

    return balances

def clean_zombies(verbose=True, dry_run=False):
    pk, _ = load_env()
    from solders.keypair import Keypair
    kp = Keypair.from_base58_string(pk)
    wallet = kp.pubkey()

    # Use Chainstack (paid RPC) for reliable balance checks
    balances = get_wallet_balances(wallet, CHAINSTACK_RPC)

    if verbose:
        print(f"[INFO] Found {len(balances)} tokens with balance on wallet")

    r = redis.Redis()

    # === Clean whale:positions hash ===
    data = r.hgetall('whale:positions')
    removed_redis = 0
    kept_redis = 0

    for k, v in data.items():
        mint = k.decode()
        p = json.loads(v)
        name = p.get('symbol', p.get('token_name', mint[:8]))

        if mint not in balances:
            if dry_run:
                print(f"  [DRY-RUN REMOVE] {name} ({mint[:12]}...)")
            else:
                r.hdel('whale:positions', mint)
                print(f"  [REMOVED] {name} ({mint[:12]}...)")
            removed_redis += 1
        else:
            kept_redis += 1
            if verbose:
                b = balances[mint]
                moonbag = " [MOONBAG]" if p.get('is_moonbag') else ""
                prog = f" [{b['program']}]" if b['program'] != 'SPL' else ""
                print(f"  [KEPT]{moonbag}{prog} {name} ({mint[:12]}...) bal={b['ui']:.4f}")

    print(f"\n[REDIS] Removed: {removed_redis} | Kept: {kept_redis}")

    # === Sync positions.json ===
    positions_file = '/opt/pumpfun-bonkfun-bot/positions.json'
    if os.path.exists(positions_file):
        try:
            with open(positions_file) as f:
                positions = json.load(f)

            original_count = len(positions)
            alive = [p for p in positions if p.get('mint', '') in balances]
            removed_json = original_count - len(alive)

            if removed_json > 0:
                if dry_run:
                    print(f"[DRY-RUN] Would remove {removed_json} zombies from positions.json")
                else:
                    # Backup first
                    import shutil
                    shutil.copy2(positions_file, positions_file + '.bak')
                    with open(positions_file, 'w') as f:
                        json.dump(alive, f, indent=2, default=str)
                    print(f"[JSON] Removed {removed_json} zombies from positions.json (backup: .bak)")
            else:
                print(f"[JSON] Clean — {len(alive)} positions, no zombies")
        except Exception as e:
            print(f"[JSON] Error: {e}")
    else:
        print(f"[JSON] {positions_file} not found")

    return removed_redis + removed_json

if __name__ == '__main__':
    dry = '--dry-run' in sys.argv
    quiet = '--quiet' in sys.argv
    if dry:
        print("=== DRY RUN MODE ===")
    clean_zombies(verbose=(not quiet), dry_run=dry)
