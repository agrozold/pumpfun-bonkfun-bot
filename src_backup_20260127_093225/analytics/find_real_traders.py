"""Поиск реальных трейдеров (не девов, не airdrop)"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query
import pandas as pd

SOL = "So11111111111111111111111111111111111111112"

def find_real_traders(days: int = 30, max_trades: int = 300, min_trades: int = 10, min_spent: float = 10):
    """
    Ищем реальных трейдеров:
    - Нормальное соотношение buy/sell (не только sell)
    - Реально тратили SOL
    - Регулярная активность
    """

    print(f"\n📊 Поиск реальных трейдеров за {days} дней")
    print(f"   Фильтры: {min_trades}-{max_trades} сделок, spent >= {min_spent} SOL\n")

    # Pumpfun
    sql_pf = f"""
    SELECT
        toString(signing_wallet) as wallet,
        'pumpfun' as platform,
        count(*) as trades,
        uniqExact(toString(base_coin)) as tokens,
        countIf(direction = 'buy') as buys,
        countIf(direction = 'sell') as sells,
        sumIf(quote_coin_amount, direction = 'buy') / 1e9 as spent,
        sumIf(quote_coin_amount, direction = 'sell') / 1e9 as received,
        uniqExact(toDate(block_time)) as active_days
    FROM default.pumpfun_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
    GROUP BY signing_wallet
    HAVING 
        trades >= {min_trades} 
        AND trades <= {max_trades} 
        AND spent >= {min_spent}
        AND buys >= 5
        AND sells >= 5
        AND buys * 1.0 / sells BETWEEN 0.3 AND 3.0
    """

    # Raydium  
    sql_ray = f"""
    SELECT
        toString(signing_wallet) as wallet,
        'raydium' as platform,
        count(*) as trades,
        uniqExact(
            CASE WHEN toString(base_coin) = '{SOL}' THEN toString(quote_coin)
            ELSE toString(base_coin) END
        ) as tokens,
        countIf(direction = 'B') as buys,
        countIf(direction = 'S') as sells,
        sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'B'
        ) / 1e9 as spent,
        sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'S'
        ) / 1e9 as received,
        uniqExact(toDate(block_time)) as active_days
    FROM default.raydium_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
      AND (toString(base_coin) = '{SOL}' OR toString(quote_coin) = '{SOL}')
    GROUP BY signing_wallet
    HAVING 
        trades >= {min_trades} 
        AND trades <= {max_trades} 
        AND spent >= {min_spent}
        AND buys >= 5
        AND sells >= 5
        AND buys * 1.0 / sells BETWEEN 0.3 AND 3.0
    """

    print("Загружаю pumpfun...")
    df_pf = query(sql_pf)
    print(f"  Найдено: {len(df_pf)}")

    print("Загружаю raydium...")
    df_ray = query(sql_ray)
    print(f"  Найдено: {len(df_ray)}")

    if df_pf.empty and df_ray.empty:
        print("Ничего не найдено")
        return

    all_data = pd.concat([df_pf, df_ray], ignore_index=True)
    
    combined = all_data.groupby('wallet').agg({
        'trades': 'sum',
        'tokens': 'sum', 
        'buys': 'sum',
        'sells': 'sum',
        'spent': 'sum',
        'received': 'sum',
        'active_days': 'max',
        'platform': lambda x: '+'.join(sorted(set(x)))
    }).reset_index()

    # Метрики
    combined['pnl'] = combined['received'] - combined['spent']
    combined['roi'] = ((combined['received'] / combined['spent']) - 1) * 100
    combined['pnl_per_trade'] = combined['pnl'] / combined['trades']
    combined['buy_sell_ratio'] = combined['buys'] / combined['sells']

    # Только профитные
    combined = combined[combined['pnl'] > 0]
    
    # Минимум 5 активных дней
    combined = combined[combined['active_days'] >= 5]

    # Сортируем по PnL
    combined = combined.sort_values('pnl', ascending=False).head(100)

    print(f"\n{'='*160}")
    print(f"{'#':<3} | {'Кошелёк':<44} | {'Платф':<8} | {'Trades':>6} | {'B/S':>7} | {'Ratio':>5} | {'Days':>4} | {'Spent':>8} | {'PnL':>9} | {'ROI':>6}")
    print(f"{'='*160}")

    for i, (_, row) in enumerate(combined.iterrows(), 1):
        w = row['wallet'][:18] + "..." + row['wallet'][-6:]
        bs = f"{int(row['buys'])}/{int(row['sells'])}"
        
        flag = ""
        if row['active_days'] >= 10 and row['pnl'] > 100:
            flag = "💎"
        if row['roi'] > 100 and row['buy_sell_ratio'] > 0.5:
            flag += "🚀"
            
        print(f"{i:<3} | {w} | {row['platform']:<8} | {row['trades']:>6.0f} | {bs:>7} | {row['buy_sell_ratio']:>5.2f} | {row['active_days']:>4.0f} | {row['spent']:>8.1f} | {row['pnl']:>9.1f} | {row['roi']:>5.0f}% {flag}")

    print(f"{'='*160}")
    print(f"\n💎 = 10+ активных дней, PnL > 100 SOL")
    print(f"🚀 = ROI > 100%, B/S ratio > 0.5")

    # Рекомендации
    recommended = combined[
        (combined['active_days'] >= 7) &
        (combined['pnl'] >= 50) &
        (combined['buy_sell_ratio'] >= 0.5)
    ].head(20)
    
    print(f"\n\n🏆 РЕКОМЕНДУЕМЫЕ для smart_money ({len(recommended)} шт):")
    print("-" * 80)
    for _, row in recommended.iterrows():
        print(f'"{row["wallet"]}",  # PnL:{row["pnl"]:.0f} ROI:{row["roi"]:.0f}% Days:{row["active_days"]:.0f} B/S:{row["buy_sell_ratio"]:.2f}')

    combined.to_csv('/tmp/real_traders.csv', index=False)
    print(f"\n📁 Сохранено в /tmp/real_traders.csv")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    max_t = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    min_t = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    min_spent = float(sys.argv[4]) if len(sys.argv) > 4 else 10
    find_real_traders(days, max_t, min_t, min_spent)
