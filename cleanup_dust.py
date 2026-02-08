#!/usr/bin/env python3
"""Найти и сжечь/закрыть мусорные позиции < порога"""
import os, sys, json, requests, base58, asyncio, time
from dotenv import load_dotenv
load_dotenv('/opt/pumpfun-bonkfun-bot/.env')

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solana.rpc.async_api import AsyncClient
from spl.token.instructions import close_account, CloseAccountParams, burn, BurnParams

THRESHOLD = float(sys.argv[1]) if len(sys.argv) > 1 else 0.4
DRY_RUN = "--dry" in sys.argv

pk = os.getenv('SOLANA_PRIVATE_KEY')
rpc = os.getenv('SOLANA_NODE_RPC_ENDPOINT')
kp = Keypair.from_bytes(base58.b58decode(pk))
wallet = kp.pubkey()

TOKEN_SPL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Защита: позиции бота
try:
    with open('/opt/pumpfun-bonkfun-bot/positions.json') as f:
        bot_mints = {p['mint'] for p in json.load(f)}
except:
    bot_mints = set()

try:
    with open('/opt/pumpfun-bonkfun-bot/data/no_sl_mints.json') as f:
        no_sl = set(json.load(f))
except:
    no_sl = set()

PROTECTED = bot_mints | no_sl

# Собираем все аккаунты (SPL + Token2022)
all_accs = []
for prog in [TOKEN_SPL, TOKEN_2022]:
    resp = requests.post(rpc, json={
        'jsonrpc': '2.0', 'id': 1,
        'method': 'getTokenAccountsByOwner',
        'params': [str(wallet), {'programId': prog}, {'encoding': 'jsonParsed'}]
    })
    for acc in resp.json().get('result', {}).get('value', []):
        info = acc['account']['data']['parsed']['info']
        all_accs.append({
            'mint': info['mint'],
            'raw': int(info['tokenAmount']['amount']),
            'amount': float(info['tokenAmount']['uiAmountString']),
            'ata': acc['pubkey'],
            'program': prog,
        })

# Цены через DexScreener
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

# Разделяем
to_keep = []
to_burn = []

for a in all_accs:
    p = prices.get(a['mint'], {})
    value = a['amount'] * p.get('priceUsd', 0)
    a['symbol'] = p.get('symbol', '???')
    a['value_usd'] = value
    
    if a['mint'] in PROTECTED:
        to_keep.append(a)
    elif value >= THRESHOLD:
        to_keep.append(a)
    else:
        to_burn.append(a)

print(f"🛡️  Защищено (бот/no_sl): {len([a for a in to_keep if a['mint'] in PROTECTED])}")
print(f"💰 Выше ${THRESHOLD:.2f}: {len([a for a in to_keep if a['mint'] not in PROTECTED])}")
print(f"🗑️  Мусор (<${THRESHOLD:.2f}): {len(to_burn)}")

if to_burn:
    print(f"\n🗑️  К удалению:")
    for t in sorted(to_burn, key=lambda x: -x['value_usd'])[:20]:
        prog_label = "T22" if "Tokenz" in t['program'] else "SPL"
        print(f"  [{prog_label}] {t['symbol']:<15} ${t['value_usd']:>8.4f}  {t['mint'][:35]}...")
    if len(to_burn) > 20:
        print(f"  ... и ещё {len(to_burn) - 20}")
    print(f"\n💰 Возврат ренты: ~{len(to_burn) * 0.00204:.4f} SOL")

if DRY_RUN:
    print(f"\n⚠️  DRY RUN — ничего не удалено. Убери --dry чтобы удалить.")
    sys.exit(0)

if not to_burn:
    print(f"\n✅ Нечего удалять!")
    sys.exit(0)

print(f"\n🚀 Сжигаю {len(to_burn)} аккаунтов...")

BATCH_SIZE = 8

async def main():
    async with AsyncClient(rpc) as client:
        total_ok = 0
        total_fail = 0
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
                
                bh = await client.get_latest_blockhash()
                msg = MessageV0.try_compile(payer=wallet, instructions=ixs,
                    address_lookup_table_accounts=[], recent_blockhash=bh.value.blockhash)
                tx = VersionedTransaction(msg, [kp])
                result = await client.send_transaction(tx)
                total_ok += len(batch)
                print(f"  ✅ Batch {bi+1}/{len(batches)}: {len(batch)} tokens | {str(result.value)[:45]}...")
                await asyncio.sleep(0.5)
            except Exception as e:
                # По одному
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
                        bh = await client.get_latest_blockhash()
                        msg = MessageV0.try_compile(payer=wallet, instructions=ixs2,
                            address_lookup_table_accounts=[], recent_blockhash=bh.value.blockhash)
                        tx = VersionedTransaction(msg, [kp])
                        await client.send_transaction(tx)
                        total_ok += 1
                        await asyncio.sleep(0.3)
                    except:
                        total_fail += 1
        
        # Убираем сожжённые из positions.json и Redis
        burned_mints = {t['mint'] for t in to_burn}
        try:
            with open('/opt/pumpfun-bonkfun-bot/positions.json') as f:
                positions = json.load(f)
            before = len(positions)
            positions = [p for p in positions if p['mint'] not in burned_mints]
            if len(positions) < before:
                with open('/opt/pumpfun-bonkfun-bot/positions.json', 'w') as f:
                    json.dump(positions, f, indent=2)
                print(f"  📝 positions.json: {before} -> {len(positions)}")
                import subprocess
                for m in burned_mints:
                    subprocess.run(["redis-cli", "HDEL", "whale:positions", m], capture_output=True)
        except:
            pass
        
        print(f"\n📊 Закрыто: {total_ok}, ошибок: {total_fail}")
        print(f"💰 Возврат: ~{total_ok * 0.00204:.4f} SOL")

asyncio.run(main())
