#!/usr/bin/env python3
"""
Run all replay files with improvements and compare results.
"""

import json
import pandas as pd
from pathlib import Path
from typing import List, Dict
import subprocess
import sys


def run_replay(replay_file: Path, output_dir: Path, config: str = 'config.json') -> Dict:
    """Run a single replay file and return results."""
    try:
        output_subdir = output_dir / replay_file.stem
        output_subdir.mkdir(exist_ok=True)
        
        result = subprocess.run(
            [sys.executable, 'replay.py', 
             '--replay-file', str(replay_file),
             '--config', config,
             '--output', str(output_subdir)],
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode != 0:
            print(f"Error running {replay_file.name}: {result.stderr}")
            return None
        
        # Load results
        results_file = output_subdir / 'replay_results.json'
        if results_file.exists():
            with open(results_file, 'r') as f:
                return json.load(f)
        else:
            print(f"Results file not found for {replay_file.name}")
            return None
            
    except Exception as e:
        print(f"Exception running {replay_file.name}: {e}")
        return None


def aggregate_replay_results(results: List[Dict]) -> Dict:
    """Aggregate results from multiple replay files."""
    if not results:
        return {}
    
    total_trades = sum(r.get('total_trades', 0) for r in results)
    total_pnl = sum(r.get('total_pnl', 0) for r in results)
    total_wins = sum(r.get('winning_trades', 0) for r in results)
    total_losses = sum(r.get('losing_trades', 0) for r in results)
    
    gross_profit = sum(r.get('gross_profit', 0) for r in results)
    gross_loss = sum(r.get('gross_loss', 0) for r in results)
    
    win_rate = total_wins / total_trades if total_trades > 0 else 0
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else 0
    avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
    
    max_drawdown = max((r.get('max_drawdown', 0) for r in results), default=0)
    
    return {
        'total_trades': total_trades,
        'total_pnl': total_pnl,
        'winning_trades': total_wins,
        'losing_trades': total_losses,
        'win_rate': win_rate,
        'gross_profit': gross_profit,
        'gross_loss': gross_loss,
        'profit_factor': profit_factor,
        'avg_pnl_per_trade': avg_pnl,
        'max_drawdown': max_drawdown,
        'num_replay_files': len(results)
    }


def compare_replay_results(old_results: Dict, new_results: Dict) -> None:
    """Compare old and new replay results."""
    
    print("\n" + "="*80)
    print("REPLAY RESULTS COMPARISON: BEFORE vs AFTER CANDLE COMPLETION FIX")
    print("="*80)
    
    metrics = [
        ('num_replay_files', 'Replay Files', ''),
        ('total_trades', 'Total Trades', ''),
        ('win_rate', 'Win Rate', '%'),
        ('total_pnl', 'Total P&L', '$'),
        ('avg_pnl_per_trade', 'Avg P&L/Trade', '$'),
        ('profit_factor', 'Profit Factor', ''),
        ('max_drawdown', 'Max Drawdown', '$'),
        ('gross_profit', 'Gross Profit', '$'),
        ('gross_loss', 'Gross Loss', '$'),
    ]
    
    print(f"\n{'Metric':<25} {'Before':<20} {'After':<20} {'Change':<15}")
    print("-" * 80)
    
    for key, label, unit in metrics:
        old_val = old_results.get(key, 0)
        new_val = new_results.get(key, 0)
        
        if key == 'win_rate':
            old_val = old_val * 100
            new_val = new_val * 100
            change = new_val - old_val
            change_str = f"{change:+.2f}%"
        elif isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
            change = new_val - old_val
            if unit == '$':
                change_str = f"${change:+.2f}"
            else:
                change_str = f"{change:+.2f}"
        else:
            change_str = "N/A"
        
        if unit == '%' and key == 'win_rate':
            old_str = f"{old_val:.2f}%"
            new_str = f"{new_val:.2f}%"
        elif unit == '$':
            old_str = f"${old_val:.2f}"
            new_str = f"${new_val:.2f}"
        else:
            old_str = str(int(old_val)) if isinstance(old_val, float) and old_val.is_integer() else str(old_val)
            new_str = str(int(new_val)) if isinstance(new_val, float) and new_val.is_integer() else str(new_val)
        
        print(f"{label:<25} {old_str:<20} {new_str:<20} {change_str:<15}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Run all replays and compare results')
    parser.add_argument('--replay-dir', type=str, default='replay_data',
                        help='Directory containing replay files')
    parser.add_argument('--config', type=str, default='config.json',
                        help='Config file path')
    parser.add_argument('--output', type=str, default='replay_comparison_output',
                        help='Output directory for replay results')
    parser.add_argument('--old-results', type=str, default=None,
                        help='Path to old aggregated replay results JSON (optional)')
    parser.add_argument('--max-files', type=int, default=None,
                        help='Maximum number of replay files to process (for testing)')
    
    args = parser.parse_args()
    
    replay_dir = Path(args.replay_dir)
    output_dir = Path(args.output)
    output_dir.mkdir(exist_ok=True)
    
    # Find all replay files
    replay_files = sorted(list(replay_dir.glob('replay_*.csv')))
    
    if args.max_files:
        replay_files = replay_files[:args.max_files]
    
    if not replay_files:
        print(f"No replay files found in {replay_dir}")
        return
    
    print(f"Found {len(replay_files)} replay files to process...")
    print(f"Running replays with improvements...")
    
    results = []
    for i, replay_file in enumerate(replay_files, 1):
        print(f"[{i}/{len(replay_files)}] Processing {replay_file.name}...")
        result = run_replay(replay_file, output_dir, args.config)
        if result:
            results.append(result)
    
    if not results:
        print("No results generated!")
        return
    
    # Aggregate results
    aggregated = aggregate_replay_results(results)
    
    # Save aggregated results
    results_file = output_dir / 'aggregated_replay_results.json'
    with open(results_file, 'w') as f:
        json.dump(aggregated, f, indent=2)
    print(f"\nAggregated results saved to: {results_file}")
    
    # Compare with old results if provided
    if args.old_results and Path(args.old_results).exists():
        with open(args.old_results, 'r') as f:
            old_results = json.load(f)
        compare_replay_results(old_results, aggregated)
    else:
        print("\n" + "="*80)
        print("AGGREGATED REPLAY RESULTS (WITH IMPROVEMENTS)")
        print("="*80)
        for key, value in aggregated.items():
            if isinstance(value, float):
                if 'rate' in key or 'factor' in key:
                    print(f"{key}: {value:.2%}")
                elif 'pnl' in key.lower() or 'drawdown' in key.lower():
                    print(f"{key}: ${value:.2f}")
                else:
                    print(f"{key}: {value:.2f}")
            else:
                print(f"{key}: {value}")


if __name__ == '__main__':
    main()

