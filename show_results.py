#!/usr/bin/env python3
import pandas as pd
import json

print("=" * 70)
print("FINAL BACKTEST RESULTS - MGC SCALPING STRATEGY")
print("=" * 70)

# Load results
results = json.load(open('results.json'))
df = pd.read_csv('trades.csv')

print("\n" + "=" * 70)
print("OVERALL PERFORMANCE")
print("=" * 70)
print(f"Total Trades:           {results['total_trades']}")
print(f"Win Rate:               {results['win_rate']:.1%}")
print(f"Winning Trades:         {results['winning_trades']}")
print(f"Losing Trades:          {results['losing_trades']}")
print(f"\nTotal P&L:              ${results['total_pnl']:,.2f}")
print(f"Gross Profit:          ${results['gross_profit']:,.2f}")
print(f"Gross Loss:             ${results['gross_loss']:,.2f}")
print(f"Average P&L/Trade:      ${results['avg_pnl_per_trade']:.2f}")
print(f"Average Win:            ${results['avg_win']:.2f}")
print(f"Average Loss:           ${results['avg_loss']:.2f}")
print(f"Profit Factor:          {results['profit_factor']:.2f}")
print(f"Max Drawdown:           ${results['max_drawdown']:.2f}")

print("\n" + "=" * 70)
print("TRADE DIRECTION BREAKDOWN")
print("=" * 70)
print(f"Long Trades:            {results['long_trades']} (${results['long_pnl']:,.2f})")
print(f"Short Trades:           {results['short_trades']} (${results['short_pnl']:,.2f})")

print("\n" + "=" * 70)
print("SESSION PERFORMANCE")
print("=" * 70)
for session, stats in results['session_breakdown'].items():
    print(f"\n{session.upper()} Session:")
    print(f"  Trades:              {stats['trades']}")
    print(f"  P&L:                 ${stats['pnl']:,.2f}")
    print(f"  Win Rate:            {stats['win_rate']:.1%}")
    print(f"  Avg P&L/Trade:       ${stats['avg_pnl']:.2f}")

print("\n" + "=" * 70)
print("RISK MANAGEMENT IMPACT")
print("=" * 70)
enh = results['enhancement_impact']
print(f"\nBreak Even:")
print(f"  Triggered:            {enh['break_even']['triggered_count']} times")
print(f"  Wins Preserved:       {enh['break_even']['wins_preserved']}")
print(f"  Avg P&L with BE:      ${enh['break_even']['avg_pnl_with_be']:.2f}")
print(f"  Avg P&L without BE:   ${enh['break_even']['avg_pnl_without_be']:.2f}")

print(f"\nPartial Profits:")
print(f"  Trades with Partial:  {enh['partial_profits']['trades_with_partial']}")
print(f"  Partial P&L Captured: ${enh['partial_profits']['partial_pnl_captured']:,.2f}")
print(f"  Avg Partial P&L:      ${enh['partial_profits']['avg_partial_pnl']:.2f}")

print("\n" + "=" * 70)
print("DATA SOURCE")
print("=" * 70)
print("TopStep API - CON.F.US.MGC.G26 (Micro Gold)")
print("3-minute bars | 32 days of data | Nov 23 - Dec 29, 2025")
print("=" * 70)

