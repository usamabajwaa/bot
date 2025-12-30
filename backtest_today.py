#!/usr/bin/env python3
"""
Backtest today's data only to compare with full backtest results.
This helps identify if current market conditions (low volume, end of year) are causing issues.
"""

import json
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
import argparse
import sys

from backtest import BacktestEngine


def filter_today_data(df: pd.DataFrame, date: datetime.date = None) -> pd.DataFrame:
    """Filter dataframe to only include today's data."""
    if date is None:
        # Use UTC date for today
        date = datetime.now(timezone.utc).date()
    
    # Ensure timestamp is timezone-aware
    if df['timestamp'].dtype == 'object' or df['timestamp'].dt.tz is None:
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    
    # Filter to today's date (in UTC)
    today_data = df[df['timestamp'].dt.date == date].copy()
    
    return today_data


def filter_last_n_days(df: pd.DataFrame, n_days: int = 2) -> pd.DataFrame:
    """Filter dataframe to only include the last N days of data."""
    # Ensure timestamp is timezone-aware
    if df['timestamp'].dtype == 'object' or df['timestamp'].dt.tz is None:
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    
    # Get unique dates and sort
    unique_dates = sorted(df['timestamp'].dt.date.unique())
    
    if len(unique_dates) < n_days:
        print(f"Warning: Only {len(unique_dates)} days available, using all available data")
        n_days = len(unique_dates)
    
    # Get last N dates
    last_n_dates = unique_dates[-n_days:]
    
    # Filter to those dates
    filtered_data = df[df['timestamp'].dt.date.isin(last_n_dates)].copy()
    
    return filtered_data, last_n_dates


