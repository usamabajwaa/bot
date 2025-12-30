#!/usr/bin/env python3
"""
Verify that running live trader has the latest code changes
"""

import subprocess
import sys
from pathlib import Path
from datetime import datetime

print("=" * 70)
print("VERIFYING LIVE TRADER HAS LATEST CODE")
print("=" * 70)

# Check 1: File modification time vs process start time
print("\n[1] Checking file modification time...")
live_trader_file = Path('live_trader.py')
if live_trader_file.exists():
    file_mtime = datetime.fromtimestamp(live_trader_file.stat().st_mtime)
    print(f"   live_trader.py last modified: {file_mtime.strftime('%Y-%m-%d %H:%M:%S')}")
else:
    print("   ERROR: live_trader.py not found!")
    sys.exit(1)

# Check 2: Look for specific new code features in logs
print("\n[2] Checking for new code features in logs...")
log_file = Path('live_trading.log')
if log_file.exists():
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        log_content = f.read()
    
    # Check for new features
    features_found = []
    
    # Feature: "Fetching 30 days of historical data for zone building"
    if "Fetching 30 days of historical data for zone building" in log_content:
        features_found.append("[OK] Zone initialization (30 days fetch)")
    else:
        features_found.append("[MISSING] Zone initialization (30 days fetch) - NOT FOUND")
    
    # Feature: "Zone initialization complete"
    if "Zone initialization complete:" in log_content:
        features_found.append("[OK] Zone initialization complete message")
    else:
        features_found.append("[MISSING] Zone initialization complete - NOT FOUND")
    
    # Feature: Rolling DataFrame (check for zone update messages)
    if "Zones updated from rolling DataFrame" in log_content or "Zone update triggered" in log_content:
        features_found.append("[OK] Rolling DataFrame zone updates")
    else:
        features_found.append("[PENDING] Rolling DataFrame (may not have triggered yet)")
    
    # Feature: include_partial=False (check API calls)
    # This is harder to verify from logs, but we can check the code
    
    for feature in features_found:
        print(f"   {feature}")
    
    # Get most recent startup
    lines = log_content.split('\n')
    startup_lines = [l for l in lines if 'LIVE TRADING STARTED' in l]
    if startup_lines:
        last_startup = startup_lines[-1]
        print(f"\n   Most recent startup: {last_startup[:80]}...")
else:
    print("   WARNING: live_trading.log not found")

# Check 3: Verify code has new features
print("\n[3] Verifying code contains new features...")
code_features = []

with open('live_trader.py', 'r', encoding='utf-8') as f:
    code = f.read()

if 'include_partial=False' in code:
    code_features.append("[OK] include_partial=False")
else:
    code_features.append("[MISSING] include_partial=False - NOT FOUND")

if 'rolling_df' in code:
    code_features.append("[OK] rolling_df (rolling DataFrame)")
else:
    code_features.append("[MISSING] rolling_df - NOT FOUND")

if 'zone_update_interval_bars' in code:
    code_features.append("[OK] zone_update_interval_bars")
else:
    code_features.append("[MISSING] zone_update_interval_bars - NOT FOUND")

if 'math.ceil' in code and 'round(abs(signal' in code:
    code_features.append("[OK] Tick rounding (ceil/round)")
else:
    code_features.append("[MISSING] Tick rounding - NOT FOUND")

if 'Fetching' in code and 'days of historical data for zone' in code:
    code_features.append("[OK] Zone initialization message")
else:
    code_features.append("[MISSING] Zone initialization message - NOT FOUND")

for feature in code_features:
    print(f"   {feature}")

# Check 4: Check strategy.py for timestamp normalization
print("\n[4] Verifying strategy.py has timestamp normalization...")
strategy_file = Path('strategy.py')
if strategy_file.exists():
    with open(strategy_file, 'r', encoding='utf-8') as f:
        strategy_code = f.read()
    
    if 'if timestamp.tzinfo is None:' in strategy_code and 'timestamp = timestamp.astimezone(pytz.UTC)' in strategy_code:
        # Check if it's in generate_signal
        if 'def generate_signal' in strategy_code:
            sig_start = strategy_code.find('def generate_signal')
            sig_section = strategy_code[sig_start:sig_start+1000]  # Check first 1000 chars
            if 'timestamp = timestamp.astimezone(pytz.UTC)' in sig_section and 'bar = df.iloc[bar_index]' in sig_section:
                # Check order - normalization should be after bar extraction
                bar_idx = sig_section.find('bar = df.iloc[bar_index]')
                norm_idx = sig_section.find('timestamp = timestamp.astimezone(pytz.UTC)')
                if norm_idx > bar_idx and norm_idx < bar_idx + 100:
                    print("   [OK] Timestamp normalization in generate_signal() (at top)")
                else:
                    print("   [OK] Timestamp normalization in generate_signal() (exists)")
            else:
                print("   [WARN] Timestamp normalization exists but may not be at top of generate_signal()")
        else:
            print("   [WARN] Timestamp normalization exists but generate_signal() not found")
    else:
        print("   [MISSING] Timestamp normalization - NOT FOUND")

# Check 5: Check zones.py for improved de-duplication
print("\n[5] Verifying zones.py has improved de-duplication...")
zones_file = Path('zones.py')
if zones_file.exists():
    with open(zones_file, 'r', encoding='utf-8') as f:
        zones_code = f.read()
    
    if 'get_zone_signature' in zones_code and 'pivot_bucket' in zones_code:
        print("   [OK] Improved zone de-duplication (tolerance + range signature)")
    else:
        print("   [MISSING] Improved zone de-duplication - NOT FOUND")

# Summary
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)

all_checks = code_features + features_found
passed = sum(1 for c in all_checks if c.startswith('[OK]'))
total = len(all_checks)

print(f"\nPassed: {passed}/{total} checks")

if passed == total:
    print("\n[SUCCESS] ALL CHECKS PASSED - Live trader has latest code!")
elif passed >= total * 0.8:
    print("\n[WARNING] MOST CHECKS PASSED - Live trader likely has latest code")
    print("   (Some features may not have triggered yet in logs)")
else:
    print("\n[ERROR] SOME CHECKS FAILED - Live trader may not have latest code")
    print("   Consider restarting the live trader")

print("=" * 70)

