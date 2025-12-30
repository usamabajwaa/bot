#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diagnose why signals are being filtered out
"""

import json
from pathlib import Path

print("=" * 70)
print("SIGNAL FILTER DIAGNOSIS")
print("=" * 70)

# Read recent log to get current market conditions
log_file = Path('live_trading.log')
if log_file.exists():
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    # Find most recent signal check
    recent_checks = []
    for i in range(len(lines) - 1, max(0, len(lines) - 200), -1):
        if 'Signal check:' in lines[i]:
            recent_checks.append(lines[i])
            if len(recent_checks) >= 3:
                break
    
    if recent_checks:
        print("\nMost recent signal checks:")
        for check in reversed(recent_checks[:3]):
            print(f"   {check.strip()}")
    
    # Find filter information
    print("\n" + "=" * 70)
    print("FILTER ANALYSIS")
    print("=" * 70)
    
    # Get last signal check details
    last_check_idx = None
    for i in range(len(lines) - 1, max(0, len(lines) - 50), -1):
        if 'Signal check:' in lines[i]:
            last_check_idx = i
            break
    
    if last_check_idx:
        print("\nLast signal check details:")
        for i in range(last_check_idx, min(len(lines), last_check_idx + 15)):
            line = lines[i].strip()
            if 'Signal check' in line or 'Price range' in line or 'VWAP' in line or 'zones touched' in line or 'No signal generated' in line:
                print(f"   {line}")

# Load config to show filter settings
config_file = Path('config_production.json')
if not config_file.exists():
    config_file = Path('config.json')

if config_file.exists():
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    print("\n" + "=" * 70)
    print("FILTER SETTINGS")
    print("=" * 70)
    
    # VWAP filter
    vwap_config = config.get('vwap_filter', {})
    print(f"\nVWAP Filter:")
    print(f"   Enabled: {vwap_config.get('enabled', True)}")
    print(f"   Allow reversals: {vwap_config.get('allow_reversals', False)}")
    
    # HTF filter
    htf_config = config.get('higher_tf_filter', {})
    print(f"\nHigher Timeframe Filter:")
    print(f"   Enabled: {htf_config.get('enabled', True)}")
    print(f"   Timeframe: {htf_config.get('timeframe_minutes', 15)} minutes")
    print(f"   EMA Period: {htf_config.get('ema_period', 20)}")
    
    # Chop filter
    chop_config = config.get('chop_filter', {})
    print(f"\nChop Filter:")
    print(f"   Enabled: {chop_config.get('enabled', True)}")
    print(f"   Max crosses: {chop_config.get('max_crosses', 6)}")
    print(f"   Lookback: {chop_config.get('lookback_bars', 30)} bars")
    
    # Volume filter
    volume_config = config.get('volume_filter', {})
    print(f"\nVolume Filter:")
    print(f"   Enabled: {volume_config.get('enabled', True)}")
    
    # Confirmation
    confirm_config = config.get('confirmation', {})
    print(f"\nConfirmation:")
    print(f"   Required: {confirm_config.get('required', True)}")
    print(f"   Require both: {confirm_config.get('require_both', False)}")
    
    # R:R
    print(f"\nRisk/Reward:")
    print(f"   Min R:R: {config.get('min_rr', 1.5)}")
    
    # Session filters
    sessions = config.get('sessions', {})
    print(f"\nSession-Specific Filters:")
    for sess_name, sess_config in sessions.items():
        if sess_config.get('enabled', True):
            sess_filters = sess_config.get('filters', {})
            print(f"   {sess_name}:")
            if sess_filters.get('min_rr'):
                print(f"      Min R:R: {sess_filters.get('min_rr')}")
            if sess_filters.get('chop_max_crosses'):
                print(f"      Max chop crosses: {sess_filters.get('chop_max_crosses')}")
            if sess_filters.get('require_volume_filter') is not None:
                print(f"      Require volume: {sess_filters.get('require_volume_filter')}")

print("\n" + "=" * 70)
print("LIKELY REASONS FOR NO SIGNALS")
print("=" * 70)
print("""
Based on the logs showing:
- Price: ~$4348.80
- VWAP: ~$4440.66 (price is BELOW VWAP)
- Demand zones touched: 2-3 zones
- No signal generated

Most likely causes:
1. VWAP Filter: Price is below VWAP, so LONG signals may be blocked
   (unless reversal exceptions are enabled)
2. HTF Filter: Higher timeframe trend may not align for LONG
3. Confirmation: No confirmation candle pattern detected
4. R:R Ratio: Risk/reward may be below minimum threshold
5. Chop Filter: Too many VWAP crosses (market may be choppy)

To see detailed filter results, enable debug logging in the strategy.
""")
print("=" * 70)

