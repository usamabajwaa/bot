#!/usr/bin/env python3
"""
Check all open orders and their types
"""
import json
from broker.topstepx_client import TopstepXClient

def load_credentials(path: str = 'credentials.json') -> dict:
    with open(path, 'r') as f:
        return json.load(f)

def main():
    print("=" * 60)
    print("CHECKING ALL OPEN ORDERS")
    print("=" * 60)
    
    creds = load_credentials()
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key']
    )
    
    if not client.authenticate():
        print("Authentication failed")
        return
    
    client.set_account(creds.get('account_id'))
    contract = client.find_mgc_contract()
    
    print(f"\nContract: {contract.id}")
    
    open_orders = client.get_open_orders()
    print(f"\nTotal open orders: {len(open_orders)}")
    
    # Order types: 1=LIMIT, 2=MARKET, 3=STOP_LIMIT, 4=STOP, 5=TRAILING_STOP
    order_type_names = {
        1: "LIMIT",
        2: "MARKET", 
        3: "STOP_LIMIT",
        4: "STOP",
        5: "TRAILING_STOP"
    }
    
    mgc_orders = []
    for order in open_orders:
        if order.get('contractId') == contract.id:
            mgc_orders.append(order)
    
    print(f"\nMGC orders: {len(mgc_orders)}")
    print("\n" + "-" * 60)
    
    limit_orders = []
    stop_orders = []
    other_orders = []
    
    for order in mgc_orders:
        order_type = order.get('type', 0)
        order_type_name = order_type_names.get(order_type, f"UNKNOWN({order_type})")
        side = "SELL" if order.get('side') == 1 else "BUY"
        size = order.get('size', 0)
        order_id = order.get('id')
        
        limit_price = order.get('limitPrice', 0)
        stop_price = order.get('stopPrice', 0)
        
        info = {
            'id': order_id,
            'type': order_type_name,
            'side': side,
            'size': size,
            'limit_price': limit_price,
            'stop_price': stop_price
        }
        
        if order_type == 1:  # LIMIT
            limit_orders.append(info)
        elif order_type == 4:  # STOP
            stop_orders.append(info)
        else:
            other_orders.append(info)
    
    print(f"\nLIMIT ORDERS ({len(limit_orders)}):")
    for o in limit_orders:
        print(f"  ID: {o['id']}, {o['side']} {abs(o['size'])} @ ${o['limit_price']:.2f}")
    
    print(f"\nSTOP ORDERS ({len(stop_orders)}):")
    for o in stop_orders:
        print(f"  ID: {o['id']}, {o['side']} {abs(o['size'])} @ ${o['stop_price']:.2f}")
    
    if other_orders:
        print(f"\nOTHER ORDERS ({len(other_orders)}):")
        for o in other_orders:
            print(f"  ID: {o['id']}, Type: {o['type']}, {o['side']} {abs(o['size'])}")
    
    # Check position
    positions = client.get_positions()
    for pos in positions:
        if pos.contract_id == contract.id:
            side = 'long' if pos.size > 0 else 'short'
            print(f"\nCURRENT POSITION: {side.upper()} {abs(pos.size)} @ ${pos.average_price:.2f}")
            break

if __name__ == '__main__':
    main()

