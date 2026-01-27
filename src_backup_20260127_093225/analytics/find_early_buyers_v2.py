"""Поиск early buyers v2 - по объёму (исправленный)"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

def find_early_buyers(days: int = 14, min_volume: float = 100):
    print(f"\n🔍 Поиск early buyers за {days} дней")
    print(f"   Минимальный объём токена: {min_volume} SOL\n")

    # Шаг 1: Токены с большим объёмом
    print("📈 Ищу популярные токены...")
    
    sql_tokens = f"""
    SELECT
        toString(base_coin) as token,
        min(slot) as first_slot,
        sum(quote_coin_amount) / 1e9 as total_volume_sol
    FROM default.pumpfun_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
    GROUP BY base_coin
    HAVING total_volume_sol >= {min_volume}
    ORDER BY total_volume_sol DESC
    LIMIT 50
    """
    
    tokens_df = query(sql_tokens)
    
    if tokens_df.empty:
        print("Не найдено токенов")
        return
    
    print(f"   Найдено {len(tokens_df)} токенов")

    # Шаг 2: Early buyers (первые 100 слотов) - сразу со статистикой
    print(f"\n👀 Ищу early buyers со статистикой...")
    
    token_list = "', '".join(tokens_df['token'].tolist())
    
    sql = f"""
    WITH token_starts AS (
        SELECT
            toString(base_coin) as token,
            min(slot) as first_slot
        FROM default.pumpfun_all_swaps
        WHERE toString(base_coin) IN ('{token_list}')
        GROUP BY base_coin
    ),
    early_buyers AS (
        SELECT DISTINCT toString(s.signing_wallet) as wallet
        FROM default.pumpfun_all_swaps s
        JOIN token_starts ts ON toString(s.base_coin) = ts.token
        WHERE s.direction = 'buy'
          AND s.slot <= ts.first_slot + 100
          AND s.quote_coin_amount / 1e9 >= 0.1
    ),
    early_counts AS (
        SELECT
            toString(s.signing_wallet) as wallet,
            count(DISTINCT toString(s.base_coin)) as early_tokens
        FROM default.pumpfun_all_swaps s
        JOIN token_starts ts ON toString(s.base_coin) = ts.token
        WHERE s.direction = 'buy'
          AND s.slot <= ts.first_slot + 100
          AND toString(s.signing_wallet) IN (SELECT wallet FROM early_buyers)
        GROUP BY s.signing_wallet
        HAVING early_tokens >= 3
    )
    SELECT
        ec.wallet,
        ec.early_tokens,
        count(*) as total_trades,
        uniqExact(toString(base_coin)) as total_tokens,
        uniqExact(toDate(block_time)) as active_days,
        sumIf(quote_coin_amount, direction = 'buy') / 1e9 as total_spent,
        sumIf(quote_coin_amount, direction = 'sell') / 1e9 as total_received,
        avgIf(quote_coin_amount / 1e9, direction = 'buy') as avg_buy
    FROM default.pumpfun_all_swaps s
    JOIN early_counts ec ON toString(s.signing_wallet) = ec.wallet
    WHERE block_time > now() - INTERVAL 30 DAY
    GROUP BY ec.wallet, ec.early_tokens
    HAVING total_trades <= 500 AND avg_buy <= 5 AND active_days >= 3
    ORDER BY early_tokens DESC
    LIMIT 50
    """
    
    result = query(sql)
    
    if result.empty:
        print("Не найдено подходящих кошельков")
        return

    result['pnl'] = result['total_received'] - result['total_spent']

    print(f"\n{'='*140}")
    print(f"{'#':<3} | {'Кошелёк':<44} | {'Early':>5} | {'Total':>5} | {'Days':>4} | {'AvgBuy':>6} | {'PnL':>9}")
    print(f"{'='*140}")

    for i, (_, row) in enumerate(result.iterrows(), 1):
        w = row['wallet'][:18] + "..." + row['wallet'][-6:]
        
        flag = ""
        if row['early_tokens'] >= 5:
            flag = "💎"
        if row['pnl'] > 0:
            flag += "🎯"
            
        print(f"{i:<3} | {w} | {row['early_tokens']:>5.0f} | {row['total_tokens']:>5.0f} | {row['active_days']:>4.0f} | {row['avg_buy']:>6.2f} | {row['pnl']:>9.1f} {flag}")

    print(f"{'='*140}")

    # Топ для GMGN
    top = result[result['pnl'] > 0].head(20)
    
    print(f"\n\n🏆 ПРОВЕРИТЬ НА GMGN ({len(top)} шт):")
    print("-" * 50)
    for _, row in top.iterrows():
        print(row['wallet'])


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    min_vol = float(sys.argv[2]) if len(sys.argv) > 2 else 100
    find_early_buyers(days, min_vol)
