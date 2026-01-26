"""Поиск инсайдеров: топ-100 кошельков по PnL"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

SOL = "So11111111111111111111111111111111111111112"

def top_wallets(days: int = 30, max_trades: int = 300, min_trades: int = 10):
    """
    Топ-100 кошельков по PnL (pumpfun + raydium)
    """

    print(f"\n📊 Топ-100 кошельков за {days} дней")
    print(f"   Фильтр: {min_trades}-{max_trades} сделок\n")

    # Pumpfun PnL
    sql_pf = f"""
    SELECT
        toString(signing_wallet) as wallet,
        'pumpfun' as platform,
        count(*) as trades,
        uniqExact(toString(base_coin)) as tokens,
        countIf(direction = 'buy') as buys,
        countIf(direction = 'sell') as sells,
        sumIf(quote_coin_amount, direction = 'buy') / 1e9 as spent_sol,
        sumIf(quote_coin_amount, direction = 'sell') / 1e9 as received_sol,
        (sumIf(quote_coin_amount, direction = 'sell') - sumIf(quote_coin_amount, direction = 'buy')) / 1e9 as pnl_sol
    FROM default.pumpfun_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
    GROUP BY signing_wallet
    HAVING trades >= {min_trades} AND trades <= {max_trades}
    """

    # Raydium PnL (только пары с SOL)
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
        ) / 1e9 as received_sol,
        (sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'S'
        ) - sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'B'
        )) / 1e9 as pnl_sol
    FROM default.raydium_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
      AND (toString(base_coin) = '{SOL}' OR toString(quote_coin) = '{SOL}')
    GROUP BY signing_wallet
    HAVING trades >= {min_trades} AND trades <= {max_trades}
    """

    print("Загружаю pumpfun...")
    df_pf = query(sql_pf)
    print(f"  Найдено: {len(df_pf)} кошельков")

    print("Загружаю raydium...")
    df_ray = query(sql_ray)
    print(f"  Найдено: {len(df_ray)} кошельков")

    # Объединяем
    import pandas as pd
    
    if df_pf.empty and df_ray.empty:
        print("\nНичего не найдено")
        return

    # Группируем по кошельку (суммируем pumpfun + raydium)
    all_data = pd.concat([df_pf, df_ray], ignore_index=True)
    
    combined = all_data.groupby('wallet').agg({
        'trades': 'sum',
        'tokens': 'sum',
        'buys': 'sum',
        'sells': 'sum',
        'spent_sol': 'sum',
        'received_sol': 'sum',
        'pnl_sol': 'sum',
        'platform': lambda x: '+'.join(sorted(set(x)))
    }).reset_index()

    # Добавляем метрики
    combined['pnl_per_trade'] = combined['pnl_sol'] / combined['trades']
    combined['win_rate'] = combined['sells'] / combined['buys'].replace(0, 1)  # примерный
    combined['roi'] = (combined['received_sol'] / combined['spent_sol'].replace(0, 1) - 1) * 100

    # Сортируем по PnL
    combined = combined.sort_values('pnl_sol', ascending=False).head(100)

    # Выводим
    print(f"\n{'='*130}")
    print(f"{'#':<3} | {'Кошелёк':<46} | {'Платформа':<12} | {'Сделок':>7} | {'Токенов':>7} | {'PnL SOL':>10} | {'PnL/trade':>9} | {'ROI %':>8} | {'Spent':>8}")
    print(f"{'='*130}")

    for i, (_, row) in enumerate(combined.iterrows(), 1):
        w = row['wallet'][:18] + "..." + row['wallet'][-6:]
        pnl = row['pnl_sol']
        ppt = row['pnl_per_trade']
        roi = row['roi']
        spent = row['spent_sol']
        
        # Флаги
        flag = ""
        if ppt > 0.5 and row['trades'] < 100:
            flag = "💎"
        if roi > 200:
            flag += "🚀"
            
        print(f"{i:<3} | {w} | {row['platform']:<12} | {row['trades']:>7.0f} | {row['tokens']:>7.0f} | {pnl:>10.2f} | {ppt:>9.2f} | {roi:>7.1f}% | {spent:>8.2f} {flag}")

    print(f"{'='*130}")
    print(f"\n💎 = PnL/trade > 0.5 SOL и < 100 сделок")
    print(f"🚀 = ROI > 200%")
    
    # Сохраняем в файл для анализа
    combined.to_csv('/tmp/top_wallets.csv', index=False)
    print(f"\n📁 Полные данные сохранены в /tmp/top_wallets.csv")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    max_t = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    min_t = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    top_wallets(days, max_t, min_t)
