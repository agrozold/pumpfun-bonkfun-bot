#!/usr/bin/env python3
"""
Dust cleaner v2 — smart cleanup with Jupiter swap for mid-value tokens.

Tiers:
  < $0.10  → burn tokens + close ATA (rent refund)
  $0.10-$0.40 → Jupiter swap to SOL + close ATA
  > $0.40  → keep (don't touch)

NO_SL mints are always protected.

Usage: python3 cleanup_dust.py [--dry] [--quiet]
"""
import os, sys, json, requests, base58, asyncio, time, base64
from dotenv import load_dotenv
load_dotenv('/opt/pumpfun-bonkfun-bot/.env')

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solana.rpc.async_api import AsyncClient
from spl.token.instructions import close_account, CloseAccountParams, burn, BurnParams

BURN_THRESHOLD = 0.10   # < $0.10 = burn + close
SWAP_THRESHOLD = 0.40   # $0.10-$0.40 = Jupiter swap + close; > $0.40 = keep
DRY_RUN = "--dry" in sys.argv
QUIET = "--quiet" in sys.argv

pk = os.getenv('SOLANA_PRIVATE_KEY')
rpc = os.getenv('SOLANA_NODE_RPC_ENDPOINT')
jupiter_api_key = os.getenv('JUPITER_TRADE_API_KEY', '')
kp = Keypair.from_bytes(base58.b58decode(pk))
wallet = kp.pubkey()

SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_SPL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Protect NO_SL mints
NO_SL_MINTS = {
    "FDBnaGYQeGjkLVs2E53yg5ErKnUd2xSjL5SQMLgGy4wP",
    "4aiLCRmCkVeVGZBTCFXYCGtW4MFsq4dWhGSyNnoGTrrv",
    "8MdkXe5G77xaMheVQxLqAYV8e2m2Dfc5ZbuXup2epump",
    "FzLMPzqz9Ybn26qRzPKDKwsLV6Kpvugh31jF7T7npump",
    "4Xu4fp2FV3gkdj4rnYS7gWpuKXFnXPwroHDKcMwapump",
    "4ZR1R4oW9B4Ufr15FDVLoEx3rhU7YKFTDL8qgAFPpump",
    "CZwnGa1scLnW6QFMYeofiaw2XzCjyMRiA2FTeyo1pump",
    "2PzS5SYYWjUFvzXNFaMmRkpjkxGX6R5v8DnKYtdcpump",
    "EW7cWbNmTgL7PLQNiJ6tBVC62SJzJXa2pFYJjDPPpump",
    "Hz4L8oCSTZoepnNDTtVqPqkPnSA2grNDLA6E6aF8pump",
    "8FaSmBzQdnBPjAt5wZ7k8WaCQqBHTM8YRB9ZsJ44bonk",
}
try:
    with open('/opt/pumpfun-bonkfun-bot/data/no_sl_mints.json') as f:
        NO_SL_MINTS |= set(json.load(f))
except:
    pass

# --- Collect all token accounts ---
rpc_endpoints = [r for r in [
    os.getenv('ALCHEMY_RPC_ENDPOINT', ''),
    os.getenv('CHAINSTACK_RPC_ENDPOINT', ''),
    rpc,
] if r]

all_accs = []
for prog in [TOKEN_SPL, TOKEN_2022]:
    resp = None
    for endpoint in rpc_endpoints:
        try:
            resp = requests.post(endpoint, json={
                'jsonrpc': '2.0', 'id': 1,
                'method': 'getTokenAccountsByOwner',
                'params': [str(wallet), {'programId': prog}, {'encoding': 'jsonParsed'}]
            }, timeout=15)
            if resp.status_code == 200 and 'result' in resp.json():
                break
        except:
            resp = None
    if not resp:
        continue
    for acc in resp.json().get('result', {}).get('value', []):
        info = acc['account']['data']['parsed']['info']
        all_accs.append({
            'mint': info['mint'],
            'raw': int(info['tokenAmount']['amount']),
            'decimals': int(info['tokenAmount']['decimals']),
            'amount': float(info['tokenAmount']['uiAmountString']),
            'ata': acc['pubkey'],
            'program': prog,
        })

