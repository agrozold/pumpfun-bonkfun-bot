#!/usr/bin/env python3
"""Управление списком токенов без SL"""
import sys
import re

FILE = "/opt/pumpfun-bonkfun-bot/src/trading/universal_trader.py"

def get_no_sl_mints():
    with open(FILE) as f:
        content = f.read()
    match = re.search(r'NO_SL_MINTS = \{([^}]*)\}', content, re.DOTALL)
    if match:
        mints = re.findall(r'"([^"]+)"', match.group(1))
        return mints
    return []

def add_mint(mint):
    mints = get_no_sl_mints()
    if mint in mints:
        print(f"⚠️ {mint[:12]}... уже в списке")
        return False
    
    with open(FILE) as f:
        content = f.read()
    
    # Добавляем новый mint
    old_block = 'NO_SL_MINTS = {'
    new_mint_line = f'NO_SL_MINTS = {{\n    "{mint}",'
    content = content.replace(old_block, new_mint_line)
    
    with open(FILE, 'w') as f:
        f.write(content)
    
    print(f"✅ Добавлен: {mint[:20]}...")
    return True

def remove_mint(mint):
    with open(FILE) as f:
        content = f.read()
    
    # Удаляем mint
    pattern = rf'\s*"{mint}",?\n?'
    new_content = re.sub(pattern, '', content)
    
    if new_content == content:
        print(f"⚠️ {mint[:12]}... не найден")
        return False
    
    with open(FILE, 'w') as f:
        f.write(new_content)
    
    print(f"✅ Удалён: {mint[:20]}...")
    return True

def list_mints():
    mints = get_no_sl_mints()
    print(f"=== NO_SL токены ({len(mints)}) ===")
    for m in mints:
        print(f"  • {m}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование:")
        print("  no-sl list          - показать список")
        print("  no-sl add <MINT>    - добавить токен")
        print("  no-sl remove <MINT> - удалить токен")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "list":
        list_mints()
    elif cmd == "add" and len(sys.argv) > 2:
        if add_mint(sys.argv[2]):
            print("💡 Перезапусти бота: bot-restart")
    elif cmd == "remove" and len(sys.argv) > 2:
        if remove_mint(sys.argv[2]):
            print("💡 Перезапусти бота: bot-restart")
    else:
        print("❌ Неверная команда")
