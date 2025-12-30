#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run a test trade to verify functionality
"""

import sys
import time
from live_trader import LiveTrader
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

print("=" * 70)
print("TEST TRADE EXECUTION")
print("=" * 70)

try:
    # Initialize trader
    print("\n[1] Initializing trader...")
    trader = LiveTrader(
        config_path='config_production.json',
        credentials_path='credentials.json'
    )
    
    # Connect
    print("[2] Connecting to API...")
    if not trader.connect():
        print("[ERROR] Failed to connect to API")
        sys.exit(1)
    
    print("[OK] Connected successfully")
    
    # Check current position
    print("\n[3] Checking for existing positions...")
    status = trader.get_status()
    if status.get('current_position'):
        print(f"[WARNING] Position already exists: {status['current_position']}")
        print("Cannot place test trade - position already open")
        sys.exit(1)
    
    if status.get('positions'):
        print(f"[WARNING] Found {len(status['positions'])} position(s) on broker")
        for pos in status['positions']:
            print(f"  - {pos}")
        print("Cannot place test trade - position already open")
        sys.exit(1)
    
    print("[OK] No existing positions")
    
    # Get current price
    print("\n[4] Getting current market price...")
    current_price = trader._get_current_price()
    if current_price is None:
        print("[ERROR] Cannot get current price")
        sys.exit(1)
    
    print(f"[OK] Current price: ${current_price:.2f}")
    
    # Place test trade
    print("\n[5] Placing test trade (LONG, 1 contract)...")
    print("=" * 70)
    
    success = trader.test_trade(side='long', entry_price=None, quantity=1)
    
    if not success:
        print("\n[ERROR] Failed to place test trade")
        sys.exit(1)
    
    print("\n[OK] Test trade placed successfully!")
    
    # Monitor for a short time
    print("\n[6] Monitoring position for 30 seconds...")
    print("=" * 70)
    
    for i in range(6):  # 6 iterations, 5 seconds each = 30 seconds
        time.sleep(5)
        if trader.current_position:
            current_price = trader._get_current_price()
            if current_price:
                entry = trader.current_position.get('entry_price', 0)
                side = trader.current_position.get('side', 'unknown')
                qty = trader.current_position.get('quantity', 0)
                
                if side == 'long':
                    pnl = (current_price - entry) * qty
                    pnl_ticks = (current_price - entry) / trader.tick_size
                else:
                    pnl = (entry - current_price) * qty
                    pnl_ticks = (entry - current_price) / trader.tick_size
                
                print(f"[{i+1}/6] Position: {side.upper()} {qty} @ ${entry:.2f}, Current: ${current_price:.2f}, P&L: ${pnl:.2f} ({pnl_ticks:.1f} ticks)")
        else:
            print(f"[{i+1}/6] Position closed or not found")
            break
    
    print("\n" + "=" * 70)
    print("TEST COMPLETE")
    print("=" * 70)
    
    # Final status
    final_status = trader.get_status()
    if final_status.get('current_position'):
        print("\n[FINAL STATUS] Position still open:")
        pos = final_status['current_position']
        print(f"  Side: {pos.get('side', 'unknown')}")
        print(f"  Quantity: {pos.get('quantity', 0)}")
        print(f"  Entry: ${pos.get('entry_price', 0):.2f}")
        print(f"  Stop Loss: ${pos.get('stop_loss', 0):.2f}")
        print(f"  Take Profit: ${pos.get('take_profit', 0):.2f}")
        print("\n[SUCCESS] Test trade is active and being monitored!")
    else:
        print("\n[INFO] Position closed or not found")
    
    print("\n" + "=" * 70)
    
except KeyboardInterrupt:
    print("\n\n[INTERRUPTED] Test stopped by user")
except Exception as e:
    print(f"\n[ERROR] {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

