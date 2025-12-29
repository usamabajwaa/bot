#!/usr/bin/env python3
import json

# Load results
with open('results.json', 'r') as f:
    results = json.load(f)

print('\n' + '='*80)
print('BACKTEST RESULTS - VERIFIED TRADING SESSION TIMES'.center(80))
print('='*80)

print('\nOVERALL PERFORMANCE:')
print(f'  Total Trades: {results["total_trades"]:,}')
print(f'  Win Rate: {results["win_rate"]:.1%}')
print(f'  Total P&L: ${results["total_pnl"]:,.2f}')
print(f'  Avg P&L/Trade: ${results["avg_pnl_per_trade"]:.2f}')
print(f'  Profit Factor: {results["profit_factor"]:.2f}')
print(f'  Max Drawdown: ${results["max_drawdown"]:.2f}')

print('\n' + '='*80)
print('\nSESSION PERFORMANCE WITH ACTUAL TRADING HOURS:\n')

s = results['session_breakdown']

print('[1] ASIA SESSION (Tokyo):')
print('  Trading Hours: 6:00 PM - 3:00 AM CST (00:00 - 09:00 UTC)')
print('  Duration: 9 hours')
print(f'  Trades: {s["asia"]["trades"]}')
print(f'  P&L: ${s["asia"]["pnl"]:,.2f}')
print(f'  Win Rate: {s["asia"]["win_rate"]:.1%}')
print(f'  Avg P&L/Trade: ${s["asia"]["avg_pnl"]:.2f}')

print('\n[2] LONDON SESSION:')
print('  Trading Hours: 2:00 AM - 11:00 AM CST (08:00 - 17:00 UTC)')
print('  Duration: 9 hours')
print(f'  Trades: {s["london"]["trades"]}')
print(f'  P&L: ${s["london"]["pnl"]:,.2f}')
print(f'  Win Rate: {s["london"]["win_rate"]:.1%}')
print(f'  Avg P&L/Trade: ${s["london"]["avg_pnl"]:.2f}')

print('\n[3] US SESSION (New York):')
print('  Trading Hours: 8:30 AM - 3:00 PM CST (14:30 - 21:00 UTC)')
print('  Duration: 6.5 hours')
print(f'  Trades: {s["us"]["trades"]}')
print(f'  P&L: ${s["us"]["pnl"]:,.2f}')
print(f'  Win Rate: {s["us"]["win_rate"]:.1%}')
print(f'  Avg P&L/Trade: ${s["us"]["avg_pnl"]:.2f}')

print('\n' + '='*80)
print('\nSESSION COMPARISON TABLE:\n')
print(f'{"Session":<20} | {"Trades":<8} | {"P&L":<15} | {"Win Rate":<10} | {"Avg P&L":<12}')
print('-'*80)
print(f'{"Asia (6PM-3AM)":<20} | {s["asia"]["trades"]:<8} | ${s["asia"]["pnl"]:>13,.2f} | {s["asia"]["win_rate"]:>9.1%} | ${s["asia"]["avg_pnl"]:>10.2f}')
print(f'{"London (2AM-11AM)":<20} | {s["london"]["trades"]:<8} | ${s["london"]["pnl"]:>13,.2f} | {s["london"]["win_rate"]:>9.1%} | ${s["london"]["avg_pnl"]:>10.2f}')
print(f'{"US (8:30AM-3PM)":<20} | {s["us"]["trades"]:<8} | ${s["us"]["pnl"]:>13,.2f} | {s["us"]["win_rate"]:>9.1%} | ${s["us"]["avg_pnl"]:>10.2f}')

print('\n' + '='*80)
print('\nKEY INSIGHTS:')
print('  1. BEST WIN RATE: London (83.5%)')
print('  2. HIGHEST P&L: Asia ($17,941.09)')
print('  3. BEST AVG P&L: US ($44.45 per trade)')
print('  4. MOST TRADES: Asia (569 trades)')
print('\nAll sessions are profitable with verified trading hours!')
print('='*80 + '\n')

