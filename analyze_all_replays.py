#!/usr/bin/env python3
"""
Run replay backtests on all replay files and aggregate results for comparison.
"""

import json
import pandas as pd
from pathlib import Path
from typing import List, Dict
import subprocess
import sys


class ReplayAggregator:
    def __init__(self, replay_dir: str = 'replay_data', config_path: str = 'config.json', output_dir: str = 'replay_aggregated_results'):
        self.replay_dir = Path(replay_dir)
        self.config_path = config_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.results = []
    
    def run_replay_on_file(self, replay_file: Path) -> Dict:
        """Run replay engine on a single file and return results."""
        try:
            # Create a unique output directory for this replay
            file_output = self.output_dir / replay_file.stem
            file_output.mkdir(exist_ok=True)
            
            # Run replay.py
            cmd = [
                sys.executable,
                'replay.py',
                '--replay-file', str(replay_file),
                '--config', self.config_path,
                '--output', str(file_output)
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode != 0:
                print(f"Error running replay on {replay_file.name}: {result.stderr}")
                return None
            
            # Load results
            results_file = file_output / 'replay_results.json'
            if not results_file.exists():
                print(f"Results file not found for {replay_file.name}")
                return None
            
            with open(results_file, 'r') as f:
                metrics = json.load(f)
            
            # Add file info
            metrics['replay_file'] = replay_file.name
            metrics['replay_path'] = str(replay_file)
            
            return metrics
            
        except Exception as e:
            print(f"Exception processing {replay_file.name}: {e}")
            return None
    
    def run_all_replays(self) -> List[Dict]:
        """Run replay on all replay files."""
        replay_files = sorted(list(self.replay_dir.glob('replay_*.csv')))
        
        if not replay_files:
            print(f"No replay files found in {self.replay_dir}")
            return []
        
        print(f"Found {len(replay_files)} replay files to process...")
        print("=" * 70)
        
        results = []
        for i, replay_file in enumerate(replay_files, 1):
            print(f"\n[{i}/{len(replay_files)}] Processing {replay_file.name}...")
            result = self.run_replay_on_file(replay_file)
            if result:
                results.append(result)
                print(f"  -> {result.get('total_trades', 0)} trades, "
                      f"${result.get('total_pnl', 0):.2f} P&L, "
                      f"{result.get('win_rate', 0):.1%} win rate")
        
        self.results = results
        return results
    
    def aggregate_results(self) -> Dict:
        """Aggregate all replay results."""
        if not self.results:
            return {}
        
        # Aggregate metrics
        total_trades = sum(r.get('total_trades', 0) for r in self.results)
        total_wins = sum(r.get('winning_trades', 0) for r in self.results)
        total_losses = sum(r.get('losing_trades', 0) for r in self.results)
        total_pnl = sum(r.get('total_pnl', 0) for r in self.results)
        total_gross_profit = sum(r.get('gross_profit', 0) for r in self.results)
        total_gross_loss = sum(r.get('gross_loss', 0) for r in self.results)
        
        # Calculate aggregated metrics
        win_rate = total_wins / total_trades if total_trades > 0 else 0
        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        profit_factor = abs(total_gross_profit / total_gross_loss) if total_gross_loss != 0 else 0
        
        # Session breakdown
        session_breakdown = {}
        for result in self.results:
            session_data = result.get('session_breakdown', {})
            for session, data in session_data.items():
                if session not in session_breakdown:
                    session_breakdown[session] = {
                        'trades': 0,
                        'pnl': 0,
                        'wins': 0,
                        'losses': 0
                    }
                session_breakdown[session]['trades'] += data.get('trades', 0)
                session_breakdown[session]['pnl'] += data.get('pnl', 0)
                session_trades = data.get('trades', 0)
                session_wr = data.get('win_rate', 0)
                session_breakdown[session]['wins'] += int(session_trades * session_wr)
                session_breakdown[session]['losses'] += session_trades - int(session_trades * session_wr)
        
        # Calculate session win rates
        for session in session_breakdown:
            s = session_breakdown[session]
            s['win_rate'] = s['wins'] / s['trades'] if s['trades'] > 0 else 0
            s['avg_pnl'] = s['pnl'] / s['trades'] if s['trades'] > 0 else 0
        
        # Find max drawdown across all replays
        max_drawdown = max((r.get('max_drawdown', 0) for r in self.results), default=0)
        
        # Enhancement impact
        total_be_triggered = sum(r.get('enhancement_impact', {}).get('break_even', {}).get('triggered_count', 0) for r in self.results)
        total_be_wins = sum(r.get('enhancement_impact', {}).get('break_even', {}).get('wins_preserved', 0) for r in self.results)
        total_partial_pnl = sum(r.get('enhancement_impact', {}).get('partial_profits', {}).get('partial_pnl_captured', 0) for r in self.results)
        
        aggregated = {
            'total_replay_files': len(self.results),
            'total_trades': total_trades,
            'winning_trades': total_wins,
            'losing_trades': total_losses,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'gross_profit': total_gross_profit,
            'gross_loss': total_gross_loss,
            'avg_pnl_per_trade': avg_pnl,
            'profit_factor': profit_factor,
            'max_drawdown': max_drawdown,
            'session_breakdown': session_breakdown,
            'enhancement_impact': {
                'break_even': {
                    'triggered_count': total_be_triggered,
                    'wins_preserved': total_be_wins
                },
                'partial_profits': {
                    'total_captured': total_partial_pnl
                }
            }
        }
        
        return aggregated
    
    def print_summary(self):
        """Print aggregated summary."""
        if not self.results:
            print("No results to summarize.")
            return
        
        aggregated = self.aggregate_results()
        
        print("\n" + "=" * 70)
        print("REPLAY AGGREGATED RESULTS")
        print("=" * 70)
        print(f"Replay Files Processed: {aggregated['total_replay_files']}")
        print(f"\nOverall Performance:")
        print(f"  Total Trades: {aggregated['total_trades']}")
        print(f"  Win Rate: {aggregated['win_rate']:.1%}")
        print(f"  Total P&L: ${aggregated['total_pnl']:,.2f}")
        print(f"  Avg P&L/Trade: ${aggregated['avg_pnl_per_trade']:.2f}")
        print(f"  Profit Factor: {aggregated['profit_factor']:.2f}")
        print(f"  Max Drawdown: ${aggregated['max_drawdown']:.2f}")
        
        print(f"\nSession Breakdown:")
        for session, data in aggregated['session_breakdown'].items():
            print(f"  {session.upper()}:")
            print(f"    Trades: {data['trades']}")
            print(f"    P&L: ${data['pnl']:,.2f}")
            print(f"    Win Rate: {data['win_rate']:.1%}")
            print(f"    Avg P&L: ${data['avg_pnl']:.2f}")
        
        print(f"\nEnhancement Impact:")
        be = aggregated['enhancement_impact']['break_even']
        print(f"  Break-Even Triggered: {be['triggered_count']} times")
        print(f"  Wins Preserved: {be['wins_preserved']}")
        pp = aggregated['enhancement_impact']['partial_profits']
        print(f"  Partial Profits Captured: ${pp['total_captured']:,.2f}")
        
        print("=" * 70)
    
    def save_results(self):
        """Save aggregated results."""
        aggregated = self.aggregate_results()
        
        # Save aggregated JSON
        aggregated_file = self.output_dir / 'aggregated_results.json'
        with open(aggregated_file, 'w') as f:
            json.dump(aggregated, f, indent=2, default=str)
        
        # Save individual results CSV
        if self.results:
            df = pd.DataFrame(self.results)
            csv_file = self.output_dir / 'individual_results.csv'
            df.to_csv(csv_file, index=False)
        
        print(f"\nResults saved to {self.output_dir}/")
        print(f"  - aggregated_results.json")
        print(f"  - individual_results.csv")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Run replay analysis on all replay files')
    parser.add_argument('--replay-dir', type=str, default='replay_data', help='Directory containing replay files')
    parser.add_argument('--config', type=str, default='config.json', help='Config file path')
    parser.add_argument('--output', type=str, default='replay_aggregated_results', help='Output directory')
    
    args = parser.parse_args()
    
    aggregator = ReplayAggregator(
        replay_dir=args.replay_dir,
        config_path=args.config,
        output_dir=args.output
    )
    
    aggregator.run_all_replays()
    aggregator.print_summary()
    aggregator.save_results()


if __name__ == '__main__':
    main()

