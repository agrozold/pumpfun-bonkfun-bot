#!/usr/bin/env python3
"""Manage deployer blacklist: add/del/list"""
import sys, json, os
from datetime import date

BLACKLIST_FILE = "/opt/pumpfun-bonkfun-bot/blacklisted_deployers.json"

def load():
    if not os.path.exists(BLACKLIST_FILE):
        return {"deployers": []}
    with open(BLACKLIST_FILE) as f:
        return json.load(f)

def save(data):
    with open(BLACKLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)

def cmd_add(wallet, label=None):
    data = load()
    for d in data["deployers"]:
        if d["wallet"] == wallet:
            print(f"⚠️  Already in blacklist: {d.get('label','')} ({wallet[:20]}...)")
            return
    if not label:
        label = f"scammer-{len(data['deployers'])+1}"
    data["deployers"].append({"wallet": wallet, "label": label, "added": str(date.today())})
    save(data)
    print(f"⛔ Blacklisted: {label} ({wallet[:20]}...)")
    print(f"📊 Total deployers: {len(data['deployers'])}")
    print(f"⏳ Bot will pick up changes within 5 min (next refresh)")

def cmd_del(query):
    data = load()
    removed = None
    new = []
    for d in data["deployers"]:
        if d["wallet"] == query or d.get("label","").lower() == query.lower():
            removed = d
        else:
            new.append(d)
    if not removed:
        print(f"❌ Not found: {query}")
        return
    data["deployers"] = new
    save(data)
    print(f"✅ Removed: {removed.get('label','')} ({removed['wallet'][:20]}...)")
    print(f"📊 Remaining: {len(new)}")

def cmd_list():
    data = load()
    deployers = data.get("deployers", [])
    print(f"⛔ Blacklisted deployers: {len(deployers)}")
    for d in deployers:
        print(f"  {d.get('label','?'):<25} {d['wallet'][:35]}... ({d.get('added','')})")

if len(sys.argv) < 2:
    print("Usage:")
    print("  blacklist add <WALLET> [label]  — block deployer")
    print("  blacklist del <WALLET|LABEL>     — unblock")
    print("  blacklist list                   — show all")
    sys.exit(0)

cmd = sys.argv[1].lower()
if cmd == "add":
    if len(sys.argv) < 3:
        print("❌ Usage: blacklist add <WALLET> [label]")
        sys.exit(1)
    cmd_add(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
elif cmd in ("del", "rm", "remove"):
    if len(sys.argv) < 3:
        print("❌ Usage: blacklist del <WALLET|LABEL>")
        sys.exit(1)
    cmd_del(sys.argv[2])
elif cmd in ("list", "ls"):
    cmd_list()
else:
    print(f"❌ Unknown command: {cmd}")
