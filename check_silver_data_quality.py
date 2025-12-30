#!/usr/bin/env python3
"""Check silver data quality and compare with expected values"""
import pandas as pd
import numpy as np

print("=" * 60)
print("SILVER DATA QUALITY CHECK")
print("=" * 60)

df = pd.read_csv('silver_data.csv', comment='#')
df['timestamp'] = pd.to_datetime(df['timestamp'])

print(f"\n1. BASIC INFO:")
print(f"   Total bars: {len(df)}")
print(f"   Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")
print(f"   Contract: {df['contract'].unique()}")

print(f"\n2. PRICE STATISTICS:")
print(f"   Min: ${df['close'].min():.2f}")
print(f"   Max: ${df['close'].max():.2f}")
print(f"   Mean: ${df['close'].mean():.2f}")
print(f"   Std: ${df['close'].std():.2f}")

print(f"\n3. MISSING VALUES:")
missing = df.isnull().sum()
if missing.sum() > 0:
    print("   WARNING: Missing values found!")
    print(missing[missing > 0])
else:
    print("   OK: No missing values")

print(f"\n4. PRICE JUMPS (>10%):")
df['pct_change'] = df['close'].pct_change().abs() * 100
jumps = df[df['pct_change'] > 10]
print(f"   Found {len(jumps)} bars with >10% price change")
if len(jumps) > 0:
    print("   WARNING: Large price jumps detected!")
    print(jumps[['timestamp', 'close', 'pct_change']].head(10))

print(f"\n5. VOLUME CHECK:")
zero_vol = (df['volume'] == 0).sum()
print(f"   Zero volume bars: {zero_vol}")
if zero_vol > len(df) * 0.1:
    print("   WARNING: Too many zero volume bars!")

print(f"\n6. TIME GAPS:")
df = df.sort_values('timestamp')
df['time_diff'] = df['timestamp'].diff()
expected_interval = pd.Timedelta(minutes=3)
gaps = df[df['time_diff'] > expected_interval * 2]
print(f"   Gaps >6 minutes: {len(gaps)}")
if len(gaps) > 0:
    print("   WARNING: Time gaps detected!")
    print(gaps[['timestamp', 'time_diff']].head(10))

print(f"\n7. PRICE VALIDITY CHECK:")
print(f"   Expected silver price range: $20-35/oz (typical)")
print(f"   Our prices: ${df['close'].min():.2f} to ${df['close'].max():.2f}")
if df['close'].min() < 15 or df['close'].max() > 50:
    print("   WARNING: Prices outside typical silver range!")
    print("   This could indicate:")
    print("     - Wrong contract")
    print("     - Data corruption")
    print("     - Different time period (silver was higher in past)")

print(f"\n8. COMPARING WITH MGC DATA:")
try:
    mgc_df = pd.read_csv('data.csv', comment='#')
    mgc_df['timestamp'] = pd.to_datetime(mgc_df['timestamp'])
    print(f"   MGC price range: ${mgc_df['close'].min():.2f} to ${mgc_df['close'].max():.2f}")
    print(f"   Silver price range: ${df['close'].min():.2f} to ${df['close'].max():.2f}")
    print(f"   Ratio (Silver/MGC): {df['close'].mean() / mgc_df['close'].mean():.4f}")
    print(f"   Expected ratio: ~0.015 (silver ~$25, gold ~$2000)")
    actual_ratio = df['close'].mean() / mgc_df['close'].mean()
    if actual_ratio > 0.02:
        print("   WARNING: Silver prices seem too high relative to gold!")
except:
    print("   Could not load MGC data for comparison")

print(f"\n9. CONFIG CHECK:")
print(f"   Tick size: 0.005")
print(f"   Tick value: $5.0")
print(f"   This means: Each $0.005 move = $5 per contract")
print(f"   At $50 price: 10,000 ticks * $5 = $50,000 per contract")
print(f"   This seems correct for Micro Silver (1,000 oz contract)")

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
issues = []
if missing.sum() > 0:
    issues.append("Missing values")
if len(jumps) > 0:
    issues.append("Large price jumps")
if zero_vol > len(df) * 0.1:
    issues.append("Too many zero volume bars")
if len(gaps) > 100:
    issues.append("Many time gaps")

if issues:
    print("ISSUES FOUND:")
    for issue in issues:
        print(f"  - {issue}")
else:
    print("OK: No major data quality issues detected")
    
if df['close'].min() < 15 or df['close'].max() > 50:
    print("\nWARNING: Price range seems unusual for silver")
    print("  Silver typically trades $20-35/oz")
    print("  Current range: ${:.2f} to ${:.2f}".format(df['close'].min(), df['close'].max()))
    print("  This might be correct if:")
    print("    - Data is from a different time period")
    print("    - Contract specifications are different")
    print("    - Need to verify with Topstep contract details")

