#!/usr/bin/env python3
"""
Валидация конфигураций ботов.
Проверяет соответствие platform <-> listener_type

Запуск: python validate_bot_configs.py
"""
import os
import sys
import re
from pathlib import Path

try:
    import yaml
    from dotenv import load_dotenv
except ImportError:
    print("Установите зависимости: pip install pyyaml python-dotenv")
    sys.exit(1)

load_dotenv()

# Актуальная матрица совместимости (Jan 2026)
# PumpPortal поддерживает pump.fun И bonk.fun!
PLATFORM_LISTENER_COMPATIBILITY = {
    "pump_fun": ["pumpportal", "logs", "blocks", "geyser", "fallback"],
    "lets_bonk": ["pumpportal", "bonk_logs", "logs", "blocks", "geyser", "fallback"],
    "bags": ["bags_logs", "logs", "blocks", "geyser", "fallback"],
}

# Оптимальные listener для каждой платформы
OPTIMAL_LISTENERS = {
    "pump_fun": "pumpportal",
    "lets_bonk": "pumpportal",  # PumpPortal теперь поддерживает bonk.fun!
    "bags": "bags_logs",        # bags.fm НЕ поддерживается PumpPortal
}

# PumpPortal НЕ поддерживает эти платформы
PUMPPORTAL_UNSUPPORTED = ["bags"]


def validate_config(config_path: Path) -> tuple[list[str], list[str]]:
    """Валидирует конфиг. Возвращает (errors, warnings)."""
    errors = []
    warnings = []
    
    with open(config_path) as f:
        raw = f.read()
    
    # Проверка переменных окружения
    unresolved = re.findall(r'\$\{([^}]+)\}', raw)
    for var in unresolved:
        if not os.environ.get(var):
            errors.append(f"${{{var}}} не установлена в .env")
    
    config = yaml.safe_load(raw)
    if not config:
        return ([f"Невалидный YAML"], [])
    
    platform = config.get("platform")
    listener = config.get("filters", {}).get("listener_type")
    
    if not platform:
        errors.append("Не указан platform")
        return (errors, warnings)
    
    if not listener:
        errors.append("Не указан filters.listener_type")
        return (errors, warnings)
    
    # Проверка совместимости
    valid = PLATFORM_LISTENER_COMPATIBILITY.get(platform, [])
    if listener not in valid:
        errors.append(f"listener_type='{listener}' недопустим для {platform}. Допустимые: {valid}")
    
    # Критическая ошибка: pumpportal для bags
    if listener == "pumpportal" and platform in PUMPPORTAL_UNSUPPORTED:
        errors.append(f"КРИТИЧНО: pumpportal НЕ поддерживает {platform}!")
    
    # Проверка оптимальности
    optimal = OPTIMAL_LISTENERS.get(platform)
    if optimal and listener != optimal:
        if listener == "fallback":
            warnings.append(f"fallback работает, но '{optimal}' быстрее для {platform}")
        elif listener not in ["pumpportal", optimal]:
            warnings.append(f"'{listener}' работает, но '{optimal}' оптимальнее для {platform}")
    
    return (errors, warnings)


def main():
    print("=" * 60)
    print("ВАЛИДАЦИЯ КОНФИГУРАЦИЙ БОТОВ")
    print("=" * 60)
    
    all_errors = []
    all_warnings = []
    
    configs = list(Path("bots").glob("*.yaml")) + list(Path("bots").glob("*.yml"))
    
    for path in sorted(configs):
        print(f"\n📄 {path}")
        errors, warnings = validate_config(path)
        
        for e in errors:
            print(f"   ❌ {e}")
            all_errors.append(f"{path}: {e}")
        
        for w in warnings:
            print(f"   ⚠️  {w}")
            all_warnings.append(f"{path}: {w}")
        
        if not errors and not warnings:
            print("   ✅ OK")
    
    print("\n" + "=" * 60)
    print(f"ИТОГО: {len(all_errors)} ошибок, {len(all_warnings)} предупреждений")
    print("=" * 60)
    
    sys.exit(1 if all_errors else 0)


if __name__ == "__main__":
    main()
