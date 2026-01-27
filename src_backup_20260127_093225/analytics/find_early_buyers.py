"""Поиск early buyers успешных токенов"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

def find_early_buyers(days: int = 7, min_multiple: float = 10):
    """
    1. Найти токены которые выросли в min_multiple раз
    2. Найти кто купил их в первые 200 слотов (~80 сек)
    3. Посмотреть кто из них покупал рано несколько успешных токенов
    """

    print(f"\n🔍 Поиск early buyers за {days} дней")
    print(f"   Минимальный рост токена: {min_multiple}x\n")

    # Шаг 1: Найти успешные токены
    print("📈 Ищу успешные токены...")
    
    sql_tokens = f"""
    WITH token_stats AS (
        SELECT
            toString(base_coin) as token,
            min(slot) as first_slot,
            argMin(quote_coin_amount / base_coin_amount, slot) as first_price,
            argMax(quote_coin_amount / base_coin_amount, slot) as max_price,
            count(*) as total_trades,
            sum(quote_coin_amount) / 1e9 as total_volume_sol
        FROM default.pumpfun_all_swaps
        WHERE block_time > now() - INTERVAL {days} DAY
          AND base_coin_amount > 0
          AND quote_coin_amount > 0
        GROUP BY base_coin
        HAVING 
            total_trades >= 50
            AND total_volume_sol >= 10
            AND first_price > 0
    )
    SELECT 
        token,
        first_slot,
        max_price / first_price as growth_multiple,
        total_volume_sol
    FROM token_stats
    WHERE max_price / first_price >= {min_multiple}
    ORDER BY growth_multiple DESC
    LIMIT 50
    """
    
    tokens_df = query(sql_tokens)
    
    if tokens_df.empty:
        print("Не найдено успешных токенов")
        return
    
    print(f"   Найдено {len(tokens_df)} токенов с ростом >= {min_multiple}x")
    
    for _, row in tokens_df.head(10).iterrows():
        t = row['token'][:16] + "..." + row['token'][-4:]
        print(f"   {t} | {row['growth_multiple']:.0f}x | {row['total_volume_sol']:.0f} SOL")

    # Шаг 2: Найти early buyers
    print(f"\n👀 Ищу early buyers...")
    
    token_list = "', '".join(tokens_df['token'].tolist())
    
    sql_early = f"""
    WITH successful_tokens AS (
        SELECT
            toString(base_coin) as token,
            min(slot) as token_first_slot
        FROM default.pumpfun_all_swaps
        WHERE toString(base_coin) IN ('{token_list}')
        GROUP BY base_coin
    ),
    early_buys AS (
        SELECT
            toString(s.signing_wallet) as wallet,
            toString(s.base_coin) as token,
            min(s.slot) as buy_slot,
            sum(s.quote_coin_amount) / 1e9 as spent_sol,
            st.token_first_slot
        FROM default.pumpfun_all_swaps s
        JOIN successful_tokens st ON toString(s.base_coin) = st.token
        WHERE s.direction = 'buy'
          AND s.slot <= st.token_first_slot + 200
        GROUP BY s.signing_wallet, s.base_coin, st.token_first_slot
        HAVING spent_sol >= 0.1
    )
    SELECT
        wallet,
        count(DISTINCT token) as early_tokens_count,
        sum(spent_sol) as total_early_spent,
        avg(buy_slot - token_first_slot) as avg_slots_after_start
    FROM early_buys
    GROUP BY wallet
    HAVING early_tokens_count >= 2
    ORDER BY early_tokens_count DESC, avg_slots_after_start ASC
    LIMIT 100
    """
    
    early_df = query(sql_early)
    
    if early_df.empty:
        print("Не найдено early buyers")
        return

    print(f"   Найдено {len(early_df)} кошельков с 2+ early токенами")

    # Шаг 3: Статистика кошельков
    print(f"\n📊 Получаю статистику...")
    
    wallet_list = "', '".join(early_df['wallet'].tolist())
    
    sql_stats = f"""
    SELECT
        toString(signing_wallet) as wallet,
        count(*) as total_trades,
        uniqExact(toString(base_coin)) as total_tokens,
        uniqExact(toDate(block_time)) as active_days,
        sumIf(quote_coin_amount, direction = 'buy') / 1e9 as total_spent,
        sumIf(quote_coin_amount, direction = 'sell') / 1e9 as total_received,
        avgIf(quote_coin_amount / 1e9, direction = 'buy') as avg_buy_size
    FROM default.pumpfun_all_swaps
    WHERE toString(signing_wallet) IN ('{wallet_list}')
      AND block_time > now() - INTERVAL 30 DAY
    GROUP BY signing_wallet
    """
    
    stats_df = query(sql_stats)
    
    import pandas as pd
    result = pd.merge(early_df, stats_df, on='wallet', how='left')
    result['pnl'] = result['total_received'] - result['total_spent']
    
    # Фильтр: не боты
    result = result[
        (result['active_days'] >= 2) &
        (result['total_trades'] <= 500) &
        (result['avg_buy_size'] <= 5)
    ]
    
    result = result.sort_values('early_tokens_count', ascending=False)

    print(f"\n{'='*150}")
    print(f"{'#':<3} | {'Кошелёк':<44} | {'Early':>5} | {'AvgSlot':>7} | {'Trades':>6} | {'Days':>4} | {'AvgBuy':>6} | {'PnL':>8}")
    print(f"{'='*150}")

    for i, (_, row) in enumerate(result.head(50).iterrows(), 1):
        w = row['wallet'][:18] + "..." + row['wallet'][-6:]
        
        flag = ""
        if row['early_tokens_count'] >= 3 and row['avg_slots_after_start'] < 50:
            flag = "💎"
        if row['pnl'] > 0 and row['early_tokens_count'] >= 2:
            flag += "🎯"
            
        print(f"{i:<3} | {w} | {row['early_tokens_count']:>5.0f} | {row['avg_slots_after_start']:>7.0f} | {row['total_trades']:>6.0f} | {row['active_days']:>4.0f} | {row['avg_buy_size']:>6.2f} | {row['pnl']:>8.1f} {flag}")

    print(f"{'='*150}")
    print(f"\n💎 = 3+ early + avg < 50 слотов")
    print(f"🎯 = PnL > 0")

    # Для проверки на GMGN
    top = result[result['early_tokens_count'] >= 2].head(20)
    
    print(f"\n\n🏆 ПРОВЕРИТЬ НА GMGN ({len(top)} шт):")
    print("-" * 60)
    for _, row in top.iterrows():
        print(f'{row["wallet"]}')
        print(f'   Early:{row["early_tokens_count"]:.0f} | Slot:{row["avg_slots_after_start"]:.0f} | PnL:{row["pnl"]:.0f}')
        print()


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    min_mult = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    find_early_buyers(days, min_mult)
