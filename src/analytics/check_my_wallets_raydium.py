"""Проверить PnL кошельков на Raydium"""

import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

WALLETS_FILE = "/opt/pumpfun-bonkfun-bot/smart_money_wallets.json"
SOL = "So11111111111111111111111111111111111111112"

def check_my_wallets_raydium(days: int = 7):
    """Показать PnL на Raydium"""
    
    with open(WALLETS_FILE) as f:
        data = json.load(f)
    
    wallets = [w['wallet'] for w in data['whales']]
    wallets_str = "', '".join(wallets)
    
    print(f"\nПроверяю {len(wallets)} кошельков на Raydium за {days} дней...\n")
    
    sql = f"""
    SELECT 
        toString(signing_wallet) as wallet,
        count(*) as trades,
        uniqExact(CASE 
            WHEN toString(base_coin) = '{SOL}' THEN toString(quote_coin)
            ELSE toString(base_coin)
        END) as tokens,
        countIf(direction = 'B') as buys,
        countIf(direction = 'S') as sells,
        sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'S'
        ) - sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'B'
        ) as pnl_raw
    FROM default.raydium_all_swaps
    WHERE toString(signing_wallet) IN ('{wallets_str}')
      AND block_time > now() - INTERVAL {days} DAY
      AND (toString(base_coin) = '{SOL}' OR toString(quote_coin) = '{SOL}')
    GROUP BY signing_wallet
    ORDER BY pnl_raw DESC
    """
    
    df = query(sql)
    
    if df.empty:
        print("Нет данных по этим кошелькам на Raydium")
        return
    
    print(f"{'Кошелёк':<46} | Сделок |   PnL (SOL) | B/S")
    print("-"*80)
    
    total_pnl = 0
    
    for _, row in df.iterrows():
        w = row['wallet'][:20] + "..." + row['wallet'][-8:]
        pnl_sol = row['pnl_raw'] / 1e9
        total_pnl += pnl_sol
        print(f"{w} | {row['trades']:>6} | {pnl_sol:>11.2f} | {row['buys']}/{row['sells']}")
    
    print("-"*80)
    print(f"Активных: {len(df)}/{len(wallets)} | Общий PnL: {total_pnl:.2f} SOL")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    check_my_wallets_raydium(days)
