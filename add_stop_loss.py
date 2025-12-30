#!/usr/bin/env python3
"""
Add stop loss order for existing position if missing
"""

import json
import sys
from pathlib import Path

def main():
    print("=" * 70)
    print("ADD STOP LOSS FOR EXISTING POSITION")
    print("=" * 70)
    
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        print("\nERROR: credentials.json not found!")
        return 1
    
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    from broker import TopstepXClient
    from broker.topstepx_client import OrderSide
    
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key'],
        base_url=creds.get('base_url'),
        rtc_url=creds.get('rtc_url')
    )
    
    print("\n[1] Authenticating...")
    if not client.authenticate():
        print("ERROR: Authentication failed")
        return 1
    
    print("OK: Authenticated")
    
    # Set account
    account_id = creds.get('account_id')
    if account_id:
        client.set_account(account_id)
        print(f"OK: Account set to {account_id}")
    else:
        print("ERROR: No account_id in credentials")
        return 1
    
    print("\n[2] Checking positions...")
    positions = client.get_positions()
    
    if not positions:
        print("No open positions found")
        return 0
    
    print(f"Found {len(positions)} position(s):")
    for pos in positions:
        side = "LONG" if pos.size > 0 else "SHORT"
        print(f"  {side} {abs(pos.size)} contracts @ ${pos.average_price:.2f}")
    
    # Get MGC contract from position
    print("\n[3] Finding MGC contract from position...")
    
    # Use the contract_id from the position
    mgc_position = positions[0]  # Use first position
    contract_id = mgc_position.contract_id
    
    print(f"OK: Using contract from position: {contract_id}")
    
    # Get contract details
    contracts = client.get_available_contracts(live=True)
    mgc_contract = next((c for c in contracts if c.id == contract_id), None)
    
    if not mgc_contract:
        # If contract not found in available, use default tick size
        print(f"WARNING: Contract {contract_id} not in available contracts, using default tick size")
        from broker.topstepx_client import Contract
        mgc_contract = Contract(
            id=contract_id,
            name=contract_id,
            description=contract_id,
            tick_size=0.10,  # Default for MGC
            tick_value=1.0,
            active=True,
            symbol_id=''
        )
    
    if not mgc_position:
        print("\nNo MGC position found")
        return 0
    
    side = "LONG" if mgc_position.size > 0 else "SHORT"
    print(f"\n[4] Found MGC position: {side} {abs(mgc_position.size)} @ ${mgc_position.average_price:.2f}")
    
    # Check open orders
    print("\n[5] Checking open orders...")
    open_orders = client.get_open_orders()
    
    stop_orders = [o for o in open_orders if o.get('type') == 4 and o.get('contractId') == mgc_contract.id]
    
    if stop_orders:
        print(f"Found {len(stop_orders)} stop loss order(s):")
        position_size = abs(mgc_position.size)
        valid_stop = None
        
        for order in stop_orders:
            order_size = abs(order.get('size', 0))
            stop_price = order.get('stopPrice', 0)
            print(f"  Stop Order ID: {order.get('id')}, Price: ${stop_price:.2f}, Size: {order_size} contracts")
            
            # Check if order size matches position size
            if order_size == position_size:
                valid_stop = order
                print(f"    -> Matches position size ({position_size} contracts)")
            else:
                print(f"    -> Size mismatch: order={order_size}, position={position_size}")
        
        if valid_stop:
            print(f"\nValid stop loss order found: ID {valid_stop.get('id')} @ ${valid_stop.get('stopPrice', 0):.2f}")
            print("Stop loss order already exists - no action needed")
            return 0
        else:
            print(f"\nWARNING: Stop orders found but none match position size ({position_size} contracts)")
            print("Will place new stop loss order with correct size")
    
    print("No stop loss order found - need to add one")
    
    # Get current market price
    print("\n[5.5] Getting current market price...")
    try:
        quotes = client.get_quotes([mgc_contract.id])
        if quotes and mgc_contract.id in quotes:
            quote = quotes[mgc_contract.id]
            current_price = (quote.bid + quote.ask) / 2
            print(f"OK: Current price: ${current_price:.2f} (Bid: ${quote.bid:.2f}, Ask: ${quote.ask:.2f})")
        else:
            # Fallback: use entry price
            current_price = mgc_position.average_price
            print(f"WARNING: Could not get quote, using entry price: ${current_price:.2f}")
    except Exception as e:
        print(f"WARNING: Could not get quote: {e}, using entry price")
        current_price = mgc_position.average_price
    
    # Calculate stop loss (use min_sl_ticks from config or default 4 ticks minimum)
    entry_price = mgc_position.average_price
    tick_size = 0.10  # MGC tick size
    
    # Load config to get min_sl_ticks
    try:
        with open('config_production.json', 'r') as f:
            config = json.load(f)
        min_sl_ticks = config.get('min_sl_ticks', 4)
        stop_distance_ticks = max(min_sl_ticks, 10)  # Use at least 10 ticks for safety
    except:
        stop_distance_ticks = 10
    
    if mgc_position.size > 0:  # LONG
        # Stop loss should be below entry
        stop_price = entry_price - (stop_distance_ticks * tick_size)
        # Round to nearest tick
        stop_price = round(stop_price / tick_size) * tick_size
        order_side = OrderSide.ASK
        # Ensure stop is below current price
        if stop_price >= current_price:
            stop_price = current_price - tick_size
            stop_price = round(stop_price / tick_size) * tick_size
            print(f"WARNING: Adjusted stop to ${stop_price:.2f} (below current price)")
    else:  # SHORT
        # Stop loss should be above entry
        stop_price = entry_price + (stop_distance_ticks * tick_size)
        # Round to nearest tick
        stop_price = round(stop_price / tick_size) * tick_size
        order_side = OrderSide.BID
        # Ensure stop is above current price
        if stop_price <= current_price:
            stop_price = current_price + tick_size
            stop_price = round(stop_price / tick_size) * tick_size
            print(f"WARNING: Adjusted stop to ${stop_price:.2f} (above current price)")
    
    print(f"\n[6] Placing stop loss order...")
    print(f"   Entry: ${entry_price:.2f}")
    print(f"   Stop Loss: ${stop_price:.2f} ({stop_distance_ticks} ticks)")
    print(f"   Size: {abs(mgc_position.size)} contracts")
    
    result = client.place_stop_order(
        contract_id=mgc_contract.id,
        side=order_side,
        size=abs(mgc_position.size),
        stop_price=stop_price
    )
    
    if result.get('success'):
        print(f"\nOK: Stop loss order placed successfully!")
        print(f"   Order ID: {result.get('orderId')}")
        print(f"   Stop Price: ${stop_price:.2f}")
    else:
        print(f"\nERROR: Failed to place stop loss order")
        print(f"   Error: {result.get('errorMessage', 'Unknown error')}")
        return 1
    
    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

