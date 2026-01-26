"""Поиск инсайдеров с небольшими входами"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

def find_small_smart_money(days: int = 30, max_avg_buy: float = 5.0, min_pnl: float = 10):
    """
    Ищем тех кто:
    - Входит небольшими суммами (avg buy <= 5 SOL)
    - Но делает хороший профит
    - И держит часть позиций
    """

    print(f"\n🎯 Поиск small-cap инсайдеров за {days} дней")
    print(f"   Фильтры: avg buy <= {max_avg_buy} SOL, PnL >= {min_pnl} SOL\n")

    sql = f"""
    WITH wallet_stats AS (
        SELECT
            toString(signing_wallet) as wallet,
            count(*) as trades,
            countIf(direction = 'buy') as buys,
            countIf(direction = 'sell') as sells,
            uniqExact(toString(base_coin)) as tokens,
            
            -- Средний размер покупки
            avgIf(quote_coin_amount, direction = 'buy') / 1e9 as avg_buy_sol,
            
            -- Макс покупка
            maxIf(quote_coin_amount, direction = 'buy') / 1e9 as max_buy_sol,
            
            sumIf(quote_coin_amount, direction = 'buy') / 1e9 as total_spent,
            sumIf(quote_coin_amount, direction = 'sell') / 1e9 as total_received,
            
            uniqExact(toDate(block_time)) as active_days
        FROM default.pumpfun_all_swaps
        WHERE block_time > now() - INTERVAL {days} DAY
        GROUP BY signing_wallet
        HAVING 
            buys >= 5 
            AND sells >= 3
            AND avg_buy_sol <= {max_avg_buy}
            AND avg_buy_sol >= 0.1
            AND trades <= 200
    )
    SELECT 
        *,
        total_received - total_spent as pnl,
        (total_received / total_spent - 1) * 100 as roi
    FROM wallet_stats
    WHERE pnl >= {min_pnl}
    ORDER BY roi DESC
    LIMIT 100
    """

    df = query(sql)

    if df.empty:
        print("Ничего не найдено")
        return

    print(f"{'='*150}")
    print(f"{'#':<3} | {'Кошелёк':<44} | {'Trades':>6} | {'Tokens':>6} | {'AvgBuy':>7} | {'MaxBuy':>7} | {'Days':>4} | {'Spent':>8} | {'PnL':>9} | {'ROI':>7}")
    print(f"{'='*150}")

    for i, (_, row) in enumerate(df.iterrows(), 1):
        w = row['wallet'][:18] + "..." + row['wallet'][-6:]
        
        flag = ""
        if row['active_days'] >= 7 and row['roi'] > 100:
            flag = "💎"
        if row['avg_buy_sol'] <= 2 and row['pnl'] > 30:
            flag += "🎯"  # Маленькие входы, хороший профит
            
        print(f"{i:<3} | {w} | {row['trades']:>6.0f} | {row['tokens']:>6.0f} | {row['avg_buy_sol']:>7.2f} | {row['max_buy_sol']:>7.2f} | {row['active_days']:>4.0f} | {row['total_spent']:>8.1f} | {row['pnl']:>9.1f} | {row['roi']:>6.0f}% {flag}")

    print(f"{'='*150}")
    print(f"\n💎 = 7+ дней активности + ROI > 100%")
    print(f"🎯 = Avg buy <= 2 SOL + PnL > 30 SOL")

    good = df[(df['avg_buy_sol'] <= 3) & (df['active_days'] >= 5) & (df['pnl'] >= 20)]
    
    print(f"\n\n🏆 ПОДХОДЯЩИЕ ДЛЯ КОПИРОВАНИЯ ({len(good)} шт):")
    print("-" * 80)
    for _, row in good.head(20).iterrows():
        print(f'"{row["wallet"]}",  # AvgBuy:{row["avg_buy_sol"]:.1f} PnL:{row["pnl"]:.0f} ROI:{row["roi"]:.0f}%')


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    max_buy = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    min_pnl = float(sys.argv[3]) if len(sys.argv) > 3 else 10
    find_small_smart_money(days, max_buy, min_pnl)
