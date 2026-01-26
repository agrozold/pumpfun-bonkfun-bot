"""Поиск инсайдеров v3: проверяем consistency"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query
import pandas as pd

SOL = "So11111111111111111111111111111111111111112"

def find_consistent_winners(days: int = 30, max_trades: int = 300, min_trades: int = 10, min_spent: float = 10, min_tokens: int = 3):
    """
    Ищем тех кто стабильно в плюсе на РАЗНЫХ токенах
    """

    print(f"\n📊 Поиск стабильных инсайдеров за {days} дней")
    print(f"   Фильтры: {min_trades}-{max_trades} сделок, spent >= {min_spent} SOL, токенов >= {min_tokens}\n")

    # Pumpfun: сразу агрегируем по кошельку
    sql_pf = f"""
    SELECT
        toString(signing_wallet) as wallet,
        count(*) as trades,
        uniqExact(toString(base_coin)) as total_tokens,
        sumIf(quote_coin_amount, direction = 'buy') / 1e9 as total_spent,
        sumIf(quote_coin_amount, direction = 'sell') / 1e9 as total_received,
        uniqExact(toDate(block_time)) as active_days
    FROM default.pumpfun_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
    GROUP BY signing_wallet
    HAVING trades >= {min_trades} AND trades <= {max_trades} AND total_spent >= {min_spent}
    """

    print("Загружаю pumpfun...")
    df_pf = query(sql_pf)
    print(f"  Найдено: {len(df_pf)}")

    # Raydium
    sql_ray = f"""
    SELECT
        toString(signing_wallet) as wallet,
        count(*) as trades,
        uniqExact(
            CASE 
                WHEN toString(base_coin) = '{SOL}' THEN toString(quote_coin)
                ELSE toString(base_coin)
            END
        ) as total_tokens,
        sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'B'
        ) / 1e9 as total_spent,
        sumIf(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
            direction = 'S'
        ) / 1e9 as total_received,
        uniqExact(toDate(block_time)) as active_days
    FROM default.raydium_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
      AND (toString(base_coin) = '{SOL}' OR toString(quote_coin) = '{SOL}')
    GROUP BY signing_wallet
    HAVING trades >= {min_trades} AND trades <= {max_trades} AND total_spent >= {min_spent}
    """

    print("Загружаю raydium...")
    df_ray = query(sql_ray)
    print(f"  Найдено: {len(df_ray)}")

    if df_pf.empty and df_ray.empty:
        print("Нет данных")
        return

    # Объединяем
    all_data = pd.concat([df_pf, df_ray], ignore_index=True)
    
    combined = all_data.groupby('wallet').agg({
        'trades': 'sum',
        'total_tokens': 'sum',
        'total_spent': 'sum',
        'total_received': 'sum',
        'active_days': 'max'
    }).reset_index()

    # Метрики
    combined['pnl'] = combined['total_received'] - combined['total_spent']
    combined['roi'] = ((combined['total_received'] / combined['total_spent'].replace(0, 0.001)) - 1) * 100
    combined['pnl_per_trade'] = combined['pnl'] / combined['trades']
    combined['trades_per_day'] = combined['trades'] / combined['active_days'].replace(0, 1)

    # Фильтры
    combined = combined[
        (combined['pnl'] > 0) &
        (combined['total_tokens'] >= min_tokens) &
        (combined['active_days'] >= 3)  # минимум 3 активных дня
    ]

    # Сортируем по PnL
    combined = combined.sort_values('pnl', ascending=False).head(100)

    print(f"\n{'='*150}")
    print(f"{'#':<3} | {'Кошелёк':<46} | {'Сделок':>6} | {'Токенов':>7} | {'Дней':>5} | {'Tr/day':>6} | {'Spent':>9} | {'PnL':>10} | {'PnL/tr':>7} | {'ROI':>7}")
    print(f"{'='*150}")

    for i, (_, row) in enumerate(combined.iterrows(), 1):
        w = row['wallet'][:18] + "..." + row['wallet'][-6:]
        
        flag = ""
        # Хорошие признаки
        if row['active_days'] >= 7 and row['pnl'] > 50:
            flag = "💎"  # Активен долго
        if row['trades_per_day'] < 15 and row['pnl_per_trade'] > 1:
            flag += "🎯"  # Не спамит, но профитный
        if row['roi'] > 100:
            flag += "🚀"
            
        print(f"{i:<3} | {w} | {row['trades']:>6.0f} | {row['total_tokens']:>7.0f} | {row['active_days']:>5.0f} | {row['trades_per_day']:>6.1f} | {row['total_spent']:>9.1f} | {row['pnl']:>10.1f} | {row['pnl_per_trade']:>7.2f} | {row['roi']:>6.0f}% {flag}")

    print(f"{'='*150}")
    print(f"\n💎 = Активен 7+ дней + PnL > 50 SOL")
    print(f"🎯 = < 15 сделок/день + PnL/trade > 1 SOL")
    print(f"🚀 = ROI > 100%")
    
    combined.to_csv('/tmp/consistent_winners.csv', index=False)
    print(f"\n📁 Сохранено в /tmp/consistent_winners.csv")

    # Рекомендации
    recommended = combined[
        (combined['active_days'] >= 5) &
        (combined['pnl'] >= 30) &
        (combined['trades_per_day'] < 20)
    ].head(20)
    
    print(f"\n\n🏆 РЕКОМЕНДУЕМЫЕ ({len(recommended)} шт):")
    print("-" * 70)
    for _, row in recommended.iterrows():
        print(f'"{row["wallet"]}",  # PnL:{row["pnl"]:.0f} Days:{row["active_days"]:.0f} Tokens:{row["total_tokens"]:.0f}')


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    max_t = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    min_t = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    min_spent = float(sys.argv[4]) if len(sys.argv) > 4 else 10
    min_tokens = int(sys.argv[5]) if len(sys.argv) > 5 else 5
    find_consistent_winners(days, max_t, min_t, min_spent, min_tokens)