# --- Get prices via DexScreener ---
mints_with_balance = [a['mint'] for a in all_accs if a['raw'] > 0]
prices = {}
for i in range(0, len(mints_with_balance), 30):
    batch = mints_with_balance[i:i+30]
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}", timeout=10)
        for pair in r.json().get('pairs', []):
            bm = pair.get('baseToken', {}).get('address', '')
            if bm in batch and bm not in prices:
                prices[bm] = {
                    'symbol': pair['baseToken'].get('symbol', '?'),
                    'priceUsd': float(pair.get('priceUsd', 0) or 0),
                }
    except:
        pass
    time.sleep(0.3)

# --- Categorize ---
to_keep = []
to_swap = []   # $0.10-$0.40 → Jupiter sell
to_burn = []   # < $0.10 → burn + close

for a in all_accs:
    p = prices.get(a['mint'], {})
    value = a['amount'] * p.get('priceUsd', 0)
    a['symbol'] = p.get('symbol', '???')
    a['value_usd'] = value

    if a['mint'] in NO_SL_MINTS:
        to_keep.append(a)
    elif a['raw'] == 0:
        to_burn.append(a)  # Empty ATA — always close
    elif value >= SWAP_THRESHOLD:
        to_keep.append(a)
    elif value >= BURN_THRESHOLD:
        to_swap.append(a)  # Mid-value → swap first
    else:
        to_burn.append(a)  # Low value → burn

# --- Protect active positions ---
import redis
r_client = redis.Redis()
redis_positions = set()
for k in r_client.hgetall('whale:positions'):
    redis_positions.add(k.decode())

to_swap = [a for a in to_swap if a['mint'] not in redis_positions]
to_burn = [a for a in to_burn if a['mint'] not in redis_positions]

if not QUIET:
    print(f"🛡️  Protected (NO_SL): {len([a for a in to_keep if a['mint'] in NO_SL_MINTS])}")
    print(f"🛡️  Active positions: {len(redis_positions)}")
    print(f"💰 Keep (>${SWAP_THRESHOLD:.2f}): {len([a for a in to_keep if a['mint'] not in NO_SL_MINTS])}")
    print(f"🔄 Swap (${BURN_THRESHOLD:.2f}-${SWAP_THRESHOLD:.2f}): {len(to_swap)}")
    print(f"🗑️  Burn (<${BURN_THRESHOLD:.2f}): {len(to_burn)}")

if to_swap and not QUIET:
    print(f"\n🔄 To swap via Jupiter:")
    for t in sorted(to_swap, key=lambda x: -x['value_usd']):
        prog_label = "T22" if "Tokenz" in t['program'] else "SPL"
        print(f"  [{prog_label}] {t['symbol']:<15} ${t['value_usd']:>8.4f}  {t['mint'][:35]}...")

if to_burn and not QUIET:
    print(f"\n🗑️  To burn:")
    for t in sorted(to_burn, key=lambda x: -x['value_usd'])[:20]:
        prog_label = "T22" if "Tokenz" in t['program'] else "SPL"
        if t['raw'] == 0:
            print(f"  [{prog_label}] EMPTY ATA                       {t['mint'][:35]}...")
        else:
            print(f"  [{prog_label}] {t['symbol']:<15} ${t['value_usd']:>8.4f}  {t['mint'][:35]}...")
    if len(to_burn) > 20:
        print(f"  ... and {len(to_burn) - 20} more")

total_rent = (len(to_swap) + len(to_burn)) * 0.00204
swap_value = sum(t['value_usd'] for t in to_swap)
burn_value = sum(t['value_usd'] for t in to_burn)
if not QUIET:
    print(f"\n💰 Swap value: ~${swap_value:.3f} (recovered as SOL)")
    print(f"🗑️  Burn value lost: ~${burn_value:.4f}")
    print(f"💰 Rent return: ~{total_rent:.4f} SOL")

if DRY_RUN:
    if not QUIET:
        print(f"\n⚠️  DRY RUN — nothing executed. Remove --dry to run.")
    sys.exit(0)

if not to_swap and not to_burn:
    if not QUIET:
        print(f"\n✅ Nothing to clean!")
    sys.exit(0)

# ============================================================
# EXECUTION
# ============================================================

BATCH_SIZE = 8

