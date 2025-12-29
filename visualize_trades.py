#!/usr/bin/env python3
"""
Visualize trades on candlestick charts showing:
- Entry points
- Stop loss levels
- Take profit levels
- Break-even trigger levels
- Partial profit levels
- Structure levels
"""
import json
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Rectangle
import numpy as np
from pathlib import Path


def load_trades(trades_file: str = 'trades.csv') -> pd.DataFrame:
    """Load trade results from CSV."""
    df = pd.read_csv(trades_file)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['final_exit_time'] = pd.to_datetime(df['final_exit_time'])
    return df


def load_data(data_file: str = 'data.csv') -> pd.DataFrame:
    """Load price data."""
    df = pd.read_csv(data_file)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df


def load_config(config_file: str = 'config.json') -> dict:
    """Load config."""
    with open(config_file, 'r') as f:
        return json.load(f)


def plot_trade(
    trade: pd.Series,
    data: pd.DataFrame,
    config: dict,
    bars_before: int = 10,
    bars_after: int = 15,
    save_path: str = None
):
    """Plot a single trade with all levels annotated."""
    
    entry_time = trade['entry_time']
    exit_time = trade['final_exit_time']
    
    # Find the bar indices
    mask = (data['timestamp'] >= entry_time - pd.Timedelta(hours=bars_before * 0.25)) & \
           (data['timestamp'] <= exit_time + pd.Timedelta(hours=bars_after * 0.25))
    
    subset = data[mask].copy()
    
    if len(subset) < 5:
        print(f"Not enough data for trade {trade['trade_id']}")
        return
    
    # Calculate levels
    entry_price = trade['entry_price']
    stop_loss = trade['stop_loss']
    take_profit = trade['take_profit']
    side = trade['side']
    
    risk = abs(entry_price - stop_loss)
    
    # Break-even trigger (1.2R from config)
    be_trigger_r = config.get('break_even', {}).get('trigger_r', 1.2)
    if side == 'long':
        be_trigger_price = entry_price + (be_trigger_r * risk)
    else:
        be_trigger_price = entry_price - (be_trigger_r * risk)
    
    # Partial profit trigger (1.0R from config)
    partial_r = config.get('partial_profit', {}).get('first_exit_r', 1.0)
    if side == 'long':
        partial_price = entry_price + (partial_r * risk)
    else:
        partial_price = entry_price - (partial_r * risk)
    
    # Create figure
    fig, ax = plt.subplots(figsize=(16, 10))
    
    # Plot candlesticks
    width = 0.6
    up_color = '#26a69a'
    down_color = '#ef5350'
    
    for i, (idx, bar) in enumerate(subset.iterrows()):
        color = up_color if bar['close'] >= bar['open'] else down_color
        
        # Body
        body_bottom = min(bar['open'], bar['close'])
        body_height = abs(bar['close'] - bar['open'])
        rect = Rectangle((i - width/2, body_bottom), width, body_height, 
                         facecolor=color, edgecolor=color)
        ax.add_patch(rect)
        
        # Wicks
        ax.plot([i, i], [bar['low'], body_bottom], color=color, linewidth=1)
        ax.plot([i, i], [body_bottom + body_height, bar['high']], color=color, linewidth=1)
    
    # Find entry bar index
    entry_idx = None
    for i, (idx, bar) in enumerate(subset.iterrows()):
        if bar['timestamp'] >= entry_time:
            entry_idx = i
            break
    
    if entry_idx is None:
        entry_idx = len(subset) // 3
    
    # Plot levels as horizontal lines
    x_range = [0, len(subset) - 1]
    
    # Entry level
    ax.hlines(entry_price, x_range[0], x_range[1], colors='blue', linestyles='-', 
              linewidth=2, label=f'Entry: ${entry_price:.2f}')
    
    # Stop loss
    ax.hlines(stop_loss, x_range[0], x_range[1], colors='red', linestyles='--', 
              linewidth=2, label=f'Stop Loss: ${stop_loss:.2f}')
    
    # Take profit
    ax.hlines(take_profit, x_range[0], x_range[1], colors='green', linestyles='--', 
              linewidth=2, label=f'Take Profit: ${take_profit:.2f}')
    
    # Break-even trigger
    ax.hlines(be_trigger_price, x_range[0], x_range[1], colors='orange', linestyles=':', 
              linewidth=2, label=f'BE Trigger ({be_trigger_r}R): ${be_trigger_price:.2f}')
    
    # Partial profit level
    ax.hlines(partial_price, x_range[0], x_range[1], colors='purple', linestyles=':', 
              linewidth=2, label=f'Partial ({partial_r}R): ${partial_price:.2f}')
    
    # Mark entry point
    marker = '^' if side == 'long' else 'v'
    color = 'green' if side == 'long' else 'red'
    ax.scatter([entry_idx], [entry_price], marker=marker, s=200, c=color, 
               zorder=10, edgecolors='black', linewidth=2)
    
    # Add arrow and label for entry
    ax.annotate(f'ENTRY\n${entry_price:.2f}', 
                xy=(entry_idx, entry_price),
                xytext=(entry_idx + 2, entry_price + (risk if side == 'long' else -risk)),
                fontsize=10, fontweight='bold', color='blue',
                arrowprops=dict(arrowstyle='->', color='blue'))
    
    # Mark exit point
    exit_price = trade['final_exit_price']
    exit_reason = trade['exit_reason']
    
    # Find exit bar index
    exit_idx = len(subset) - 3
    for i, (idx, bar) in enumerate(subset.iterrows()):
        if bar['timestamp'] >= exit_time:
            exit_idx = i
            break
    
    exit_marker = 'o'
    exit_color = 'green' if trade['total_pnl'] > 0 else 'red'
    ax.scatter([exit_idx], [exit_price], marker=exit_marker, s=200, c=exit_color,
               zorder=10, edgecolors='black', linewidth=2)
    
    ax.annotate(f'EXIT ({exit_reason})\n${exit_price:.2f}', 
                xy=(exit_idx, exit_price),
                xytext=(exit_idx - 3, exit_price + (risk * 0.5 if side == 'long' else -risk * 0.5)),
                fontsize=10, fontweight='bold', color=exit_color,
                arrowprops=dict(arrowstyle='->', color=exit_color))
    
    # Add R-multiple annotations on the right
    ax_right = entry_price + (5 * risk) if side == 'long' else entry_price - (5 * risk)
    
    for r in [1, 2, 3]:
        if side == 'long':
            r_price = entry_price + (r * risk)
        else:
            r_price = entry_price - (r * risk)
        
        if min(subset['low']) < r_price < max(subset['high']):
            ax.text(len(subset) - 0.5, r_price, f' {r}R', fontsize=9, va='center', 
                    color='gray', fontweight='bold')
    
    # Style
    ax.set_xlim(-1, len(subset))
    
    # Y-axis padding
    y_min = min(subset['low'].min(), stop_loss) - (risk * 0.5)
    y_max = max(subset['high'].max(), take_profit) + (risk * 0.5)
    ax.set_ylim(y_min, y_max)
    
    # X-axis labels (show every 5th timestamp)
    x_labels = []
    x_ticks = []
    for i, (idx, bar) in enumerate(subset.iterrows()):
        if i % 5 == 0:
            x_ticks.append(i)
            x_labels.append(bar['timestamp'].strftime('%m/%d %H:%M'))
    
    ax.set_xticks(x_ticks)
    ax.set_xticklabels(x_labels, rotation=45, ha='right')
    
    # Title
    pnl = trade['total_pnl']
    pnl_str = f"+${pnl:.2f}" if pnl > 0 else f"-${abs(pnl):.2f}"
    pnl_color = 'green' if pnl > 0 else 'red'
    
    title = f"Trade #{trade['trade_id']} - {side.upper()} | {trade['session'].upper()} Session\n"
    title += f"Entry: {entry_time.strftime('%Y-%m-%d %H:%M')} | P&L: {pnl_str}"
    
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.set_ylabel('Price ($)', fontsize=12)
    ax.set_xlabel('Time', fontsize=12)
    
    # Legend
    ax.legend(loc='upper left', fontsize=10)
    
    # Grid
    ax.grid(True, alpha=0.3)
    ax.set_facecolor('#f8f9fa')
    
    # Add info box
    info_text = f"""Trade Details:
• Side: {side.upper()}
• Risk: {abs(trade['stop_loss'] - trade['entry_price']) / config.get('tick_size', 0.1):.0f} ticks (${risk:.2f})
• Reward: {abs(trade['take_profit'] - trade['entry_price']) / config.get('tick_size', 0.1):.0f} ticks
• R:R = 1:{abs(trade['take_profit'] - trade['entry_price']) / risk:.1f}
• Result: {trade['exit_reason']}
• BE Triggered: {'Yes' if trade['break_even_triggered'] else 'No'}
• Partial Exit: {'Yes' if pd.notna(trade.get('partial_exit_price')) else 'No'}"""
    
    props = dict(boxstyle='round', facecolor='white', alpha=0.9)
    ax.text(0.98, 0.02, info_text, transform=ax.transAxes, fontsize=9,
            verticalalignment='bottom', horizontalalignment='right', bbox=props)
    
    plt.tight_layout()
    
    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"Saved: {save_path}")
    
    plt.close()


