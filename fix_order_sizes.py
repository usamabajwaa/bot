#!/usr/bin/env python3
"""
Fix order sizes to match current position quantity
"""
import json
from broker.topstepx_client import TopstepXClient

def load_credentials(path: str = 'credentials.json') -> dict:
    with open(path, 'r') as f:
        return json.load(f)

def main():
    print("=" * 60)
    print("FIX: Order Sizes to Match Position")
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
    
    # Get position
    positions = client.get_positions()
    mgc_position = None
    for pos in positions:
        if pos.contract_id == contract.id:
            mgc_position = pos
            break
    
    if not mgc_position:
        print("No position found")
        return
    
    position_qty = abs(mgc_position.size)
    side = 'long' if mgc_position.size > 0 else 'short'
    print(f"\nPosition: {side.upper()} {position_qty} contracts @ ${mgc_position.average_price:.2f}")
    
    # Get all orders
    open_orders = client.get_open_orders()
    mgc_orders = []
    for order in open_orders:
        if order.get('contractId') == contract.id:
            mgc_orders.append(order)
    
    print(f"\nFound {len(mgc_orders)} open orders for MGC")
    
    # Update orders that don't match position size
    updated = 0
    for order in mgc_orders:
        order_id = order.get('id')
        order_size = abs(order.get('size', 0))
        order_type = order.get('type')
        order_type_name = "STOP" if order_type == 4 else "LIMIT" if order_type == 1 else f"TYPE_{order_type}"
        
        if order_size != position_qty:
            print(f"\nUpdating {order_type_name} order #{order_id}: {order_size} -> {position_qty} contracts")
            
            try:
                if order_type == 4:  # STOP
                    result = client.modify_order(
                        order_id=order_id,
                        size=position_qty,
                        stop_price=order.get('stopPrice')
                    )
                elif order_type == 1:  # LIMIT (TP)
                    result = client.modify_order(
                        order_id=order_id,
                        size=position_qty,
                        limit_price=order.get('limitPrice')
                    )
                else:
                    print(f"  SKIP: Unknown order type {order_type}")
                    continue
                
                if result.get('success'):
                    print(f"  OK: Updated successfully")
                    updated += 1
                else:
                    print(f"  FAIL: {result.get('errorMessage')}")
            except Exception as e:
                print(f"  ERROR: {e}")
        else:
            print(f"{order_type_name} order #{order_id}: Already correct size ({order_size})")
    
    print(f"\n{'='*60}")
    print(f"Updated {updated} order(s) to match position size: {position_qty} contracts")
    print(f"{'='*60}")

if __name__ == '__main__':
    main()

