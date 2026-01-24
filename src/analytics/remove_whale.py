import json

WALLETS_FILE = "/opt/pumpfun-bonkfun-bot/smart_money_wallets.json"
REMOVE = "GijFWw4oNyh9ko3FaZforNsi3jk6wDovARpkKahPD4o5"

with open(WALLETS_FILE) as f:
    data = json.load(f)

before = len(data['whales'])
data['whales'] = [w for w in data['whales'] if w['wallet'] != REMOVE]
after = len(data['whales'])

with open(WALLETS_FILE, 'w') as f:
    json.dump(data, f, indent=2)

print(f"Удалён: {REMOVE[:20]}...{REMOVE[-8:]}")
print(f"Было: {before} | Стало: {after}")
