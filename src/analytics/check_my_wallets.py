"""Проверить PnL кошельков из smart_money_wallets.json"""

import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

WALLETS_FILE = "/opt/pumpfun-bonkfun-bot/smart_money_wallets.json"

def check_my_wallets(days: int = 7):
    """Показать PnL для всех кошельков из smart_money_wallets.json"""
    
    # Загружаем кошельки
    with open(WALLETS_FILE) as f:
        data = json.load(f)
    
    wallets = [w['wallet'] for w in data['whales']]
    wallets_str = "', '".join(wallets)
    
    print(f"\nПроверяю {len(wallets)} кошельков за {days} дней...\n")
    
    sql = f"""
    SELECT 
        toString(signing_wallet) as wallet,
        count(*) as trades,
        uniqExact(toString(base_coin)) as tokens,
        countIf(direction = 'buy') as buys,
        countIf(direction = 'sell') as sells,
        (sumIf(quote_coin_amount, direction = 'sell') - sumIf(quote_coin_amount, direction = 'buy')) as pnl_raw
    FROM default.pumpfun_all_swaps
    WHERE toString(signing_wallet) IN ('{wallets_str}')
      AND block_time > now() - INTERVAL {days} DAY
    GROUP BY signing_wallet
    ORDER BY pnl_raw DESC
    """
    
    df = query(sql)
    
    if df.empty:
        print("Нет данных по этим кошелькам на pump.fun")
        return
    
    print(f"{'Кошелёк':<46} | Сделок |   PnL (SOL) | B/S")
    print("-"*80)
    
    total_pnl = 0
    active_count = 0
    
    for _, row in df.iterrows():
        w = row['wallet'][:20] + "..." + row['wallet'][-8:]
        pnl_sol = row['pnl_raw'] / 1e9
        total_pnl += pnl_sol
        active_count += 1
        print(f"{w} | {row['trades']:>6} | {pnl_sol:>11.2f} | {row['buys']}/{row['sells']}")
    
    print("-"*80)
    print(f"Активных: {active_count}/{len(wallets)} | Общий PnL: {total_pnl:.2f} SOL")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    check_my_wallets(days)
