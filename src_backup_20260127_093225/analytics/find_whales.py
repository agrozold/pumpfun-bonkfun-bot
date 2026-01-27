"""Поиск активных кошельков через индексер"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

def find_active_traders(days: int = 3, min_trades: int = 10):
    """Найти кошельки с высокой активностью"""
    
    print(f"\nИщу активных трейдеров за {days} дней (мин. {min_trades} сделок)...\n")
    
    sql = f"""
    SELECT 
        toString(signing_wallet) as wallet,
        count(*) as trades,
        uniqExact(toString(base_coin)) as tokens,
        countIf(direction = 'buy') as buys,
        countIf(direction = 'sell') as sells
    FROM default.pumpfun_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
    GROUP BY signing_wallet
    HAVING trades >= {min_trades}
    ORDER BY trades DESC
    LIMIT 50
    """
    
    df = query(sql)
    
    if df.empty:
        print("Ничего не найдено")
        return
    
    print(f"{'Кошелёк':<46} | Сделок | Токенов | B/S")
    print("-"*70)
    
    for _, row in df.iterrows():
        w = row['wallet'][:20] + "..." + row['wallet'][-8:]
        print(f"{w} | {row['trades']:>6} | {row['tokens']:>7} | {row['buys']}/{row['sells']}")
    
    print(f"\nНайдено: {len(df)}")


def find_by_pnl(days: int = 7, min_trades: int = 10):
    """Найти кошельки отсортированные по PnL"""
    
    print(f"\nИщу по PnL за {days} дней (мин. {min_trades} сделок)...\n")
    
    sql = f"""
    SELECT 
        toString(signing_wallet) as wallet,
        count(*) as trades,
        uniqExact(toString(base_coin)) as tokens,
        countIf(direction = 'buy') as buys,
        countIf(direction = 'sell') as sells,
        sumIf(quote_coin_amount, direction = 'sell') as sol_received,
        sumIf(quote_coin_amount, direction = 'buy') as sol_spent,
        (sumIf(quote_coin_amount, direction = 'sell') - sumIf(quote_coin_amount, direction = 'buy')) as pnl_raw
    FROM default.pumpfun_all_swaps
    WHERE block_time > now() - INTERVAL {days} DAY
    GROUP BY signing_wallet
    HAVING trades >= {min_trades}
    ORDER BY pnl_raw DESC
    LIMIT 50
    """
    
    df = query(sql)
    
    if df.empty:
        print("Ничего не найдено")
        return
    
    print(f"{'Кошелёк':<46} | Сделок |   PnL (SOL) | B/S")
    print("-"*80)
    
    for _, row in df.iterrows():
        w = row['wallet'][:20] + "..." + row['wallet'][-8:]
        # quote_coin_amount в lamports, делим на 1e9 для SOL
        pnl_sol = row['pnl_raw'] / 1e9
        print(f"{w} | {row['trades']:>6} | {pnl_sol:>11.2f} | {row['buys']}/{row['sells']}")
    
    print(f"\nНайдено: {len(df)}")
    print("Примечание: PnL = SOL получено от продаж - SOL потрачено на покупки")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "active"
    
    if cmd == "active":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        min_t = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        find_active_traders(days, min_t)
    elif cmd == "pnl":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        min_t = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        find_by_pnl(days, min_t)
    else:
        print("Команды:")
        print("  python find_whales.py active [DAYS] [MIN_TRADES]")
        print("  python find_whales.py pnl [DAYS] [MIN_TRADES]")