def main():
    print("=" * 60)
    print("TRADE VISUALIZATION")
    print("=" * 60)
    
    # Load data
    trades = load_trades('trades.csv')
    data = load_data('data.csv')
    config = load_config('config.json')
    
    print(f"\nFound {len(trades)} trades")
    
    # Create output directory
    output_dir = Path('trade_charts')
    output_dir.mkdir(exist_ok=True)
    
    # Plot sample trades (mix of winners and losers)
    winners = trades[trades['total_pnl'] > 0].head(3)
    losers = trades[trades['total_pnl'] < 0].head(2)
    
    sample_trades = pd.concat([winners, losers]).sort_values('entry_time')
    
    print(f"\nGenerating charts for {len(sample_trades)} sample trades...")
    
    for _, trade in sample_trades.iterrows():
        result = 'WIN' if trade['total_pnl'] > 0 else 'LOSS'
        filename = f"trade_{trade['trade_id']:02d}_{trade['side']}_{result}.png"
        save_path = output_dir / filename
        
        plot_trade(trade, data, config, save_path=str(save_path))
    
    # Also create a summary image showing key levels
    print("\n" + "=" * 60)
    print("KEY LEVELS EXPLANATION")
    print("=" * 60)
    
    be_r = config.get('break_even', {}).get('trigger_r', 1.2)
    partial_r = config.get('partial_profit', {}).get('first_exit_r', 1.0)
    trailing_r = config.get('trailing_stop', {}).get('activation_r', 1.0)
    trail_dist = config.get('trailing_stop', {}).get('trail_distance_r', 0.4)
    
    print(f"""
For a LONG trade with Entry at $100 and Stop at $99 (Risk = $1):

LEVELS:
├── Entry:        $100.00
├── Stop Loss:    $99.00  (-1R)
├── BE Trigger:   ${100 + be_r:.2f}  (+{be_r}R) → Move SL to entry
├── Partial:      ${100 + partial_r:.2f}  (+{partial_r}R) → Exit 50%, lock 1R profit
├── Trailing:     ${100 + trailing_r:.2f}  (+{trailing_r}R) → Trail SL {trail_dist}R behind price
└── Take Profit:  Based on structure levels or min R:R

FLOW:
1. Entry at $100
2. Price hits ${100 + be_r:.2f} → SL moves to $100 (break-even)
3. Price hits ${100 + partial_r:.2f} → Close 50%, SL to ${100 + 1:.2f} (1R locked)
4. Price continues → Trailing stop follows, {trail_dist}R behind highs
5. Exit at TP or trailing stop hit
""")
    
    print(f"\nCharts saved to: {output_dir}/")
    print(f"Open the PNG files to see trade examples.")


if __name__ == '__main__':
    main()


