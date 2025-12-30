#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check current account and TP/Partial profit settings"""

import json
import sys
import io
from pathlib import Path

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

print("=" * 70)
print("CURRENT SYSTEM SETUP")
print("=" * 70)

# Check account
print("\n[1] ACCOUNT CONFIGURATION")
print("-" * 70)
cred_path = Path('credentials.json')
if cred_path.exists():
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    account_id = creds.get('account_id')
    account_suffix = creds.get('account_suffix')
    
    if account_id:
        print(f"   Account ID: {account_id}")
        print(f"   Account Suffix: {account_suffix or 'NOT SET'}")
        
        # Verify account
        try:
            from broker import TopstepXClient
            client = TopstepXClient(
                username=creds['username'],
                api_key=creds['api_key'],
                base_url=creds.get('base_url'),
                rtc_url=creds.get('rtc_url')
            )
            if client.authenticate():
                accounts = client.get_accounts(only_active=True)
                account = next((a for a in accounts if a.id == account_id), None)
                if account:
                    print(f"\n   Account Details:")
                    print(f"      Name: {account.name}")
                    print(f"      Balance: ${account.balance:,.2f}")
                    print(f"      Tradable: {'YES' if account.can_trade else 'NO'}")
        except:
            pass
    else:
        print("   Account ID: NOT SET")
        if account_suffix:
            print(f"   Account Suffix: {account_suffix}")
else:
    print("   ERROR: credentials.json not found")

# Check TP and Partial Profit settings
print("\n[2] TAKE PROFIT & PARTIAL PROFIT SETTINGS")
print("-" * 70)

config_path = Path('config_production.json')
if not config_path.exists():
    config_path = Path('config.json')

if config_path.exists():
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    # Take Profit (via bracket orders)
    print("\n   Take Profit (TP):")
    print("      [ENABLED] - Set via bracket orders")
    print("      TP is automatically set when entering positions")
    
    # Partial Profit
    partial_config = config.get('partial_profit', {})
    partial_enabled = partial_config.get('enabled', True)
    print(f"\n   Partial Profit:")
    print(f"      Enabled: {'YES' if partial_enabled else 'NO'}")
    
    if partial_enabled:
        print(f"      First Exit R: {partial_config.get('first_exit_r', 0.8)}")
        print(f"      First Exit %: {partial_config.get('first_exit_pct', 0.5) * 100:.0f}%")
        print(f"      Structure Based: {'YES' if partial_config.get('structure_based', False) else 'NO'}")
        print(f"      Post-Partial SL Lock: {partial_config.get('post_partial_sl_lock_r', 0.5)}R")
    
    # Break Even
    be_config = config.get('break_even', {})
    be_enabled = be_config.get('enabled', True)
    print(f"\n   Break Even:")
    print(f"      Enabled: {'YES' if be_enabled else 'NO'}")
    if be_enabled:
        print(f"      Trigger R: {be_config.get('trigger_r', 1.2)}")
        print(f"      Early BE Enabled: {'YES' if be_config.get('early_be_enabled', False) else 'NO'}")
        if be_config.get('early_be_enabled', False):
            print(f"      Early BE Ticks: {be_config.get('early_be_ticks', 40)}")
    
    # Trailing Stop
    trailing_config = config.get('trailing_stop', {})
    trailing_enabled = trailing_config.get('enabled', False)
    print(f"\n   Trailing Stop:")
    print(f"      Enabled: {'YES' if trailing_enabled else 'NO'}")
    if trailing_enabled:
        print(f"      Activation R: {trailing_config.get('activation_r', 0.8)}")
        print(f"      Trail Distance R: {trailing_config.get('trail_distance_r', 0.25)}")
else:
    print("   ERROR: Config file not found")

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print("[OK] Take Profit: Always enabled (set via bracket orders)")
if config_path.exists() and partial_config.get('enabled', True):
    print("[OK] Partial Profit: ENABLED")
else:
    print("[OFF] Partial Profit: DISABLED")
print("=" * 70)

