#!/bin/bash
echo "=== SpyDefi Status ==="
grep -i "spydefi.*Started\|spydefi.*Fatal\|spydefi.*error\|telethon.*Disconnect" /opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log | tail -3
echo ""
echo "=== Seen contracts ==="
python3 << 'PY'
import json
d = json.load(open("/opt/pumpfun-bonkfun-bot/data/spydefi_seen.json"))
contracts = d.get("contracts", [])
updated = d.get("updated", "?")
print(f"Total: {len(contracts)} | Updated: {updated}")
for c in contracts[-5:]:
    print(f"  {c}")
PY
echo ""
echo "=== Last signals ==="
grep -i "spydefi.*Achievement\|spydefi.*SIGNAL" /opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log | tail -10
echo ""
echo "=== Skips/Errors ==="
grep -i "spydefi.*Skip\|spydefi.*Dedup\|spydefi.*No contract\|spydefi.*already" /opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log | tail -5
