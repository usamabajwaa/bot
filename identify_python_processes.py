#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Identify which Python processes are live_trader
"""

import subprocess
import sys
import os

print("=" * 70)
print("IDENTIFYING PYTHON PROCESSES")
print("=" * 70)

if sys.platform == 'win32':
    try:
        # Get detailed process information using wmic
        result = subprocess.run(
            ['wmic', 'process', 'where', 'name="python.exe"', 'get', 'ProcessId,CommandLine,WorkingSetSize'],
            capture_output=True,
            text=True
        )
        
        lines = result.stdout.strip().split('\n')
        
        print("\nPython Processes:")
        print("-" * 70)
        
        live_trader_pids = []
        other_pids = []
        
        for line in lines[1:]:  # Skip header
            if not line.strip():
                continue
                
            parts = line.split()
            if len(parts) >= 2:
                try:
                    pid = parts[0]
                    mem_mb = int(parts[-1]) / (1024 * 1024) if parts[-1].isdigit() else 0
                    cmdline = ' '.join(parts[1:-1]) if len(parts) > 2 else ' '.join(parts[1:])
                    
                    if 'live_trader' in cmdline.lower():
                        print(f"\n[LIVE TRADER] PID: {pid}")
                        print(f"   Memory: {mem_mb:.1f} MB")
                        print(f"   Command: {cmdline[:100]}...")
                        live_trader_pids.append(pid)
                    else:
                        print(f"\n[OTHER] PID: {pid}")
                        print(f"   Memory: {mem_mb:.1f} MB")
                        if cmdline:
                            print(f"   Command: {cmdline[:80]}...")
                        other_pids.append(pid)
                except:
                    pass
        
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"Live Trader Processes: {len(live_trader_pids)}")
        if live_trader_pids:
            print(f"   PIDs: {', '.join(live_trader_pids)}")
        print(f"\nOther Python Processes: {len(other_pids)}")
        if other_pids:
            print(f"   PIDs: {', '.join(other_pids)}")
        
        if len(live_trader_pids) > 1:
            print("\n⚠️  WARNING: Multiple live_trader instances detected!")
            print("   You should stop all but one instance.")
            print("\n   To stop specific PIDs:")
            for pid in live_trader_pids:
                print(f"      taskkill /F /PID {pid}")
        elif len(live_trader_pids) == 1:
            print("\n✅ Only one live_trader instance running (good)")
        else:
            print("\n⚠️  No live_trader processes found")
            
    except Exception as e:
        print(f"Error: {e}")
        print("\nTrying alternative method...")
        
        # Fallback: simple tasklist
        result = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq python.exe', '/FO', 'LIST'],
            capture_output=True,
            text=True
        )
        print(result.stdout)
else:
    print("Linux/Mac identification not implemented")
    print("Use: ps aux | grep live_trader")

print("\n" + "=" * 70)

