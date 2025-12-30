#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Check current position status via API
"""

import json
from broker import TopstepXClient
from pathlib import Path

print("=" * 70)
print("CHECKING POSITION STATUS")
print("=" * 70)

# Load credentials
credentials_path = Path('credentials.json')
if not credentials_path.exists():
    print("[ERROR] credentials.json not found")
    exit(1)

with open(credentials_path, 'r') as f:
    credentials = json.load(f)

# Connect to API
client = TopstepXClient(
    username=credentials['username'],
    api_key=credentials['api_key'],
    base_url=credentials.get('base_url'),
    rtc_url=credentials.get('rtc_url')
)

print("\n[1] Authenticating...")
try:
    # Get positions
    positions = client.get_positions()
    
    print(f"\n[2] Found {len(positions)} position(s):")
    
    mgc_positions = []
    for pos in positions:
        if 'MGC' in str(pos.contract_id) or 'MGC' in str(pos):
            mgc_positions.append(pos)
            print(f"\n  Contract: {pos.contract_id}")
            print(f"  Size: {pos.size}")
            print(f"  Side: {'LONG' if pos.size > 0 else 'SHORT' if pos.size < 0 else 'FLAT'}")
            if hasattr(pos, 'entry_price'):
                print(f"  Entry Price: ${pos.entry_price:.2f}")
            if hasattr(pos, 'unrealized_pnl'):
                print(f"  Unrealized P&L: ${pos.unrealized_pnl:.2f}")
    
    if not mgc_positions:
        print("\n  [INFO] No MGC positions found")
        print("  All positions are flat")
    
    # Check open orders
    print("\n[3] Checking open orders...")
    orders = client.get_open_orders()
    mgc_orders = [o for o in orders if 'MGC' in str(o.get('contractId', ''))]
    
    if mgc_orders:
        print(f"  Found {len(mgc_orders)} MGC order(s):")
        for order in mgc_orders:
            print(f"    Order ID: {order.get('id')}")
            print(f"    Type: {order.get('type')}")
            print(f"    Side: {order.get('side')}")
            print(f"    Quantity: {order.get('quantity')}")
            print(f"    Price: ${order.get('price', 0):.2f}")
    else:
        print("  [INFO] No open MGC orders")
    
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    if mgc_positions:
        print(f"[ACTIVE] {len(mgc_positions)} MGC position(s) open")
        print("  The trader should be monitoring this position")
    else:
        print("[FLAT] No MGC positions - trader should be looking for signals")
    
    if mgc_orders:
        print(f"[PENDING] {len(mgc_orders)} order(s) pending")
    else:
        print("[NO ORDERS] No pending orders")
    
except Exception as e:
    print(f"\n[ERROR] Failed to check positions: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)

