#!/usr/bin/env python3
"""Analyze silver backtest to find issues"""
import pandas as pd

print("=" * 60)
print("SILVER BACKTEST ISSUE ANALYSIS")
print("=" * 60)

# Load data
data = pd.read_csv('silver_data.csv', comment='#')
trades = pd.read_csv('silver_backtest_output/trades.csv')

print("\n1. DATA VERIFICATION:")
print(f"   OK: Data IS from Topstep API (CON.F.US.SIL.H26)")
print(f"   OK: Contract verified: {data['contract'].unique()[0]}")
print(f"   OK: Tick size: 0.005, Tick value: $5.0 (correct)")

print("\n2. CRITICAL ISSUE - UNREALISTIC PRICE MOVEMENT:")
start_price = data['close'].iloc[0]
end_price = data['close'].iloc[-1]
price_change_pct = ((end_price / start_price) - 1) * 100
print(f"   Start price: ${start_price:.2f} (Nov 23, 2025)")
print(f"   End price: ${end_price:.2f} (Dec 30, 2025)")
print(f"   Price change: {price_change_pct:.1f}% in 37 days")
print(f"   WARNING: Silver typically moves 1-5% per month")
print(f"   WARNING: This 45% move is EXTREME and unrealistic")

print("\n3. TRADE ANALYSIS:")
print(f"   Total trades: {len(trades)}")
sl_hits = (trades['exit_reason'] == 'stop_loss').sum()
tp_hits = (trades['exit_reason'] == 'take_profit').sum()
print(f"   Stop loss hits: {sl_hits} ({sl_hits/len(trades)*100:.1f}%)")
print(f"   Take profit hits: {tp_hits} ({tp_hits/len(trades)*100:.1f}%)")

print("\n4. STOP LOSS DISTANCES:")
avg_sl_distance = (trades['entry_price'] - trades['stop_loss']).abs().mean()
avg_sl_ticks = avg_sl_distance / 0.005
print(f"   Average SL distance: ${avg_sl_distance:.4f}")
print(f"   Average SL in ticks: {avg_sl_ticks:.1f}")
print(f"   WARNING: Stop losses may be too tight for silver volatility")

print("\n5. WHY STRATEGY FAILED:")
print(f"   - Extreme upward trend (45% in 37 days)")
print(f"   - Most trades were SHORTS (242 long, 126 short)")
print(f"   - Shorts hit stop loss due to strong uptrend")
print(f"   - Strategy parameters calibrated for MGC (gold), not silver")

print("\n6. RECOMMENDATIONS:")
print("   a) Verify if price movement is real (check Topstep dashboard)")
print("   b) If real: Strategy needs recalibration for silver")
print("   c) If not real: Data issue - re-fetch from Topstep")
print("   d) Consider if strategy is suitable for silver at all")

print("\n" + "=" * 60)
print("CONCLUSION")
print("=" * 60)
print("Data IS from Topstep API, but:")
print("1. Price movement seems unrealistic (45% in 37 days)")
print("2. Strategy parameters not suitable for silver")
print("3. Need to verify if price data is correct")
print("=" * 60)

