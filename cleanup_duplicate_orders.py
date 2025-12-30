#!/usr/bin/env python3
"""
Emergency script to cancel all duplicate stop orders
"""
import json
import sys
from broker.topstepx_client import TopstepXClient

def load_credentials(path: str = 'credentials.json') -> dict:
    with open(path, 'r') as f:
        return json.load(f)

def main():
    print("=" * 60)
    print("EMERGENCY: Cleanup Duplicate Stop Orders")
    print("=" * 60)
    
    # Load credentials
    creds = load_credentials()
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key']
    )
    
    # Authenticate
    print("\n1. Authenticating...")
    if not client.authenticate():
        print("   FAIL: Authentication failed")
        return False
    print("   OK: Authenticated")
    
    # Set account
    account_id = creds.get('account_id')
    if account_id:
        client.set_account(account_id)
        print(f"   OK: Account set to {account_id}")
    else:
        print("   FAIL: No account_id in credentials")
        return False
    
    # Find contract
    print("\n2. Finding MGC contract...")
    contract = client.find_mgc_contract()
    if not contract:
        print("   FAIL: Could not find MGC contract")
        return False
    print(f"   OK: Found contract (ID: {contract.id})")
    
    # Get all open orders
    print("\n3. Fetching open orders...")
    try:
        open_orders = client.get_open_orders()
        print(f"   Found {len(open_orders)} total open orders")
    except Exception as e:
        print(f"   FAIL: Could not fetch orders: {e}")
        return False
    
    # Find all stop orders for MGC
    stop_orders = []
    for order in open_orders:
        if order.get('contractId') == contract.id and order.get('type') == 4:  # STOP order
            stop_orders.append(order)
    
    if not stop_orders:
        print("\n   OK: No stop orders found. Nothing to clean up.")
        return True
    
    print(f"\n4. Found {len(stop_orders)} stop orders for MGC:")
    for i, order in enumerate(stop_orders, 1):
        order_id = order.get('id')
        stop_price = order.get('stopPrice', 0)
        size = order.get('size', 0)
        side = "SELL" if order.get('side') == 1 else "BUY"
        print(f"   {i}. Order ID: {order_id}, {side} {abs(size)} @ ${stop_price:.2f}")
    
    # Get current position to determine which stop to keep
    print("\n5. Checking current position...")
    positions = client.get_positions()
    mgc_position = None
    for pos in positions:
        if pos.contract_id == contract.id:
            mgc_position = pos
            break
    
    if mgc_position:
        side = 'long' if mgc_position.size > 0 else 'short'
        print(f"   Position: {side.upper()} {abs(mgc_position.size)} contracts @ ${mgc_position.average_price:.2f}")
        
        # Find the stop order that matches the position (should be ASK for long, BID for short)
        correct_side = 1 if side == 'long' else 0  # ASK=1 for long, BID=0 for short
        correct_stop = None
        for order in stop_orders:
            if order.get('side') == correct_side:
                if correct_stop is None:
                    correct_stop = order
                elif abs(order.get('stopPrice', 0) - mgc_position.average_price) < abs(correct_stop.get('stopPrice', 0) - mgc_position.average_price):
                    # Prefer stop closest to entry (break-even)
                    correct_stop = order
        
        if correct_stop:
            print(f"\n6. Keeping stop order: ID {correct_stop.get('id')} @ ${correct_stop.get('stopPrice', 0):.2f}")
            stop_orders.remove(correct_stop)
        else:
            print(f"\n6. WARNING: Could not identify correct stop order. Will cancel all.")
    else:
        print("   No position found. Will cancel all stop orders.")
    
    # Cancel duplicate stop orders
    if stop_orders:
        print(f"\n7. Cancelling {len(stop_orders)} duplicate stop orders...")
        cancelled = 0
        failed = 0
        for order in stop_orders:
            order_id = order.get('id')
            try:
                result = client.cancel_order(order_id)
                if result.get('success'):
                    print(f"   OK: Cancelled order #{order_id}")
                    cancelled += 1
                else:
                    error = result.get('errorMessage', 'Unknown error')
                    print(f"   FAIL: Could not cancel order #{order_id}: {error}")
                    failed += 1
            except Exception as e:
                print(f"   ERROR: Exception cancelling order #{order_id}: {e}")
                failed += 1
        
        print(f"\n8. Summary:")
        print(f"   Cancelled: {cancelled}")
        print(f"   Failed: {failed}")
        return failed == 0
    else:
        print("\n7. No duplicate orders to cancel.")
        return True

if __name__ == '__main__':
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nCancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