def main():
    parser = argparse.ArgumentParser(description='Backtest today\'s data only')
    parser.add_argument('--config', type=str, default='config.json',
                        help='Path to config file')
    parser.add_argument('--data', type=str, default='data.csv',
                        help='Path to data file')
    parser.add_argument('--date', type=str, default=None,
                        help='Date to backtest (YYYY-MM-DD), defaults to today')
    parser.add_argument('--last-n-days', type=int, default=None,
                        help='Backtest last N days instead of single date')
    parser.add_argument('--output', type=str, default='today_backtest_output',
                        help='Output directory')
    
    args = parser.parse_args()
    
    # Parse date if provided
    target_date = None
    if args.date:
        target_date = datetime.strptime(args.date, '%Y-%m-%d').date()
    else:
        target_date = datetime.now(timezone.utc).date()
    
    # Load full data
    print(f"Loading data from {args.data}...")
    engine = BacktestEngine(config_path=args.config)
    full_df = engine.load_data(args.data)
    
    print(f"Full dataset: {len(full_df)} bars")
    print(f"  Date range: {full_df['timestamp'].min()} to {full_df['timestamp'].max()}")
    
    # Check if we're doing last N days or single date
    if args.last_n_days:
        print("=" * 70)
        print(f"BACKTESTING LAST {args.last_n_days} DAYS")
        print("=" * 70)
        print(f"Data file: {args.data}")
        print(f"Config file: {args.config}")
        print("=" * 70)
        print()
        
        # Filter to last N days
        today_df, date_range = filter_last_n_days(full_df, args.last_n_days)
        target_date = f"{date_range[0]} to {date_range[-1]}"
        
        if len(today_df) == 0:
            print("ERROR: No data found for last N days")
            sys.exit(1)
        
        print(f"\nLast {args.last_n_days} days data: {len(today_df)} bars")
        print(f"  Date range: {date_range[0]} to {date_range[-1]}")
        print(f"  Time range: {today_df['timestamp'].min()} to {today_df['timestamp'].max()}")
        print(f"  Price range: ${today_df['low'].min():.2f} to ${today_df['high'].max():.2f}")
        
        if 'volume' in today_df.columns:
            avg_volume = today_df['volume'].mean()
            print(f"  Average volume: {avg_volume:.0f}")
    else:
        print("=" * 70)
        print("BACKTESTING TODAY'S DATA ONLY")
        print("=" * 70)
        print(f"Target date: {target_date}")
        print(f"Data file: {args.data}")
        print(f"Config file: {args.config}")
        print("=" * 70)
        print()
        
        # Filter to today
        today_df = filter_today_data(full_df, target_date)
        
        if len(today_df) == 0:
            print(f"\nERROR: No data found for {target_date}")
            print(f"Available dates in dataset:")
            available_dates = sorted(full_df['timestamp'].dt.date.unique())
            print(f"  Latest date: {available_dates[-1]}")
            print(f"  Last 10 dates:")
            for d in available_dates[-10:]:
                print(f"    - {d}")
            
            # Use latest available date instead
            latest_date = available_dates[-1]
            print(f"\nUsing latest available date: {latest_date}")
            today_df = filter_today_data(full_df, latest_date)
            target_date = latest_date
            
            if len(today_df) == 0:
                print("ERROR: Still no data found after using latest date")
                sys.exit(1)
        
        print(f"\nToday's data: {len(today_df)} bars")
        print(f"  Time range: {today_df['timestamp'].min()} to {today_df['timestamp'].max()}")
        print(f"  Price range: ${today_df['low'].min():.2f} to ${today_df['high'].max():.2f}")
        
        if 'volume' in today_df.columns:
            avg_volume = today_df['volume'].mean()
            print(f"  Average volume: {avg_volume:.0f}")
    
    print()
    print("=" * 70)
    print("RUNNING BACKTEST...")
    print("=" * 70)
    print()
    
    # Create new engine with filtered data
    engine_today = BacktestEngine(config_path=args.config)
    engine_today.data = today_df  # Set filtered data directly
    
    # Load blackout dates if exists
    blackout_path = 'blackout_dates.csv'
    if Path(blackout_path).exists():
        engine_today.load_blackout_dates(blackout_path)
    
    # Run backtest
    results = engine_today.run()
    
    # Generate reports
    output_dir = Path(args.output)
    output_dir.mkdir(exist_ok=True)
    
    metrics = engine_today.generate_reports(output_dir=output_dir)
    
    # Print results
    print("\n" + "=" * 70)
    if args.last_n_days:
        print(f"LAST {args.last_n_days} DAYS BACKTEST RESULTS")
    else:
        print("TODAY'S BACKTEST RESULTS")
    print("=" * 70)
    print(f"Date: {target_date}")
    print(f"Total Trades: {metrics.get('total_trades', 0)}")
    print(f"Winning Trades: {metrics.get('winning_trades', 0)}")
    print(f"Losing Trades: {metrics.get('losing_trades', 0)}")
    print(f"Win Rate: {metrics.get('win_rate', 0):.1%}")
    print(f"Total P&L: ${metrics.get('total_pnl', 0):.2f}")
    print(f"Avg P&L/Trade: ${metrics.get('avg_pnl_per_trade', 0):.2f}")
    print(f"Avg Win: ${metrics.get('avg_win', 0):.2f}")
    print(f"Avg Loss: ${metrics.get('avg_loss', 0):.2f}")
    print(f"Profit Factor: {metrics.get('profit_factor', 0):.2f}")
    print(f"Max Drawdown: ${metrics.get('max_drawdown', 0):.2f}")
    print("=" * 70)
    
    if 'session_breakdown' in metrics:
        print("\nSession Breakdown:")
        for session, stats in metrics['session_breakdown'].items():
            print(f"  {session}: {stats.get('trades', 0)} trades, "
                  f"${stats.get('pnl', 0):.2f} P&L, "
                  f"{stats.get('win_rate', 0):.1%} win rate")
    
    print(f"\nResults saved to {output_dir}/")
    print()
    
    # Compare with full backtest if results file exists
    full_results_path = Path('test_backtest_output/results.json')
    if full_results_path.exists():
        print("=" * 70)
        print("COMPARISON WITH FULL BACKTEST")
        print("=" * 70)
        with open(full_results_path, 'r') as f:
            full_metrics = json.load(f)
        
        print(f"\nFull Backtest:")
        print(f"  Total Trades: {full_metrics.get('total_trades', 0)}")
        print(f"  Win Rate: {full_metrics.get('win_rate', 0):.1%}")
        print(f"  Total P&L: ${full_metrics.get('total_pnl', 0):.2f}")
        print(f"  Avg P&L/Trade: ${full_metrics.get('avg_pnl_per_trade', 0):.2f}")
        print(f"  Profit Factor: {full_metrics.get('profit_factor', 0):.2f}")
        
        if args.last_n_days:
            print(f"\nLast {args.last_n_days} Days:")
        else:
            print(f"\nToday Only:")
        print(f"  Total Trades: {metrics.get('total_trades', 0)}")
        print(f"  Win Rate: {metrics.get('win_rate', 0):.1%}")
        print(f"  Total P&L: ${metrics.get('total_pnl', 0):.2f}")
        print(f"  Avg P&L/Trade: ${metrics.get('avg_pnl_per_trade', 0):.2f}")
        print(f"  Profit Factor: {metrics.get('profit_factor', 0):.2f}")
        
        if metrics.get('total_trades', 0) > 0:
            trades_per_day_full = full_metrics.get('total_trades', 0) / max(1, len(full_df['timestamp'].dt.date.unique()))
            print(f"\nAnalysis:")
            print(f"  Full backtest avg trades/day: {trades_per_day_full:.1f}")
            print(f"  Today's trades: {metrics.get('total_trades', 0)}")
            
            period_name = f"Last {args.last_n_days} days" if args.last_n_days else "Today"
            avg_trades_per_day = metrics.get('total_trades', 0) / (args.last_n_days if args.last_n_days else 1)
            
            if avg_trades_per_day < trades_per_day_full * 0.5:
                print(f"  WARNING: {period_name} has fewer trades than average (possible low volume)")
            
            if metrics.get('win_rate', 0) < full_metrics.get('win_rate', 0) * 0.8:
                print(f"  WARNING: {period_name}'s win rate is significantly lower than full backtest")
            
            if metrics.get('total_pnl', 0) < 0:
                print(f"  WARNING: {period_name} is losing money - market conditions may be unfavorable")
        
        print("=" * 70)


if __name__ == '__main__':
    main()

