"""Строгий поиск инсайдеров"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

def find_strict(days: int = 30):
    print(f"\n🎯 Строгий поиск инсайдеров за {days} дней\n")

    sql = f"""
    WITH token_pnl AS (
        SELECT
            toString(signing_wallet) as wallet,
            toString(base_coin) as token,
            sumIf(quote_coin_amount, direction = 'buy') / 1e9 as bought,
            sumIf(quote_coin_amount, direction = 'sell') / 1e9 as sold,
            min(toDate(block_time)) as first_trade,
            max(toDate(block_time)) as last_trade
        FROM default.pumpfun_all_swaps
        WHERE block_time > now() - INTERVAL {days} DAY
        GROUP BY signing_wallet, base_coin
        HAVING bought >= 0.1 AND bought <= 10
    ),
    wallet_activity AS (
        SELECT 
            toString(signing_wallet) as wallet,
            uniqExact(toDate(block_time)) as active_days,
            count(*) as total_txs
        FROM default.pumpfun_all_swaps
        WHERE block_time > now() - INTERVAL {days} DAY
        GROUP BY signing_wallet
    ),
    closed AS (
        SELECT
            wallet, token, bought, sold,
            sold - bought as pnl,
            sold / bought as multiplier
        FROM token_pnl
        WHERE sold >= bought * 0.8
    )
    SELECT
        c.wallet,
        count(*) as closed_tokens,
        countIf(c.pnl > 0) as wins,
        sum(c.bought) as total_invested,
        sum(c.pnl) as realized_pnl,
        avg(c.bought) as avg_buy,
        avg(c.multiplier) as avg_x,
        max(c.multiplier) as best_x,
        countIf(c.pnl > 0) * 100.0 / count(*) as win_rate,
        wa.active_days,
        wa.total_txs
    FROM closed c
    JOIN wallet_activity wa ON c.wallet = wa.wallet
    GROUP BY c.wallet, wa.active_days, wa.total_txs
    HAVING 
        closed_tokens >= 5
        AND closed_tokens <= 50
        AND realized_pnl >= 20
        AND win_rate >= 55
        AND avg_buy <= 3
        AND active_days >= 5
        AND total_txs <= 500
    ORDER BY 
        (win_rate / 100) * avg_x * realized_pnl DESC
    LIMIT 50
    """

    df = query(sql)

    if df.empty:
        print("Ничего не найдено")
        return

    print(f"{'='*160}")
    print(f"{'#':<3} | {'Кошелёк':<44} | {'Closed':>6} | {'Wins':>5} | {'WR':>5} | {'Days':>4} | {'AvgBuy':>6} | {'AvgX':>5} | {'BestX':>6} | {'PnL':>8}")
    print(f"{'='*160}")

    for i, (_, row) in enumerate(df.iterrows(), 1):
        w = row['wallet'][:18] + "..." + row['wallet'][-6:]
        
        flag = ""
        if row['win_rate'] >= 70 and row['avg_x'] >= 2:
            flag = "💎🚀"
        elif row['win_rate'] >= 60 and row['avg_x'] >= 1.5:
            flag = "💎"
        elif row['best_x'] >= 10:
            flag = "🚀"
            
        print(f"{i:<3} | {w} | {row['closed_tokens']:>6.0f} | {row['wins']:>5.0f} | {row['win_rate']:>4.0f}% | {row['active_days']:>4.0f} | {row['avg_buy']:>6.2f} | {row['avg_x']:>5.2f} | {row['best_x']:>6.1f} | {row['realized_pnl']:>8.1f} {flag}")

    print(f"{'='*160}")

    print(f"\n\n🏆 ТОП ДЛЯ КОПИРОВАНИЯ:")
    print("-" * 80)
    for _, row in df.head(15).iterrows():
        print(f'"{row["wallet"]}",  # WR:{row["win_rate"]:.0f}% AvgX:{row["avg_x"]:.1f} Days:{row["active_days"]:.0f} PnL:{row["realized_pnl"]:.0f}')


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    find_strict(days)
