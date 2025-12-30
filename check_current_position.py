#!/usr/bin/env python3
"""Check current position and orders"""
import json
from broker import TopstepXClient

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

print("OK Authenticated")

# Set account ID
print(f"\nSetting account: {creds.get('account_id', 'NOT SET')}")
client.set_account(creds.get('account_id'))
print("OK Account set\n")

# Find MGC contract
print("Finding MGC contract...")
contract = client.find_mgc_contract()
if not contract:
    print("FAILED: MGC contract not found")
    exit(1)

print(f"OK Found: {contract.id} - {contract.description}\n")

# Get current position
print("=" * 60)
print("CURRENT POSITION")
print("=" * 60)
positions = client.get_positions()
mgc_position = None
for pos in positions:
    if pos.contract_id == contract.id:
        mgc_position = pos
        break

if mgc_position:
    side_str = "LONG" if mgc_position.position_type.value == 1 else "SHORT"
    print(f"Side: {side_str}")
    print(f"Quantity: {mgc_position.size}")
    print(f"Entry Price: ${mgc_position.average_price:.2f}")
    print(f"Contract: {mgc_position.contract_id}")
    
    # Get current price from market data
    try:
        bars = client.get_historical_bars(
            contract_id=contract.id,
            interval=3,
            count=1,
            live=False,
            unit=2
        )
        current_price = bars[0]['c'] if bars else mgc_position.average_price
        print(f"Current Price: ${current_price:.2f}")
        
        # Calculate P&L
        if mgc_position.position_type.value == 1:  # LONG
            pnl = (current_price - mgc_position.average_price) * mgc_position.size * 10  # 10 = tick value for MGC
        else:  # SHORT
            pnl = (mgc_position.average_price - current_price) * mgc_position.size * 10
        print(f"Estimated P&L: ${pnl:.2f}")
    except:
        print("Could not fetch current price")
else:
    print("No position found")

# Get open orders
print("\n" + "=" * 60)
print("OPEN ORDERS")
print("=" * 60)
orders = client.get_open_orders()
mgc_orders = [o for o in orders if o.get('contractId') == contract.id]

if mgc_orders:
    print(f"Found {len(mgc_orders)} open order(s) for MGC:")
    for order in mgc_orders:
        order_price = order.get('price') or order.get('stopPrice') or order.get('limitPrice') or 0.0
        print(f"\n  Order ID: {order.get('id')}")
        print(f"  Type: {order.get('type')} ({'LIMIT' if order.get('type') == 1 else 'STOP' if order.get('type') == 4 else 'MARKET'})")
        print(f"  Side: {order.get('side')} ({'BID' if order.get('side') == 0 else 'ASK'})")
        print(f"  Size: {order.get('size')}")
        print(f"  Price: ${order_price:.2f}")
        print(f"  Status: {order.get('status')}")
else:
    print("WARNING: No open orders found for MGC!")
    print("This means there are NO stop loss or take profit orders active.")

print("\n" + "=" * 60)
if mgc_position and not mgc_orders:
    print("CRITICAL: Position is open but NO protective orders!")
    print("You need to place stop loss and take profit orders immediately.")
print("=" * 60)

