#!/usr/bin/env python3
"""
Replay trades from 2026 and generate statistics.
Reads trade_journal.jsonl, filters for 2026 trades, and replays them using replay_data files.
"""

import json
import pandas as pd
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import pytz

from risk import RiskManager, Position, PositionStatus, TradeResult
from strategy import Strategy


def load_trade_journal(journal_path: str = 'trade_journal.jsonl') -> List[Dict]:
    """Load all trades from journal file."""
    trades = []
    if not Path(journal_path).exists():
        print(f"Warning: {journal_path} not found")
        return trades
    
    with open(journal_path, 'r') as f:
        for line in f:
            if line.strip():
                try:
                    trade = json.loads(line)
                    trades.append(trade)
                except json.JSONDecodeError as e:
                    print(f"Error parsing line: {e}")
    
    return trades


def filter_today_trades(trades: List[Dict], target_date: str = None) -> List[Dict]:
    """Filter trades from today or a specific date."""
    from datetime import datetime
    if target_date is None:
        target_date = datetime.now().strftime('%Y-%m-%d')
    filtered = []
    for trade in trades:
        timestamp_str = trade.get('timestamp', '')
        if target_date in timestamp_str:
            filtered.append(trade)
    
    return filtered

def filter_2026_trades(trades: List[Dict]) -> List[Dict]:
    """Filter all trades from year 2026."""
    filtered = []
    for trade in trades:
        timestamp_str = trade.get('timestamp', '')
        if '2026' in timestamp_str:
            filtered.append(trade)
    
    return filtered


def find_replay_file(trade: Dict, replay_dir: Path) -> Optional[Path]:
    """Find the replay data file for a given trade."""
    timestamp_str = trade.get('timestamp', '')
    side = trade.get('side', '').lower()
    
    # Parse timestamp - format: "2026-01-01 18:33:45.298047-06:00"
    try:
        # Parse with timezone awareness
        dt = pd.to_datetime(timestamp_str)
        
        # Convert to UTC (replay files are saved in UTC)
        if dt.tz is not None:
            dt_utc = dt.tz_convert('UTC')
        else:
            dt_utc = dt.tz_localize('UTC')
        
        # Get all replay files for the same side
        all_replay_files = list(replay_dir.glob(f'replay_*_{side}.csv'))
        
        if not all_replay_files:
            return None
        
        # Find the closest match by time (within 5 minutes)
        best_match = None
        min_diff = None
        
        for replay_file in all_replay_files:
            try:
                # Extract time from filename: replay_YYYYMMDD_HHMMSS_side.csv
                parts = replay_file.stem.split('_')
                if len(parts) >= 3:
                    # Reconstruct timestamp string with underscore: YYYYMMDD_HHMMSS
                    match_time_str = f"{parts[1]}_{parts[2]}"
                    match_dt = pd.to_datetime(match_time_str, format='%Y%m%d_%H%M%S', utc=True)
                    diff_seconds = abs((match_dt - dt_utc).total_seconds())
                    
                    # Accept matches within 5 minutes (300 seconds)
                    if diff_seconds < 300:
                        if min_diff is None or diff_seconds < min_diff:
                            min_diff = diff_seconds
                            best_match = replay_file
            except Exception as e:
                # Skip files that can't be parsed
                continue
        
        return best_match
        
    except Exception as e:
        print(f"Error finding replay file for trade {trade.get('timestamp')}: {e}")
        import traceback
        traceback.print_exc()
    
    return None


