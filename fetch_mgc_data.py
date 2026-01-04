#!/usr/bin/env python3
"""
Fetch MGC historical bar data from TopStep API for the past 3 calendar days
and save it as CSV matching backtest_mgc_data.csv format.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd

from broker.topstepx_client import TopstepXClient


def load_credentials() -> dict:
    """Load credentials from credentials.json or credentials.json.backup"""
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        cred_path = Path('credentials.json.backup')
        if not cred_path.exists():
            print("ERROR: No credentials file found (credentials.json or credentials.json.backup)")
            sys.exit(1)
    
    with open(cred_path, 'r') as f:
        return json.load(f)


def main():
    """Main function to fetch MGC data for past 3 days"""
    print("=" * 60)
    print("FETCHING MGC DATA - PAST 3 CALENDAR DAYS")
    print("=" * 60)
    
    # Load credentials
    print("\n[1/5] LOADING CREDENTIALS")
    print("-" * 40)
    try:
        credentials = load_credentials()
        print("OK Credentials loaded")
    except Exception as e:
        print(f"ERROR: Failed to load credentials: {e}")
        return 1
    
    # Initialize Topstep client
    print("\n[2/5] INITIALIZING CLIENT")
    print("-" * 40)
    try:
        client = TopstepXClient(
            username=credentials['username'],
            api_key=credentials['api_key'],
            base_url=credentials.get('base_url'),
            rtc_url=credentials.get('rtc_url')
        )
        print("OK Client initialized")
    except Exception as e:
        print(f"ERROR: Failed to initialize client: {e}")
        return 1
    
    # Authenticate
    print("\n[3/5] AUTHENTICATING")
    print("-" * 40)
    if not client.authenticate():
        print("ERROR: Authentication FAILED")
        return 1
    print("OK Authenticated")
    
    # Find MGC contract
    print("\n[4/5] FINDING MGC CONTRACT")
    print("-" * 40)
    contract = client.find_mgc_contract()
    if not contract:
        print("ERROR: MGC contract not found")
        return 1
    print(f"OK Found contract: {contract.id} - {contract.description}")
    contract_id = contract.id
    
    # Calculate date range (past 3 calendar days)
    print("\n[5/5] FETCHING HISTORICAL DATA")
    print("-" * 40)
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(days=3)
    
    start_time_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_time_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    print(f"Date range: {start_time_str} to {end_time_str}")
    print(f"Bar interval: 3 minutes")
    
    # Calculate expected number of bars (3 days * 24 hours * 20 bars/hour = 1440 bars)
    # Use 1500 to be safe
    expected_bars = 1500
    
    print(f"Fetching up to {expected_bars} bars...")
    
    # Fetch historical bars
    bars = client.get_historical_bars(
        contract_id=contract_id,
        interval=3,  # 3-minute bars
        start_time=start_time_str,
        end_time=end_time_str,
        count=expected_bars,
        live=False,
        unit=2,  # Minutes
        include_partial=False
    )
    
    if not bars:
        print("ERROR: No bars returned from API")
        return 1
    
    print(f"OK Fetched {len(bars)} bars from API")
    
    # Convert to DataFrame
    df = pd.DataFrame(bars)
    
    # Rename columns if needed (API returns 't', 'o', 'h', 'l', 'c', 'v')
    if 't' in df.columns:
        df = df.rename(columns={
            't': 'timestamp',
            'o': 'open',
            'h': 'high',
            'l': 'low',
            'c': 'close',
            'v': 'volume'
        })
    
    # Parse timestamps
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df = df.sort_values('timestamp').drop_duplicates(subset=['timestamp']).reset_index(drop=True)
    
    # Add contract column
    df['contract'] = contract_id
    
    # Select and order columns
    df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume', 'contract']]
    
    # Format timestamps to match CSV format (ISO with timezone: 2025-11-23 23:00:00+00:00)
    def format_timestamp(ts):
        """Format timestamp as 'YYYY-MM-DD HH:MM:SS+00:00'"""
        if pd.isna(ts):
            return ''
        # Ensure it's timezone-aware UTC
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        # Format as string with +00:00 timezone
        return ts.strftime('%Y-%m-%d %H:%M:%S+00:00')
    
    df['timestamp'] = df['timestamp'].apply(format_timestamp)
    
    print(f"OK Processed {len(df)} bars")
    
    # Save to CSV
    output_file = 'mgc_data_past_3_days.csv'
    print(f"\nSAVING TO CSV: {output_file}")
    print("-" * 40)
    
    # Generate header comments
    generation_time = datetime.now(timezone.utc).isoformat()
    header_lines = [
        f"# Data Source: TopStep API - MGC Contracts",
        f"# Generated: {generation_time}",
        f"# Contracts: {contract_id}",
        f"# Total Bars: {len(df)}",
        f"# Columns: timestamp,open,high,low,close,volume,contract"
    ]
    
    # Write CSV with header comments
    with open(output_file, 'w', encoding='utf-8') as f:
        # Write header comments
        for line in header_lines:
            f.write(line + '\n')
        
        # Write CSV data
        df.to_csv(f, index=False)
    
    print(f"OK Saved {len(df)} bars to {output_file}")
    
    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Contract: {contract_id}")
    print(f"Date range: {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")
    print(f"Total bars: {len(df)}")
    print(f"Output file: {output_file}")
    print("=" * 60)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

