#!/usr/bin/env python3
"""
Fetch missing replay data from Topstep API for 2026 trades.
"""

import json
import pandas as pd
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import pytz
import time

from broker.topstepx_client import TopstepXClient
from strategy import Strategy


def load_trade_journal(journal_path: str = 'trade_journal.jsonl') -> List[Dict]:
    """Load all trades from journal file."""
    trades = []
    if not Path(journal_path).exists():
        print(f"Warning: {journal_path} not found")
        return trades
    
    with open(journal_path, 'r') as f:
        for line in f:
            if line.strip():
                try:
                    trade = json.loads(line)
                    trades.append(trade)
                except json.JSONDecodeError as e:
                    print(f"Error parsing line: {e}")
    
    return trades


def filter_2026_trades(trades: List[Dict]) -> List[Dict]:
    """Filter trades from year 2026."""
    filtered = []
    for trade in trades:
        timestamp_str = trade.get('timestamp', '')
        if '2026' in timestamp_str:
            filtered.append(trade)
    
    return filtered


def find_replay_file(trade: Dict, replay_dir: Path) -> Optional[Path]:
    """Find the replay data file for a given trade."""
    timestamp_str = trade.get('timestamp', '')
    side = trade.get('side', '').lower()
    
    # Parse timestamp - format: "2026-01-01 18:33:45.298047-06:00"
    try:
        # Parse with timezone awareness
        dt = pd.to_datetime(timestamp_str)
        
        # Convert to UTC (replay files are saved in UTC)
        if dt.tz is not None:
            dt_utc = dt.tz_convert('UTC')
        else:
            dt_utc = dt.tz_localize('UTC')
        
        # Get all replay files for the same side
        all_replay_files = list(replay_dir.glob(f'replay_*_{side}.csv'))
        
        if not all_replay_files:
            return None
        
        # Find the closest match by time (within 5 minutes)
        best_match = None
        min_diff = None
        
        for replay_file in all_replay_files:
            try:
                # Extract time from filename: replay_YYYYMMDD_HHMMSS_side.csv
                parts = replay_file.stem.split('_')
                if len(parts) >= 3:
                    # Reconstruct timestamp string with underscore: YYYYMMDD_HHMMSS
                    match_time_str = f"{parts[1]}_{parts[2]}"
                    match_dt = pd.to_datetime(match_time_str, format='%Y%m%d_%H%M%S', utc=True)
                    diff_seconds = abs((match_dt - dt_utc).total_seconds())
                    
                    # Accept matches within 5 minutes (300 seconds)
                    if diff_seconds < 300:
                        if min_diff is None or diff_seconds < min_diff:
                            min_diff = diff_seconds
                            best_match = replay_file
            except Exception as e:
                # Skip files that can't be parsed
                continue
        
        return best_match
        
    except Exception as e:
        print(f"Error finding replay file for trade {trade.get('timestamp')}: {e}")
    
    return None


