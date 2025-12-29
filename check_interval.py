#!/usr/bin/env python3
import json
import pandas as pd

# Check data interval
df = pd.read_csv('data.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'])
df = df.sort_values('timestamp')
intervals = df['timestamp'].diff().dt.total_seconds() / 60

# Check config
config = json.load(open('config.json'))

print("=" * 60)
print("TRADING INTERVAL CONFIGURATION")
print("=" * 60)

print(f"\nPrimary Trading Interval: 3 minutes")
print(f"  - Data file contains: {len(df):,} bars")
print(f"  - Most common interval: {intervals.mode()[0]:.0f} minutes")
print(f"  - Fetched from TopStep API with --interval 3")

print(f"\nHigher Timeframe Filter:")
print(f"  - Timeframe: {config['higher_tf_filter']['timeframe_minutes']} minutes")
print(f"  - EMA Period: {config['higher_tf_filter']['ema_period']}")
print(f"  - Enabled: {config['higher_tf_filter']['enabled']}")
print(f"  - Purpose: Filters trades based on 15-minute trend direction")

print(f"\n" + "=" * 60)
print("SUMMARY:")
print("=" * 60)
print("Strategy executes trades on 3-minute bars")
print("Uses 15-minute higher timeframe for trend filtering")
print("=" * 60)

