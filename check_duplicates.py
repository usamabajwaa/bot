#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Check for duplicate log entries in live_trading.log
"""

from pathlib import Path
from collections import Counter
from datetime import datetime

print("=" * 70)
print("CHECKING FOR DUPLICATE LOG ENTRIES")
print("=" * 70)

log_file = Path('live_trading.log')
if not log_file.exists():
    print("\n[ERROR] Log file not found")
    exit(1)

# Read last 500 lines
with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

last_500 = lines[-500:] if len(lines) > 500 else lines

print(f"\nAnalyzing last {len(last_500)} lines of log file...")

# Check for exact duplicate lines (excluding timestamps)
print("\n[1] Checking for exact duplicate lines...")
line_counts = Counter(line.strip() for line in last_500 if line.strip())
exact_duplicates = {line: count for line, count in line_counts.items() if count > 1 and len(line) > 30}

if exact_duplicates:
    print(f"   Found {len(exact_duplicates)} types of duplicate lines:")
    for line, count in sorted(exact_duplicates.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"   {count}x: {line[:80]}...")
else:
    print("   [OK] No exact duplicate lines found")

# Check for "LIVE TRADING STARTED" entries
print("\n[2] Checking 'LIVE TRADING STARTED' entries...")
started_entries = [i for i, line in enumerate(last_500, len(lines) - len(last_500)) if 'LIVE TRADING STARTED' in line]
print(f"   Found {len(started_entries)} 'LIVE TRADING STARTED' entries in last {len(last_500)} lines")

if len(started_entries) > 1:
    print("   Recent entries:")
    for idx in started_entries[-5:]:
        line_num = idx
        if idx < len(lines):
            print(f"     Line {line_num}: {lines[idx].strip()[:80]}")
else:
    print("   [OK] Only one or zero entries found (expected)")

# Check for duplicate messages (same message text, different timestamps)
print("\n[3] Checking for duplicate messages (same content, different timestamps)...")
message_counts = {}
for line in last_500:
    if ' - INFO - ' in line or ' - ERROR - ' in line or ' - WARNING - ' in line:
        try:
            # Extract message part (after timestamp and level)
            parts = line.split(' - ', 2)
            if len(parts) >= 3:
                message = parts[2].strip()
                if len(message) > 20:  # Only check substantial messages
                    message_counts[message] = message_counts.get(message, 0) + 1
        except:
            pass

duplicate_messages = {msg: count for msg, count in message_counts.items() if count > 1}

if duplicate_messages:
    print(f"   Found {len(duplicate_messages)} types of duplicate messages:")
    for msg, count in sorted(duplicate_messages.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"   {count}x: {msg[:70]}...")
else:
    print("   [OK] No duplicate messages found")

# Check recent activity for patterns
print("\n[4] Recent log activity (last 20 lines):")
for line in last_500[-20:]:
    print(f"   {line.strip()[:100]}")

# Summary
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)

if exact_duplicates or duplicate_messages:
    print("[WARNING] Duplicates detected!")
    print("  - Exact duplicate lines:", len(exact_duplicates))
    print("  - Duplicate messages:", len(duplicate_messages))
else:
    print("[OK] No duplicates detected in recent log entries")
    print("  - Logging fix appears to be working correctly")

print("\n" + "=" * 70)

