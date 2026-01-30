#!/bin/bash
cd /opt/pumpfun-bonkfun-bot
echo "HELIUS WEBHOOK DIAGNOSTICS"
echo "Time: $(date)"
echo ""
echo "1. Stats:"
curl -s http://localhost:8000/stats 2>/dev/null | python3 -m json.tool || echo "Bot not responding"
echo ""
echo "2. Recent events:"
grep -E "EMIT|SWAP.*Detected" logs/bot-whale-copy.log 2>/dev/null | tail -10
