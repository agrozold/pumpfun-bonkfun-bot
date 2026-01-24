"""Проверить историю кошелька через индексер"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from indexer_db import query

def check_wallet(wallet: str, days: int = 7):
    """Показать статистику по кошельку"""
    
    print(f"\n{'='*60}")
    print(f"Кошелёк: {wallet}")
    print(f"Период: последние {days} дней")
    print('='*60)
    
    sql_stats = f"""
    SELECT 
        count(*) as total_trades,
        uniqExact(toString(base_coin)) as unique_tokens,
        countIf(direction = 'buy') as buys,
        countIf(direction = 'sell') as sells
    FROM default.pumpfun_all_swaps
    WHERE toString(signing_wallet) = '{wallet}'
      AND block_time > now() - INTERVAL {days} DAY
    """
    
    stats = query(sql_stats)
    if stats.empty or stats['total_trades'].iloc[0] == 0:
        print("Нет сделок на pump.fun за этот период")
        return
    
    print(f"\nСделок: {stats['total_trades'].iloc[0]}")
    print(f"Уникальных токенов: {stats['unique_tokens'].iloc[0]}")
    print(f"Покупок: {stats['buys'].iloc[0]} | Продаж: {stats['sells'].iloc[0]}")
    
    sql_recent = f"""
    SELECT 
        formatDateTime(block_time, '%Y-%m-%d %H:%i:%S') as ts,
        toString(direction) as direction,
        toString(base_coin) as base_coin
    FROM default.pumpfun_all_swaps
    WHERE toString(signing_wallet) = '{wallet}'
      AND block_time > now() - INTERVAL {days} DAY
    ORDER BY block_time DESC
    LIMIT 10
    """
    
    recent = query(sql_recent)
    print(f"\nПоследние 10 сделок:")
    print("-"*60)
    for _, row in recent.iterrows():
        direction = "BUY " if row['direction'] == 'buy' else "SELL"
        token_short = row['base_coin'][:8] + "..."
        print(f"{row['ts']} | {direction} | {token_short}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python check_wallet.py <WALLET> [DAYS]")
        print("Пример: python check_wallet.py 4Be9Cvxq...ha7t 7")
        sys.exit(1)
    
    wallet_address = sys.argv[1]
    days_back = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    check_wallet(wallet_address, days_back)
