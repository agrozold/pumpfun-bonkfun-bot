from indexer_db import query

sql = """
SELECT 
    toString(signing_wallet) as wallet,
    toString(direction) as direction,
    toString(base_coin) as base_coin,
    base_coin_amount,
    quote_coin_amount,
    formatDateTime(block_time, '%Y-%m-%d %H:%i:%S') as ts
FROM default.pumpfun_all_swaps
LIMIT 3
"""

df = query(sql)
print(df.to_string())
