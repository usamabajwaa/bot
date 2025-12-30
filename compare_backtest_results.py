#!/usr/bin/env python3
"""
Compare old vs new backtest results to see impact of candle completion fix.
"""

import json
from pathlib import Path
from typing import Dict, Optional


def load_results(filepath: str) -> Optional[Dict]:
    """Load JSON results file."""
    path = Path(filepath)
    if not path.exists():
        return None
    with open(path, 'r') as f:
        return json.load(f)


def compare_results(old_results: Dict, new_results: Dict):
    """Compare two result sets and print differences."""
    print("\n" + "=" * 70)
    print("BACKTEST RESULTS COMPARISON")
    print("=" * 70)
    
    # Overall metrics
    print("\nOVERALL PERFORMANCE:")
    print("-" * 70)
    
    metrics = [
        ('total_trades', 'Total Trades', ''),
        ('win_rate', 'Win Rate', '%'),
        ('total_pnl', 'Total P&L', '$'),
        ('avg_pnl_per_trade', 'Avg P&L/Trade', '$'),
        ('profit_factor', 'Profit Factor', ''),
        ('max_drawdown', 'Max Drawdown', '$'),
    ]
    
    for key, label, suffix in metrics:
        old_val = old_results.get(key, 0)
        new_val = new_results.get(key, 0)
        
        if suffix == '%':
            old_str = f"{old_val:.1%}"
            new_str = f"{new_val:.1%}"
        elif suffix == '$':
            old_str = f"${old_val:,.2f}"
            new_str = f"${new_val:,.2f}"
        else:
            old_str = str(old_val)
            new_str = str(new_val)
        
        diff = new_val - old_val
        if suffix == '%':
            diff_str = f"{diff:+.1%}"
        elif suffix == '$':
            diff_str = f"${diff:+,.2f}"
        else:
            diff_str = f"{diff:+,.0f}"
        
        print(f"  {label:20s} | Old: {old_str:>15s} | New: {new_str:>15s} | Change: {diff_str:>15s}")
    
    # Session breakdown
    print("\nSESSION BREAKDOWN:")
    print("-" * 70)
    
    old_sessions = old_results.get('session_breakdown', {})
    new_sessions = new_results.get('session_breakdown', {})
    
    all_sessions = set(old_sessions.keys()) | set(new_sessions.keys())
    
    for session in sorted(all_sessions):
        old_s = old_sessions.get(session, {})
        new_s = new_sessions.get(session, {})
        
        old_trades = old_s.get('trades', 0)
        new_trades = new_s.get('trades', 0)
        old_pnl = old_s.get('pnl', 0)
        new_pnl = new_s.get('pnl', 0)
        old_wr = old_s.get('win_rate', 0)
        new_wr = new_s.get('win_rate', 0)
        
        print(f"\n  {session.upper()}:")
        print(f"    Trades:  {old_trades:>4d} -> {new_trades:>4d} ({new_trades - old_trades:+,d})")
        print(f"    P&L:     ${old_pnl:>10,.2f} -> ${new_pnl:>10,.2f} (${new_pnl - old_pnl:+,.2f})")
        print(f"    Win Rate: {old_wr:>6.1%} -> {new_wr:>6.1%} ({new_wr - old_wr:+.1%})")
    
    # Enhancement impact
    print("\nENHANCEMENT IMPACT:")
    print("-" * 70)
    
    old_enh = old_results.get('enhancement_impact', {})
    new_enh = new_results.get('enhancement_impact', {})
    
    # Break-even
    old_be = old_enh.get('break_even', {})
    new_be = new_enh.get('break_even', {})
    old_be_count = old_be.get('triggered_count', 0)
    new_be_count = new_be.get('triggered_count', 0)
    old_be_wins = old_be.get('wins_preserved', 0)
    new_be_wins = new_be.get('wins_preserved', 0)
    
    print(f"  Break-Even:")
    print(f"    Triggered: {old_be_count:>4d} -> {new_be_count:>4d} ({new_be_count - old_be_count:+,d})")
    print(f"    Wins Preserved: {old_be_wins:>4d} -> {new_be_wins:>4d} ({new_be_wins - old_be_wins:+,d})")
    
    # Partial profits
    old_pp = old_enh.get('partial_profits', {})
    new_pp = new_enh.get('partial_profits', {})
    old_pp_captured = old_pp.get('partial_pnl_captured', 0)
    new_pp_captured = new_pp.get('partial_pnl_captured', 0)
    
    print(f"  Partial Profits:")
    print(f"    Captured: ${old_pp_captured:>10,.2f} -> ${new_pp_captured:>10,.2f} (${new_pp_captured - old_pp_captured:+,.2f})")
    
    print("\n" + "=" * 70)
    
    # Summary assessment
    print("\nSUMMARY ASSESSMENT:")
    print("-" * 70)
    
    pnl_change = new_results.get('total_pnl', 0) - old_results.get('total_pnl', 0)
    wr_change = new_results.get('win_rate', 0) - old_results.get('win_rate', 0)
    trades_change = new_results.get('total_trades', 0) - old_results.get('total_trades', 0)
    
    if pnl_change > 0:
        print(f"[+] Total P&L improved by ${pnl_change:,.2f}")
    else:
        print(f"[-] Total P&L decreased by ${abs(pnl_change):,.2f}")
    
    if wr_change > 0:
        print(f"[+] Win rate improved by {wr_change:.1%}")
    else:
        print(f"[-] Win rate decreased by {abs(wr_change):.1%}")
    
    if trades_change > 0:
        print(f"  Total trades increased by {trades_change} (more opportunities)")
    elif trades_change < 0:
        print(f"  Total trades decreased by {abs(trades_change)} (fewer opportunities)")
    else:
        print(f"  Total trades unchanged")
    
    print("=" * 70)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Compare old vs new backtest results')
    parser.add_argument('--old', type=str, default='test_backtest_output/results.json', 
                        help='Path to old results JSON')
    parser.add_argument('--new', type=str, default='backtest_new_results/results.json',
                        help='Path to new results JSON')
    
    args = parser.parse_args()
    
    old_results = load_results(args.old)
    new_results = load_results(args.new)
    
    if not old_results:
        print(f"Error: Could not load old results from {args.old}")
        return
    
    if not new_results:
        print(f"Error: Could not load new results from {args.new}")
        return
    
    compare_results(old_results, new_results)


if __name__ == '__main__':
    main()
