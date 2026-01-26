#!/usr/bin/env python3
"""Анализ whale copy trades из логов"""

import re
import glob
from datetime import datetime
from collections import defaultdict

def parse_whale_logs():
    logs = sorted(glob.glob("logs/bot-whale-copy*.log"))
    
    trades = []
    current_trade = {}
    
    for log_file in logs:
        with open(log_file, 'r') as f:
            for line in f:
                # Парсим сигнал покупки
                match = re.search(
                    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Buy qualifies: ([\d.]+) SOL.*token: (\w+).*platform: (\w+)',
                    line
                )
                if match:
                    current_trade = {
                        'time': match.group(1),
                        'whale_amount': float(match.group(2)),
                        'token': match.group(3),
                        'platform': match.group(4),
                        'status': 'pending'
                    }
                
                # Парсим результат SUCCESS
                if 'WHALE COPY] SUCCESS' in line and current_trade:
                    match_time = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                    current_trade['status'] = 'SUCCESS'
                    current_trade['end_time'] = match_time.group(1) if match_time else ''
                    trades.append(current_trade.copy())
                    current_trade = {}
                
                # Парсим результат FAILED
                if 'WHALE COPY] FAILED' in line and current_trade:
                    match_time = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                    reason = re.search(r'FAILED - (.+)', line)
                    current_trade['status'] = 'FAILED'
                    current_trade['reason'] = reason.group(1) if reason else 'unknown'
                    current_trade['end_time'] = match_time.group(1) if match_time else ''
                    trades.append(current_trade.copy())
                    current_trade = {}
    
    return trades

def analyze_trades(trades):
    print("=" * 70)
    print("WHALE COPY TRADES ANALYSIS")
    print("=" * 70)
    
    # Общая статистика
    total = len(trades)
    success = sum(1 for t in trades if t['status'] == 'SUCCESS')
    failed = sum(1 for t in trades if t['status'] == 'FAILED')
    
    print(f"\n📊 ОБЩАЯ СТАТИСТИКА:")
    print(f"   Всего сигналов:    {total}")
    print(f"   ✅ Успешных:        {success} ({100*success/total:.0f}%)" if total > 0 else "")
    print(f"   ❌ Неудачных:       {failed} ({100*failed/total:.0f}%)" if total > 0 else "")
    
    # По платформам
    by_platform = defaultdict(lambda: {'total': 0, 'success': 0, 'failed': 0})
    for t in trades:
        p = t['platform']
        by_platform[p]['total'] += 1
        if t['status'] == 'SUCCESS':
            by_platform[p]['success'] += 1
        else:
            by_platform[p]['failed'] += 1
    
    print(f"\n📈 ПО ПЛАТФОРМАМ:")
    for platform, stats in sorted(by_platform.items()):
        success_rate = 100 * stats['success'] / stats['total'] if stats['total'] > 0 else 0
        print(f"   {platform:12} | {stats['total']:2} сигналов | ✅ {stats['success']} | ❌ {stats['failed']} | {success_rate:.0f}% success")
    
    # Детали каждой сделки
    print(f"\n📋 ДЕТАЛИ СДЕЛОК:")
    print("-" * 70)
    
    for i, t in enumerate(trades, 1):
        status_icon = "✅" if t['status'] == 'SUCCESS' else "❌"
        token_short = t['token'][:8] + "..." + t['token'][-4:]
        
        print(f"\n{i}. {status_icon} {t['time']}")
        print(f"   Token:    {token_short}")
        print(f"   Platform: {t['platform']}")
        print(f"   Whale:    {t['whale_amount']:.4f} SOL")
        if t['status'] == 'FAILED':
            print(f"   Reason:   {t.get('reason', 'unknown')}")
        
        # Ссылки
        print(f"   DexScreener: https://dexscreener.com/solana/{t['token']}")
        print(f"   Solscan:     https://solscan.io/token/{t['token']}")
    
    # Причины неудач
    if failed > 0:
        print(f"\n❌ ПРИЧИНЫ НЕУДАЧ:")
        reasons = defaultdict(int)
        for t in trades:
            if t['status'] == 'FAILED':
                reasons[t.get('reason', 'unknown')] += 1
        
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"   {count}x {reason}")
    
    print("\n" + "=" * 70)

if __name__ == "__main__":
    trades = parse_whale_logs()
    analyze_trades(trades)
