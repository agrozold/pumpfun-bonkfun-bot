# Helius Webhooks - Важные заметки

## Проблема: GET /webhooks не показывает адреса

При запросе GET https://api.helius.xyz/v0/webhooks?api-key=XXX поле accountAddresses отсутствует или пустое.

## Это НЕ баг!

Helius оптимизирует ответ API и не включает массив адресов (может быть до 100,000). Webhook при этом работает корректно.

## Как проверить что webhook работает

Статистика бота: curl -s http://localhost:8000/stats | python3 -m json.tool
Если webhooks_received > 0 и buys_emitted > 0 - всё работает.

Логи: grep -E "EMIT|SWAP|WEBHOOK" logs/bot-whale-copy.log | tail -20

## FAQ

Q: Почему SKIP fee_payer not in whale list?
A: При Jito bundles fee_payer может быть bundler, а не кит. Это нормально.

Q: Сколько адресов можно отслеживать?
A: До 100,000 на один webhook.
