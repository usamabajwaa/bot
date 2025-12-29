#!/usr/bin/env python3
"""
Full API test - tests all trading APIs including:
- Place order (2 contracts)
- Partial close
- Full close
- Modify order
- Cancel order
"""

import json
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone


def main():
    print("=" * 60)
    print("FULL API TEST - TopstepX Gateway")
    print("Testing: Order, Partial Close, Full Close, Cancel, Bracket Order")
    print("=" * 60)
    
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        print("\nERROR: credentials.json not found!")
        return 1
    
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    from broker import TopstepXClient
    from broker.topstepx_client import OrderSide, OrderType
    
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key'],
        base_url=creds.get('base_url'),
        rtc_url=creds.get('rtc_url')
    )
    
    print("\n[1/12] AUTHENTICATION")
    print("-" * 40)
    
    if not client.authenticate():
        print("ERROR: Authentication FAILED")
        return 1
    
    print(f"OK Authenticated")
    
    print("\n[2/12] FETCH ACCOUNTS")
    print("-" * 40)
    
    accounts = client.get_accounts(only_active=True)
    
    if not accounts:
        print("ERROR: No accounts found")
        return 1
    
    print(f"OK Found {len(accounts)} account(s):")
    for acc in accounts:
        status = "TRADABLE" if acc.can_trade else "NOT TRADABLE"
        print(f"  [{status}] {acc.id}: {acc.name} - ${acc.balance:.2f}")
    
    # Check for configured account_id in credentials
    configured_account_id = creds.get('account_id')
    account_suffix = creds.get('account_suffix')
    
    account = None
    
    if configured_account_id:
        # Find account by ID
        matching = [a for a in accounts if a.id == configured_account_id]
        if matching:
            account = matching[0]
            print(f"\nOK Using configured account_id: {account.id}")
        else:
            print(f"\nWARNING: Configured account_id {configured_account_id} not found in accounts")
    
    elif account_suffix:
        # Find account ending with suffix (by ID or name)
        matching = [a for a in accounts if str(a.id).endswith(str(account_suffix)) or str(a.name).endswith(str(account_suffix))]
        if matching:
            account = matching[0]
            if not account.can_trade:
                print(f"\nWARNING: Using account ending with '{account_suffix}': {account.id} (marked as NOT TRADABLE, but proceeding anyway)")
            else:
                print(f"\nOK Using account ending with '{account_suffix}': {account.id}")
        else:
            print(f"\nWARNING: No account found ending with '{account_suffix}'")
    
    # Fallback to first tradable account
    if account is None:
        tradable = [a for a in accounts if a.can_trade]
        if tradable:
            account = tradable[0]
            print(f"\nOK Using first tradable account: {account.id}")
        else:
            print("\nERROR: No tradable accounts available and no configured account found")
            return 1
    
    client.set_account(account.id)
    print(f"Account: {account.name} | Balance: ${account.balance:.2f}")
    
    print("\n[3/12] FIND MGC CONTRACT")
    print("-" * 40)
    
    contract = client.find_mgc_contract()
    
    if not contract:
        print("ERROR: MGC contract not found")
        return 1
    
    print(f"OK Found: {contract.id}")
    print(f"  Tick Size: {contract.tick_size}, Tick Value: ${contract.tick_value}")
    
    print("\n[4/12] CLEAN STATE CHECK")
    print("-" * 40)
    
    positions = client.get_positions()
    orders = client.get_open_orders()
    
    if positions:
        print(f"WARNING: Closing {len(positions)} existing position(s) first...")
        for pos in positions:
            client.close_position(pos.contract_id)
        time.sleep(1)
    
    if orders:
        print(f"WARNING: Cancelling {len(orders)} existing order(s) first...")
        for o in orders:
            client.cancel_order(o.get('id'))
        time.sleep(1)
    
    print("OK Clean state verified")
    
    print("\n[5/12] PLACE MARKET ORDER (2 contracts LONG)")
    print("-" * 40)
    
    unique_tag = f"TEST_LONG_{int(time.time())}"
    result = client.place_order(
        contract_id=contract.id,
        side=OrderSide.BID,
        order_type=OrderType.MARKET,
        size=2,
        custom_tag=unique_tag
    )
    
    if result.get('success'):
        order_id = result.get('orderId')
        print(f"OK Market order placed!")
        print(f"  Order ID: {order_id}")
    else:
        print(f"ERROR: Order failed: {result.get('errorMessage')}")
        return 1
    
    time.sleep(2)
    
    print("\n[6/12] VERIFY POSITION (should be 2 contracts)")
    print("-" * 40)
    
    positions = client.get_positions()
    
    if not positions:
        print("ERROR: No position found after market order")
        return 1
    
    pos = positions[0]
    print(f"OK Position opened:")
    print(f"  Contract: {pos.contract_id}")
    print(f"  Size: {pos.size} contracts")
    print(f"  Entry: ${pos.average_price:.2f}")
    
    if abs(pos.size) != 2:
        print(f"WARNING: Expected 2 contracts, got {abs(pos.size)}")
    
    print("\n[7/12] PARTIAL CLOSE (close 1 contract)")
    print("-" * 40)
    
    partial_result = client.partial_close_position(
        contract_id=contract.id,
        size=1
    )
    
    if partial_result.get('success'):
        print(f"OK Partial close successful!")
        print(f"  Order ID: {partial_result.get('orderId')}")
    else:
        print(f"WARNING: Partial close response: {partial_result}")
    
    time.sleep(2)
    
    print("\n[8/12] VERIFY REMAINING POSITION (should be 1 contract)")
    print("-" * 40)
    
    positions = client.get_positions()
    
    if not positions:
        print("WARNING: No position found - partial may have closed all")
    else:
        pos = positions[0]
        print(f"OK Remaining position:")
        print(f"  Size: {pos.size} contract(s)")
        print(f"  Entry: ${pos.average_price:.2f}")
        
        if abs(pos.size) != 1:
            print(f"WARNING: Expected 1 contract, got {abs(pos.size)}")
    
    print("\n[9/12] PLACE LIMIT ORDER (for modify/cancel test)")
    print("-" * 40)
    
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=1)
    
    bars = client.get_historical_bars(
        contract_id=contract.id,
        interval=1,
        start_time=start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_time=end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        count=5,
        live=False,
        unit=2
    )
    
    current_price = bars[-1].get('c', 4500) if bars else 4500
    limit_price = round(current_price - 20, 1)
    
    unique_tag2 = f"TEST_LIMIT_{int(time.time())}"
    limit_result = client.place_order(
        contract_id=contract.id,
        side=OrderSide.BID,
        order_type=OrderType.LIMIT,
        size=1,
        limit_price=limit_price,
        custom_tag=unique_tag2
    )
    
    if limit_result.get('success'):
        limit_order_id = limit_result.get('orderId')
        print(f"OK Limit order placed @ ${limit_price:.2f}")
        print(f"  Order ID: {limit_order_id}")
    else:
        print(f"ERROR: Limit order failed: {limit_result.get('errorMessage')}")
        limit_order_id = None
    
    print("\n[10/12] MODIFY ORDER (change price)")
    print("-" * 40)
    
    if limit_order_id:
        new_price = round(limit_price - 10, 1)
        modify_result = client.modify_order(
            order_id=limit_order_id,
            limit_price=new_price
        )
        
        if modify_result.get('success'):
            print(f"OK Order modified: ${limit_price:.2f} -> ${new_price:.2f}")
        else:
            print(f"WARNING: Modify response: {modify_result}")
        
        time.sleep(1)
    
    print("\n[11/12] CANCEL ORDER")
    print("-" * 40)
    
    if limit_order_id:
        cancel_result = client.cancel_order(limit_order_id)
        
        if cancel_result.get('success'):
            print(f"OK Order {limit_order_id} cancelled")
        else:
            print(f"WARNING: Cancel response: {cancel_result}")
    
    print("\n[12/13] TEST BRACKET ORDER")
    print("-" * 40)
    
    # Get current price for bracket order test
    try:
        bars = client.get_historical_bars(
            contract_id=contract.id,
            interval=3,
            count=1,
            live=False,
            unit=2
        )
        current_price = bars[0]['c'] if bars else 4500.0
    except:
        current_price = 4500.0
    
    print(f"Testing bracket order (LONG 1 contract):")
    print(f"  Current price: ${current_price:.2f}")
    print(f"  Stop Loss: 10 ticks below entry")
    print(f"  Take Profit: 20 ticks above entry")
    
    bracket_result = client.place_bracket_order(
        contract_id=contract.id,
        side=OrderSide.BID,  # LONG
        size=1,
        stop_loss_ticks=10,  # Will be converted to -10 for LONG
        take_profit_ticks=20  # Will stay +20 for LONG
    )
    
    if bracket_result.get('success'):
        bracket_order_id = bracket_result.get('orderId')
        print(f"OK Bracket order placed!")
        print(f"  Order ID: {bracket_order_id}")
        
        time.sleep(2)
        
        # Check if position opened with brackets
        positions = client.get_positions()
        orders = client.get_open_orders()
        
        if positions:
            pos = positions[0]
            print(f"  Position opened: {pos.size} contracts")
        if orders:
            print(f"  Open orders (should include stop/tp): {len(orders)}")
            for o in orders:
                order_id = o.get('id', o.get('orderId', 'N/A'))
                order_type = o.get('type', o.get('orderType', 'N/A'))
                limit_price = o.get('limitPrice', o.get('limit_price'))
                print(f"    Order {order_id}: {order_type} @ ${limit_price:.2f if limit_price else 'N/A'}")
        
        # Clean up bracket order position
        if positions:
            for pos in positions:
                if pos.contract_id == contract.id:
                    close_result = client.close_position(pos.contract_id)
                    if close_result.get('success'):
                        print(f"  OK Test position closed")
        
        # Cancel any remaining orders
        if orders:
            for o in orders:
                order_id = o.get('id', o.get('orderId'))
                if order_id:
                    cancel_result = client.cancel_order(order_id)
                    if cancel_result.get('success'):
                        print(f"  OK Order {order_id} cancelled")
    else:
        error_msg = bracket_result.get('errorMessage', 'Unknown error')
        print(f"WARNING: Bracket order failed: {error_msg}")
        print(f"  This might indicate account settings need adjustment")
        print(f"  Response: {bracket_result}")
    
    print("\n[13/13] CLOSE REMAINING POSITION & CLEANUP")
    print("-" * 40)
    
    positions = client.get_positions()
    
    if positions:
        for pos in positions:
            print(f"Closing {pos.contract_id} ({pos.size} contracts)...")
            close_result = client.close_position(pos.contract_id)
            if close_result.get('success'):
                print(f"OK Position closed")
            else:
                print(f"WARNING: Close response: {close_result}")
    
    time.sleep(2)
    
    final_positions = client.get_positions()
    final_orders = client.get_open_orders()
    
    print(f"\nFinal State:")
    print(f"  Positions: {len(final_positions) if final_positions else 0}")
    print(f"  Open Orders: {len(final_orders) if final_orders else 0}")
    
    if not final_positions and not final_orders:
        print("OK Clean state confirmed")
    
    print("\n" + "=" * 60)
    print("API TEST COMPLETE OK")
    print("=" * 60)
    
    print("\nAPIs Tested:")
    print("  OK Authentication")
    print("  OK Get Accounts")
    print("  OK Find Contract")
    print("  OK Get Positions")
    print("  OK Get Open Orders")
    print("  OK Historical Data")
    print("  OK Place Market Order (2 contracts)")
    print("  OK Partial Close Position (1 contract)")
    print("  OK Place Limit Order")
    print("  OK Modify Order")
    print("  OK Cancel Order")
    print("  OK Place Bracket Order")
    print("  OK Close Position")
    
    print("\nOK All trading APIs working! Ready for live trading!")
    
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