async def jupiter_swap(session, item, rpc_client):
    """Swap token to SOL via Jupiter, then close ATA."""
    mint = item['mint']
    raw_amount = item['raw']
    symbol = item['symbol']

    headers = {}
    if jupiter_api_key:
        headers["x-api-key"] = jupiter_api_key

    # 1. Get quote
    quote_params = {
        "inputMint": mint,
        "outputMint": SOL_MINT,
        "amount": str(raw_amount),
        "slippageBps": "2500",  # 25% slippage for dust
        "restrictIntermediateTokens": "true",
    }
    try:
        async with session.get("https://api.jup.ag/swap/v1/quote", params=quote_params, headers=headers) as resp:
            if resp.status != 200:
                err = await resp.text()
                return False, f"Quote failed: {err[:80]}"
            quote = await resp.json()
    except Exception as e:
        return False, f"Quote error: {e}"

    out_sol = int(quote.get("outAmount", 0)) / 1e9

    # 2. Build swap TX
    swap_body = {
        "quoteResponse": quote,
        "userPublicKey": str(wallet),
        "wrapAndUnwrapSol": True,
        "prioritizationFeeLamports": 200000,  # 0.0002 SOL — minimal for dust
        "dynamicComputeUnitLimit": True,
        "asLegacyTransaction": False,
    }
    try:
        async with session.post("https://api.jup.ag/swap/v1/swap", json=swap_body, headers=headers) as resp:
            if resp.status != 200:
                err = await resp.text()
                return False, f"Swap build failed: {err[:80]}"
            swap_data = await resp.json()
    except Exception as e:
        return False, f"Swap build error: {e}"

    swap_tx_b64 = swap_data.get("swapTransaction")
    if not swap_tx_b64:
        return False, "No swapTransaction in response"

    # 3. Sign and send
    try:
        tx_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [kp])
        result = await rpc_client.send_transaction(signed_tx)
        sig = str(result.value)
        if not QUIET:
            print(f"  ✅ SWAP {symbol:<12} ${item['value_usd']:.3f} -> ~{out_sol:.5f} SOL | {sig[:40]}...")
        return True, sig
    except Exception as e:
        return False, f"Send failed: {e}"


async def close_ata_batch(rpc_client, items):
    """Close ATAs after successful swap (tokens already swapped to SOL)."""
    ixs = [
        set_compute_unit_limit(200_000 + len(items) * 30_000),
        set_compute_unit_price(10_000),
    ]
    for item in items:
        ata_pk = Pubkey.from_string(item['ata'])
        prog_pk = Pubkey.from_string(item['program'])
        ixs.append(close_account(CloseAccountParams(
            program_id=prog_pk, account=ata_pk,
            dest=wallet, owner=wallet,
        )))
    bh = await rpc_client.get_latest_blockhash()
    msg = MessageV0.try_compile(payer=wallet, instructions=ixs,
        address_lookup_table_accounts=[], recent_blockhash=bh.value.blockhash)
    tx = VersionedTransaction(msg, [kp])
    result = await rpc_client.send_transaction(tx)
    return str(result.value)


