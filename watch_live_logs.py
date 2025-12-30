#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Watch live trader logs in real-time
"""

import time
import sys
from pathlib import Path

log_file = Path('live_trading.log')

if not log_file.exists():
    print("ERROR: live_trading.log not found")
    sys.exit(1)

print("=" * 70)
print("LIVE TRADER LOGS - REAL-TIME")
print("=" * 70)
print("Press Ctrl+C to stop")
print("=" * 70)
print()

# Read current file size to start from end
try:
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        # Go to end
        f.seek(0, 2)
        last_pos = f.tell()
    
    # Show last 20 lines
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
        for line in lines[-20:]:
            print(line.rstrip())
    
    print("\n" + "=" * 70)
    print("Following new log entries...")
    print("=" * 70)
    
    # Follow new entries
    while True:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            f.seek(last_pos)
            new_lines = f.readlines()
            
            if new_lines:
                for line in new_lines:
                    print(line.rstrip())
                last_pos = f.tell()
            
            time.sleep(0.5)  # Check every 0.5 seconds
            
except KeyboardInterrupt:
    print("\n\nStopped watching logs")
except Exception as e:
    print(f"\nError: {e}")