def replay_trade(trade: Dict, replay_file: Path, risk_manager: RiskManager, strategy: Strategy) -> Optional[TradeResult]:
    """Replay a single trade using its replay data file."""
    try:
        # Load replay data
        df = pd.read_csv(replay_file)
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        df = df.sort_values('timestamp').reset_index(drop=True)
        
        # Prepare data with indicators
        df = strategy.prepare_data(df)
        
        # Get trade parameters
        entry_price = trade['entry']
        stop_loss = trade['sl']
        take_profit = trade['tp']
        side = trade['side'].lower()
        quantity = trade.get('quantity', 8)
        session = trade.get('session', 'unknown')
        zone_confidence = trade.get('zone', {}).get('confidence', 0.7)
        
        # Find entry bar (closest bar to trade timestamp)
        trade_timestamp = pd.to_datetime(trade['timestamp'])
        
        # Find the bar index where we should enter
        entry_bar_idx = None
        min_time_diff = None
        
        for idx, row in df.iterrows():
            time_diff = abs((row['timestamp'] - trade_timestamp).total_seconds())
            if min_time_diff is None or time_diff < min_time_diff:
                min_time_diff = time_diff
                entry_bar_idx = idx
        
        if entry_bar_idx is None:
            print(f"Could not find entry bar for trade at {trade_timestamp}")
            return None
        
        # Create position
        entry_time = df.iloc[entry_bar_idx]['timestamp']
        entry_bar = df.iloc[entry_bar_idx]
        
        # Calculate risk/reward in ticks
        tick_size = risk_manager.tick_size
        if side == 'long':
            risk_ticks = (entry_price - stop_loss) / tick_size
            reward_ticks = (take_profit - entry_price) / tick_size
        else:
            risk_ticks = (stop_loss - entry_price) / tick_size
            reward_ticks = (entry_price - take_profit) / tick_size
        
        position = Position(
            trade_id=risk_manager.trade_counter + 1,
            side=side,
            entry_price=entry_price,
            entry_time=entry_time,
            entry_index=entry_bar_idx,
            initial_stop_loss=stop_loss,
            current_stop_loss=stop_loss,
            take_profit=take_profit,
            contracts=quantity,
            remaining_contracts=quantity,
            session=session,
            zone_confidence=zone_confidence,
            confirmation_type='replay',
            risk_ticks=risk_ticks,
            reward_ticks=reward_ticks
        )
        
        risk_manager.current_position = position
        risk_manager.trade_counter += 1
        
        # Simulate trade execution through bars
        for idx in range(entry_bar_idx + 1, len(df)):
            bar = df.iloc[idx]
            
            # Use update_position which handles all position management
            result, partial_exit = risk_manager.update_position(bar, idx)
            
            if result is not None:
                return result
        
        # If we reach here, trade didn't exit - force close at last bar
        if risk_manager.current_position is not None:
            last_bar = df.iloc[-1]
            exit_price = last_bar['close']
            timestamp = pd.to_datetime(last_bar['timestamp'])
            
            tick_size = risk_manager.tick_size
            tick_value = risk_manager.tick_value
            commission = risk_manager.commission_per_contract
            
            if side == 'long':
                pnl_ticks = (exit_price - entry_price) / tick_size
            else:
                pnl_ticks = (entry_price - exit_price) / tick_size
            
            gross_pnl = pnl_ticks * tick_value * position.remaining_contracts
            net_pnl = gross_pnl - commission * position.remaining_contracts * 2
            
            position.final_exit_time = timestamp
            position.final_exit_price = exit_price
            position.final_pnl = net_pnl
            position.total_pnl = position.partial_pnl + net_pnl
            position.exit_reason = 'end_of_data'
            position.status = PositionStatus.CLOSED
            
            result = TradeResult(
                trade_id=position.trade_id,
                side=side,
                session=session,
                entry_time=entry_time,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                zone_confidence=zone_confidence,
                confirmation_type='replay',
                partial_exit_time=position.partial_exit_time,
                partial_exit_price=position.partial_exit_price,
                partial_pnl=position.partial_pnl,
                final_exit_time=timestamp,
                final_exit_price=exit_price,
                final_pnl=net_pnl,
                total_pnl=position.total_pnl,
                result_ticks=pnl_ticks,
                break_even_triggered=position.break_even_triggered,
                exit_reason='end_of_data',
                cooldown_active=risk_manager.is_in_cooldown()
            )
            
            risk_manager.trade_results.append(result)
            
            # Update daily P&L and cooldown (matching risk.py behavior)
            date = position.entry_time.date()
            risk_manager.daily_pnl[date] = risk_manager.daily_pnl.get(date, 0.0) + position.total_pnl
            risk_manager._update_cooldown(position.total_pnl)
            
            risk_manager.current_position = None
            return result
        
        return None
        
    except Exception as e:
        print(f"Error replaying trade: {e}")
        import traceback
        traceback.print_exc()
        return None


