#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Setup Practice Account
Configures credentials.json to use the 150k practice account
"""

import json
from pathlib import Path

def main():
    print("=" * 70)
    print("SETUP PRACTICE ACCOUNT (150K)")
    print("=" * 70)
    
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        print("\nERROR: credentials.json not found!")
        print("Please create credentials.json first with your username and api_key")
        return 1
    
    # Read current credentials
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    print("\nCurrent credentials:")
    print(f"   Username: {creds.get('username', 'NOT SET')}")
    print(f"   Account ID: {creds.get('account_id', 'NOT SET')}")
    print(f"   Account Suffix: {creds.get('account_suffix', 'NOT SET')}")
    
    # Practice account ID from test results
    practice_account_id = 16095361
    
    print(f"\nSetting account_id to practice account: {practice_account_id}")
    
    # Update credentials
    creds['account_id'] = practice_account_id
    # Remove account_suffix if it exists (account_id takes precedence)
    if 'account_suffix' in creds:
        del creds['account_suffix']
    
    # Backup original
    backup_path = Path('credentials.json.backup')
    if not backup_path.exists():
        with open('credentials.json', 'r') as f:
            backup_content = f.read()
        with open('credentials.json.backup', 'w') as f:
            f.write(backup_content)
        print("   Created backup: credentials.json.backup")
    
    # Write updated credentials
    with open('credentials.json', 'w') as f:
        json.dump(creds, f, indent=2)
    
    print(f"\nOK: Updated credentials.json")
    print(f"   Account ID: {practice_account_id} (150K Practice Account)")
    
    # Verify by testing connection
    print("\nVerifying account...")
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
            target_account = next((a for a in accounts if a.id == practice_account_id), None)
            
            if target_account:
                print(f"\nOK: Account verified!")
                print(f"   ID: {target_account.id}")
                print(f"   Name: {target_account.name}")
                print(f"   Balance: ${target_account.balance:,.2f}")
                print(f"   Tradable: {'YES' if target_account.can_trade else 'NO'}")
                
                if target_account.can_trade:
                    print("\n" + "=" * 70)
                    print("READY FOR LIVE TRADING")
                    print("=" * 70)
                    print("\nYou can now run live trading with:")
                    print("   python live_trader.py --config config_production.json")
                else:
                    print("\nWARNING: Account is marked as NOT TRADABLE")
                    print("   You may need to activate it in Topstep dashboard")
            else:
                print(f"\nWARNING: Account {practice_account_id} not found")
        else:
            print("\nWARNING: Authentication failed - cannot verify account")
            
    except Exception as e:
        print(f"\nWARNING: Could not verify account: {e}")
        print("   But credentials.json has been updated")
    
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())

