import json
from datetime import datetime

WALLETS_FILE = "/opt/pumpfun-bonkfun-bot/smart_money_wallets.json"

NEW_WHALES = [
    "F2hi9SJ539wSf1HLGpnFvMkzfwA5qASA32SZa6xu81pR",
    "Gpb9EZXGBEvURHUJu5sLVUPerduzwafyEu7VjhhdPRS1",
    "2ezv4U5HmPpkt2xLsKnw1FyyGmjFBeW7c166p99Hw2xB",
    "HK3JyqDeGNvRVyHxinP1Ji7L8iPX1ashVymeW6s9VJi1",
    "5zCkbcD74hFPeBHwYdwJLJAoLVgHX45AFeR7RzC8vFiD",
    "bwamJzztZsepfkteWRChggmXuiiCQvpLqPietdNfSXa",
    "GLtshPTgYTrHLYnJEpARjJ1T15nQVsPbg1ovo8NJrVc7",
    "5TcyQLh8ojBf81DKeRC4vocTbNKJpJCsR9Kei16kLqDM",
    "Hrw3jKrbETiw7qcq4TZFtya2H3dGV2eDoxA1pZ2GWoJP",
    "a4v9H3DhmXHEXG8XcMAZpsQDyueNdWzM4eo4Q2XubNx",
    "GXJ4Up4KjR4UhEPbXN7daRZtobkJzGtpnAgibECzpFRn",
    "9yxmCNwZcHe63NucU9yvCt7b1ja3jwP9v4T3yFMkQ1Z9",
    "nDhEvgMozYMuA37zAmHnwwMGEmQSbnszWpvEHG4Y6iX",
    "7ttyeJ5795HsD479mqHa4F9Y5RN3Qy6NkowMn8Ni3WvQ",
    "7j7AA3HZR2zEjwAQEKPFh2qucLY4fqZpB9iodf39w8xW",
    "22vL22PcYcoAVCwYN8iDW9VrFEYq93TCtr7a6avNVyjL",
    "8XKhq1Ygeznsx54sTHvdjhvLXDM8j2oJeqknd1kRpBjQ",
    "2NoNSAa4F9Srj3M6myFtKTk9VWrYqyHBMbCwwepfYmwi",
    "HmU6LBGKgRCAdQKARQxY2FWWBhkRvia49q8TcaQR7S1d",
    "Bt2ruxiHngxVirnHgFSPC9pmutHRtWRokVxTVVa9ckFd",
]

# Загружаем
with open(WALLETS_FILE) as f:
    data = json.load(f)

existing = set(w['wallet'] for w in data['whales'])
added = 0

for wallet in NEW_WHALES:
    if wallet not in existing:
        data['whales'].append({
            "wallet": wallet,
            "win_rate": 0.7,
            "trades_count": 0,
            "label": "whale",
            "source": "indexer-pnl-top",
            "added_date": datetime.utcnow().isoformat() + "Z"
        })
        added += 1
        print(f"+ {wallet[:20]}...{wallet[-8:]}")

# Сохраняем
with open(WALLETS_FILE, 'w') as f:
    json.dump(data, f, indent=2)

print(f"\nДобавлено: {added} кошельков")
print(f"Всего в списке: {len(data['whales'])}")
