#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Identify all running live_trader instances and their status
"""

import subprocess
import sys
from pathlib import Path
from datetime import datetime

print("=" * 70)
print("IDENTIFYING ALL LIVE TRADER INSTANCES")
print("=" * 70)

if sys.platform == 'win32':
    try:
        # Get all Python processes with their command lines
        result = subprocess.run(
            ['wmic', 'process', 'where', "name='python.exe'", 'get', 'ProcessId,CommandLine', '/format:list'],
            capture_output=True,
            text=True
        )
        
        processes = []
        current_process = {}
        
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.startswith('ProcessId='):
                if current_process:
                    processes.append(current_process)
                current_process = {'pid': line.split('=')[1]}
            elif line.startswith('CommandLine='):
                current_process['cmd'] = line.split('=', 1)[1] if '=' in line else ''
        
        if current_process:
            processes.append(current_process)
        
        # Filter for live_trader processes
        live_traders = []
        for proc in processes:
            if proc.get('cmd') and 'live_trader.py' in proc['cmd']:
                live_traders.append(proc)
        
        print(f"\nFound {len(live_traders)} live_trader.py instance(s):\n")
        
        for i, trader in enumerate(live_traders, 1):
            pid = trader.get('pid', 'N/A')
            cmd = trader.get('cmd', 'N/A')
            print(f"[{i}] PID: {pid}")
            print(f"    Command: {cmd}")
            print()
        
        if len(live_traders) > 1:
            print("=" * 70)
            print("WARNING: MULTIPLE INSTANCES DETECTED!")
            print("=" * 70)
            print(f"\nYou have {len(live_traders)} live trader instances running.")
            print("This can cause:")
            print("  - Duplicate trades")
            print("  - Conflicting orders")
            print("  - Account issues")
            print("  - Multiple positions")
            print("\nRECOMMENDATION:")
            print("1. Stop ALL instances")
            print("2. Start only ONE instance")
            print("\nTo stop all Python processes:")
            print("   taskkill /F /IM python.exe")
            print("\nOr stop specific PIDs:")
            for trader in live_traders:
                print(f"   taskkill /F /PID {trader.get('pid')}")
        else:
            print("=" * 70)
            print("OK: Only one live trader instance detected")
            print("=" * 70)
        
        # Check log file for recent activity
        print("\n" + "=" * 70)
        print("RECENT LOG ACTIVITY")
        print("=" * 70)
        
        log_file = Path('live_trading.log')
        if log_file.exists():
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                if lines:
                    print(f"\nLast 5 log entries:")
                    for line in lines[-5:]:
                        print(f"   {line.strip()}")
                    
                    # Check for recent "LIVE TRADING STARTED" entries
                    recent_starts = []
                    for i, line in enumerate(lines[-100:], len(lines) - 100):
                        if 'LIVE TRADING STARTED' in line:
                            recent_starts.append((i, line.strip()))
                    
                    if recent_starts:
                        print(f"\nRecent 'LIVE TRADING STARTED' entries (last 100 lines):")
                        for line_num, line in recent_starts[-5:]:  # Show last 5
                            print(f"   Line {line_num}: {line}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

print("\n" + "=" * 70)





