"""Поиск инсайдеров v2: фильтруем девелоперов"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query
import pandas as pd

SOL = "So11111111111111111111111111111111111111112"

def find_real_insiders(days: int = 30, max_trades: int = 300, min_trades: int = 10, min_spent: float = 10):
    """
    Топ кошельков - только те кто реально покупал (spent > 0)
    """

    print(f"\n📊 Поиск инсайдеров за {days} дней")
    print(f"   Фильтры: {min_trades}-{max_trades} сделок, потратил >= {min_spent} SOL\n")

    # Pumpfun
    sql_pf = f"""
    SELECT
        toString(signing_wallet) as wallet,
        'pumpfun' as platform,
        count(*) as trades,
        uniqExact(toString(base_coin)) as tokens,
        countIf(direction = 'buy') as buys,
        countIf(direction = 'sell') as sells,
        sumIf(quote_coin_amount, direction = 'buy') / 1e9 as spent_sol,
        sumIf(quote_coin_amount, direction = 'sell') / 1e9 as received_sol
    FROM default.pumpfun_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
    GROUP BY signing_wallet
    HAVING trades >= {min_trades} AND trades <= {max_trades}
    """

    # Raydium
    sql_ray = f"""
    SELECT
        toString(signing_wallet) as wallet,
        'raydium' as platform,
        count(*) as trades,
        uniqExact(
            CASE 
                WHEN toString(base_coin) = '{SOL}' THEN toString(quote_coin)
                ELSE toString(base_coin)
            END
        ) as tokens,
        countIf(direction = 'B') as buys,
        countIf(direction = 'S') as sells,
        sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'B'
        ) / 1e9 as spent_sol,
        sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'S'
        ) / 1e9 as received_sol
    FROM default.raydium_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
      AND (toString(base_coin) = '{SOL}' OR toString(quote_coin) = '{SOL}')
    GROUP BY signing_wallet
    HAVING trades >= {min_trades} AND trades <= {max_trades}
    """

    print("Загружаю данные...")
    df_pf = query(sql_pf)
    df_ray = query(sql_ray)
    
    all_data = pd.concat([df_pf, df_ray], ignore_index=True)
    
    if all_data.empty:
        print("Ничего не найдено")
        return

    # Группируем по кошельку
    combined = all_data.groupby('wallet').agg({
        'trades': 'sum',
        'tokens': 'sum',
        'buys': 'sum',
        'sells': 'sum',
        'spent_sol': 'sum',
        'received_sol': 'sum',
        'platform': lambda x: '+'.join(sorted(set(x)))
    }).reset_index()

    # Считаем метрики
    combined['pnl_sol'] = combined['received_sol'] - combined['spent_sol']
    combined['pnl_per_trade'] = combined['pnl_sol'] / combined['trades']
    combined['roi_pct'] = ((combined['received_sol'] / combined['spent_sol'].replace(0, 0.001)) - 1) * 100
    combined['win_rate_approx'] = combined['sells'] / combined['buys'].replace(0, 1)

    # ФИЛЬТР: только те кто реально тратил деньги
    combined = combined[combined['spent_sol'] >= min_spent]
    
    # ФИЛЬТР: только в плюсе
    combined = combined[combined['pnl_sol'] > 0]

    # Сортируем по PnL/trade (эффективность)
    combined = combined.sort_values('pnl_per_trade', ascending=False).head(100)

    print(f"\n{'='*140}")
    print(f"{'#':<3} | {'Кошелёк':<46} | {'Платф':<8} | {'Сделок':>6} | {'Токенов':>7} | {'Spent':>8} | {'PnL SOL':>9} | {'PnL/tr':>7} | {'ROI':>7}")
    print(f"{'='*140}")

    for i, (_, row) in enumerate(combined.iterrows(), 1):
        w = row['wallet'][:18] + "..." + row['wallet'][-6:]
        plat = row['platform'][:8]
        
        flag = ""
        # Инсайдер: мало сделок, высокий pnl/trade, умеренный spent
        if row['pnl_per_trade'] > 2 and row['trades'] < 100:
            flag = "💎"
        if row['roi_pct'] > 100 and row['trades'] < 150:
            flag += "🎯"
            
        print(f"{i:<3} | {w} | {plat:<8} | {row['trades']:>6.0f} | {row['tokens']:>7.0f} | {row['spent_sol']:>8.1f} | {row['pnl_sol']:>9.1f} | {row['pnl_per_trade']:>7.2f} | {row['roi_pct']:>6.0f}% {flag}")

    print(f"{'='*140}")
    print(f"\n💎 = PnL/trade > 2 SOL, < 100 сделок (вероятный инсайдер)")
    print(f"🎯 = ROI > 100%, < 150 сделок")
    
    # Сохраняем
    combined.to_csv('/tmp/insiders.csv', index=False)
    print(f"\n📁 Сохранено в /tmp/insiders.csv")
    
    # Показываем топ-10 для добавления
    print(f"\n\n🏆 ТОП-10 для добавления в smart_money_wallets.json:")
    print("-" * 60)
    top10 = combined.head(10)
    for _, row in top10.iterrows():
        print(f'"{row["wallet"]}",')


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    max_t = int(sys.argv[2]) if len(sys.argv) > 2 else 300  
    min_t = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    min_spent = float(sys.argv[4]) if len(sys.argv) > 4 else 10
    find_real_insiders(days, max_t, min_t, min_spent)