def calculate_statistics(results: List[TradeResult]) -> Dict:
    """Calculate statistics from trade results."""
    if not results:
        return {}
    
    total_trades = len(results)
    winning_trades = [r for r in results if r.total_pnl > 0]
    losing_trades = [r for r in results if r.total_pnl < 0]
    breakeven_trades = [r for r in results if r.total_pnl == 0]
    
    total_pnl = sum(r.total_pnl for r in results)
    avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
    
    win_rate = len(winning_trades) / total_trades if total_trades > 0 else 0
    
    avg_win = sum(r.total_pnl for r in winning_trades) / len(winning_trades) if winning_trades else 0
    avg_loss = sum(r.total_pnl for r in losing_trades) / len(losing_trades) if losing_trades else 0
    
    profit_factor = abs(sum(r.total_pnl for r in winning_trades) / sum(r.total_pnl for r in losing_trades)) if losing_trades and sum(r.total_pnl for r in losing_trades) != 0 else float('inf')
    
    # Calculate equity curve and drawdown
    equity_curve = [0.0]
    peak_equity = 0.0
    max_drawdown = 0.0
    
    for result in results:
        equity_curve.append(equity_curve[-1] + result.total_pnl)
        if equity_curve[-1] > peak_equity:
            peak_equity = equity_curve[-1]
        drawdown = peak_equity - equity_curve[-1]
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    
    # Session breakdown
    session_stats = {}
    for result in results:
        session = result.session
        if session not in session_stats:
            session_stats[session] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
        session_stats[session]['trades'] += 1
        session_stats[session]['pnl'] += result.total_pnl
        if result.total_pnl > 0:
            session_stats[session]['wins'] += 1
    
    for session in session_stats:
        stats = session_stats[session]
        stats['win_rate'] = stats['wins'] / stats['trades'] if stats['trades'] > 0 else 0
    
    # Exit reason breakdown
    exit_reasons = {}
    for result in results:
        reason = result.exit_reason
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    
    return {
        'total_trades': total_trades,
        'winning_trades': len(winning_trades),
        'losing_trades': len(losing_trades),
        'breakeven_trades': len(breakeven_trades),
        'win_rate': win_rate,
        'total_pnl': total_pnl,
        'avg_pnl_per_trade': avg_pnl,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'max_drawdown': max_drawdown,
        'session_breakdown': session_stats,
        'exit_reasons': exit_reasons,
        'equity_curve': equity_curve
    }


