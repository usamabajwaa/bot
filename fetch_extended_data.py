#!/usr/bin/env python3
"""
Fetch extended MGC data by combining current and previous contracts.
This provides more historical data for backtesting.
"""
import json
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from broker import TopstepXClient


def get_mgc_contracts(client: TopstepXClient) -> List[dict]:
    """Find all available MGC contracts (current and previous)."""
    contracts = client.get_available_contracts()
    
    mgc_contracts = []
    for c in contracts:
        # Look for Micro Gold contracts
        if 'MGC' in c.id.upper() or 'Micro Gold' in c.description:
            mgc_contracts.append({
                'id': c.id,
                'name': c.name,
                'description': c.description,
                'tick_size': c.tick_size,
                'tick_value': c.tick_value
            })
    
    return mgc_contracts


def fetch_contract_data(
    client: TopstepXClient,
    contract_id: str,
    start_time: datetime,
    end_time: datetime,
    interval_minutes: int = 15
) -> pd.DataFrame:
    """Fetch historical data for a specific contract."""
    all_bars = []
    chunk_days = 7
    current_start = start_time
    
    while current_start < end_time:
        current_end = min(current_start + timedelta(days=chunk_days), end_time)
        
        try:
            bars = client.get_historical_bars(
                contract_id=contract_id,
                interval=interval_minutes,
                start_time=current_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_time=current_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                count=20000,
                live=False,
                unit=2
            )
            
            if bars:
                all_bars.extend(bars)
                print(f"    {current_start.strftime('%Y-%m-%d')} to {current_end.strftime('%Y-%m-%d')}: {len(bars)} bars")
        except Exception as e:
            print(f"    Error fetching {current_start.strftime('%Y-%m-%d')}: {e}")
        
        current_start = current_end
    
    if not all_bars:
        return pd.DataFrame()
    
    df = pd.DataFrame(all_bars)
    
    # Rename columns if needed
    if 't' in df.columns:
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
    df['contract'] = contract_id
    
    return df


def fetch_extended_data(
    days: int = 90,
    interval_minutes: int = 15,
    output_file: str = 'data.csv'
) -> Optional[pd.DataFrame]:
    """Fetch extended data from multiple MGC contracts."""
    
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        print("X credentials.json not found")
        return None
    
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key'],
        base_url=creds.get('base_url'),
        rtc_url=creds.get('rtc_url')
    )
    
    print("=" * 60)
    print("FETCHING EXTENDED MGC DATA")
    print("=" * 60)
    
    print("\nAuthenticating...")
    if not client.authenticate():
        print("X Authentication failed")
        return None
    print("OK Authenticated")
    
    # Find all MGC contracts
    print("\nSearching for MGC contracts...")
    mgc_contracts = get_mgc_contracts(client)
    
    if not mgc_contracts:
        print("X No MGC contracts found")
        return None
    
    print(f"OK Found {len(mgc_contracts)} MGC contract(s):")
    for c in mgc_contracts:
        print(f"    {c['id']} - {c['description']}")
    
    # Calculate date range
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)
    
    print(f"\nFetching {days} days of {interval_minutes}-minute bars...")
    print(f"  From: {start_time.strftime('%Y-%m-%d')}")
    print(f"  To:   {end_time.strftime('%Y-%m-%d')}")
    
    # Fetch data from each contract
    all_data = []
    
    for contract in mgc_contracts:
        print(f"\nFetching: {contract['id']} ({contract['description']})")
        
        df = fetch_contract_data(
            client=client,
            contract_id=contract['id'],
            start_time=start_time,
            end_time=end_time,
            interval_minutes=interval_minutes
        )
        
        if not df.empty:
            print(f"    OK Got {len(df)} bars")
            all_data.append(df)
        else:
            print(f"    WARNING: No data for this contract")
    
    if not all_data:
        print("\nX No data retrieved from any contract")
        return None
    
    # Combine all data
    print("\n" + "=" * 60)
    print("COMBINING DATA")
    print("=" * 60)
    
    combined = pd.concat(all_data, ignore_index=True)
    
    # Remove duplicates (prefer more recent contract data for overlapping periods)
    combined = combined.sort_values(['timestamp', 'contract'])
    combined = combined.drop_duplicates(subset=['timestamp'], keep='last')
    combined = combined.sort_values('timestamp').reset_index(drop=True)
    
    # Keep required columns plus contract info for validation
    result = combined[['timestamp', 'open', 'high', 'low', 'close', 'volume', 'contract']].copy()
    
    # Save to file with TopStep metadata
    # Write metadata comment first
    with open(output_file, 'w') as f:
        f.write("# Data Source: TopStep API - MGC Contracts\n")
        f.write(f"# Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"# Contracts: {', '.join(combined['contract'].unique())}\n")
        f.write(f"# Total Bars: {len(result)}\n")
        f.write("# Columns: timestamp,open,high,low,close,volume,contract\n")
    
    # Append data
    result.to_csv(output_file, mode='a', index=False)
    
    print(f"\nOK Saved {len(result)} bars to {output_file}")
    print(f"  Date range: {result['timestamp'].min()} to {result['timestamp'].max()}")
    print(f"  Price range: ${result['low'].min():.2f} to ${result['high'].max():.2f}")
    
    # Show data distribution
    result['date'] = pd.to_datetime(result['timestamp']).dt.date
    daily_counts = result.groupby('date').size()
    print(f"  Days with data: {len(daily_counts)}")
    print(f"  Avg bars/day: {daily_counts.mean():.0f}")
    
    return result


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Fetch extended MGC data from multiple contracts')
    parser.add_argument('--days', type=int, default=90, help='Number of days to fetch (default: 90)')
    parser.add_argument('--interval', type=int, default=15, help='Bar interval in minutes (default: 15)')
    parser.add_argument('--output', type=str, default='data.csv', help='Output file (default: data.csv)')
    
    args = parser.parse_args()
    
    fetch_extended_data(
        days=args.days,
        interval_minutes=args.interval,
        output_file=args.output
    )


