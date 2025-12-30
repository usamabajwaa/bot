#!/usr/bin/env python3
"""
Analyze replay files to check for candle completion issues and trade quality.

This script:
1. Checks if signals were generated before candles completed
2. Analyzes trade outcomes from replay files
3. Identifies patterns and potential improvements
"""

import pandas as pd
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import pytz


class ReplayAnalyzer:
    def __init__(self, replay_dir: str = 'replay_data', bar_interval_minutes: int = 3):
        self.replay_dir = Path(replay_dir)
        self.bar_interval_minutes = bar_interval_minutes
        self.bar_interval_seconds = bar_interval_minutes * 60
        self.results = []
    
    def analyze_replay_file(self, replay_file: Path) -> Optional[Dict]:
        """Analyze a single replay file for candle completion issues."""
        try:
            # Parse signal info from filename
            # Format: replay_YYYYMMDD_HHMMSS_side.csv
            filename = replay_file.stem
            parts = filename.split('_')
            if len(parts) < 3:
                return None
            
            date_str = parts[1]  # YYYYMMDD
            time_str = parts[2]  # HHMMSS
            side = parts[3] if len(parts) > 3 else 'unknown'
            
            # Parse timestamp
            signal_time_str = f"{date_str}_{time_str}"
            signal_time = datetime.strptime(signal_time_str, "%Y%m%d_%H%M%S")
            signal_time = pytz.UTC.localize(signal_time)
            
            # Load replay data
            df = pd.read_csv(replay_file)
            df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
            df = df.sort_values('timestamp').reset_index(drop=True)
            
            if len(df) == 0:
                return None
            
            # Get last bar (where signal was generated)
            last_bar = df.iloc[-1]
            last_bar_time = pd.Timestamp(last_bar['timestamp'])
            if last_bar_time.tzinfo is None:
                last_bar_time = pytz.UTC.localize(last_bar_time)
            else:
                last_bar_time = last_bar_time.astimezone(pytz.UTC)
            
            # Calculate when the bar should end
            bar_end_time = last_bar_time + pd.Timedelta(seconds=self.bar_interval_seconds)
            
            # Check if signal was generated before bar completion
            signal_before_completion = signal_time < bar_end_time
            seconds_before_completion = (bar_end_time - signal_time).total_seconds() if signal_before_completion else 0
            
            # Calculate bar age at signal time
            bar_age_at_signal = (signal_time - last_bar_time).total_seconds()
            
            # Get bar data
            bar_open = last_bar['open']
            bar_high = last_bar['high']
            bar_low = last_bar['low']
            bar_close = last_bar['close']
            bar_volume = last_bar.get('volume', 0)
            
            result = {
                'replay_file': replay_file.name,
                'signal_time': signal_time.isoformat(),
                'bar_time': last_bar_time.isoformat(),
                'bar_end_time': bar_end_time.isoformat(),
                'side': side,
                'signal_before_completion': signal_before_completion,
                'seconds_before_completion': seconds_before_completion,
                'bar_age_at_signal': bar_age_at_signal,
                'bar_open': float(bar_open),
                'bar_high': float(bar_high),
                'bar_low': float(bar_low),
                'bar_close': float(bar_close),
                'bar_volume': float(bar_volume),
                'bar_range': float(bar_high - bar_low),
                'num_bars': len(df)
            }
            
            return result
            
        except Exception as e:
            print(f"Error analyzing {replay_file}: {e}")
            return None
    
    def analyze_all_replays(self) -> List[Dict]:
        """Analyze all replay files in the directory."""
        replay_files = list(self.replay_dir.glob('replay_*.csv'))
        
        if not replay_files:
            print(f"No replay files found in {self.replay_dir}")
            return []
        
        print(f"Found {len(replay_files)} replay files to analyze...")
        
        results = []
        for replay_file in sorted(replay_files):
            result = self.analyze_replay_file(replay_file)
            if result:
                results.append(result)
        
        self.results = results
        return results
    
    def generate_report(self) -> Dict:
        """Generate analysis report."""
        if not self.results:
            return {}
        
        df = pd.DataFrame(self.results)
        
        # Count issues
        premature_signals = df[df['signal_before_completion'] == True]
        num_premature = len(premature_signals)
        num_total = len(df)
        
        # Statistics
        avg_seconds_before = premature_signals['seconds_before_completion'].mean() if num_premature > 0 else 0
        max_seconds_before = premature_signals['seconds_before_completion'].max() if num_premature > 0 else 0
        
        # Bar age statistics
        avg_bar_age = df['bar_age_at_signal'].mean()
        min_bar_age = df['bar_age_at_signal'].min()
        max_bar_age = df['bar_age_at_signal'].max()
        
        report = {
            'total_replay_files': num_total,
            'premature_signals': num_premature,
            'premature_percentage': (num_premature / num_total * 100) if num_total > 0 else 0,
            'avg_seconds_before_completion': avg_seconds_before,
            'max_seconds_before_completion': max_seconds_before,
            'avg_bar_age_at_signal': avg_bar_age,
            'min_bar_age_at_signal': min_bar_age,
            'max_bar_age_at_signal': max_bar_age,
            'bar_interval_seconds': self.bar_interval_seconds
        }
        
        return report
    
    def print_report(self):
        """Print analysis report."""
        if not self.results:
            print("No results to report.")
            return
        
        report = self.generate_report()
        
        print("\n" + "="*70)
        print("REPLAY FILE ANALYSIS REPORT")
        print("="*70)
        print(f"Total replay files analyzed: {report['total_replay_files']}")
        print(f"\nCandle Completion Issues:")
        print(f"  Premature signals (before candle close): {report['premature_signals']} ({report['premature_percentage']:.1f}%)")
        if report['premature_signals'] > 0:
            print(f"  Average seconds before completion: {report['avg_seconds_before_completion']:.1f}s")
            print(f"  Maximum seconds before completion: {report['max_seconds_before_completion']:.1f}s")
        print(f"\nBar Age Statistics:")
        print(f"  Average bar age at signal: {report['avg_bar_age_at_signal']:.1f}s")
        print(f"  Minimum bar age at signal: {report['min_bar_age_at_signal']:.1f}s")
        print(f"  Maximum bar age at signal: {report['max_bar_age_at_signal']:.1f}s")
        print(f"  Bar interval: {report['bar_interval_seconds']}s ({self.bar_interval_minutes} minutes)")
        print("="*70)
        
        # Show problematic files
        df = pd.DataFrame(self.results)
        premature = df[df['signal_before_completion'] == True]
        
        if len(premature) > 0:
            print("\nWARNING: FILES WITH PREMATURE SIGNALS:")
            print("-" * 70)
            for _, row in premature.iterrows():
                print(f"  {row['replay_file']}")
                print(f"    Signal: {row['signal_time']}")
                print(f"    Bar: {row['bar_time']} (ends at {row['bar_end_time']})")
                print(f"    {row['seconds_before_completion']:.1f}s before completion")
                print()
        
        # Show files with very young bars
        young_bars = df[df['bar_age_at_signal'] < self.bar_interval_seconds]
        if len(young_bars) > 0:
            print("\nWARNING: FILES WITH BARS YOUNGER THAN INTERVAL:")
            print("-" * 70)
            for _, row in young_bars.iterrows():
                print(f"  {row['replay_file']}")
                print(f"    Bar age at signal: {row['bar_age_at_signal']:.1f}s (interval: {self.bar_interval_seconds}s)")
                print()
    
    def save_detailed_results(self, output_file: str = 'replay_analysis.csv'):
        """Save detailed results to CSV."""
        if not self.results:
            return
        
        df = pd.DataFrame(self.results)
        df.to_csv(output_file, index=False)
        print(f"\nDetailed results saved to: {output_file}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Analyze replay files for candle completion issues')
    parser.add_argument('--replay-dir', type=str, default='replay_data', help='Directory containing replay files')
    parser.add_argument('--bar-interval', type=int, default=3, help='Bar interval in minutes (default: 3)')
    parser.add_argument('--output', type=str, default='replay_analysis.csv', help='Output CSV file for detailed results')
    
    args = parser.parse_args()
    
    analyzer = ReplayAnalyzer(replay_dir=args.replay_dir, bar_interval_minutes=args.bar_interval)
    analyzer.analyze_all_replays()
    analyzer.print_report()
    analyzer.save_detailed_results(args.output)
    
    # Save summary report
    report = analyzer.generate_report()
    report_file = Path(args.replay_dir) / 'replay_analysis_report.json'
    with open(report_file, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"Summary report saved to: {report_file}")


if __name__ == '__main__':
    main()

