"""Детальный анализ кошелька (pumpfun + raydium)"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

SOL = "So11111111111111111111111111111111111111112"

def analyze_wallet(wallet: str, days: int = 30):
    """Полный разбор активности кошелька"""

    print(f"\n{'='*70}")
    print(f"🔍 Анализ: {wallet[:20]}...{wallet[-8:]}")
    print(f"   Период: {days} дней")
    print(f"{'='*70}")

    # === PUMPFUN ===
    sql_pf = f"""
    SELECT
        count(*) as trades,
        uniqExact(toString(base_coin)) as tokens,
        countIf(direction = 'buy') as buys,
        countIf(direction = 'sell') as sells,
        sumIf(quote_coin_amount, direction = 'buy') / 1e9 as spent,
        sumIf(quote_coin_amount, direction = 'sell') / 1e9 as received,
        min(block_time) as first_trade,
        max(block_time) as last_trade,
        uniqExact(toDate(block_time)) as active_days
    FROM default.pumpfun_all_swaps
    WHERE toString(signing_wallet) = '{wallet}'
      AND block_time > now() - INTERVAL {days} DAY
    """
    
    pf = query(sql_pf)
    has_pf = not pf.empty and pf['trades'].iloc[0] > 0

    # === RAYDIUM ===
    sql_ray = f"""
    SELECT
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
        min(block_time) as first_trade,
        max(block_time) as last_trade,
        uniqExact(toDate(block_time)) as active_days
    FROM default.raydium_all_swaps
    WHERE toString(signing_wallet) = '{wallet}'
      AND block_time > now() - INTERVAL {days} DAY
      AND (toString(base_coin) = '{SOL}' OR toString(quote_coin) = '{SOL}')
    """
    
    ray = query(sql_ray)
    has_ray = not ray.empty and ray['trades'].iloc[0] > 0

    if not has_pf and not has_ray:
        print("\n❌ Нет данных ни на pumpfun, ни на raydium")
        return

    # Выводим статистику по каждой платформе
    for platform, df, has_data in [('PUMPFUN', pf, has_pf), ('RAYDIUM', ray, has_ray)]:
        if not has_data:
            continue
            
        s = df.iloc[0]
        pnl = s['received'] - s['spent']
        
        print(f"\n📊 {platform}:")
        print(f"   Сделок: {s['trades']} ({s['buys']} buy / {s['sells']} sell)")
        print(f"   Токенов: {s['tokens']}")
        print(f"   Потрачено: {s['spent']:.2f} SOL → Получено: {s['received']:.2f} SOL")
        print(f"   PnL: {pnl:.2f} SOL ({(pnl/s['spent']*100) if s['spent'] > 0 else 0:.1f}%)")
        print(f"   Активных дней: {s['active_days']}")

    # === Детали по активной платформе ===
    if has_pf:
        print(f"\n{'─'*70}")
        print("📈 ДЕТАЛИ PUMPFUN:")
        analyze_platform(wallet, days, 'pumpfun')
    
    if has_ray:
        print(f"\n{'─'*70}")
        print("📈 ДЕТАЛИ RAYDIUM:")
        analyze_platform(wallet, days, 'raydium')

    print(f"\n{'='*70}\n")


def analyze_platform(wallet: str, days: int, platform: str):
    """Детальный анализ по платформе"""
    
    if platform == 'pumpfun':
        # Активность по дням
        sql_days = f"""
        SELECT toDate(block_time) as day, count(*) as trades
        FROM default.pumpfun_all_swaps
        WHERE toString(signing_wallet) = '{wallet}'
          AND block_time > now() - INTERVAL {days} DAY
        GROUP BY day ORDER BY day
        """
        
        # PnL по токенам
        sql_tokens = f"""
        SELECT
            toString(base_coin) as token,
            countIf(direction = 'buy') as buys,
            countIf(direction = 'sell') as sells,
            sumIf(quote_coin_amount, direction = 'buy') / 1e9 as spent,
            sumIf(quote_coin_amount, direction = 'sell') / 1e9 as received,
            (sumIf(quote_coin_amount, direction = 'sell') - sumIf(quote_coin_amount, direction = 'buy')) / 1e9 as pnl
        FROM default.pumpfun_all_swaps
        WHERE toString(signing_wallet) = '{wallet}'
          AND block_time > now() - INTERVAL {days} DAY
        GROUP BY base_coin ORDER BY pnl DESC
        """
        
        # Суммы покупок
        sql_amounts = f"""
        SELECT round(quote_coin_amount / 1e9, 2) as amount, count(*) as cnt
        FROM default.pumpfun_all_swaps
        WHERE toString(signing_wallet) = '{wallet}'
          AND direction = 'buy'
          AND block_time > now() - INTERVAL {days} DAY
        GROUP BY amount ORDER BY cnt DESC LIMIT 10
        """
    else:
        # Raydium
        sql_days = f"""
        SELECT toDate(block_time) as day, count(*) as trades
        FROM default.raydium_all_swaps
        WHERE toString(signing_wallet) = '{wallet}'
          AND block_time > now() - INTERVAL {days} DAY
          AND (toString(base_coin) = '{SOL}' OR toString(quote_coin) = '{SOL}')
        GROUP BY day ORDER BY day
        """
        
        sql_tokens = f"""
        SELECT
            CASE WHEN toString(base_coin) = '{SOL}' THEN toString(quote_coin)
            ELSE toString(base_coin) END as token,
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
            (sumIf(
                CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
                direction = 'S'
            ) - sumIf(
                CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END,
                direction = 'B'
            )) / 1e9 as pnl
        FROM default.raydium_all_swaps
        WHERE toString(signing_wallet) = '{wallet}'
          AND block_time > now() - INTERVAL {days} DAY
          AND (toString(base_coin) = '{SOL}' OR toString(quote_coin) = '{SOL}')
        GROUP BY token ORDER BY pnl DESC
        """
        
        sql_amounts = f"""
        SELECT round(
            CASE WHEN toString(base_coin) = '{SOL}' THEN base_coin_amount ELSE quote_coin_amount END / 1e9,
            2
        ) as amount, count(*) as cnt
        FROM default.raydium_all_swaps
        WHERE toString(signing_wallet) = '{wallet}'
          AND direction = 'B'
          AND block_time > now() - INTERVAL {days} DAY
          AND (toString(base_coin) = '{SOL}' OR toString(quote_coin) = '{SOL}')
        GROUP BY amount ORDER BY cnt DESC LIMIT 10
        """

    # Выводим
    days_df = query(sql_days)
    print(f"\n   📅 Активность по дням:")
    for _, row in days_df.iterrows():
        bar = "█" * min(int(row['trades'] / 2), 25)
        print(f"      {row['day']} | {row['trades']:>3} | {bar}")

    tokens_df = query(sql_tokens)
    profitable = len(tokens_df[tokens_df['pnl'] > 0])
    total = len(tokens_df)
    
    print(f"\n   💰 Токены (win rate: {profitable}/{total} = {profitable/total*100:.0f}%):")
    for _, row in tokens_df.head(10).iterrows():
        t = row['token'][:16] + "..." + row['token'][-4:]
        status = "✅" if row['pnl'] > 0 else "❌"
        print(f"      {t} | {row['buys']}/{row['sells']} | spent:{row['spent']:>6.2f} | pnl:{row['pnl']:>+8.2f} {status}")

    amounts_df = query(sql_amounts)
    print(f"\n   💵 Суммы покупок:")
    for _, row in amounts_df.head(5).iterrows():
        print(f"      {row['amount']:>8.2f} SOL × {row['cnt']}")

    # Вердикт
    unique_amounts = len(amounts_df)
    top_pct = (amounts_df['cnt'].iloc[0] / amounts_df['cnt'].sum() * 100) if len(amounts_df) > 0 else 0
    active_days = len(days_df)
    trades_per_day = days_df['trades'].sum() / max(active_days, 1)
    
    print(f"\n   🔎 Вердикт:")
    if active_days >= 5:
        print(f"      ✅ Регулярная активность ({active_days} дней)")
    else:
        print(f"      ⚠️  Мало активных дней ({active_days})")
    
    if profitable/total >= 0.5:
        print(f"      ✅ Хороший win rate ({profitable/total*100:.0f}%)")
    else:
        print(f"      ⚠️  Низкий win rate ({profitable/total*100:.0f}%)")
    
    if unique_amounts >= 3 and top_pct < 70:
        print(f"      ✅ Разные суммы (не бот)")
    else:
        print(f"      ⚠️  Однотипные суммы (возможно бот)")
    
    if trades_per_day < 15:
        print(f"      ✅ Умеренная активность ({trades_per_day:.1f}/день)")
    else:
        print(f"      ⚠️  Высокая активность ({trades_per_day:.1f}/день)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python analyze_wallet.py <WALLET> [DAYS]")
        sys.exit(1)
    
    wallet = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    analyze_wallet(wallet, days)
