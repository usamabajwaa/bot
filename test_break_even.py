#!/usr/bin/env python3
"""
Test script to manually trigger break-even stop order modification
"""
import json
import sys
from broker.topstepx_client import TopstepXClient, OrderSide, OrderType
from pathlib import Path

def load_credentials(path: str = 'credentials.json') -> dict:
    with open(path, 'r') as f:
        return json.load(f)

def main():
    print("=" * 60)
    print("TEST: Break-Even Stop Order Modification")
    print("=" * 60)
    
    # Load credentials
    creds = load_credentials()
    # TopstepXClient needs username and api_key
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key']
    )
    
    # Authenticate
    print("\n1. Authenticating...")
    auth_success = client.authenticate()
    if not auth_success:
        print(f"   FAIL: Authentication failed")
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
    
    # Get current position
    print("\n3. Checking current position...")
    positions = client.get_positions()
    mgc_position = None
    for pos in positions:
        if pos.contract_id == contract.id:
            mgc_position = pos
            break
    
    if not mgc_position:
        print("   FAIL: No open position found")
        return False
    
    side = 'long' if mgc_position.size > 0 else 'short'
    entry_price = mgc_position.average_price
    print(f"   OK: Position found: {side.upper()} {abs(mgc_position.size)} contracts @ ${entry_price:.2f}")
    
    # Get current stop orders
    print("\n4. Finding current stop orders...")
    open_orders = client.get_open_orders()
    stop_orders = []
    for order in open_orders:
        if order.get('contractId') == contract.id and order.get('type') == 4:  # STOP order
            stop_orders.append(order)
            print(f"   Found stop order: ID {order.get('id')} at ${order.get('stopPrice', 0):.2f}")
    
    if not stop_orders:
        print("   WARNING: No stop orders found. Cannot test modification.")
        return False
    
    # Use the first stop order
    stop_order = stop_orders[0]
    stop_order_id = stop_order.get('id')
    current_stop_price = stop_order.get('stopPrice', 0)
    
    print(f"\n5. Testing break-even modification...")
    print(f"   Current stop: ${current_stop_price:.2f}")
    print(f"   Entry price: ${entry_price:.2f}")
    print(f"   Target (BE): ${entry_price:.2f}")
    
    # Validate stop price direction
    # For testing, we'll use entry price as BE, but validate it's reasonable
    # MGC tick size is 0.1, so we need to round to nearest tick
    tick_size = 0.1
    be_stop_price = round(entry_price / tick_size) * tick_size
    print(f"   Entry price: ${entry_price:.2f}")
    print(f"   Break-even (rounded to tick): ${be_stop_price:.2f}")
    
    # Note: In live trading, the system validates against current price
    # For this test, we'll proceed with entry price
    print(f"\n6. Ready to modify stop order...")
    print(f"   Stop Order ID: {stop_order_id}")
    print(f"   Current stop: ${current_stop_price:.2f}")
    print(f"   New stop (BE): ${be_stop_price:.2f}")
    print(f"   NOTE: This will move stop to break-even price")
    
    # Modify the stop order
    print(f"\n7. Modifying stop order...")
    result = client.modify_order(
        order_id=stop_order_id,
        stop_price=be_stop_price
    )
    
    if result.get('success'):
        print(f"   OK: Stop order modified successfully!")
        print(f"   Stop order #{stop_order_id} now at ${be_stop_price:.2f}")
        
        # Verify by getting updated orders
        print(f"\n8. Verifying modification...")
        open_orders = client.get_open_orders()
        for order in open_orders:
            if order.get('id') == stop_order_id:
                updated_stop = order.get('stopPrice', 0)
                print(f"   Verified: Stop order #{stop_order_id} is now at ${updated_stop:.2f}")
                if abs(updated_stop - be_stop_price) < 0.01:
                    print(f"   SUCCESS: Break-even modification confirmed!")
                    return True
                else:
                    print(f"   WARNING: Stop price mismatch (expected ${be_stop_price:.2f}, got ${updated_stop:.2f})")
                    return False
        print(f"   WARNING: Could not find updated order")
        return False
    else:
        error_msg = result.get('errorMessage', 'Unknown error')
        print(f"   FAIL: Modification failed: {error_msg}")
        return False

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

