#!/usr/bin/env python3
"""Найти какой кит купил токен"""
import os, sys, json, requests, datetime
from dotenv import load_dotenv
load_dotenv('/opt/pumpfun-bonkfun-bot/.env')

if len(sys.argv) < 2:
    print("Usage: whale <MINT_ADDRESS>")
    print("       whale <SYMBOL>")
    sys.exit(1)

query = sys.argv[1]
helius_key = os.getenv('HELIUS_API_KEY')

# Если передали символ — ищем mint в positions.json
mint = query
try:
    with open('/opt/pumpfun-bonkfun-bot/positions.json') as f:
        positions = json.load(f)
    for p in positions:
        if p.get('symbol', '').lower() == query.lower():
            mint = p['mint']
            print(f"📍 {query} -> {mint}")
            break
except:
    pass

# Загружаем китов
with open('/opt/pumpfun-bonkfun-bot/smart_money_wallets.json') as f:
    data = json.load(f)

whale_map = {}
whales_list = data.get('whales', []) if isinstance(data, dict) else data
for w in whales_list:
    if isinstance(w, dict):
        addr = w.get('wallet', w.get('address', ''))
        label = w.get('label', w.get('name', ''))
        if addr:
            whale_map[addr] = label

print(f"🐋 Китов в списке: {len(whale_map)}")

# Наш кошелёк
pk = os.getenv('SOLANA_PRIVATE_KEY')
import base58
from solders.keypair import Keypair
kp = Keypair.from_bytes(base58.b58decode(pk))
our_wallet = str(kp.pubkey())

# 1. Ищем нашу покупку
print(f"\n🔍 Ищем покупки токена {mint[:25]}...")

url = f"https://api.helius.xyz/v0/addresses/{mint}/transactions?api-key={helius_key}"
resp = requests.get(url, timeout=20)
txs = resp.json()

if not isinstance(txs, list):
    print(f"❌ Helius error: {txs}")
    sys.exit(1)

# Ищем нашу покупку и покупки китов
our_buys = []
whale_buys = []
all_buyers = {}

for tx in txs:
    if not isinstance(tx, dict):
        continue
    ts = tx.get('timestamp', 0)
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    
    for tt in tx.get('tokenTransfers', []):
        if tt.get('mint') == mint:
            buyer = tt.get('toUserAccount', '')
            amount = tt.get('tokenAmount', 0)
            
            if not buyer:
                continue
            
            if buyer not in all_buyers:
                all_buyers[buyer] = {'first_ts': ts, 'total': 0, 'count': 0}
            all_buyers[buyer]['total'] += (amount or 0)
            all_buyers[buyer]['count'] += 1
            
            if buyer == our_wallet:
                our_buys.append({'time': dt, 'ts': ts, 'amount': amount, 'sig': tx.get('signature')})
            
            if buyer in whale_map:
                whale_buys.append({
                    'wallet': buyer, 'label': whale_map[buyer],
                    'time': dt, 'ts': ts, 'amount': amount,
                    'sig': tx.get('signature'),
                })

# Вывод
if our_buys:
    our_buys.sort(key=lambda x: x['ts'])
    first = our_buys[0]
    print(f"\n⭐ Наша первая покупка:")
    print(f"   Время: {first['time']}")
    print(f"   Кол-во: {first['amount']:,.2f}")
    print(f"   TX: https://solscan.io/tx/{first['sig']}")
else:
    print(f"\n⭐ Наша покупка не найдена в последних {len(txs)} транзакциях токена")

if whale_buys:
    whale_buys.sort(key=lambda x: x['ts'])
    print(f"\n🐋 КИТЫ купившие этот токен ({len(whale_buys)} покупок):")
    
    seen = set()
    for w in whale_buys:
        key = w['wallet']
        if key in seen:
            continue
        seen.add(key)
        
        delta_str = ""
        if our_buys:
            delta = our_buys[0]['ts'] - w['ts']
            if delta > 0:
                delta_str = f" ({delta}с ДО нас)"
            else:
                delta_str = f" ({-delta}с ПОСЛЕ нас)"
        
        print(f"\n   🐋 {w['label']}")
        print(f"      Кошелёк: {w['wallet']}")
        print(f"      Время:   {w['time']}{delta_str}")
        print(f"      Кол-во:  {w['amount']:,.2f}")
        print(f"      TX: https://solscan.io/tx/{w['sig']}")
else:
    print(f"\n❌ Ни один кит из списка не найден среди покупателей")
    print(f"   (проверено {len(txs)} транзакций)")

# Также проверим логи бота
import subprocess, glob

log_files = sorted(glob.glob('/opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log*'))
for lf in log_files:
    if lf.endswith('.gz'):
        cmd = f"zgrep -m5 '{mint[:20]}' '{lf}' 2>/dev/null"
    else:
        cmd = f"grep -m5 '{mint[:20]}' '{lf}' 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    lines = [l for l in result.stdout.strip().split('\n') if l and ('EMIT' in l or 'CALLBACK' in l or 'BUY' in l.upper() or 'signal' in l.lower())]
    if lines:
        print(f"\n📜 Из логов ({os.path.basename(lf)}):")
        for l in lines[:5]:
            print(f"   {l.strip()}")
        break
