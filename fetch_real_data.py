#!/usr/bin/env python3
import json
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path

from broker import TopstepXClient


def fetch_mgc_data(days: int = 30, interval_minutes: int = 15, output_file: str = 'data.csv'):
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        print("❌ credentials.json not found")
        return None
    
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key'],
        base_url=creds.get('base_url'),
        rtc_url=creds.get('rtc_url')
    )
    
    print("Authenticating...")
    if not client.authenticate():
        print("❌ Authentication failed")
        return None
    
    print("Finding MGC contract...")
    contract = client.find_mgc_contract()
    if not contract:
        print("❌ MGC contract not found")
        return None
    
    print(f"✓ Found: {contract.id} - {contract.description}")
    
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)
    
    print(f"\nFetching {days} days of {interval_minutes}-minute bars...")
    print(f"  From: {start_time.strftime('%Y-%m-%d')}")
    print(f"  To:   {end_time.strftime('%Y-%m-%d')}")
    
    all_bars = []
    chunk_days = 7
    current_start = start_time
    
    while current_start < end_time:
        current_end = min(current_start + timedelta(days=chunk_days), end_time)
        
        bars = client.get_historical_bars(
            contract_id=contract.id,
            interval=interval_minutes,
            start_time=current_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_time=current_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            count=20000,
            live=False,
            unit=2,
            include_partial=False  # Explicitly exclude partial bars
        )
        
        if bars:
            all_bars.extend(bars)
            print(f"  Fetched {len(bars)} bars for {current_start.strftime('%Y-%m-%d')} to {current_end.strftime('%Y-%m-%d')}")
        
        current_start = current_end
    
    if not all_bars:
        print("❌ No data retrieved")
        return None
    
    df = pd.DataFrame(all_bars)
    
    df = df.rename(columns={
        't': 'timestamp',
        'o': 'open',
        'h': 'high',
        'l': 'low',
        'c': 'close',
        'v': 'volume'
    })
    
    # TopStep API returns UTC timestamps - parse with UTC timezone
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df = df.sort_values('timestamp').drop_duplicates(subset=['timestamp']).reset_index(drop=True)
    
    df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
    
    df.to_csv(output_file, index=False)
    
    print(f"\n✓ Saved {len(df)} bars to {output_file}")
    print(f"  Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")
    print(f"  Price range: ${df['low'].min():.2f} to ${df['high'].max():.2f}")
    
    return df


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Fetch real MGC data from TopstepX')
    parser.add_argument('--days', type=int, default=30, help='Number of days to fetch')
    parser.add_argument('--interval', type=int, default=15, help='Bar interval in minutes')
    parser.add_argument('--output', type=str, default='data.csv', help='Output file')
    
    args = parser.parse_args()
    
    fetch_mgc_data(
        days=args.days,
        interval_minutes=args.interval,
        output_file=args.output
    )


