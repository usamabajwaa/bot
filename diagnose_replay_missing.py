#!/usr/bin/env python3
"""Diagnose why replay data is missing for trades."""

import json
import pandas as pd
from pathlib import Path
from datetime import datetime
import pytz

# Load all 2026 trades
trades_2026 = []
with open('trade_journal.jsonl', 'r') as f:
    for line in f:
        if line.strip() and '2026' in line:
            trades_2026.append(json.loads(line))

print(f"Total 2026 trades in journal: {len(trades_2026)}\n")

# Get all 2026 replay files
replay_dir = Path('replay_data')
replay_files_2026 = list(replay_dir.glob('replay_2026*.csv'))
print(f"Total 2026 replay files: {len(replay_files_2026)}\n")

# Extract timestamps from replay files
replay_timestamps = []
for rf in replay_files_2026:
    try:
        # Format: replay_YYYYMMDD_HHMMSS_side.csv
        parts = rf.stem.split('_')
        if len(parts) >= 3:
            date_str = parts[1]  # YYYYMMDD
            time_str = parts[2]  # HHMMSS
            timestamp_str = f"{date_str}_{time_str}"
            dt = pd.to_datetime(timestamp_str, format='%Y%m%d_%H%M%S', utc=True)
            side = parts[3] if len(parts) > 3 else 'unknown'
            replay_timestamps.append({
                'file': rf.name,
                'timestamp': dt,
                'side': side
            })
    except Exception as e:
        print(f"Error parsing {rf.name}: {e}")

print(f"Parsed replay timestamps: {len(replay_timestamps)}\n")

# Convert trade timestamps to UTC for comparison
trade_timestamps_utc = []
for trade in trades_2026:
    try:
        ts_str = trade.get('timestamp', '')
        dt = pd.to_datetime(ts_str)
        if dt.tz is not None:
            dt_utc = dt.tz_convert('UTC')
        else:
            dt_utc = dt.tz_localize('UTC')
        trade_timestamps_utc.append({
            'timestamp': dt_utc,
            'side': trade.get('side', '').lower(),
            'entry': trade.get('entry'),
            'original': ts_str
        })
    except Exception as e:
        print(f"Error parsing trade timestamp {trade.get('timestamp')}: {e}")

print("="*80)
print("ANALYSIS: Why replay data is missing")
print("="*80)

# Find matches
matched = []
unmatched_trades = []
unmatched_replays = []

for trade in trade_timestamps_utc:
    trade_time = trade['timestamp']
    trade_side = trade['side']
    
    # Find closest replay file (within 5 minutes)
    best_match = None
    min_diff = None
    
    for replay in replay_timestamps:
        if replay['side'] == trade_side:
            diff_seconds = abs((replay['timestamp'] - trade_time).total_seconds())
            if diff_seconds < 300:  # Within 5 minutes
                if min_diff is None or diff_seconds < min_diff:
                    min_diff = diff_seconds
                    best_match = replay
    
    if best_match:
        matched.append({
            'trade': trade,
            'replay': best_match,
            'diff_seconds': min_diff
        })
    else:
        unmatched_trades.append(trade)

# Find replay files without matching trades
for replay in replay_timestamps:
    has_match = False
    for match in matched:
        if match['replay']['file'] == replay['file']:
            has_match = True
            break
    if not has_match:
        unmatched_replays.append(replay)

print(f"\nMatched: {len(matched)} trades have replay files")
print(f"Unmatched trades: {len(unmatched_trades)} trades missing replay files")
print(f"Unmatched replay files: {len(unmatched_replays)} replay files without trades\n")

if matched:
    print("Sample matches:")
    for m in matched[:3]:
        trade = m['trade']
        replay = m['replay']
        print(f"  Trade: {trade['original']} ({trade['side']})")
        print(f"  Replay: {replay['file']} (diff: {m['diff_seconds']:.0f}s)")
        print()

if unmatched_trades:
    print(f"\nFirst 5 unmatched trades (missing replay files):")
    for trade in unmatched_trades[:5]:
        print(f"  {trade['original']} ({trade['side']}) @ ${trade['entry']}")
    print()

if unmatched_replays:
    print(f"\nFirst 5 unmatched replay files (no corresponding trade):")
    for replay in unmatched_replays[:5]:
        print(f"  {replay['file']} ({replay['side']}) @ {replay['timestamp']}")
    print()

# Check if replay files were cleaned up
print("="*80)
print("REPLAY FILE CLEANUP ANALYSIS")
print("="*80)
print(f"Replay files are limited to last 200 files (max_replay_files = 200)")
print(f"Older files get automatically deleted when limit is exceeded")
print(f"\nCurrent replay files: {len(list(replay_dir.glob('replay_*.csv')))}")
print(f"2026 replay files: {len(replay_files_2026)}")

# Check date distribution
print("\nDate distribution of 2026 trades:")
from collections import Counter
trade_dates = [pd.to_datetime(t['timestamp']).date() for t in trades_2026]
date_counts = Counter(trade_dates)
for date, count in sorted(date_counts.items()):
    print(f"  {date}: {count} trades")

print("\nDate distribution of 2026 replay files:")
replay_dates = [r['timestamp'].date() for r in replay_timestamps]
replay_date_counts = Counter(replay_dates)
for date, count in sorted(replay_date_counts.items()):
    print(f"  {date}: {count} replay files")