async def main():
    import aiohttp

    async with AsyncClient(rpc) as rpc_client, aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        swap_ok = 0
        swap_fail = 0
        burn_ok = 0
        burn_fail = 0
        swapped_items = []  # Successfully swapped — close ATAs later

        # --- Phase 1: Jupiter swaps ($0.10 - $0.40) ---
        if to_swap:
            if not QUIET:
                print(f"\n🔄 Swapping {len(to_swap)} tokens via Jupiter...")
            for item in to_swap:
                ok, msg = await jupiter_swap(http, item, rpc_client)
                if ok:
                    swap_ok += 1
                    swapped_items.append(item)
                else:
                    swap_fail += 1
                    if not QUIET:
                        print(f"  ❌ SWAP {item['symbol']:<12} FAILED: {msg}")
                await asyncio.sleep(0.5)  # Rate limit

            # Close ATAs of swapped tokens in batches
            if swapped_items:
                if not QUIET:
                    print(f"\n  Closing {len(swapped_items)} swapped ATAs...")
                await asyncio.sleep(2)  # Wait for swaps to confirm
                for i in range(0, len(swapped_items), BATCH_SIZE):
                    batch = swapped_items[i:i+BATCH_SIZE]
                    try:
                        sig = await close_ata_batch(rpc_client, batch)
                        if not QUIET:
                            print(f"  ✅ Closed {len(batch)} ATAs: {sig[:40]}...")
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        if not QUIET:
                            print(f"  ⚠️  ATA close failed: {e}")

        # --- Phase 2: Burn low-value tokens (< $0.10) ---
        if to_burn:
            if not QUIET:
                print(f"\n🗑️  Burning {len(to_burn)} tokens...")
            batches = [to_burn[i:i+BATCH_SIZE] for i in range(0, len(to_burn), BATCH_SIZE)]

            for bi, batch in enumerate(batches):
                try:
                    ixs = [
                        set_compute_unit_limit(300_000 + len(batch) * 50_000),
                        set_compute_unit_price(10_000),
                    ]
                    for item in batch:
                        mint_pk = Pubkey.from_string(item['mint'])
                        ata_pk = Pubkey.from_string(item['ata'])
                        prog_pk = Pubkey.from_string(item['program'])
                        if item['raw'] > 0:
                            ixs.append(burn(BurnParams(
                                program_id=prog_pk, account=ata_pk,
                                mint=mint_pk, owner=wallet, amount=item['raw'],
                            )))
                        ixs.append(close_account(CloseAccountParams(
                            program_id=prog_pk, account=ata_pk,
                            dest=wallet, owner=wallet,
                        )))

                    bh = await rpc_client.get_latest_blockhash()
                    msg = MessageV0.try_compile(payer=wallet, instructions=ixs,
                        address_lookup_table_accounts=[], recent_blockhash=bh.value.blockhash)
                    tx = VersionedTransaction(msg, [kp])
                    result = await rpc_client.send_transaction(tx)
                    burn_ok += len(batch)
                    if not QUIET:
                        print(f"  ✅ Batch {bi+1}/{len(batches)}: {len(batch)} burned | {str(result.value)[:40]}...")
                    await asyncio.sleep(0.5)
                except Exception as e:
                    # Retry individually
                    for item in batch:
                        try:
                            ixs2 = [set_compute_unit_limit(200_000), set_compute_unit_price(10_000)]
                            mint_pk = Pubkey.from_string(item['mint'])
                            ata_pk = Pubkey.from_string(item['ata'])
                            prog_pk = Pubkey.from_string(item['program'])
                            if item['raw'] > 0:
                                ixs2.append(burn(BurnParams(program_id=prog_pk, account=ata_pk,
                                    mint=mint_pk, owner=wallet, amount=item['raw'])))
                            ixs2.append(close_account(CloseAccountParams(program_id=prog_pk,
                                account=ata_pk, dest=wallet, owner=wallet)))
                            bh = await rpc_client.get_latest_blockhash()
                            msg = MessageV0.try_compile(payer=wallet, instructions=ixs2,
                                address_lookup_table_accounts=[], recent_blockhash=bh.value.blockhash)
                            tx = VersionedTransaction(msg, [kp])
                            await rpc_client.send_transaction(tx)
                            burn_ok += 1
                            await asyncio.sleep(0.3)
                        except:
                            burn_fail += 1

        # --- Cleanup Redis/positions.json ---
        all_cleaned = {t['mint'] for t in to_burn} | {t['mint'] for t in swapped_items}
        try:
            with open('/opt/pumpfun-bonkfun-bot/positions.json') as f:
                positions = json.load(f)
            before = len(positions)
            positions = [p for p in positions if p['mint'] not in all_cleaned]
            if len(positions) < before:
                with open('/opt/pumpfun-bonkfun-bot/positions.json', 'w') as f:
                    json.dump(positions, f, indent=2)
                if not QUIET:
                    print(f"\n  📝 positions.json: {before} -> {len(positions)}")
            import subprocess
            for m in all_cleaned:
                subprocess.run(["redis-cli", "HDEL", "whale:positions", m], capture_output=True)
                subprocess.run(["redis-cli", "SADD", "sold_mints", m], capture_output=True)
        except:
            pass

        # --- Summary ---
        if not QUIET:
            print(f"\n📊 Results:")
            print(f"  Swapped: {swap_ok} ok, {swap_fail} failed")
            print(f"  Burned:  {burn_ok} ok, {burn_fail} failed")
            print(f"  Rent:    ~{(swap_ok + burn_ok) * 0.00204:.4f} SOL")
        else:
            total = swap_ok + burn_ok
            if total > 0:
                print(f"[DUST] Swapped {swap_ok}, burned {burn_ok}, rent ~{total * 0.00204:.4f} SOL")

asyncio.run(main())
