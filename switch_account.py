#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Switch to a different practice account
Lists all available accounts and allows selection
"""

import json
import sys
from pathlib import Path

def main():
    print("=" * 70)
    print("SWITCH PRACTICE ACCOUNT")
    print("=" * 70)
    
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        print("\nERROR: credentials.json not found!")
        return 1
    
    # Read current credentials
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    print("\nCurrent configuration:")
    print(f"   Username: {creds.get('username', 'NOT SET')}")
    print(f"   Account ID: {creds.get('account_id', 'NOT SET')}")
    print(f"   Account Suffix: {creds.get('account_suffix', 'NOT SET')}")
    
    # Connect and get accounts
    print("\nFetching available accounts...")
    try:
        from broker import TopstepXClient
        
        client = TopstepXClient(
            username=creds['username'],
            api_key=creds['api_key'],
            base_url=creds.get('base_url'),
            rtc_url=creds.get('rtc_url')
        )
        
        if not client.authenticate():
            print("ERROR: Authentication failed")
            return 1
        
        accounts = client.get_accounts(only_active=True)
        
        if not accounts:
            print("ERROR: No accounts found")
            return 1
        
        print(f"\nFound {len(accounts)} available account(s):")
        print("\n" + "-" * 70)
        for i, acc in enumerate(accounts, 1):
            status = "TRADABLE" if acc.can_trade else "NOT TRADABLE"
            current = " [CURRENT]" if acc.id == creds.get('account_id') else ""
            print(f"  {i}. [{status}] ID: {acc.id}")
            print(f"     Name: {acc.name}")
            print(f"     Balance: ${acc.balance:,.2f}")
            print(f"     Simulated: {'Yes' if acc.simulated else 'No'}{current}")
            print()
        
        # Filter practice accounts (simulated)
        practice_accounts = [a for a in accounts if a.simulated]
        
        if not practice_accounts:
            print("WARNING: No practice (simulated) accounts found")
            print("Showing all accounts instead...")
            practice_accounts = accounts
        
        if len(practice_accounts) == 1:
            # Only one practice account, switch to it
            selected = practice_accounts[0]
            print(f"\nOnly one practice account found. Switching to: {selected.id}")
        else:
            # Multiple practice accounts - show them
            print(f"\nFound {len(practice_accounts)} practice account(s):")
            for i, acc in enumerate(practice_accounts, 1):
                current = " [CURRENT]" if acc.id == creds.get('account_id') else ""
                print(f"  {i}. ID: {acc.id} - {acc.name} - ${acc.balance:,.2f}{current}")
            
            # Auto-select the other practice account (not the current one)
            current_id = creds.get('account_id')
            other_accounts = [a for a in practice_accounts if a.id != current_id]
            
            if other_accounts:
                selected = other_accounts[0]  # Take the first one that's not current
                print(f"\nAuto-selecting other practice account: {selected.id} ({selected.name})")
            else:
                print("\nNo other practice account found (only current account available)")
                return 0
        
        # Confirm switch
        print(f"\n" + "=" * 70)
        print(f"SWITCHING TO ACCOUNT:")
        print(f"   ID: {selected.id}")
        print(f"   Name: {selected.name}")
        print(f"   Balance: ${selected.balance:,.2f}")
        print(f"   Tradable: {'YES' if selected.can_trade else 'NO'}")
        print("=" * 70)
        
        # Backup current credentials
        backup_path = Path('credentials.json.backup')
        if not backup_path.exists() or True:  # Always backup before switching
            with open('credentials.json', 'r') as f:
                backup_content = f.read()
            with open('credentials.json.backup', 'w') as f:
                f.write(backup_content)
            print("\nCreated backup: credentials.json.backup")
        
        # Update credentials
        creds['account_id'] = selected.id
        # Remove account_suffix if it exists (account_id takes precedence)
        if 'account_suffix' in creds:
            del creds['account_suffix']
        
        # Write updated credentials
        with open('credentials.json', 'w') as f:
            json.dump(creds, f, indent=2)
        
        print(f"\nOK: Updated credentials.json")
        print(f"   New Account ID: {selected.id}")
        
        print("\n" + "=" * 70)
        print("NEXT STEPS")
        print("=" * 70)
        print("\n1. Stop the current live trader (if running):")
        print("   python check_running_instances.py")
        print("   taskkill /F /IM python.exe")
        print("\n2. Start live trader with new account:")
        print("   python live_trader.py --config config_production.json")
        print("\nOr use the restart script:")
        print("   python restart_fresh_live_trader.py")
        print("=" * 70)
        
        return 0
        
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())

