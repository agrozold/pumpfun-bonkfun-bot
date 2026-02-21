import re
import os

# Проверяем, какой файл использовать
CONFIG_FILE = "bots/bot-whale-copy.yaml"
if not os.path.exists(CONFIG_FILE):
    CONFIG_FILE = "bots/bot-whale-copy.example.yaml"
    print(f"⚠️ Основной конфиг не найден, редактируем {CONFIG_FILE}")

try:
    with open(CONFIG_FILE, 'r') as f:
        content = f.read()

    # Словарь замен (паттерн: новое значение)
    replacements = {
        r'(trade_size_sol:\s*)[0-9.]+': r'\g<1>0.25',
        r'(max_positions:\s*)[0-9]+': r'\g<1>5',
        r'(take_profit_pct:\s*)[0-9.]+': r'\g<1>15.0',
        r'(stop_loss_pct:\s*)[0-9.]+': r'\g<1>15.0',
        r'(tsl_activation_pct:\s*)[0-9.]+': r'\g<1>15.0',
        r'(tsl_trail_pct:\s*)[0-9.]+': r'\g<1>5.0',
        r'(jito_tip_sol:\s*)[0-9.]+': r'\g<1>0.001',
    }

    for pattern, repl in replacements.items():
        content = re.sub(pattern, repl, content)

    with open(CONFIG_FILE, 'w') as f:
        f.write(content)

    print(f"✅ Конфигурация {CONFIG_FILE} успешно обновлена!")
    print("🔹 Установлено: trade_size_sol=0.25, TP=15%, SL=15%, TSL=15/5, Jito=0.001")

except Exception as e:
    print(f"❌ Ошибка при обновлении конфига: {e}")
