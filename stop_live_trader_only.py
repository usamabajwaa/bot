#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop only live_trader processes (safely)
"""

import subprocess
import sys
import time

print("=" * 70)
print("STOP LIVE TRADER PROCESSES ONLY")
print("=" * 70)

if sys.platform == 'win32':
    try:
        # Find processes with live_trader in command line
        result = subprocess.run(
            ['wmic', 'process', 'where', 'name="python.exe"', 'get', 'ProcessId,CommandLine'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )
        
        lines = result.stdout.strip().split('\n')
        live_trader_pids = []
        
        for line in lines[1:]:  # Skip header
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 2:
                pid = parts[0]
                cmdline = ' '.join(parts[1:])
                if 'live_trader' in cmdline.lower():
                    live_trader_pids.append(pid)
        
        if live_trader_pids:
            print(f"\nFound {len(live_trader_pids)} live_trader process(es):")
            for pid in live_trader_pids:
                print(f"   PID: {pid}")
            
            print("\nStopping live_trader processes...")
            for pid in live_trader_pids:
                try:
                    result = subprocess.run(
                        ['taskkill', '/F', '/PID', pid],
                        capture_output=True,
                        text=True
                    )
                    if result.returncode == 0:
                        print(f"   ✓ Stopped PID {pid}")
                    else:
                        print(f"   ✗ Failed to stop PID {pid}: {result.stdout}")
                except Exception as e:
                    print(f"   ✗ Error stopping PID {pid}: {e}")
            
            print("\n✓ Done! All live_trader processes stopped.")
            print("\nOther Python processes are still running.")
            
        else:
            print("\nNo live_trader processes found.")
            print("All Python processes may be other scripts.")
            
    except Exception as e:
        print(f"Error: {e}")
        print("\nManual method:")
        print("1. Open Task Manager (Ctrl+Shift+Esc)")
        print("2. Find python.exe processes")
        print("3. Right-click -> End Task for processes running live_trader.py")
else:
    print("Linux/Mac: Use: pkill -f live_trader.py")

print("\n" + "=" * 70)





