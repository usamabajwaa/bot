#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Check for running live trader instances
"""

import subprocess
import sys
import os
from pathlib import Path

print("=" * 70)
print("CHECKING FOR RUNNING LIVE TRADER INSTANCES")
print("=" * 70)

# Check for Python processes
try:
    if sys.platform == 'win32':
        # Windows: Use tasklist
        result = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq python.exe', '/FO', 'CSV'],
            capture_output=True,
            text=True
        )
        
        lines = result.stdout.strip().split('\n')
        python_processes = [line for line in lines if 'python.exe' in line.lower()]
        
        if len(python_processes) > 1:  # More than header
            print(f"\nFound {len(python_processes) - 1} Python process(es) running:")
            for i, line in enumerate(python_processes[1:], 1):
                parts = line.split(',')
                if len(parts) >= 2:
                    pid = parts[1].strip('"')
                    mem = parts[4].strip('"') if len(parts) > 4 else 'N/A'
                    print(f"   Process {i}: PID {pid}, Memory: {mem}")
        else:
            print("\nNo Python processes found running")
    else:
        # Linux/Mac: Use ps
        result = subprocess.run(
            ['ps', 'aux'],
            capture_output=True,
            text=True
        )
        
        lines = result.stdout.split('\n')
        python_processes = [line for line in lines if 'python' in line.lower() and 'live_trader' in line.lower()]
        
        if python_processes:
            print(f"\nFound {len(python_processes)} live_trader process(es):")
            for proc in python_processes:
                print(f"   {proc}")
        else:
            print("\nNo live_trader processes found")
            
except Exception as e:
    print(f"\nCould not check processes: {e}")

# Check log file for recent activity
print("\n" + "=" * 70)
print("RECENT LOG ACTIVITY")
print("=" * 70)

log_file = Path('live_trading.log')
if log_file.exists():
    try:
        # Get last 10 lines
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            recent = lines[-10:] if len(lines) > 10 else lines
            
        print("\nLast 10 log entries:")
        for line in recent:
            print(f"   {line.strip()}")
            
        # Check for "LIVE TRADING STARTED" entries
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            start_count = content.count('LIVE TRADING STARTED')
            print(f"\n   Total 'LIVE TRADING STARTED' entries: {start_count}")
            
    except Exception as e:
        print(f"Error reading log: {e}")
else:
    print("\nNo log file found")

print("\n" + "=" * 70)
print("RECOMMENDATION")
print("=" * 70)
print("If you have multiple instances running, you should:")
print("1. Stop all instances")
print("2. Start only one instance")
print("\nTo stop all Python processes (Windows):")
print("   taskkill /F /IM python.exe")
print("\nOr manually check Task Manager for python.exe processes")
print("=" * 70)

