import re
import os

CONFIG_FILE = "bots/bot-whale-copy.yaml"

try:
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Файл {CONFIG_FILE} не найден.")
        exit(1)

    with open(CONFIG_FILE, 'r') as f:
        content = f.read()

    # Возвращаем твои изначальные значения из логов первой сессии
    replacements = {
        r'(trade_size_sol:\s*)[0-9.]+': r'\g<1>0.05',
        r'(take_profit_pct:\s*)[0-9.]+': r'\g<1>10.0',
        r'(stop_loss_pct:\s*)[0-9.]+': r'\g<1>20.0',
        r'(tsl_activation_pct:\s*)[0-9.]+': r'\g<1>15.0',
        r'(tsl_trail_pct:\s*)[0-9.]+': r'\g<1>30.0',
        r'(jito_tip_sol:\s*)[0-9.]+': r'\g<1>0.0015',
    }

    for pattern, repl in replacements.items():
        content = re.sub(pattern, repl, content)

    with open(CONFIG_FILE, 'w') as f:
        f.write(content)

    print("✅ Конфигурация успешно откатана!")
    print("🔹 Вернули: trade_size_sol=0.05, TP=10%, SL=20%, TSL=15/30, Jito=0.0015")

except Exception as e:
    print(f"❌ Ошибка при откате конфига: {e}")
