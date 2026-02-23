#!/usr/bin/env python3
"""Управление списком китов: add/del/list + auto sync webhook"""
import os, sys, json, asyncio
sys.path.insert(0, "/opt/pumpfun-bonkfun-bot/src")
from dotenv import load_dotenv
load_dotenv('/opt/pumpfun-bonkfun-bot/.env')

WALLETS_FILE = '/opt/pumpfun-bonkfun-bot/smart_money_wallets.json'

def load_whales():
    with open(WALLETS_FILE) as f:
        return json.load(f)

def save_whales(data):
    with open(WALLETS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

async def sync_webhook():
    """Синхронизировать Helius webhook с текущим списком"""
    try:
        from utils.helius_webhook_sync import sync_helius_webhook
        helius_key = os.getenv('HELIUS_API_KEY')
        ok = await sync_helius_webhook(wallets_file=WALLETS_FILE, helius_api_key=helius_key)
        if ok:
            print("✅ Helius webhook синхронизирован!")
        else:
            print("❌ Ошибка синхронизации webhook!")
        return ok
    except Exception as e:
        print(f"❌ Sync error: {e}")
        return False

def cmd_add(address, label=None):
    data = load_whales()
    whales = data.get('whales', [])
    
    # Проверяем дубликат
    for w in whales:
        if w.get('wallet') == address:
            print(f"⚠️  Кит уже в списке: {w.get('label', '')} ({address[:25]}...)")
            return False
    
    if not label:
        # Авто-лейбл
        max_num = 0
        for w in whales:
            l = w.get('label', '')
            if l.startswith('whale-'):
                try:
                    num = int(l.split('-')[1])
                    max_num = max(max_num, num)
                except:
                    pass
        label = f"whale-{max_num + 1}"
    
    whales.append({'wallet': address, 'label': label})
    data['whales'] = whales
    save_whales(data)
    print(f"✅ Добавлен: {label} ({address[:25]}...)")
    print(f"📊 Всего китов: {len(whales)}")
    
    # Sync webhook
    asyncio.run(sync_webhook())
    return True

def cmd_del(query):
    data = load_whales()
    whales = data.get('whales', [])
    before = len(whales)
    
    # Ищем по адресу или лейблу
    removed = None
    new_whales = []
    for w in whales:
        if w.get('wallet') == query or w.get('label', '').lower() == query.lower():
            removed = w
        else:
            new_whales.append(w)
    
    if not removed:
        print(f"❌ Не найден: {query}")
        print(f"   Попробуй полный адрес или лейбл (whale-140)")
        return False
    
    data['whales'] = new_whales
    save_whales(data)
    print(f"✅ Удалён: {removed.get('label', '')} ({removed.get('wallet', '')[:25]}...)")
    print(f"📊 Осталось китов: {len(new_whales)}")
    
    # Sync webhook
    asyncio.run(sync_webhook())
    return True

def cmd_list(search=None):
    data = load_whales()
    whales = data.get('whales', [])
    
    if search:
        whales = [w for w in whales if 
                  search.lower() in w.get('wallet', '').lower() or 
                  search.lower() in w.get('label', '').lower() or
                  search.lower() in w.get('notes', '').lower()]
        print(f"🔍 Найдено: {len(whales)}")
    else:
        print(f"🐋 Всего китов: {len(whales)}")
    
    print()
    for w in whales:
        label = w.get('label', '')
        addr = w.get('wallet', '')
        notes = w.get('notes', '')
        extra = f" | {notes}" if notes else ""
        print(f"  {label:<25} {addr}{extra}")

def cmd_info(query):
    """Подробная информация о ките"""
    data = load_whales()
    whales = data.get('whales', [])
    
    found = None
    for w in whales:
        if w.get('wallet') == query or w.get('label', '').lower() == query.lower():
            found = w
            break
    
    if not found:
        print(f"❌ Не найден: {query}")
        return
    
    print(f"🐋 {found.get('label', '')}")
    print(f"   Кошелёк: {found.get('wallet', '')}")
    if found.get('notes'):
        print(f"   Заметки: {found['notes']}")
    if found.get('priority'):
        print(f"   Приоритет: {found['priority']}")
    print(f"\n   Solscan: https://solscan.io/account/{found.get('wallet', '')}")

# === MAIN ===
if len(sys.argv) < 2:
    print("Использование:")
    print("  whale add <ADDRESS> [label]  — добавить кита")
    print("  whale del <ADDRESS|LABEL>    — удалить кита")
    print("  whale list [search]          — список китов")
    print("  whale info <ADDRESS|LABEL>   — инфо о ките")
    print("  whale sync                   — синхронизировать webhook")
    print("  whale <MINT|SYMBOL>          — найти кита по токену")
    sys.exit(0)

cmd = sys.argv[1].lower()

if cmd == 'add':
    if len(sys.argv) < 3:
        print("❌ Укажи адрес: whale add <ADDRESS> [label]")
        sys.exit(1)
    address = sys.argv[2]
    label = sys.argv[3] if len(sys.argv) > 3 else None
    cmd_add(address, label)

elif cmd == 'del' or cmd == 'rm' or cmd == 'remove':
    if len(sys.argv) < 3:
        print("❌ Укажи адрес или лейбл: whale del <ADDRESS|LABEL>")
        sys.exit(1)
    cmd_del(sys.argv[2])

elif cmd == 'list' or cmd == 'ls':
    search = sys.argv[2] if len(sys.argv) > 2 else None
    cmd_list(search)

elif cmd == 'info':
    if len(sys.argv) < 3:
        print("❌ Укажи адрес или лейбл: whale info <ADDRESS|LABEL>")
        sys.exit(1)
    cmd_info(sys.argv[2])

elif cmd == 'sync':
    asyncio.run(sync_webhook())

else:
    # Передали mint/symbol — запускаем find_whale.py
    os.execvp(sys.executable, [sys.executable, '/opt/pumpfun-bonkfun-bot/find_whale.py'] + sys.argv[1:])
