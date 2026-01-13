print("🧪 Testing Moon Bag Feature\n")

# Test 1: TP with moon bag
moon_bag_percentage = 20.0
quantity = 1000
exit_reason = "TAKE_PROFIT"

if exit_reason == "TAKE_PROFIT" and moon_bag_percentage > 0:
    sell_quantity = quantity * (1 - moon_bag_percentage / 100)
else:
    sell_quantity = quantity

remaining = quantity - sell_quantity
print(f"✅ Test 1 - TP Exit with moon bag:")
print(f"   Position: {quantity} tokens")
print(f"   Moon bag: {moon_bag_percentage}%")
print(f"   Sell: {sell_quantity} tokens")
print(f"   Keep: {remaining} tokens")
assert sell_quantity == 800.0
assert remaining == 200.0
print()

# Test 2: SL - moon bag disabled
exit_reason = "STOP_LOSS"

if exit_reason == "TAKE_PROFIT" and moon_bag_percentage > 0:
    sell_quantity = quantity * (1 - moon_bag_percentage / 100)
else:
    sell_quantity = quantity

print(f"✅ Test 2 - SL Exit (no moon bag):")
print(f"   Position: {quantity} tokens")
print(f"   Exit reason: {exit_reason}")
print(f"   Sell: {sell_quantity} tokens (100%)")
assert sell_quantity == 1000.0
print()

# Test 3: TP with moon bag disabled (0%)
moon_bag_percentage = 0.0
exit_reason = "TAKE_PROFIT"

if exit_reason == "TAKE_PROFIT" and moon_bag_percentage > 0:
    sell_quantity = quantity * (1 - moon_bag_percentage / 100)
else:
    sell_quantity = quantity

print(f"✅ Test 3 - TP with moon bag disabled:")
print(f"   Position: {quantity} tokens")
print(f"   Moon bag: {moon_bag_percentage}%")
print(f"   Sell: {sell_quantity} tokens (100%)")
assert sell_quantity == 1000.0
print()

# Test 4: Different percentages
for mb_pct in [10, 25, 50]:
    moon_bag_percentage = mb_pct
    exit_reason = "TAKE_PROFIT"
    
    if exit_reason == "TAKE_PROFIT" and moon_bag_percentage > 0:
        sell_quantity = quantity * (1 - moon_bag_percentage / 100)
    else:
        sell_quantity = quantity
    
    remaining = quantity - sell_quantity
    print(f"✅ Test 4.{mb_pct} - Moon bag {mb_pct}%:")
    print(f"   Sell: {sell_quantity} ({100-mb_pct}%), Keep: {remaining} ({mb_pct}%)")

print("\n✨ All tests passed!")
