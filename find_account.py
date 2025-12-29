#!/usr/bin/env python3
"""
Find account ending with specified suffix and update credentials.json
"""

import json
import sys
from pathlib import Path

def main():
    print("=" * 60)
    print("Finding account ending with 4256...")
    print("=" * 60)
    
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        print("\nERROR: credentials.json not found!")
        return 1
    
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    from broker.topstepx_client import TopstepXClient
    
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key'],
        base_url=creds.get('base_url'),
        rtc_url=creds.get('rtc_url')
    )
    
    print("\n[1/3] AUTHENTICATING")
    print("-" * 40)
    
    if not client.authenticate():
        print("ERROR: Authentication FAILED")
        return 1
    
    print("OK Authenticated")
    
    print("\n[2/3] FETCHING ACCOUNTS")
    print("-" * 40)
    
    accounts = client.get_accounts(only_active=True)
    
    if not accounts:
        print("ERROR: No accounts found")
        return 1
    
    print(f"OK Found {len(accounts)} account(s):")
    for acc in accounts:
        status = "TRADABLE" if acc.can_trade else "NOT TRADABLE"
        print(f"  [{status}] ID: {acc.id} | Name: {acc.name} | Balance: ${acc.balance:.2f}")
    
    print("\n[3/3] FINDING ACCOUNT ENDING WITH 4256")
    print("-" * 40)
    
    target_suffix = "4256"
    # Try to find by account ID ending with suffix
    matching_accounts = [a for a in accounts if a.can_trade and str(a.id).endswith(target_suffix)]
    
    # If not found, try by account name ending with suffix
    if not matching_accounts:
        matching_accounts = [a for a in accounts if a.can_trade and str(a.name).endswith(target_suffix)]
    
    # If still not found, try any account (ID or name) ending with suffix (even if not tradable)
    if not matching_accounts:
        matching_accounts = [a for a in accounts if str(a.id).endswith(target_suffix) or str(a.name).endswith(target_suffix)]
    
    if not matching_accounts:
        print(f"ERROR: No account found ending with '{target_suffix}'")
        print("\nAvailable accounts:")
        for acc in accounts:
            print(f"  ID: {acc.id} | Name: {acc.name} | Balance: ${acc.balance:.2f} | Tradable: {acc.can_trade}")
        return 1
    
    account = matching_accounts[0]
    print(f"OK Found account: {account.id} ({account.name})")
    print(f"   Balance: ${account.balance:.2f}")
    print(f"   Can Trade: {account.can_trade}")
    
    if not account.can_trade:
        print(f"\nWARNING: Account {account.id} is marked as NOT TRADABLE")
        print("   Proceeding anyway as requested...")
    
    # Update credentials.json
    print("\n[4/4] UPDATING credentials.json")
    print("-" * 40)
    
    # Remove account_suffix and add account_id
    if 'account_suffix' in creds:
        del creds['account_suffix']
    
    creds['account_id'] = account.id
    
    with open('credentials.json', 'w') as f:
        json.dump(creds, f, indent=4)
    
    print(f"OK Updated credentials.json with account_id: {account.id}")
    print("\n" + "=" * 60)
    print("SUCCESS! Account ID updated in credentials.json")
    print("=" * 60)
    
    return 0

if __name__ == '__main__':
    sys.exit(main())

