#!/usr/bin/env python3
"""
Cleanup ALL duplicate orders (both stop and limit/take-profit)
"""
import json
from broker.topstepx_client import TopstepXClient

def load_credentials(path: str = 'credentials.json') -> dict:
    with open(path, 'r') as f:
        return json.load(f)

def main():
    print("=" * 60)
    print("CLEANUP: All Duplicate Orders")
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
    
    # Get position
    positions = client.get_positions()
    mgc_position = None
    for pos in positions:
        if pos.contract_id == contract.id:
            mgc_position = pos
            break
    
    if mgc_position:
        side = 'long' if mgc_position.size > 0 else 'short'
        print(f"Position: {side.upper()} {abs(mgc_position.size)} @ ${mgc_position.average_price:.2f}")
    else:
        print("No position found")
        return
    
    # Get all orders
    open_orders = client.get_open_orders()
    print(f"\nTotal open orders: {len(open_orders)}")
    
    mgc_orders = []
    for order in open_orders:
        if order.get('contractId') == contract.id:
            mgc_orders.append(order)
    
    print(f"MGC orders: {len(mgc_orders)}")
    
    # Separate by type
    stop_orders = []
    limit_orders = []
    
    for order in mgc_orders:
        order_type = order.get('type', 0)
        if order_type == 4:  # STOP
            stop_orders.append(order)
        elif order_type == 1:  # LIMIT (take-profit)
            limit_orders.append(order)
    
    print(f"\nSTOP ORDERS: {len(stop_orders)}")
    print(f"LIMIT ORDERS (TP): {len(limit_orders)}")
    
    # For LONG position:
    # - Stop should be SELL (side=1) below entry
    # - TP should be SELL (side=1) above entry
    
    correct_side = 1 if side == 'long' else 0  # ASK=1 for long, BID=0 for short
    
    # Find correct stop order (closest to entry, correct side)
    correct_stop = None
    if stop_orders:
        for order in stop_orders:
            if order.get('side') == correct_side:
                if correct_stop is None:
                    correct_stop = order
                else:
                    # Prefer stop closest to entry
                    stop_price = order.get('stopPrice', 0)
                    current_stop_price = correct_stop.get('stopPrice', 0)
                    if side == 'long':
                        # For long, stop should be below entry - prefer higher stop (closer to entry)
                        if stop_price > current_stop_price:
                            correct_stop = order
                    else:
                        # For short, stop should be above entry - prefer lower stop (closer to entry)
                        if stop_price < current_stop_price:
                            correct_stop = order
    
    # Find correct TP order (closest to expected TP, correct side)
    correct_tp = None
    if limit_orders:
        for order in limit_orders:
            if order.get('side') == correct_side:
                if correct_tp is None:
                    correct_tp = order
                else:
                    # Prefer TP with largest size (most likely the correct one)
                    if order.get('size', 0) > correct_tp.get('size', 0):
                        correct_tp = order
    
    # Cancel duplicates
    cancelled_stops = 0
    cancelled_tps = 0
    
    print(f"\n{'='*60}")
    print("CLEANUP PLAN:")
    print(f"{'='*60}")
    
    if correct_stop:
        print(f"KEEP Stop Order: ID {correct_stop.get('id')} @ ${correct_stop.get('stopPrice', 0):.2f}")
        for order in stop_orders:
            if order.get('id') != correct_stop.get('id'):
                print(f"  CANCEL Stop: ID {order.get('id')} @ ${order.get('stopPrice', 0):.2f}")
    else:
        print("KEEP: No stop orders to keep")
        for order in stop_orders:
            print(f"  CANCEL Stop: ID {order.get('id')} @ ${order.get('stopPrice', 0):.2f}")
    
    if correct_tp:
        print(f"KEEP TP Order: ID {correct_tp.get('id')} @ ${correct_tp.get('limitPrice', 0):.2f} ({abs(correct_tp.get('size', 0))} contracts)")
        for order in limit_orders:
            if order.get('id') != correct_tp.get('id'):
                print(f"  CANCEL TP: ID {order.get('id')} @ ${order.get('limitPrice', 0):.2f} ({abs(order.get('size', 0))} contracts)")
    else:
        print("KEEP: No TP orders to keep")
        for order in limit_orders:
            print(f"  CANCEL TP: ID {order.get('id')} @ ${order.get('limitPrice', 0):.2f}")
    
    print(f"\n{'='*60}")
    response = input("Proceed with cleanup? (yes/no): ")
    
    if response.lower() != 'yes':
        print("Cancelled")
        return
    
    # Cancel duplicate stops
    for order in stop_orders:
        if correct_stop and order.get('id') == correct_stop.get('id'):
            continue
        try:
            result = client.cancel_order(order.get('id'))
            if result.get('success'):
                print(f"  OK: Cancelled stop order #{order.get('id')}")
                cancelled_stops += 1
            else:
                print(f"  FAIL: Could not cancel stop #{order.get('id')}: {result.get('errorMessage')}")
        except Exception as e:
            print(f"  ERROR: Exception cancelling stop #{order.get('id')}: {e}")
    
    # Cancel duplicate TPs
    for order in limit_orders:
        if correct_tp and order.get('id') == correct_tp.get('id'):
            continue
        try:
            result = client.cancel_order(order.get('id'))
            if result.get('success'):
                print(f"  OK: Cancelled TP order #{order.get('id')}")
                cancelled_tps += 1
            else:
                print(f"  FAIL: Could not cancel TP #{order.get('id')}: {result.get('errorMessage')}")
        except Exception as e:
            print(f"  ERROR: Exception cancelling TP #{order.get('id')}: {e}")
    
    print(f"\n{'='*60}")
    print("SUMMARY:")
    print(f"  Cancelled Stop Orders: {cancelled_stops}")
    print(f"  Cancelled TP Orders: {cancelled_tps}")
    print(f"  Total Cancelled: {cancelled_stops + cancelled_tps}")
    print(f"{'='*60}")

if __name__ == '__main__':
    main()

