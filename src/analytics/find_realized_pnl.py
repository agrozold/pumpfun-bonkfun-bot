"""Поиск по REALIZED PnL (только закрытые позиции)"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

def find_realized_winners(days: int = 30, min_pnl: float = 10, max_avg_buy: float = 5):
    """
    Считаем только ЗАКРЫТЫЕ позиции (продал >= 80% от купленного)
    """

    print(f"\n📊 Поиск по Realized PnL за {days} дней")
    print(f"   Только закрытые позиции, avg buy <= {max_avg_buy} SOL\n")

    sql = f"""
    WITH token_pnl AS (
        SELECT
            toString(signing_wallet) as wallet,
            toString(base_coin) as token,
            sumIf(quote_coin_amount, direction = 'buy') / 1e9 as bought,
            sumIf(quote_coin_amount, direction = 'sell') / 1e9 as sold,
            countIf(direction = 'buy') as buy_cnt,
            countIf(direction = 'sell') as sell_cnt
        FROM default.pumpfun_all_swaps
        WHERE block_time > now() - INTERVAL {days} DAY
        GROUP BY signing_wallet, base_coin
        HAVING bought >= 0.05
    ),
    closed_positions AS (
        SELECT
            wallet,
            token,
            bought,
            sold,
            sold - bought as pnl,
            CASE WHEN bought > 0 THEN sold / bought ELSE 0 END as multiplier
        FROM token_pnl
        WHERE sold >= bought * 0.8  -- Закрытая позиция: продал >= 80%
    )
    SELECT
        wallet,
        count(*) as closed_tokens,
        countIf(pnl > 0) as winning_tokens,
        sum(bought) as total_spent,
        sum(pnl) as realized_pnl,
        avg(bought) as avg_position_size,
        avg(multiplier) as avg_multiplier,
        countIf(pnl > 0) * 100.0 / count(*) as win_rate
    FROM closed_positions
    GROUP BY wallet
    HAVING 
        closed_tokens >= 5
        AND realized_pnl >= {min_pnl}
        AND avg_position_size <= {max_avg_buy}
        AND win_rate >= 40
    ORDER BY realized_pnl DESC
    LIMIT 100
    """

    df = query(sql)

    if df.empty:
        print("Ничего не найдено")
        return

    print(f"{'='*140}")
    print(f"{'#':<3} | {'Кошелёк':<44} | {'Closed':>6} | {'Wins':>5} | {'WinRate':>7} | {'AvgSize':>7} | {'AvgX':>5} | {'R.PnL':>9}")
    print(f"{'='*140}")

    for i, (_, row) in enumerate(df.iterrows(), 1):
        w = row['wallet'][:18] + "..." + row['wallet'][-6:]
        
        flag = ""
        if row['win_rate'] >= 60 and row['closed_tokens'] >= 10:
            flag = "💎"
        if row['avg_multiplier'] >= 2 and row['win_rate'] >= 50:
            flag += "🚀"
            
        print(f"{i:<3} | {w} | {row['closed_tokens']:>6.0f} | {row['winning_tokens']:>5.0f} | {row['win_rate']:>6.1f}% | {row['avg_position_size']:>7.2f} | {row['avg_multiplier']:>5.2f} | {row['realized_pnl']:>9.1f} {flag}")

    print(f"{'='*140}")
    print(f"\n💎 = Win rate >= 60% + 10+ закрытых токенов")
    print(f"🚀 = Avg multiplier >= 2x + win rate >= 50%")

    good = df[(df['win_rate'] >= 50) & (df['closed_tokens'] >= 7)]
    
    print(f"\n\n🏆 ПРОВЕРЕННЫЕ ({len(good)} шт):")
    print("-" * 80)
    for _, row in good.head(25).iterrows():
        print(f'"{row["wallet"]}",  # WR:{row["win_rate"]:.0f}% Closed:{row["closed_tokens"]:.0f} AvgX:{row["avg_multiplier"]:.1f} PnL:{row["realized_pnl"]:.0f}')


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    min_pnl = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    max_avg = float(sys.argv[3]) if len(sys.argv) > 3 else 5
    find_realized_winners(days, min_pnl, max_avg)
