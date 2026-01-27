from indexer_db import query
import json

WALLETS_FILE = "/opt/pumpfun-bonkfun-bot/smart_money_wallets.json"

# Загружаем твои кошельки
with open(WALLETS_FILE) as f:
    data = json.load(f)
my_wallets = set(w['wallet'] for w in data['whales'])

# Топ по PnL за 14 дней
sql = """
SELECT 
    toString(signing_wallet) as wallet,
    count(*) as trades,
    (sumIf(quote_coin_amount, direction = 'sell') - sumIf(quote_coin_amount, direction = 'buy')) / 1e9 as pnl_sol
FROM default.pumpfun_all_swaps
WHERE block_time > now() - INTERVAL 14 DAY
GROUP BY signing_wallet
HAVING trades >= 20
ORDER BY pnl_sol DESC
LIMIT 100
"""

df = query(sql)

print("=" * 80)
print("ТОП КИТЫ ПО PnL (14 дней) — которых НЕТ в твоём списке")
print("=" * 80)
print()

count = 0
for _, row in df.iterrows():
    if row['wallet'] not in my_wallets:
        count += 1
        print(f"{count:>2}. {row['wallet']}")
        print(f"    PnL: {row['pnl_sol']:>10.2f} SOL | Сделок: {row['trades']}")
        print()
        if count >= 20:
            break
