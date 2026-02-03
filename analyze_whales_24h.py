#!/usr/bin/env python3
"""
Анализ сделок всех китов за последние 24 часа через Zerion API
"""

import json
import asyncio
import aiohttp
from datetime import datetime, timedelta
from collections import defaultdict
import os
from dotenv import load_dotenv

load_dotenv()

ZERION_API_KEY = os.getenv("ZERION_API_KEY", "")

async def get_wallet_transactions(session, wallet: str, label: str) -> list:
    """Получить транзакции кошелька за 24 часа"""
    
    url = f"https://api.zerion.io/v1/wallets/{wallet}/transactions/"
    headers = {
        "accept": "application/json",
        "authorization": f"Basic {ZERION_API_KEY}"
    }
    params = {
        "currency": "usd",
        "page[size]": 100,
        "filter[chain_ids]": "solana",
        "filter[trash]": "only_non_trash"
    }
    
    try:
        async with session.get(url, headers=headers, params=params, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("data", [])
            else:
                print(f"  ⚠️ {label}: HTTP {resp.status}")
                return []
    except Exception as e:
        print(f"  ❌ {label}: {e}")
        return []

async def analyze_whales():
    """Анализ всех китов"""
    
    # Загружаем китов
    with open("/opt/pumpfun-bonkfun-bot/smart_money_wallets.json", "r") as f:
        data = json.load(f)
    
    whales = data.get("whales", [])
    print(f"📊 Анализируем {len(whales)} китов за последние 24 часа...\n")
    
    cutoff = datetime.utcnow() - timedelta(hours=24)
    
    results = []
    
    async with aiohttp.ClientSession() as session:
        for i, whale in enumerate(whales):
            wallet = whale.get("wallet", "")
            label = whale.get("label", "unknown")
            
            print(f"[{i+1}/{len(whales)}] {label[:40]}...")
            
            txs = await get_wallet_transactions(session, wallet, label)
            
            # Фильтруем за 24 часа и только swaps
            swaps = []
            for tx in txs:
                attrs = tx.get("attributes", {})
                
                # Проверяем время
                mined_at = attrs.get("mined_at")
                if mined_at:
                    tx_time = datetime.fromisoformat(mined_at.replace("Z", "+00:00")).replace(tzinfo=None)
                    if tx_time < cutoff:
                        continue
                
                # Только swaps
                op_type = attrs.get("operation_type", "")
                if op_type != "trade":
                    continue
                
                # Парсим transfers
                transfers = attrs.get("transfers", [])
                
                sol_out = 0
                sol_in = 0
                token_symbol = "?"
                
                for t in transfers:
                    fungible = t.get("fungible_info", {})
                    symbol = fungible.get("symbol", "")
                    value = float(t.get("value", 0) or 0)
                    direction = t.get("direction", "")
                    
                    if symbol in ["SOL", "WSOL"]:
                        if direction == "out":
                            sol_out += value
                        else:
                            sol_in += value
                    else:
                        token_symbol = symbol
                
                if sol_out > 0:  # BUY
                    swaps.append({
                        "type": "BUY",
                        "sol": sol_out,
                        "token": token_symbol,
                        "time": mined_at
                    })
                elif sol_in > 0:  # SELL
                    swaps.append({
                        "type": "SELL",
                        "sol": sol_in,
                        "token": token_symbol,
                        "time": mined_at
                    })
            
            # Статистика
            buys = [s for s in swaps if s["type"] == "BUY"]
            sells = [s for s in swaps if s["type"] == "SELL"]
            
            total_buy_sol = sum(s["sol"] for s in buys)
            total_sell_sol = sum(s["sol"] for s in sells)
            avg_buy = total_buy_sol / len(buys) if buys else 0
            
            results.append({
                "wallet": wallet,
                "label": label,
                "buys_count": len(buys),
                "sells_count": len(sells),
                "total_buy_sol": round(total_buy_sol, 2),
                "total_sell_sol": round(total_sell_sol, 2),
                "avg_buy_sol": round(avg_buy, 2),
                "swaps": swaps[:20]  # Последние 20 сделок
            })
            
            await asyncio.sleep(0.3)  # Rate limit
    
    # Сортируем по активности
    results.sort(key=lambda x: x["buys_count"], reverse=True)
    
    # Выводим результат
    print("\n" + "="*80)
    print("📊 РЕЗУЛЬТАТЫ АНАЛИЗА КИТОВ ЗА 24 ЧАСА")
    print("="*80)
    
    print(f"\n{'Кит':<45} {'Buys':>6} {'Sells':>6} {'Buy SOL':>10} {'Avg Buy':>10}")
    print("-"*80)
    
    active_count = 0
    inactive_count = 0
    
    for r in results:
        if r["buys_count"] > 0:
            active_count += 1
            print(f"{r['label'][:44]:<45} {r['buys_count']:>6} {r['sells_count']:>6} {r['total_buy_sol']:>10.2f} {r['avg_buy_sol']:>10.2f}")
        else:
            inactive_count += 1
    
    print("-"*80)
    print(f"\n✅ Активных китов (с покупками): {active_count}")
    print(f"😴 Неактивных китов: {inactive_count}")
    
    # Сохраняем полный отчёт
    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "period": "24h",
        "total_whales": len(whales),
        "active_whales": active_count,
        "inactive_whales": inactive_count,
        "whales": results
    }
    
    with open("/opt/pumpfun-bonkfun-bot/whale_analysis_24h.json", "w") as f:
        json.dump(report, f, indent=2)
    
    print(f"\n📁 Полный отчёт сохранён: whale_analysis_24h.json")
    
    # Показываем неактивных
    print(f"\n😴 НЕАКТИВНЫЕ КИТЫ (0 покупок за 24ч):")
    for r in results:
        if r["buys_count"] == 0:
            print(f"  - {r['label'][:50]}")

if __name__ == "__main__":
    asyncio.run(analyze_whales())
