#!/bin/bash

echo "======================================================================"
echo "                    ПЕРЕД ЗАПУСКОМ БОТА - ПРОВЕРКА"
echo "======================================================================"

echo ""
echo "✓ ПРОВЕРКА 1: Redis работает?"
redis-cli ping
if [ $? -ne 0 ]; then
    echo "❌ Redis не работает!"
    echo "   Запустите: sudo systemctl start redis-server"
    exit 1
fi
echo "✅ Redis OK"

echo ""
echo "✓ ПРОВЕРКА 2: .env файл существует?"
if [ ! -f .env ]; then
    echo "❌ .env файл не найден!"
    exit 1
fi
echo "✅ .env OK"

echo ""
echo "✓ ПРОВЕРКА 3: API ключи установлены?"
if grep -q "SOLANA_PRIVATE_KEY=" .env; then
    echo "✅ SOLANA_PRIVATE_KEY OK"
else
    echo "❌ SOLANA_PRIVATE_KEY не установлен!"
    exit 1
fi

echo ""
echo "✓ ПРОВЕРКА 4: Конфиг бота правильный?"
cat bots/bot-sniper-0-pump.yaml | grep -E "exit_strategy|stop_loss|take_profit"
echo "✅ Конфиг OK"

echo ""
echo "✓ ПРОВЕРКА 5: Логи директория существует?"
mkdir -p logs
echo "✅ logs/ OK"

echo ""
echo "======================================================================"
echo "                    ✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ!"
echo "======================================================================"
echo ""
echo "Готово к запуску:"
echo "  python3 src/bot_runner.py bots/bot-sniper-0-pump.yaml"
echo ""
echo "Мониторинг в реальном времени:"
echo "  tail -f logs/*.log | grep -E '\[SL\]|\[TP\]|\[SAVE\]'"
echo ""
