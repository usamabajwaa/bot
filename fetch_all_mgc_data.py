#!/usr/bin/env python3
"""
Fetch ALL available MGC historical bar data from TopStep API (3-minute candles)
and save it as CSV. Fetches data in chunks to handle API limits.
"""

import json
import sys
import time
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
    """Main function to fetch ALL available MGC data"""
    print("=" * 60)
    print("FETCHING ALL AVAILABLE MGC DATA - 3 MINUTE CANDLES")
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
    
    # Fetch all available data in chunks
    print("\n[5/5] FETCHING ALL HISTORICAL DATA")
    print("-" * 40)
    
    now = datetime.now(timezone.utc)
    # Start from 2 years ago (adjust if you need more/less)
    # You can increase this if you want to try fetching older data
    start_date = now - timedelta(days=730)  # 2 years
    
    # Fetch in chunks to avoid API limits
    # Each chunk is 7 days to ensure we get all data
    chunk_days = 7
    all_bars = []
    current_start = start_date
    chunk_number = 0
    
    print(f"Starting from: {start_date.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Ending at: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Fetching in chunks of {chunk_days} days...")
    print()
    
    while current_start < now:
        chunk_number += 1
        current_end = min(current_start + timedelta(days=chunk_days), now)
        
        start_time_str = current_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_time_str = current_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        print(f"Chunk {chunk_number}: {current_start.strftime('%Y-%m-%d')} to {current_end.strftime('%Y-%m-%d')}...", end=' ')
        
        try:
            # Fetch with large count to get all bars in the chunk
            # For 7 days of 3-min bars: 7 * 24 * 20 = 3360 bars max
            bars = client.get_historical_bars(
                contract_id=contract_id,
                interval=3,  # 3-minute bars
                start_time=start_time_str,
                end_time=end_time_str,
                count=10000,  # Large count to get all bars in chunk
                live=False,
                unit=2,  # Minutes
                include_partial=False
            )
            
            if bars:
                all_bars.extend(bars)
                print(f"OK {len(bars)} bars")
            else:
                print("No data")
            
            # Small delay to respect rate limits (50 requests / 30 seconds)
            # So we can do ~1.6 requests per second, use 0.7s delay to be safe
            time.sleep(0.7)
            
        except Exception as e:
            print(f"Error: {e}")
            # Continue with next chunk
        
        current_start = current_end
        
        # Progress update every 10 chunks
        if chunk_number % 10 == 0:
            print(f"  Progress: {len(all_bars)} total bars collected so far...")
    
    print()
    print(f"Total bars fetched: {len(all_bars)}")
    
    if not all_bars:
        print("ERROR: No bars returned from API")
        return 1
    
    # Convert to DataFrame
    print("\nPROCESSING DATA...")
    print("-" * 40)
    df = pd.DataFrame(all_bars)
    
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
    
    print(f"OK Processed {len(df)} unique bars")
    
    # Save to CSV
    output_file = 'mgc_data_all_available.csv'
    print(f"\nSAVING TO CSV: {output_file}")
    print("-" * 40)
    
    # Generate header comments
    generation_time = datetime.now(timezone.utc).isoformat()
    header_lines = [
        f"# Data Source: TopStep API - MGC Contracts",
        f"# Generated: {generation_time}",
        f"# Contracts: {contract_id}",
        f"# Total Bars: {len(df)}",
        f"# Bar Interval: 3 minutes",
        f"# Date Range: {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}",
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
    print(f"Bar interval: 3 minutes")
    print(f"Output file: {output_file}")
    print("=" * 60)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

