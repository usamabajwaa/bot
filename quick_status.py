#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quick status check for live trader
"""

from pathlib import Path
from datetime import datetime

print("=" * 70)
print("LIVE TRADER QUICK STATUS")
print("=" * 70)

log_file = Path('live_trading.log')
if log_file.exists():
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
        
    if lines:
        last_line = lines[-1].strip()
        print(f"\nLast log entry:")
        print(f"   {last_line}")
        
        # Extract timestamp
        if ' - ' in last_line:
            try:
                timestamp_str = last_line.split(' - ')[0]
                log_time = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S,%f')
                now = datetime.now()
                age_seconds = (now - log_time).total_seconds()
                
                if age_seconds < 120:
                    print(f"\n   [ACTIVE] Last activity {age_seconds:.0f} seconds ago")
                elif age_seconds < 300:
                    print(f"\n   [IDLE] Last activity {age_seconds:.0f} seconds ago (may be waiting)")
                else:
                    print(f"\n   [STALE] Last activity {age_seconds:.0f} seconds ago")
            except:
                pass
        
        # Check for signal activity
        recent_lines = lines[-20:]
        signal_checks = sum(1 for line in recent_lines if 'Signal check' in line or 'SIGNAL GENERATED' in line)
        data_refreshes = sum(1 for line in recent_lines if 'Data refresh' in line)
        
        print(f"\n   Recent activity (last 20 lines):")
        print(f"      Signal checks: {signal_checks}")
        print(f"      Data refreshes: {data_refreshes}")
        
        if signal_checks > 0 or data_refreshes > 0:
            print(f"\n   ✓ System is actively running")
        else:
            print(f"\n   ⚠ System may be idle or stopped")

print("\n" + "=" * 70)
print("OPTIONS:")
print("=" * 70)
print("\n1. Keep running (if active)")
print("2. Restart: Stop all Python, then start fresh")
print("   python live_trader.py --config config_production.json")
print("\n3. Check Task Manager manually")
print("=" * 70)

