#!/usr/bin/env python3
"""
Find silver contract symbol in Topstep
"""
import json
from broker.topstepx_client import TopstepXClient

def load_credentials(path: str = 'credentials.json') -> dict:
    with open(path, 'r') as f:
        return json.load(f)

def main():
    creds = load_credentials()
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key']
    )
    
    if not client.authenticate():
        print("Authentication failed")
        return
    
    print("Fetching available contracts...")
    contracts = client.get_available_contracts(live=False)
    
    print(f"\nFound {len(contracts)} contracts")
    print("\nSearching for Silver contracts...")
    print("=" * 60)
    
    silver_keywords = ['SILVER', 'SI', 'MES', 'SIL']
    silver_contracts = []
    
    for c in contracts:
        name_upper = c.name.upper()
        id_upper = c.id.upper()
        desc_upper = c.description.upper() if c.description else ""
        
        for keyword in silver_keywords:
            if keyword in name_upper or keyword in id_upper or keyword in desc_upper:
                silver_contracts.append(c)
                break
    
    if silver_contracts:
        print(f"\nFound {len(silver_contracts)} Silver contract(s):\n")
        for c in silver_contracts:
            status = "ACTIVE" if c.active else "INACTIVE"
            print(f"  {status}: {c.id}")
            print(f"    Name: {c.name}")
            print(f"    Description: {c.description}")
            print(f"    Tick Size: {c.tick_size}")
            print(f"    Tick Value: ${c.tick_value}")
            print()
    else:
        print("\nNo Silver contracts found. Showing all contracts with 'S' in name/id:")
        for c in contracts:
            if 'S' in c.id.upper() or 'S' in c.name.upper():
                status = "ACTIVE" if c.active else "INACTIVE"
                print(f"  {status}: {c.id} - {c.name} - {c.description}")

if __name__ == '__main__':
    main()

