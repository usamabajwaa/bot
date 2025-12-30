#!/usr/bin/env python3
from live_trader import LiveTrader

trader = LiveTrader()
if trader.connect():
    status = trader.get_status()
    pos = status.get('current_position')
    if pos:
        print("Current Position:")
        print(f"  Side: {pos.get('side')}")
        print(f"  Quantity: {pos.get('quantity')}")
        print(f"  Entry: ${pos.get('entry_price', 0):.2f}")
        print(f"  Stop Loss: ${pos.get('stop_loss', 0):.2f}")
        print(f"  Take Profit: ${pos.get('take_profit', 0):.2f}")
        
        current_price = trader._get_current_price()
        if current_price:
            entry = pos.get('entry_price', 0)
            side = pos.get('side', 'long')
            qty = pos.get('quantity', 0)
            
            if side == 'long':
                pnl = (current_price - entry) * qty
                pnl_ticks = (current_price - entry) / trader.tick_size
            else:
                pnl = (entry - current_price) * qty
                pnl_ticks = (entry - current_price) / trader.tick_size
            
            print(f"  Current Price: ${current_price:.2f}")
            print(f"  P&L: ${pnl:.2f} ({pnl_ticks:.1f} ticks)")
    else:
        print("No position found")
    
    # Check broker positions
    positions = status.get('positions', [])
    if positions:
        print(f"\nBroker Positions: {len(positions)}")
        for p in positions:
            print(f"  - {p}")

