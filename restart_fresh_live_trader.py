#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop all Python processes and start fresh live trader
"""

import subprocess
import sys
import time
import os

print("=" * 70)
print("RESTART FRESH LIVE TRADER")
print("=" * 70)

if sys.platform == 'win32':
    print("\n[1] Stopping all Python processes...")
    
    try:
        result = subprocess.run(
            ['taskkill', '/F', '/IM', 'python.exe'],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("   OK: All Python processes stopped")
            print(f"   {result.stdout.strip()}")
        else:
            if "not found" in result.stdout.lower() or "not found" in result.stderr.lower():
                print("   OK: No Python processes were running")
            else:
                print(f"   Result: {result.stdout}")
                print(f"   Error: {result.stderr}")
    except Exception as e:
        print(f"   Error: {e}")
    
    # Wait for processes to fully terminate
    print("\n[2] Waiting for processes to terminate...")
    time.sleep(3)
    
    print("\n[3] Starting fresh live trader...")
    print("   Command: python live_trader.py --config config_production.json")
    
    try:
        # Start in background (detached)
        process = subprocess.Popen(
            [sys.executable, 'live_trader.py', '--config', 'config_production.json'],
            cwd=os.getcwd(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == 'win32' else 0
        )
        
        print(f"   OK: Live trader started (PID: {process.pid})")
        print("\n   The live trader is now running in the background")
        print("   Monitor with: python check_running_instances.py")
        print("   Or view logs: Get-Content live_trading.log -Tail 20 -Wait")
        
    except Exception as e:
        print(f"   ERROR: Failed to start live trader: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    # Wait a moment and check if it started
    time.sleep(2)
    
    print("\n[4] Verifying startup...")
    log_file = os.path.join(os.getcwd(), 'live_trading.log')
    if os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            if lines:
                last_line = lines[-1].strip()
                if 'LIVE TRADING STARTED' in last_line or 'Authentication successful' in last_line:
                    print("   OK: Live trader appears to be starting up")
                else:
                    print(f"   Last log: {last_line[:80]}...")
    else:
        print("   Log file not found yet (may take a moment)")
    
else:
    print("Linux/Mac restart not fully implemented")
    print("Use: pkill -f python")
    print("Then: python live_trader.py --config config_production.json")

print("\n" + "=" * 70)
print("DONE")
print("=" * 70)
print("\nLive trader should now be running.")
print("Check status with: python check_running_instances.py")
print("=" * 70)