def main():
    """Main function to replay trades and show statistics."""
    from datetime import datetime, timedelta
    import sys
    
    # Load trades first to find most recent date
    print("="*60)
    print(f"BACKTESTING ALL 2026 TRADES")
    print("="*60)
    
    # Load configuration
    config_path = 'config_production.json'
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    # Initialize components
    risk_manager = RiskManager(config)
    strategy = Strategy(config)
    
    # Load trades
    print("\nLoading trade journal...")
    all_trades = load_trade_journal('trade_journal.jsonl')
    print(f"Total trades in journal: {len(all_trades)}")
    
    # Filter all 2026 trades
    print("\nFiltering all 2026 trades...")
    trades_2026 = filter_2026_trades(all_trades)
    print(f"Found {len(trades_2026)} trades from 2026")
    
    if not trades_2026:
        print("No trades found for 2026. Exiting.")
        return
    
    # Show date breakdown
    dates_2026 = sorted(set([t['timestamp'][:10] for t in trades_2026]))
    print(f"Dates: {dates_2026}")
    
    # Replay each trade
    print("\nReplaying trades...")
    replay_dir = Path('replay_data')
    results = []
    missing_replay_files = []
    skipped_cooldown = []
    
    # Sort trades by timestamp to process in chronological order
    trades_sorted = sorted(trades_2026, key=lambda t: t.get('timestamp', ''))
    
    for i, trade in enumerate(trades_sorted, 1):
        timestamp = trade.get('timestamp', 'unknown')
        side = trade.get('side', 'unknown')
        print(f"[{i}/{len(trades_sorted)}] Processing trade: {timestamp} {side}")
        
        # Check cooldown before processing trade
        if risk_manager.is_in_cooldown():
            print(f"  [SKIPPED] Trade blocked by cooldown (bars remaining: {risk_manager.cooldown_bars_remaining})")
            skipped_cooldown.append(trade)
            # Simulate bars passing between trades (tick cooldown)
            # Estimate ~1 bar per minute, so tick cooldown once per trade skipped
            risk_manager.tick_cooldown()
            continue
        
        replay_file = find_replay_file(trade, replay_dir)
        if replay_file is None:
            print(f"  [WARNING] No replay file found for this trade")
            missing_replay_files.append(trade)
            continue
        
        print(f"  [OK] Found replay file: {replay_file.name}")
        
        # Reset only current position (keep cooldown state)
        risk_manager.current_position = None
        
        result = replay_trade(trade, replay_file, risk_manager, strategy)
        if result:
            results.append(result)
            # Cooldown is automatically updated by risk_manager when trade closes
            # Check cooldown state
            if risk_manager.is_in_cooldown():
                print(f"  [COOLDOWN] Triggered! Bars remaining: {risk_manager.cooldown_bars_remaining}, Consecutive losses: {risk_manager.consecutive_losses}")
            cooldown_status = " (COOLDOWN ACTIVE)" if risk_manager.is_in_cooldown() else ""
            print(f"  [OK] Trade completed: P&L=${result.total_pnl:.2f}, Exit: {result.exit_reason}{cooldown_status}")
            
            # Simulate bars passing between trades
            # Estimate time difference between trades and tick cooldown accordingly
            if i < len(trades_sorted):
                next_trade = trades_sorted[i]
                try:
                    current_time = pd.to_datetime(trade['timestamp'])
                    next_time = pd.to_datetime(next_trade['timestamp'])
                    time_diff_minutes = (next_time - current_time).total_seconds() / 60
                    # Tick cooldown for estimated bars (assuming 1 bar per minute)
                    bars_to_tick = min(int(time_diff_minutes), risk_manager.cooldown_bars_remaining)
                    if bars_to_tick > 0 and risk_manager.is_in_cooldown():
                        print(f"  [COOLDOWN] Ticking {bars_to_tick} bars, {risk_manager.cooldown_bars_remaining} remaining")
                    for _ in range(bars_to_tick):
                        risk_manager.tick_cooldown()
                except Exception as e:
                    # If we can't calculate time diff, just tick once
                    if risk_manager.is_in_cooldown():
                        risk_manager.tick_cooldown()
        else:
            print(f"  [FAIL] Trade failed to complete")
    
    print(f"\n{'='*60}")
    print(f"BACKTEST SUMMARY")
    print(f"{'='*60}")
    print(f"Total trades processed: {len(trades_sorted)}")
    print(f"Successfully replayed: {len(results)}")
    print(f"Skipped due to cooldown: {len(skipped_cooldown)}")
    print(f"Missing replay files: {len(missing_replay_files)}")
    
    if not results:
        print("\nNo trades were successfully replayed. Cannot generate statistics.")
        return
    
    # Calculate and display statistics
    stats = calculate_statistics(results)
    
    print(f"\n{'='*60}")
    print(f"STATISTICS")
    print(f"{'='*60}")
    print(f"Total Trades: {stats['total_trades']}")
    print(f"Winning Trades: {stats['winning_trades']}")
    print(f"Losing Trades: {stats['losing_trades']}")
    print(f"Breakeven Trades: {stats['breakeven_trades']}")
    print(f"Win Rate: {stats['win_rate']:.1%}")
    print(f"\nTotal P&L: ${stats['total_pnl']:.2f}")
    print(f"Average P&L/Trade: ${stats['avg_pnl_per_trade']:.2f}")
    print(f"Average Win: ${stats['avg_win']:.2f}")
    print(f"Average Loss: ${stats['avg_loss']:.2f}")
    print(f"Profit Factor: {stats['profit_factor']:.2f}")
    print(f"Max Drawdown: ${stats['max_drawdown']:.2f}")
    
    if stats.get('session_breakdown'):
        print(f"\n{'='*60}")
        print(f"SESSION BREAKDOWN")
        print(f"{'='*60}")
        for session, session_stats in stats['session_breakdown'].items():
            print(f"{session.upper()}:")
            print(f"  Trades: {session_stats['trades']}")
            print(f"  Win Rate: {session_stats['win_rate']:.1%}")
            print(f"  Total P&L: ${session_stats['pnl']:.2f}")
    
    if stats.get('exit_reasons'):
        print(f"\n{'='*60}")
        print(f"EXIT REASONS")
        print(f"{'='*60}")
        for reason, count in stats['exit_reasons'].items():
            print(f"{reason}: {count}")
    
    print(f"\n{'='*60}")


if __name__ == '__main__':
    main()

