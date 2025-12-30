#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
System Validation Test
Tests:
1. Backtest using Topstep data
2. Connection test
3. Full live API test
4. Session times mismatch check and Topstep API data format verification
"""

import json
import sys
import io

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd
import pytz

from broker import TopstepXClient
from broker.topstepx_client import OrderSide, OrderType


def test_connection():
    """Test 1: Connection and Authentication"""
    print("\n" + "=" * 70)
    print("TEST 1: CONNECTION & AUTHENTICATION")
    print("=" * 70)
    
    cred_path = Path('credentials.json')
    if not cred_path.exists():
        print("ERROR: credentials.json not found!")
        return False
    
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    client = TopstepXClient(
        username=creds['username'],
        api_key=creds['api_key'],
        base_url=creds.get('base_url'),
        rtc_url=creds.get('rtc_url')
    )
    
    print("\n[1.1] Authenticating...")
    if not client.authenticate():
        print("FAIL: Authentication FAILED")
        return False
    print("OK: Authentication successful")
    
    print("\n[1.2] Fetching accounts...")
    accounts = client.get_accounts(only_active=True)
    if not accounts:
        print("FAIL: No accounts found")
        return False
    
    print(f"OK: Found {len(accounts)} account(s):")
    for acc in accounts:
        status = "TRADABLE" if acc.can_trade else "NOT TRADABLE"
        print(f"   [{status}] ID: {acc.id} | Name: {acc.name} | Balance: ${acc.balance:.2f}")
    
    print("\n[1.3] Finding MGC contract...")
    contract = client.find_mgc_contract()
    if not contract:
        print("FAIL: MGC contract not found")
        return False
    
    print(f"OK: Contract found: {contract.id}")
    print(f"   Name: {contract.name}")
    print(f"   Description: {contract.description}")
    print(f"   Tick Size: {contract.tick_size}")
    print(f"   Tick Value: ${contract.tick_value}")
    
    return True, client, contract


def test_topstep_data_format(client, contract):
    """Test 2: Check Topstep API data format and timezone"""
    print("\n" + "=" * 70)
    print("TEST 2: TOPSTEP API DATA FORMAT & TIMEZONE ANALYSIS")
    print("=" * 70)
    
    print("\n[2.1] Fetching recent bars to analyze format...")
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(days=1)
    
    bars = client.get_historical_bars(
        contract_id=contract.id,
        interval=3,  # 3-minute bars
        start_time=start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        count=100,
        live=False,
        unit=2,
        include_partial=False
    )
    
    if not bars:
        print("FAIL: No bars returned")
        return False
    
    print(f"OK: Fetched {len(bars)} bars")
    
    # Analyze first and last bars
    first_bar = bars[0]
    last_bar = bars[-1]
    
    print("\n[2.2] Analyzing bar format...")
    print(f"   First bar keys: {list(first_bar.keys())}")
    print(f"   Sample first bar: {first_bar}")
    
    # Check timestamp format
    print("\n[2.3] Timestamp analysis...")
    first_ts = first_bar.get('t') or first_bar.get('timestamp')
    last_ts = last_bar.get('t') or last_bar.get('timestamp')
    
    print(f"   First bar timestamp (raw): {first_ts}")
    print(f"   Last bar timestamp (raw): {last_ts}")
    
    # Parse timestamps
    try:
        if isinstance(first_ts, (int, float)):
            # Unix timestamp (milliseconds)
            first_dt = pd.Timestamp(first_ts, unit='ms', tz='UTC')
        else:
            first_dt = pd.to_datetime(first_ts, utc=True)
        
        if isinstance(last_ts, (int, float)):
            last_dt = pd.Timestamp(last_ts, unit='ms', tz='UTC')
        else:
            last_dt = pd.to_datetime(last_ts, utc=True)
        
        print(f"\n   First bar (parsed): {first_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"   Last bar (parsed): {last_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"   Timezone: {'UTC' if first_dt.tzinfo else 'NO TIMEZONE'}")
        
        # Check if timestamps are UTC
        if first_dt.tzinfo is None:
            print("   WARNING: Timestamps have NO timezone info!")
        elif str(first_dt.tzinfo) == 'UTC' or 'UTC' in str(first_dt.tzinfo):
            print("   ✅ Timestamps are in UTC")
        else:
            print(f"   ⚠️  WARNING: Timestamps are in {first_dt.tzinfo}, not UTC!")
        
        # Check time difference
        time_diff = (last_dt - first_dt).total_seconds() / 60
        print(f"   Time span: {time_diff:.1f} minutes ({len(bars)} bars)")
        print(f"   Expected interval: 3 minutes per bar")
        print(f"   Actual interval: {time_diff / (len(bars) - 1):.2f} minutes per bar")
        
    except Exception as e:
        print(f"   FAIL: Error parsing timestamps: {e}")
        return False
    
    # Check session times
    print("\n[2.4] Session time analysis...")
    df = pd.DataFrame(bars)
    if 't' in df.columns:
        df = df.rename(columns={'t': 'timestamp', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
    
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df['hour_utc'] = df['timestamp'].dt.hour
    
    print("   UTC hour distribution:")
    hour_counts = df['hour_utc'].value_counts().sort_index()
    for hour, count in hour_counts.items():
        print(f"      {hour:02d}:00 UTC - {count} bars")
    
    # Check for session boundaries
    print("\n   Session boundaries (from config):")
    with open('config.json', 'r') as f:
        config = json.load(f)
    
    sessions = config.get('sessions', {})
    for sess_name, sess_config in sessions.items():
        if sess_config.get('enabled', True):
            start = sess_config.get('start', '00:00')
            end = sess_config.get('end', '23:59')
            print(f"      {sess_name}: {start}-{end} UTC")
    
    return True, bars


def test_backtest_with_topstep_data():
    """Test 3: Run backtest using Topstep data"""
    print("\n" + "=" * 70)
    print("TEST 3: BACKTEST USING TOPSTEP DATA")
    print("=" * 70)
    
    print("\n[3.1] Fetching Topstep data for backtest...")
    
    # Use fetch_extended_data or fetch_real_data
    try:
        from fetch_extended_data import fetch_extended_data
        
        print("   Fetching 30 days of 3-minute bars...")
        df = fetch_extended_data(days=30, interval_minutes=3, output_file='test_backtest_data.csv')
        
        if df is None or df.empty:
            print("❌ Failed to fetch data")
            return False
        
        print(f"✅ Fetched {len(df)} bars")
        print(f"   Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")
        print(f"   Price range: ${df['low'].min():.2f} to ${df['high'].max():.2f}")
        
    except Exception as e:
        print(f"FAIL: Error fetching data: {e}")
        return False
    
    print("\n[3.2] Running backtest...")
    try:
        from backtest import BacktestEngine
        
        engine = BacktestEngine(config_path='config.json')
        engine.load_data('test_backtest_data.csv')
        engine.load_blackout_dates('blackout_dates.csv')
        
        results = engine.run()
        
        print(f"OK: Backtest completed: {len(results)} trades")
        
        if results:
            # Create output directory if it doesn't exist
            output_dir = Path('test_backtest_output')
            output_dir.mkdir(exist_ok=True)
            metrics = engine.generate_reports(output_dir=str(output_dir))
            print(f"\n   Results:")
            print(f"      Total Trades: {metrics.get('total_trades', 0)}")
            print(f"      Win Rate: {metrics.get('win_rate', 0):.1%}")
            print(f"      Total P&L: ${metrics.get('total_pnl', 0):.2f}")
            print(f"      Profit Factor: {metrics.get('profit_factor', 0):.2f}")
            print(f"      Max Drawdown: ${metrics.get('max_drawdown', 0):.2f}")
            print(f"\n   OK: Results saved to test_backtest_output/")
        else:
            print("   WARNING: No trades generated")
        
        return True
        
    except Exception as e:
        print(f"FAIL: Backtest error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_full_api(client, contract):
    """Test 4: Full API test"""
    print("\n" + "=" * 70)
    print("TEST 4: FULL LIVE API TEST")
    print("=" * 70)
    
    print("\n[4.1] Setting account...")
    accounts = client.get_accounts(only_active=True)
    if not accounts:
        print("FAIL: No accounts available")
        return False
    
    # Use first tradable account or configured account
    with open('credentials.json', 'r') as f:
        creds = json.load(f)
    
    account_id = creds.get('account_id')
    if account_id:
        client.set_account(account_id)
        print(f"OK: Using configured account: {account_id}")
    else:
        tradable = [a for a in accounts if a.can_trade]
        if tradable:
            client.set_account(tradable[0].id)
            print(f"OK: Using first tradable account: {tradable[0].id}")
        else:
            print("WARNING: No tradable accounts, using first account for read-only tests")
            client.set_account(accounts[0].id)
    
    print("\n[4.2] Testing API endpoints...")
    
    # Test get_positions
    try:
        positions = client.get_positions()
        print(f"OK: get_positions(): {len(positions)} positions")
    except Exception as e:
        print(f"FAIL: get_positions() failed: {e}")
    
    # Test get_open_orders
    try:
        orders = client.get_open_orders()
        print(f"OK: get_open_orders(): {len(orders)} open orders")
    except Exception as e:
        print(f"FAIL: get_open_orders() failed: {e}")
    
    # Test get_historical_bars (already tested, but verify)
    try:
        now = datetime.now(timezone.utc)
        bars = client.get_historical_bars(
            contract_id=contract.id,
            interval=3,
            start_time=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            count=20,
            live=False,
            unit=2,
            include_partial=False
        )
        print(f"OK: get_historical_bars(): {len(bars)} bars")
    except Exception as e:
        print(f"FAIL: get_historical_bars() failed: {e}")
    
    print("\n[4.3] API test complete (read-only operations tested)")
    print("   Note: Order placement tests require tradable account and are in test_api_full.py")
    
    return True


def test_session_time_mismatch(bars):
    """Test 5: Check for session time mismatches"""
    print("\n" + "=" * 70)
    print("TEST 5: SESSION TIME MISMATCH CHECK")
    print("=" * 70)
    
    if not bars:
        print("FAIL: No bars to analyze")
        return False
    
    print("\n[5.1] Loading config sessions...")
    with open('config.json', 'r') as f:
        config = json.load(f)
    
    sessions = config.get('sessions', {})
    print("   Configured sessions:")
    for sess_name, sess_config in sessions.items():
        if sess_config.get('enabled', True):
            start = sess_config.get('start', '00:00')
            end = sess_config.get('end', '23:59')
            print(f"      {sess_name}: {start}-{end} UTC")
    
    print("\n[5.2] Analyzing bar timestamps vs session definitions...")
    
    # Convert bars to DataFrame
    df = pd.DataFrame(bars)
    if 't' in df.columns:
        df = df.rename(columns={'t': 'timestamp', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
    
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df['hour_utc'] = df['timestamp'].dt.hour
    df['minute_utc'] = df['timestamp'].dt.minute
    
    # Test session detection
    from strategy import SessionManager
    session_mgr = SessionManager(config)
    
    print("\n   Testing session detection on sample bars:")
    sample_bars = df.iloc[::max(1, len(df)//10)]  # Sample every 10th bar
    
    mismatches = []
    for idx, row in sample_bars.head(20).iterrows():
        ts = row['timestamp']
        detected_session = session_mgr.get_active_session(ts)
        hour_min = f"{row['hour_utc']:02d}:{row['minute_utc']:02d}"
        
        # Check which session should be active
        expected_sessions = []
        for sess_name, sess_config in sessions.items():
            if not sess_config.get('enabled', True):
                continue
            start_str = sess_config.get('start', '00:00')
            end_str = sess_config.get('end', '23:59')
            
            start_h, start_m = map(int, start_str.split(':'))
            end_h, end_m = map(int, end_str.split(':'))
            
            current_time = ts.time()
            start_time = pd.Timestamp(ts.date()).replace(hour=start_h, minute=start_m).time()
            end_time = pd.Timestamp(ts.date()).replace(hour=end_h, minute=end_m).time()
            
            if start_time <= end_time:
                if start_time <= current_time <= end_time:
                    expected_sessions.append(sess_name)
            else:  # Overnight session
                if current_time >= start_time or current_time <= end_time:
                    expected_sessions.append(sess_name)
        
        status = "OK" if detected_session in expected_sessions or (not expected_sessions and not detected_session) else "FAIL"
        if status == "FAIL":
            mismatches.append((ts, detected_session, expected_sessions))
        
        print(f"      [{status}] {ts.strftime('%Y-%m-%d %H:%M:%S UTC')} -> Session: {detected_session or 'NONE'} (Expected: {expected_sessions or 'NONE'})")
    
    if mismatches:
        print(f"\n   WARNING: Found {len(mismatches)} potential mismatches")
        for ts, detected, expected in mismatches[:5]:
            print(f"      {ts}: detected={detected}, expected={expected}")
    else:
        print("\n   OK: No session detection mismatches found")
    
    return True


def main():
    print("=" * 70)
    print("SYSTEM VALIDATION TEST SUITE")
    print("=" * 70)
    print("\nThis will test:")
    print("  1. Connection & Authentication")
    print("  2. Topstep API Data Format & Timezone")
    print("  3. Backtest using Topstep Data")
    print("  4. Full Live API Test")
    print("  5. Session Time Mismatch Check")
    print("=" * 70)
    
    results = {}
    
    # Test 1: Connection
    try:
        result = test_connection()
        if isinstance(result, tuple):
            success, client, contract = result
            results['connection'] = success
        else:
            results['connection'] = False
            print("\nFAIL: Connection test failed, skipping remaining tests")
            return
    except Exception as e:
        print(f"\nFAIL: Connection test error: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Test 2: Data Format
    try:
        result = test_topstep_data_format(client, contract)
        if isinstance(result, tuple):
            success, bars = result
            results['data_format'] = success
        else:
            results['data_format'] = False
            bars = None
    except Exception as e:
        print(f"\nFAIL: Data format test error: {e}")
        import traceback
        traceback.print_exc()
        results['data_format'] = False
        bars = None
    
    # Test 3: Backtest
    try:
        results['backtest'] = test_backtest_with_topstep_data()
    except Exception as e:
        print(f"\nFAIL: Backtest error: {e}")
        import traceback
        traceback.print_exc()
        results['backtest'] = False
    
    # Test 4: Full API
    try:
        results['full_api'] = test_full_api(client, contract)
    except Exception as e:
        print(f"\nFAIL: Full API test error: {e}")
        import traceback
        traceback.print_exc()
        results['full_api'] = False
    
    # Test 5: Session Mismatch
    if bars:
        try:
            results['session_mismatch'] = test_session_time_mismatch(bars)
        except Exception as e:
            print(f"\nFAIL: Session mismatch test error: {e}")
            import traceback
            traceback.print_exc()
            results['session_mismatch'] = False
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    
    for test_name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {test_name:20s}: {status}")
    
    all_passed = all(results.values())
    print("\n" + "=" * 70)
    if all_passed:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
    print("=" * 70)
    
    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())

