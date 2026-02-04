#!/usr/bin/env python3
"""Check which whale we followed for a token"""
import sys
import json
import subprocess
import requests
from datetime import datetime

if len(sys.argv) < 2:
    print("Usage: whale <TOKEN_ADDRESS>")
    sys.exit(1)

mint = sys.argv[1]

print(f"🔍 Проверяю токен: {mint[:8]}...{mint[-4:]}")
print("=" * 50)

# 1. Проверяем Redis позицию
result = subprocess.run(["redis-cli", "HGET", "whale:positions", mint], capture_output=True, text=True)
if not result.stdout.strip():
    print("❌ Позиция не найдена в Redis")
    sys.exit(1)

pos = json.loads(result.stdout.strip())

print(f"\n📊 ПОЗИЦИЯ:")
print(f"   Symbol: {pos.get('symbol')}")
print(f"   Quantity: {pos.get('quantity'):,.2f}")
print(f"   Entry: {pos.get('entry_price'):.10f}")
print(f"   SL: {pos.get('stop_loss_price'):.10f}")

# 2. Проверяем whale_address из позиции
whale_wallet = pos.get('whale_wallet')
whale_label = pos.get('whale_label')

# 3. Если нет в позиции - проверяем history
if not whale_wallet:
    try:
        with open("/opt/pumpfun-bonkfun-bot/data/purchased_tokens_history.json", "r") as f:
            history = json.load(f)
        h = history.get("purchased_tokens", {}).get(mint) or history.get(mint)
        if h:
            whale_wallet = h.get('whale_wallet')
            whale_label = h.get('whale_label')
    except:
        pass

print(f"\n🐋 КИТ:")
if whale_wallet:
    print(f"   Wallet: {whale_wallet}")
    print(f"   Label: {whale_label or 'Unknown'}")
    
    # Ищем в конфиге вебхуков
    try:
        with open("/opt/pumpfun-bonkfun-bot/config/webhooks.json", "r") as f:
            webhooks = json.load(f)
        
        for wh in webhooks.get("webhooks", []):
            wallets = wh.get("wallets", [])
            if whale_wallet in wallets:
                print(f"   Webhook: {wh.get('name', 'Unknown')}")
                break
    except:
        pass
else:
    print("   Не указан (старая позиция до обновления)")

# 4. История покупки
try:
    with open("/opt/pumpfun-bonkfun-bot/data/purchased_tokens_history.json", "r") as f:
        history = json.load(f)
    h = history.get("purchased_tokens", {}).get(mint) or history.get(mint)
    if h:
        print(f"\n📜 ИСТОРИЯ ПОКУПКИ:")
        print(f"   Bot: {h.get('bot_name', 'Unknown')}")
        print(f"   Platform: {h.get('platform', 'Unknown')}")
        if h.get('timestamp'):
            try:
                ts = datetime.fromisoformat(h['timestamp'].replace('Z', '+00:00'))
                print(f"   Time: {ts.strftime('%Y-%m-%d %H:%M:%S')}")
            except:
                print(f"   Time: {h.get('timestamp')}")
except:
    pass

# 5. Текущая цена и PnL
try:
    resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=10)
    pairs = resp.json().get("pairs", [])
    if pairs:
        current = float(pairs[0].get("priceNative", 0))
        entry = pos.get("entry_price", 0)
        sl = pos.get("stop_loss_price", 0)
        
        pnl = ((current - entry) / entry) * 100 if entry > 0 else 0
        sl_dist = ((current - sl) / current) * 100 if current > 0 else 0
        
        print(f"\n💰 ТЕКУЩЕЕ СОСТОЯНИЕ:")
        print(f"   Price: {current:.10f}")
        print(f"   PnL: {pnl:+.1f}%")
        print(f"   До SL: {sl_dist:.1f}%")
        
        # DCA статус
        dca_pending = pos.get("dca_pending", False)
        dca_bought = pos.get("dca_bought", False)
        if dca_pending:
            print(f"   DCA: Ожидает (±25%)")
        elif dca_bought:
            print(f"   DCA: Сработала")
        else:
            print(f"   DCA: Выкл")
            
        # TSL статус
        tsl_active = pos.get("tsl_active", False)
        if tsl_active:
            hwm = pos.get("high_water_mark", 0)
            tsl_trigger = pos.get("tsl_trigger_price", 0)
            print(f"   TSL: Активен (HWM: {hwm:.10f}, trigger: {tsl_trigger:.10f})")
        else:
            print(f"   TSL: Не активен")
except Exception as e:
    print(f"\n⚠️ Ошибка получения цены: {e}")

print("")
