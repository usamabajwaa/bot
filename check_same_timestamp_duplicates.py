#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Check for duplicate log entries with the SAME timestamp
This indicates the logging handler duplication issue
"""

from pathlib import Path
from collections import defaultdict

print("=" * 70)
print("CHECKING FOR SAME-TIMESTAMP DUPLICATES")
print("=" * 70)
print("(This is the real issue - same message logged multiple times at same time)")
print()

log_file = Path('live_trading.log')
if not log_file.exists():
    print("[ERROR] Log file not found")
    exit(1)

# Read last 1000 lines
with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

last_1000 = lines[-1000:] if len(lines) > 1000 else lines

print(f"Analyzing last {len(last_1000)} lines...\n")

# Group lines by timestamp + message
timestamp_message_groups = defaultdict(list)

for i, line in enumerate(last_1000, len(lines) - len(last_1000)):
    line = line.strip()
    if not line:
        continue
    
    # Parse timestamp and message
    try:
        # Format: "2025-12-29 20:58:07,329 - INFO - MESSAGE"
        if ' - INFO - ' in line or ' - ERROR - ' in line or ' - WARNING - ' in line:
            parts = line.split(' - ', 2)
            if len(parts) >= 3:
                timestamp = parts[0]  # "2025-12-29 20:58:07,329"
                level = parts[1]      # "INFO"
                message = parts[2]    # The actual message
                
                # Create key: timestamp + message
                key = (timestamp, message)
                timestamp_message_groups[key].append(i)
    except Exception as e:
        pass

# Find duplicates (same timestamp + same message)
duplicates = {k: v for k, v in timestamp_message_groups.items() if len(v) > 1}

if duplicates:
    print(f"[FOUND] {len(duplicates)} types of same-timestamp duplicates:\n")
    
    # Sort by number of duplicates (most first)
    sorted_dups = sorted(duplicates.items(), key=lambda x: len(x[1]), reverse=True)
    
    for (timestamp, message), line_numbers in sorted_dups[:20]:  # Show top 20
        count = len(line_numbers)
        print(f"  {count}x duplicate at {timestamp}:")
        print(f"    Message: {message[:80]}...")
        print(f"    Line numbers: {line_numbers[:5]}{'...' if len(line_numbers) > 5 else ''}")
        print()
    
    print("=" * 70)
    print("[WARNING] Same-timestamp duplicates detected!")
    print("This indicates the logging handler duplication issue is still present.")
    print("=" * 70)
else:
    print("[OK] No same-timestamp duplicates found!")
    print("The logging fix is working correctly - each message is logged only once.")
    print()
    
    # Show recent "LIVE TRADING STARTED" entries to verify
    print("Recent 'LIVE TRADING STARTED' entries (should be from different restarts):")
    started_entries = []
    for i, line in enumerate(last_1000, len(lines) - len(last_1000)):
        if 'LIVE TRADING STARTED' in line:
            started_entries.append((i, line.strip()[:100]))
    
    for line_num, line_text in started_entries[-5:]:
        print(f"  Line {line_num}: {line_text}")
    
    print()
    print("=" * 70)
    print("[SUCCESS] Logging fix is working - no duplicate handlers detected!")
    print("=" * 70)