def fetch_and_save_replay_data(
    trade: Dict,
    client: TopstepXClient,
    contract_id: str,
    strategy: Strategy,
    replay_dir: Path
) -> bool:
    """Fetch historical bar data from Topstep API and save as replay file."""
    try:
        timestamp_str = trade.get('timestamp', '')
        side = trade.get('side', '').lower()
        
        # Parse timestamp
        dt = pd.to_datetime(timestamp_str)
        if dt.tz is not None:
            dt_utc = dt.tz_convert('UTC')
        else:
            dt_utc = dt.tz_localize('UTC')
        
        # Fetch data: need ~500 bars before the trade time
        # 15-minute bars, so 500 bars = ~125 hours = ~5 days
        # Fetch from 6 days before to 1 day after to ensure we have enough data
        start_time = dt_utc - timedelta(days=6)
        end_time = dt_utc + timedelta(days=1)
        
        start_time_str = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_time_str = end_time.strftime('%Y-%m-%dT%H:%M:%SZ')
        
        print(f"  Fetching bars from {start_time_str} to {end_time_str}...")
        
        # Fetch historical bars (15-minute interval, unit=2 means minutes)
        bars = client.get_historical_bars(
            contract_id=contract_id,
            interval=15,
            start_time=start_time_str,
            end_time=end_time_str,
            count=1000,  # Max bars to fetch
            live=False,
            unit=2,  # Minutes
            include_partial=False
        )
        
        if not bars:
            print(f"  ERROR: No bars returned from API")
            return False
        
        print(f"  Fetched {len(bars)} bars from API")
        
        # Debug: print first bar structure
        if bars:
            print(f"  DEBUG: First bar keys: {bars[0].keys() if isinstance(bars[0], dict) else 'Not a dict'}")
            print(f"  DEBUG: First bar sample: {str(bars[0])[:200]}")
        
        # Convert to DataFrame
        df_data = []
        for bar in bars:
            # Handle different possible timestamp field names
            ts = bar.get('timestamp') or bar.get('time') or bar.get('timeStamp') or ''
            if not ts:
                continue
            
            # Handle different possible field names
            open_price = bar.get('open') or bar.get('openPrice') or 0
            high_price = bar.get('high') or bar.get('highPrice') or 0
            low_price = bar.get('low') or bar.get('lowPrice') or 0
            close_price = bar.get('close') or bar.get('closePrice') or 0
            volume_val = bar.get('volume') or bar.get('vol') or 0
            
            df_data.append({
                'timestamp': ts,
                'open': open_price,
                'high': high_price,
                'low': low_price,
                'close': close_price,
                'volume': volume_val
            })
        
        df = pd.DataFrame(df_data)
        
        if len(df) == 0:
            print(f"  ERROR: Empty DataFrame")
            return False
        
        # Convert timestamp to datetime and remove NaT values
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        df = df.dropna(subset=['timestamp'])  # Remove rows with NaT timestamps
        
        if len(df) == 0:
            print(f"  ERROR: No valid timestamps after conversion")
            return False
        
        df = df.sort_values('timestamp').reset_index(drop=True)
        
        # Find the bar closest to trade time
        trade_timestamp = dt_utc
        time_diffs = abs((df['timestamp'] - trade_timestamp).dt.total_seconds())
        closest_idx = time_diffs.idxmin()
        
        # Get last 500 bars up to and including the closest bar (similar to _save_replay_data)
        # But we want bars around the trade, so take bars before and after
        start_idx = max(0, closest_idx - 400)  # 400 bars before
        end_idx = min(len(df), closest_idx + 100)  # 100 bars after
        
        df_subset = df.iloc[start_idx:end_idx].copy()
        
        # But replay files save last 500 bars, so let's take last 500 bars from available data
        if len(df_subset) > 500:
            df_subset = df_subset.iloc[-500:].copy()
        
        # Prepare data with indicators using Strategy
        print(f"  Preparing data with indicators ({len(df_subset)} bars)...")
        df_prepared = strategy.prepare_data(df_subset)
        
        # Save to file
        timestamp_str_file = dt_utc.strftime('%Y%m%d_%H%M%S')
        filename = replay_dir / f"replay_{timestamp_str_file}_{side}.csv"
        df_prepared.to_csv(filename, index=False)
        
        print(f"  SUCCESS: Saved replay data to {filename.name} ({len(df_prepared)} bars)")
        return True
        
    except Exception as e:
        print(f"  ERROR: Failed to fetch/save replay data: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Main function to fetch missing replay data."""
    print("="*60)
    print("FETCHING MISSING REPLAY DATA FOR 2026 TRADES")
    print("="*60)
    
    # Load configuration
    config_path = 'config_production.json'
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    # Initialize strategy
    strategy = Strategy(config)
    
    # Load credentials
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        cred_path = Path('credentials.json.backup')
        if not cred_path.exists():
            print("ERROR: No credentials file found (credentials.json or credentials.json.backup)")
            return 1
    
    with open(cred_path, 'r') as f:
        credentials = json.load(f)
    
    # Initialize Topstep client
    client = TopstepXClient(
        username=credentials['username'],
        api_key=credentials['api_key'],
        base_url=credentials.get('base_url'),
        rtc_url=credentials.get('rtc_url')
    )
    
    print("\n[1/5] AUTHENTICATING")
    print("-" * 40)
    if not client.authenticate():
        print("ERROR: Authentication FAILED")
        return 1
    print("OK Authenticated")
    
    print("\n[2/5] FINDING MGC CONTRACT")
    print("-" * 40)
    contract = client.find_mgc_contract()
    if not contract:
        print("ERROR: MGC contract not found")
        return 1
    print(f"OK Found contract: {contract.id} - {contract.description}")
    contract_id = contract.id
    
    # Load trades
    print("\n[3/5] LOADING 2026 TRADES")
    print("-" * 40)
    all_trades = load_trade_journal('trade_journal.jsonl')
    trades_2026 = filter_2026_trades(all_trades)
    print(f"Found {len(trades_2026)} trades from 2026")
    
    # Find missing replay files
    print("\n[4/5] IDENTIFYING MISSING REPLAY FILES")
    print("-" * 40)
    replay_dir = Path('replay_data')
    missing_trades = []
    
    for trade in trades_2026:
        replay_file = find_replay_file(trade, replay_dir)
        if replay_file is None:
            missing_trades.append(trade)
            timestamp = trade.get('timestamp', 'unknown')
            side = trade.get('side', 'unknown')
            print(f"  MISSING: {timestamp} {side}")
    
    if not missing_trades:
        print("  All replay files exist! Nothing to fetch.")
        return 0
    
    print(f"\nFound {len(missing_trades)} trades with missing replay files")
    
    # Fetch missing data
    print("\n[5/5] FETCHING MISSING REPLAY DATA")
    print("-" * 40)
    
    success_count = 0
    for i, trade in enumerate(missing_trades, 1):
        timestamp = trade.get('timestamp', 'unknown')
        side = trade.get('side', 'unknown')
        print(f"\n[{i}/{len(missing_trades)}] Fetching data for: {timestamp} {side}")
        
        if fetch_and_save_replay_data(trade, client, contract_id, strategy, replay_dir):
            success_count += 1
        
        # Rate limiting: wait a bit between requests
        if i < len(missing_trades):
            time.sleep(1)
    
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Missing trades: {len(missing_trades)}")
    print(f"Successfully fetched: {success_count}")
    print(f"Failed: {len(missing_trades) - success_count}")
    
    if success_count > 0:
        print(f"\nRe-run replay_2026_trades.py to replay all trades with the new data.")
    
    return 0


if __name__ == '__main__':
    exit(main())

