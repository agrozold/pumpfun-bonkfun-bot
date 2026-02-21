import json
import shutil

WALLETS_FILE = "smart_money_wallets.json"
BACKUP_FILE = "smart_money_wallets.backup.json"

# Список лейблов, которые прошли нашу проверку (WinRate > 60% и адекватное время)
WINNERS = ["whale-3", "whale-6", "whale-12", "whale-18", "whale-30", "whale-42"]

def clean_wallets():
    try:
        # Делаем бекап на всякий случай
        shutil.copy(WALLETS_FILE, BACKUP_FILE)
        
        with open(WALLETS_FILE, "r") as f:
            data = json.load(f)
            
        cleaned_whales = []
        original_whales = data.get("whales", data) if isinstance(data, dict) else data
        
        for w in original_whales:
            label = w.get("label", "")
            if label in WINNERS:
                cleaned_whales.append(w)
                
        # Сохраняем обратно в том же формате
        new_data = {"whales": cleaned_whales} if isinstance(data, dict) and "whales" in data else cleaned_whales
        
        with open(WALLETS_FILE, "w") as f:
            json.dump(new_data, f, indent=4)
            
        print(f"✅ Очистка завершена! Сохранено {len(cleaned_whales)} элитных китов из списка.")
        print(f"📁 Старый список сохранен в {BACKUP_FILE}")
        
    except Exception as e:
        print(f"❌ Ошибка при очистке: {e}")

if __name__ == "__main__":
    clean_wallets()
