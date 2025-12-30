#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Manage live trader - check status and provide options
"""

import subprocess
import sys
import os
from pathlib import Path

print("=" * 70)
print("LIVE TRADER STATUS")
print("=" * 70)

# Check log for most recent activity
log_file = Path('live_trading.log')
if log_file.exists():
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
        if lines:
            last_line = lines[-1].strip()
            print(f"\nMost recent log entry:")
            print(f"   {last_line}")
            
            # Check if actively running
            if 'Signal check' in last_line or 'Data refresh' in last_line or 'SIGNAL GENERATED' in last_line:
                print("\n   [ACTIVE] Live trader appears to be running and checking signals")
            elif 'LIVE TRADING STARTED' in last_line:
                print("\n   [STARTING] Live trader just started")
            else:
                print("\n   [IDLE] May be waiting for market data")

print("\n" + "=" * 70)
print("OPTIONS")
print("=" * 70)
print("\n1. Keep current instance running (recommended if it's working)")
print("   - Monitor with: tail -f live_trading.log")
print("   - Or check periodically: python check_running_instances.py")
print("\n2. Stop all Python processes (WARNING: stops ALL Python scripts)")
print("   Windows: taskkill /F /IM python.exe")
print("   Then restart: python live_trader.py --config config_production.json")
print("\n3. Use Task Manager to identify and stop only live_trader processes")
print("   - Open Task Manager (Ctrl+Shift+Esc)")
print("   - Find python.exe processes")
print("   - Check Command Line column to see which is live_trader.py")
print("   - End only those processes")
print("\n" + "=" * 70)
print("\nRECOMMENDATION:")
print("If the system is working (checking signals every 60 seconds),")
print("keep it running. Multiple 'LIVE TRADING STARTED' entries in logs")
print("just indicate restarts, not necessarily concurrent instances.")
print("=" * 70)

