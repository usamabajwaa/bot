#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Safely restart live trader - stops all instances and starts fresh
"""

import subprocess
import sys
import time
import os
from pathlib import Path

print("=" * 70)
print("RESTART LIVE TRADER")
print("=" * 70)

# Check for running instances
print("\n[1] Checking for running instances...")
try:
    if sys.platform == 'win32':
        result = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq python.exe', '/FO', 'CSV'],
            capture_output=True,
            text=True
        )
        lines = result.stdout.strip().split('\n')
        python_processes = [line for line in lines if 'python.exe' in line.lower()]
        count = len(python_processes) - 1 if len(python_processes) > 1 else 0
        print(f"   Found {count} Python process(es)")
        
        if count > 0:
            print("\n[2] Stopping all Python processes...")
            print("   WARNING: This will stop ALL Python processes, not just live_trader")
            print("   Press Ctrl+C within 3 seconds to cancel...")
            time.sleep(3)
            
            result = subprocess.run(
                ['taskkill', '/F', '/IM', 'python.exe'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                print("   OK: All Python processes stopped")
            else:
                print(f"   Result: {result.stdout}")
                if "not found" in result.stdout.lower():
                    print("   No Python processes were running")
        else:
            print("\n[2] No Python processes to stop")
        
        # Wait a moment for processes to fully terminate
        time.sleep(2)
        
        print("\n[3] Starting fresh live trader instance...")
        print("   Command: python live_trader.py --config config_production.json")
        
        # Start in background
        subprocess.Popen(
            [sys.executable, 'live_trader.py', '--config', 'config_production.json'],
            cwd=os.getcwd(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        print("   OK: Live trader started in background")
        print("\n   Check live_trading.log for status")
        print("   Or run: python check_running_instances.py")
        
    else:
        print("   Linux/Mac restart not implemented yet")
        print("   Please manually stop processes and restart")
        
except KeyboardInterrupt:
    print("\n\nCancelled by user")
    sys.exit(1)
except Exception as e:
    print(f"\nError: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 70)
print("DONE")
print("=" * 70)

