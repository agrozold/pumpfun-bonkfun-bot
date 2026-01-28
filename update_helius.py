import json
import requests

# === 45618a39-261b-49db-8c3a-876fdec6ad0f ===
API_KEY = "45618a39-261b-49db-8c3a-876fdec6ad0f"
# =============================

MY_BOT_URL = "http://212.113.112.103:8000/webhook"
FILE_NAME = "smart_money_wallets.json"

try:
    print(f"📂 Читаю адреса...")
    with open(FILE_NAME, 'r') as f:
        data = json.load(f)
    wallets = [item["wallet"] for item in data["whales"]]
    print(f"✅ Нашел {len(wallets)} адресов.")

    print(f"📡 Создаю НОВЫЙ вебхук в Helius...")
    
    url = f"https://api.helius.xyz/v0/webhooks?api-key={API_KEY}"
    
    payload = {
        "webhookURL": MY_BOT_URL,
        "accountAddresses": wallets,
        "webhookType": "enhanced",
        "txnStatus": "success",
        "transactionTypes": ["SWAP"] 
    }

    response = requests.post(url, json=payload) # Используем POST для создания

    if response.status_code == 200:
        res_json = response.json()
        new_id = res_json.get("webhookID")
        print(f"\\n🎉 УРА! Вебхук создан!")
        print(f"🆔 Его ID: {new_id}")
        print("Все 99 кошельков внутри. Можно работать.")
    else:
        print(f"\\n❌ ОШИБКА: {response.text}")

except Exception as e:
    print(f"❌ Ошибка: {e}")
