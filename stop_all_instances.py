#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop all live trader instances - both PM2 managed and manual
"""

import subprocess
import sys
import time

print("=" * 70)
print("STOPPING ALL LIVE TRADER INSTANCES")
print("=" * 70)

if sys.platform == 'win32':
    # First, stop PM2 managed instance
    print("\n[1] Stopping PM2 managed instance...")
    try:
        result = subprocess.run(['pm2', 'stop', 'mgc-live-trader'], capture_output=True, text=True)
        if result.returncode == 0:
            print("   OK: PM2 instance stopped")
        else:
            print(f"   Result: {result.stdout}")
    except Exception as e:
        print(f"   Note: {e}")
    
    time.sleep(2)
    
    # Now find and stop all live_trader.py processes
    print("\n[2] Finding all live_trader.py processes...")
    try:
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
                pid = proc.get('pid', '').strip()
                if pid and pid != 'None':
                    live_traders.append(pid)
        
        if live_traders:
            print(f"   Found {len(live_traders)} live_trader.py process(es): {', '.join(live_traders)}")
            
            print("\n[3] Stopping all live_trader.py processes...")
            for pid in live_traders:
                try:
                    result = subprocess.run(['taskkill', '/F', '/PID', pid], capture_output=True, text=True)
                    if result.returncode == 0:
                        print(f"   OK: Stopped PID {pid}")
                    else:
                        print(f"   Note: PID {pid} - {result.stderr.strip()}")
                except Exception as e:
                    print(f"   Error stopping PID {pid}: {e}")
        else:
            print("   No live_trader.py processes found")
        
        time.sleep(2)
        
        # Verify they're stopped
        print("\n[4] Verifying all instances are stopped...")
        result = subprocess.run(
            ['wmic', 'process', 'where', "name='python.exe'", 'get', 'ProcessId,CommandLine', '/format:list'],
            capture_output=True,
            text=True
        )
        
        remaining = []
        current_process = {}
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.startswith('ProcessId='):
                if current_process:
                    if current_process.get('cmd') and 'live_trader.py' in current_process['cmd']:
                        remaining.append(current_process.get('pid', 'unknown'))
                current_process = {'pid': line.split('=')[1]}
            elif line.startswith('CommandLine='):
                current_process['cmd'] = line.split('=', 1)[1] if '=' in line else ''
        
        if current_process.get('cmd') and 'live_trader.py' in current_process['cmd']:
            remaining.append(current_process.get('pid', 'unknown'))
        
        if remaining:
            print(f"   WARNING: {len(remaining)} instance(s) still running: {', '.join(remaining)}")
        else:
            print("   OK: All live_trader.py instances stopped")
        
    except Exception as e:
        print(f"   Error: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)
    print("\nTo start a single instance:")
    print("  Option 1 (PM2): pm2 start ecosystem.config.js")
    print("  Option 2 (Manual): python live_trader.py --config config_production.json")
    print("\n" + "=" * 70)

