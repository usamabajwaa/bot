import json
import subprocess
from pathlib import Path
import re

# Load base config
with open('config.json', 'r') as f:
    base_config = json.load(f)

# Test scenarios
scenarios = [
    {
        'name': 'rejection_only',
        'description': 'Rejection candle only',
        'config': {
            'use_rejection': True,
            'use_engulfing': False,
            'require_both': False
        }
    },
    {
        'name': 'engulfing_only',
        'description': 'Engulfing candle only',
        'config': {
            'use_rejection': False,
            'use_engulfing': True,
            'require_both': False
        }
    },
    {
        'name': 'both_required',
        'description': 'Both rejection AND engulfing required',
        'config': {
            'use_rejection': True,
            'use_engulfing': True,
            'require_both': True
        }
    },
    {
        'name': 'either_one',
        'description': 'Either rejection OR engulfing (one of them)',
        'config': {
            'use_rejection': True,
            'use_engulfing': True,
            'require_both': False
        }
    }
]

results = []

for scenario in scenarios:
    print(f"\n{'='*70}")
    print(f"Testing: {scenario['description']} ({scenario['name']})")
    print(f"{'='*70}")
    
    # Update config
    test_config = base_config.copy()
    test_config['confirmation'].update(scenario['config'])
    
    # Save temporary config
    temp_config = f"config_{scenario['name']}.json"
    with open(temp_config, 'w') as f:
        json.dump(test_config, f, indent=2)
    
    # Run backtest
    try:
        result = subprocess.run(
            ['python', 'backtest.py', '--config', temp_config, '--data', 'data.csv'],
            capture_output=True,
            text=True,
            timeout=600
        )
        
        # Extract key metrics from output
        output = result.stdout + result.stderr
        metrics = {}
        
        # Parse results
        for line in output.split('\n'):
            if 'Total Trades:' in line:
                match = re.search(r'Total Trades:\s*(\d+)', line)
                if match:
                    metrics['total_trades'] = int(match.group(1))
            elif 'Win Rate:' in line:
                match = re.search(r'Win Rate:\s*([\d.]+)%', line)
                if match:
                    metrics['win_rate'] = float(match.group(1))
            elif 'Total P&L:' in line:
                match = re.search(r'Total P&L:\s*\$?([\d,.-]+)', line)
                if match:
                    metrics['total_pnl'] = float(match.group(1).replace(',', ''))
            elif 'Profit Factor:' in line:
                match = re.search(r'Profit Factor:\s*([\d.]+)', line)
                if match:
                    metrics['profit_factor'] = float(match.group(1))
            elif 'Max Drawdown:' in line:
                match = re.search(r'Max Drawdown:\s*\$?([\d,.-]+)', line)
                if match:
                    metrics['max_drawdown'] = float(match.group(1).replace(',', ''))
            elif 'Avg P&L/Trade:' in line:
                match = re.search(r'Avg P&L/Trade:\s*\$?([\d,.-]+)', line)
                if match:
                    metrics['avg_pnl_per_trade'] = float(match.group(1).replace(',', ''))
        
        results.append({
            'scenario': scenario['name'],
            'description': scenario['description'],
            'metrics': metrics
        })
        
        print(f"OK Completed: {scenario['name']}")
        if metrics:
            print(f"  Trades: {metrics.get('total_trades', 'N/A')}")
            print(f"  Win Rate: {metrics.get('win_rate', 'N/A')}%")
            print(f"  Total P&L: ${metrics.get('total_pnl', 'N/A'):,.2f}")
            print(f"  Profit Factor: {metrics.get('profit_factor', 'N/A')}")
            print(f"  Max Drawdown: ${metrics.get('max_drawdown', 'N/A'):,.2f}")
        
    except subprocess.TimeoutExpired:
        print(f"X Timeout: {scenario['name']}")
        results.append({
            'scenario': scenario['name'],
            'description': scenario['description'],
            'error': 'Timeout'
        })
    except Exception as e:
        print(f"X Error: {e}")
        results.append({
            'scenario': scenario['name'],
            'description': scenario['description'],
            'error': str(e)
        })
    
    # Clean up
    Path(temp_config).unlink(missing_ok=True)

# Print comparison
print(f"\n{'='*100}")
print("CONFIRMATION METHOD COMPARISON")
print(f"{'='*100}")

if results:
    print(f"\n{'Scenario':<25} {'Trades':<10} {'Win Rate':<12} {'Total P&L':<15} {'Avg P&L':<15} {'Profit Factor':<15} {'Max DD':<15}")
    print("-" * 110)
    
    for r in results:
        if 'metrics' in r:
            m = r['metrics']
            desc = r['description'][:24]
            print(f"{desc:<25} {m.get('total_trades', 'N/A'):<10} {m.get('win_rate', 'N/A'):<11.1f}% ${m.get('total_pnl', 0):<14,.2f} ${m.get('avg_pnl_per_trade', 0):<14,.2f} {m.get('profit_factor', 'N/A'):<15.2f} ${m.get('max_drawdown', 0):<14,.2f}")
        else:
            print(f"{r['description'][:24]:<25} ERROR: {r.get('error', 'Unknown')}")

# Find best scenarios
if results and all('metrics' in r for r in results if 'error' not in r):
    valid_results = [r for r in results if 'metrics' in r]
    
    if valid_results:
        best_pnl = max(r['metrics'].get('total_pnl', 0) for r in valid_results)
        best_pf = max(r['metrics'].get('profit_factor', 0) for r in valid_results)
        best_wr = max(r['metrics'].get('win_rate', 0) for r in valid_results)
        lowest_dd = min(r['metrics'].get('max_drawdown', 0) for r in valid_results)
        
        print(f"\n{'='*100}")
        print("BEST PERFORMERS:")
        print(f"{'='*100}")
        
        for r in valid_results:
            m = r['metrics']
            highlights = []
            if m.get('total_pnl', 0) == best_pnl:
                highlights.append("BEST P&L")
            if m.get('profit_factor', 0) == best_pf:
                highlights.append("BEST PF")
            if m.get('win_rate', 0) == best_wr:
                highlights.append("BEST WR")
            if m.get('max_drawdown', 0) == lowest_dd:
                highlights.append("LOWEST DD")
            
            if highlights:
                print(f"{r['description']}: {' | '.join(highlights)}")

# Save results to file
with open('confirmation_comparison_results.txt', 'w') as f:
    f.write("CONFIRMATION METHOD COMPARISON RESULTS\n")
    f.write("=" * 100 + "\n\n")
    
    for r in results:
        f.write(f"{r['description']} ({r['scenario']})\n")
        f.write("-" * 100 + "\n")
        if 'metrics' in r:
            m = r['metrics']
            f.write(f"Total Trades: {m.get('total_trades', 'N/A')}\n")
            f.write(f"Win Rate: {m.get('win_rate', 'N/A')}%\n")
            f.write(f"Total P&L: ${m.get('total_pnl', 0):,.2f}\n")
            f.write(f"Avg P&L/Trade: ${m.get('avg_pnl_per_trade', 0):,.2f}\n")
            f.write(f"Profit Factor: {m.get('profit_factor', 'N/A')}\n")
            f.write(f"Max Drawdown: ${m.get('max_drawdown', 0):,.2f}\n")
        else:
            f.write(f"Error: {r.get('error', 'Unknown')}\n")
        f.write("\n")

print(f"\n{'='*100}")
print("Results saved to: confirmation_comparison_results.txt")
print(f"{'='*100}")

