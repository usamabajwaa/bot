#!/usr/bin/env python3
"""
Replay Backtest Mode - Verify live vs backtest signal alignment

This script loads a replay CSV file (saved by live trader) and runs
Strategy.generate_signal() at the same bar_index to verify that:
- Session detection matches
- Zone touches match
- Confirmation logic matches
- RR calculation matches
- Signal decision matches

Usage:
    python replay_backtest.py replay_data/replay_20240101_120000_long.csv
"""

import sys
import pandas as pd
from pathlib import Path
from typing import Optional
import argparse

from strategy import Strategy, SignalType


def replay_signal_check(csv_path: str, config_path: str = 'config.json') -> None:
    """Replay a signal check from live trading data"""
    
    # Load replay data
    df = pd.read_csv(csv_path)
    print(f"Loaded replay data: {len(df)} bars from {csv_path}")
    
    # Ensure timestamps are UTC
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    # Load config and create strategy
    import json
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    strategy = Strategy(config)
    
    # Prepare data (build zones from full dataset)
    print("Preparing data and building zones...")
    df = strategy.prepare_data(df, merge_zones=False)
    
    # Check signal at the last bar (where live trader generated signal)
    bar_index = len(df) - 1
    last_bar = df.iloc[bar_index]
    timestamp = last_bar['timestamp']
    
    print("\n" + "=" * 60)
    print(f"REPLAY SIGNAL CHECK")
    print("=" * 60)
    print(f"Bar Index: {bar_index}")
    print(f"Timestamp: {timestamp}")
    print(f"Price: ${last_bar['close']:.2f}")
    print(f"VWAP: ${last_bar.get('vwap', 0):.2f}")
    print(f"ATR: ${last_bar.get('atr', 0):.2f}")
    
    # Get zone stats
    zone_stats = strategy.zone_manager.get_zone_stats()
    print(f"\nZone Stats:")
    print(f"  Total zones: {zone_stats['total_zones']}")
    print(f"  Active demand: {zone_stats['active_demand']}")
    print(f"  Active supply: {zone_stats['active_supply']}")
    
    # Check touched zones
    bar_low = last_bar['low']
    bar_high = last_bar['high']
    from zones import ZoneType
    
    touched_demand = strategy.zone_manager.find_touched_zones(
        bar_low, bar_high, bar_index, ZoneType.DEMAND
    )
    touched_supply = strategy.zone_manager.find_touched_zones(
        bar_low, bar_high, bar_index, ZoneType.SUPPLY
    )
    
    print(f"\nTouched Zones:")
    print(f"  Demand zones: {len(touched_demand)}")
    for z in touched_demand[:3]:
        print(f"    @ ${z.pivot_price:.2f} (${z.low:.2f}-${z.high:.2f}), conf={z.confidence:.2f}")
    print(f"  Supply zones: {len(touched_supply)}")
    for z in touched_supply[:3]:
        print(f"    @ ${z.pivot_price:.2f} (${z.low:.2f}-${z.high:.2f}), conf={z.confidence:.2f}")
    
    # Generate signal with debug logging
    print("\n" + "=" * 60)
    print("GENERATING SIGNAL (with debug logging)")
    print("=" * 60)
    
    signal = strategy.generate_signal(
        df=df,
        bar_index=bar_index,
        daily_trades=0,
        daily_pnl=0.0,
        in_cooldown=False,
        debug_log=True  # Enable detailed logging
    )
    
    print("\n" + "=" * 60)
    print("SIGNAL RESULT")
    print("=" * 60)
    
    if signal is None:
        print("X NO SIGNAL GENERATED")
        print("\nThis means the strategy would NOT have taken a trade at this bar.")
        print("If live trader took a trade here, there's a mismatch!")
    else:
        print(f"OK SIGNAL GENERATED: {signal.signal_type.value.upper()}")
        print(f"\nEntry Price: ${signal.entry_price:.2f}")
        print(f"Stop Loss:   ${signal.stop_loss:.2f}")
        print(f"Take Profit: ${signal.take_profit:.2f}")
        print(f"Risk:        {signal.risk_ticks:.1f} ticks")
        print(f"Reward:      {signal.reward_ticks:.1f} ticks")
        print(f"R:R Ratio:   {signal.rr_ratio:.2f}")
        print(f"Session:     {signal.session}")
        print(f"Zone:        @ ${signal.zone.pivot_price:.2f} (${signal.zone.low:.2f}-${signal.zone.high:.2f})")
        print(f"Confidence:  {signal.zone_confidence:.2f}")
        print(f"Confirmation: {signal.confirmation_type}")
        
        # Extract expected signal type from filename
        filename = Path(csv_path).stem
        if 'long' in filename.lower():
            expected_type = SignalType.LONG
        elif 'short' in filename.lower():
            expected_type = SignalType.SHORT
        else:
            expected_type = None
        
        if expected_type:
            if signal.signal_type == expected_type:
                print(f"\nOK SIGNAL TYPE MATCHES: {expected_type.value} (as expected from filename)")
            else:
                print(f"\nX SIGNAL TYPE MISMATCH!")
                print(f"   Expected: {expected_type.value}")
                print(f"   Got:      {signal.signal_type.value}")


def main():
    parser = argparse.ArgumentParser(description='Replay backtest - verify live vs backtest signal alignment')
    parser.add_argument('csv_path', type=str, help='Path to replay CSV file')
    parser.add_argument('--config', type=str, default='config.json', help='Config file path (default: config.json)')
    
    args = parser.parse_args()
    
    if not Path(args.csv_path).exists():
        print(f"Error: File not found: {args.csv_path}")
        sys.exit(1)
    
    if not Path(args.config).exists():
        print(f"Error: Config file not found: {args.config}")
        sys.exit(1)
    
    try:
        replay_signal_check(args.csv_path, args.config)
    except Exception as e:
        print(f"\nError during replay: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

