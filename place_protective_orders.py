#!/usr/bin/env python3
"""Place stop loss and take profit orders for existing position"""
import json
from broker import TopstepXClient, OrderSide, OrderType

# Load credentials
with open('credentials.json', 'r') as f:
    creds = json.load(f)

client = TopstepXClient(
    username=creds['username'],
    api_key=creds['api_key'],
    base_url=creds.get('base_url'),
    rtc_url=creds.get('rtc_url')
)

print("Authenticating...")
if not client.authenticate():
    print("FAILED: Authentication failed")
    exit(1)

client.set_account(creds.get('account_id'))

# Find MGC contract
contract = client.get_available_contracts()[0]  # Get first contract for now
for c in client.get_available_contracts():
    if 'MGC' in c.id:
        contract = c
        break

print(f"Contract: {contract.id}\n")

# Get current position
positions = client.get_positions()
mgc_position = None
for pos in positions:
    if pos.contract_id == contract.id:
        mgc_position = pos
        break

if not mgc_position:
    print("No position found")
    exit(1)

# Get current price
bars = client.get_historical_bars(
    contract_id=contract.id,
    interval=3,
    count=1,
    live=False,
    unit=2
)
current_price = bars[0]['c']

side_str = "LONG" if mgc_position.position_type.value == 1 else "SHORT"
print("=" * 60)
print(f"CURRENT POSITION: {side_str} {mgc_position.size} @ ${mgc_position.average_price:.2f}")
print(f"CURRENT PRICE: ${current_price:.2f}")
print("=" * 60)

# Calculate SL/TP based on entry price
entry = mgc_position.average_price
tick_size = 0.10  # MGC tick size

# For SHORT: SL above entry, TP below entry
# For LONG: SL below entry, TP above entry

if mgc_position.position_type.value == 2:  # SHORT
    # Stop Loss: Must be ABOVE current price (to limit losses if price goes up)
    # Use 20 ticks above current price, or entry + 30 ticks (whichever is higher)
    sl_above_current = current_price + (20 * tick_size)
    sl_above_entry = entry + (30 * tick_size)
    sl_price = max(sl_above_current, sl_above_entry)
    # Take Profit: BELOW entry (to take profit if price goes down)
    tp_price = entry - (40 * tick_size)
    sl_side = OrderSide.BID  # Buy to close short
    tp_side = OrderSide.BID  # Buy to close short
else:  # LONG
    # Stop Loss: Must be BELOW current price (to limit losses if price goes down)
    sl_below_current = current_price - (20 * tick_size)
    sl_below_entry = entry - (30 * tick_size)
    sl_price = min(sl_below_current, sl_below_entry)
    # Take Profit: ABOVE entry (to take profit if price goes up)
    tp_price = entry + (40 * tick_size)
    sl_side = OrderSide.ASK  # Sell to close long
    tp_side = OrderSide.ASK  # Sell to close long

print(f"\nProposed Orders:")
print(f"  Stop Loss: {sl_side.name} {mgc_position.size} @ ${sl_price:.2f}")
print(f"  Take Profit: {tp_side.name} {mgc_position.size} @ ${tp_price:.2f}")

# Place orders (user requested via command)
print("\n" + "=" * 60)
print("Placing orders now...")
print("=" * 60)

# Place Stop Loss order
print("\nPlacing Stop Loss order...")
sl_result = client.place_stop_order(
    contract_id=contract.id,
    side=sl_side,
    size=mgc_position.size,
    stop_price=sl_price
)

if sl_result.get('success'):
    print(f"OK Stop Loss placed: Order ID {sl_result.get('orderId')}")
else:
    print(f"FAILED: {sl_result.get('errorMessage')}")

# Place Take Profit order
print("\nPlacing Take Profit order...")
tp_result = client.place_limit_order(
    contract_id=contract.id,
    side=tp_side,
    size=mgc_position.size,
    limit_price=tp_price
)

if tp_result.get('success'):
    print(f"OK Take Profit placed: Order ID {tp_result.get('orderId')}")
else:
    print(f"FAILED: {tp_result.get('errorMessage')}")

print("\n" + "=" * 60)
if sl_result.get('success') and tp_result.get('success'):
    print("SUCCESS: Both orders placed!")
else:
    print("WARNING: Some orders may have failed. Check above.")
print("=" * 60)

