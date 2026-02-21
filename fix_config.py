import re

CONFIG_FILE = "bots/bot-whale-copy.yaml"

try:
    with open(CONFIG_FILE, 'r') as f:
        content = f.read()

    # Исправляем TSL на формат коэффициентов (0.15 вместо 15.0)
    content = re.sub(r'(tsl_activation_pct:\s*)[0-9.]+', r'\g<1>0.15', content)
    content = re.sub(r'(tsl_trail_pct:\s*)[0-9.]+', r'\g<1>0.30', content)

    with open(CONFIG_FILE, 'w') as f:
        f.write(content)

    print("✅ Косяк исправлен! Значения TSL заменены на 0.15 и 0.30.")

except Exception as e:
    print(f"❌ Ошибка: {e}")
