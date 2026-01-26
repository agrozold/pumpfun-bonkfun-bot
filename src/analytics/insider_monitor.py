#!/usr/bin/env python3
"""
Мониторинг инсайдеров PENGUIN
Запуск: python insider_monitor.py
Или в cron каждые 5 минут: */5 * * * * python /path/to/insider_monitor.py
"""

import os
import requests
from datetime import datetime
import json

# Конфигурация - читаем из переменных окружения
CLICKHOUSE_URL = os.getenv('INDEXER_HOST', 'https://your-indexer-host:28123')
CLICKHOUSE_AUTH = (
    os.getenv('INDEXER_USER', 'your_username'),
    os.getenv('INDEXER_PASSWORD', 'your_password')
)

# Telegram (если нужно) - читаем из .env
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Инсайдеры которых отслеживаем
INSIDERS = {
    "4HjGze3GXy8aWzWeuR5hFZP6ezqRb3yhLbQULhefzQdN": "4HjGze (+4014 SOL PENGUIN)",
    "Bx9TNm7ztJJsg3xfu7VPyangGv3tHH3U7NM7awPe4sLs": "Bx9TNm (+3580 SOL PENGUIN)",
    "6rwXnAp6EfgfRAqaiq3zTRaBrAuNKc4CGRq7y6QesSCL": "6rwX (+2692 SOL PENGUIN)",
    "GDRTKkK5QmW9C768wmecq12hc14rhupsZCBFWJSEXsEX": "GDRT (+2048 SOL PENGUIN)",
    "DaUBRKnAjuqzozUjkH4QuVJUJiSmrbnLbCeqAgwA6dGF": "DaUB (+1662 SOL PENGUIN)",
    "21kMe9Ztcj3qLSN4Re2v9XQfXBrvJnJPHkw1CbaoPDnT": "21kM (+1493 SOL PENGUIN)",
}

def query_clickhouse(query):
    """Выполнить запрос к ClickHouse"""
    try:
        r = requests.get(
            CLICKHOUSE_URL, 
            params={"query": query}, 
            auth=CLICKHOUSE_AUTH, 
            timeout=60
        )
        if r.status_code == 200:
            return r.text.strip()
        else:
            print(f"Error: {r.status_code}")
            return None
    except Exception as e:
        print(f"Query error: {e}")
        return None

def send_telegram(message):
    """Отправить сообщение в Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def check_insider_buys(hours=1):
    """Проверить покупки инсайдеров за последние N часов"""
    insider_list = "', '".join(INSIDERS.keys())
    
    # PumpFun покупки
    query_pf = f"""
    SELECT 
        toString(signing_wallet) as wallet,
        toString(base_coin) as token,
        block_time,
        round(quote_coin_amount / 1e9, 3) as sol
    FROM default.pumpfun_all_swaps
    WHERE signing_wallet IN ('{insider_list}')
      AND direction = 'buy'
      AND block_time > now() - INTERVAL {hours} HOUR
    ORDER BY block_time DESC
    FORMAT JSONEachRow
    """
    
    result = query_clickhouse(query_pf)
    buys = []
    
    if result:
        for line in result.split('\n'):
            if line.strip():
                try:
                    buy = json.loads(line)
                    buy['source'] = 'PumpFun'
                    buy['insider_name'] = INSIDERS.get(buy['wallet'], buy['wallet'][:8])
                    buys.append(buy)
                except:
                    pass
    
    # PumpSwap покупки
    query_ps = f"""
    SELECT 
        toString(signing_wallet) as wallet,
        toString(base_token) as token,
        block_time,
        round(quote_token_amount / 1e9, 3) as sol
    FROM default.pumpswap_all_swaps
    WHERE signing_wallet IN ('{insider_list}')
      AND direction = 'B'
      AND block_time > now() - INTERVAL {hours} HOUR
    ORDER BY block_time DESC
    FORMAT JSONEachRow
    """
    
    result = query_clickhouse(query_ps)
    if result:
        for line in result.split('\n'):
            if line.strip():
                try:
                    buy = json.loads(line)
                    buy['source'] = 'PumpSwap'
                    buy['insider_name'] = INSIDERS.get(buy['wallet'], buy['wallet'][:8])
                    buys.append(buy)
                except:
                    pass
    
    return buys

def check_cluster_buys(hours=6):
    """Найти токены где 2+ инсайдера купили"""
    insider_list = "', '".join(INSIDERS.keys())
    
    query = f"""
    SELECT 
        toString(base_coin) as token,
        count(DISTINCT signing_wallet) as insider_count,
        groupArray(substring(toString(signing_wallet), 1, 6)) as wallets,
        min(block_time) as first_buy,
        round(sum(quote_coin_amount) / 1e9, 2) as total_sol
    FROM default.pumpfun_all_swaps
    WHERE signing_wallet IN ('{insider_list}')
      AND direction = 'buy'
      AND block_time > now() - INTERVAL {hours} HOUR
    GROUP BY base_coin
    HAVING insider_count >= 2
    ORDER BY insider_count DESC, first_buy DESC
    FORMAT JSONEachRow
    """
    
    result = query_clickhouse(query)
    clusters = []
    
    if result:
        for line in result.split('\n'):
            if line.strip():
                try:
                    clusters.append(json.loads(line))
                except:
                    pass
    
    return clusters

def main():
    print(f"\n{'='*60}")
    print(f"🔍 INSIDER MONITOR - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print('='*60)
    
    # Проверяем покупки за последний час
    buys = check_insider_buys(hours=1)
    
    if buys:
        print(f"\n🚨 НОВЫЕ ПОКУПКИ ({len(buys)}):\n")
        for buy in buys:
            msg = f"  [{buy['source']}] {buy['insider_name']} купил {buy['sol']} SOL"
            msg += f"\n    Token: {buy['token'][:20]}..."
            msg += f"\n    Time: {buy['block_time']}"
            print(msg)
            
            # Отправляем в Telegram если больше 1 SOL
            if float(buy['sol']) >= 1:
                tg_msg = f"🚨 <b>INSIDER BUY</b>\n"
                tg_msg += f"👤 {buy['insider_name']}\n"
                tg_msg += f"💰 {buy['sol']} SOL\n"
                tg_msg += f"🪙 <code>{buy['token']}</code>\n"
                tg_msg += f"📍 {buy['source']}"
                send_telegram(tg_msg)
    else:
        print("\n✓ Нет новых покупок за последний час")
    
    # Проверяем кластерные покупки (2+ инсайдера)
    clusters = check_cluster_buys(hours=24)
    
    if clusters:
        print(f"\n🔥 КЛАСТЕРНЫЕ ПОКУПКИ (2+ инсайдера за 24ч):\n")
        for c in clusters:
            print(f"  Token: {c['token'][:20]}...")
            print(f"  Инсайдеров: {c['insider_count']}, Total: {c['total_sol']} SOL")
            print(f"  Who: {c['wallets']}")
            print()

if __name__ == "__main__":
    main()
