import pandas as pd
import numpy as np
from typing import List, Dict
from collections import defaultdict


class ReportGenerator:
    def __init__(self, config: dict):
        self.config = config
        self.tick_size = config.get('tick_size', 0.10)
        self.tick_value = config.get('tick_value', 1.00)
        
    def results_to_dataframe(self, trade_results: List) -> pd.DataFrame:
        if not trade_results:
            return pd.DataFrame()
        
        records = []
        for t in trade_results:
            records.append({
                'trade_id': t.trade_id,
                'side': t.side,
                'session': t.session,
                'entry_time': t.entry_time,
                'entry_price': t.entry_price,
                'stop_loss': t.stop_loss,
                'take_profit': t.take_profit,
                'zone_confidence': t.zone_confidence,
                'confirmation_type': t.confirmation_type,
                'partial_exit_time': t.partial_exit_time,
                'partial_exit_price': t.partial_exit_price,
                'partial_pnl': t.partial_pnl,
                'final_exit_time': t.final_exit_time,
                'final_exit_price': t.final_exit_price,
                'final_pnl': t.final_pnl,
                'total_pnl': t.total_pnl,
                'result_ticks': t.result_ticks,
                'break_even_triggered': t.break_even_triggered,
                'exit_reason': t.exit_reason,
                'cooldown_active': t.cooldown_active
            })
        
        return pd.DataFrame(records)
    
    def calculate_metrics(self, trade_results: List) -> dict:
        if not trade_results:
            return self._empty_metrics()
        
        pnls = [t.total_pnl for t in trade_results]
        total_trades = len(trade_results)
        
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        breakeven = [p for p in pnls if p == 0]
        
        total_pnl = sum(pnls)
        win_rate = len(wins) / total_trades if total_trades > 0 else 0
        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        gross_profit = sum(wins) if wins else 0
        gross_loss = abs(sum(losses)) if losses else 0
        
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = float('inf')
        else:
            profit_factor = 0
        
        equity = [0]
        for pnl in pnls:
            equity.append(equity[-1] + pnl)
        
        equity = np.array(equity)
        running_max = np.maximum.accumulate(equity)
        drawdown = running_max - equity
        max_drawdown = float(np.max(drawdown))
        
        max_dd_idx = np.argmax(drawdown)
        peak_idx = np.argmax(equity[:max_dd_idx + 1]) if max_dd_idx > 0 else 0
        
        session_breakdown = self._calculate_session_breakdown(trade_results)
        hour_breakdown = self._calculate_hour_breakdown(trade_results)
        enhancement_impact = self._calculate_enhancement_impact(trade_results)
        
        long_trades = [t for t in trade_results if t.side == 'long']
        short_trades = [t for t in trade_results if t.side == 'short']
        
        return {
            'total_trades': total_trades,
            'winning_trades': len(wins),
            'losing_trades': len(losses),
            'breakeven_trades': len(breakeven),
            'win_rate': round(win_rate, 4),
            'total_pnl': round(total_pnl, 2),
            'gross_profit': round(gross_profit, 2),
            'gross_loss': round(gross_loss, 2),
            'avg_pnl_per_trade': round(avg_pnl, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(min(profit_factor, 100), 2),
            'max_drawdown': round(max_drawdown, 2),
            'max_drawdown_trades': max_dd_idx - peak_idx if max_dd_idx > peak_idx else 0,
            'long_trades': len(long_trades),
            'short_trades': len(short_trades),
            'long_pnl': round(sum(t.total_pnl for t in long_trades), 2),
            'short_pnl': round(sum(t.total_pnl for t in short_trades), 2),
            'session_breakdown': session_breakdown,
            'hour_breakdown': hour_breakdown,
            'enhancement_impact': enhancement_impact
        }
    
    def _calculate_session_breakdown(self, trade_results: List) -> dict:
        sessions = defaultdict(list)
        
        for t in trade_results:
            sessions[t.session].append(t)
        
        breakdown = {}
        for session, trades in sessions.items():
            pnls = [t.total_pnl for t in trades]
            wins = [p for p in pnls if p > 0]
            
            breakdown[session] = {
                'trades': len(trades),
                'pnl': round(sum(pnls), 2),
                'win_rate': round(len(wins) / len(pnls), 4) if pnls else 0,
                'avg_pnl': round(sum(pnls) / len(pnls), 2) if pnls else 0
            }
        
        return breakdown
    
    def _calculate_hour_breakdown(self, trade_results: List) -> dict:
        hours = defaultdict(list)
        
        for t in trade_results:
            hour = t.entry_time.hour
            hours[hour].append(t)
        
        breakdown = {}
        for hour, trades in sorted(hours.items()):
            pnls = [t.total_pnl for t in trades]
            wins = [p for p in pnls if p > 0]
            
            breakdown[str(hour)] = {
                'trades': len(trades),
                'pnl': round(sum(pnls), 2),
                'win_rate': round(len(wins) / len(pnls), 4) if pnls else 0
            }
        
        return breakdown
    
    def _calculate_enhancement_impact(self, trade_results: List) -> dict:
        be_triggered = [t for t in trade_results if t.break_even_triggered]
        be_not_triggered = [t for t in trade_results if not t.break_even_triggered]
        
        be_wins_preserved = len([t for t in be_triggered if t.total_pnl >= 0])
        
        partial_trades = [t for t in trade_results if t.partial_exit_time is not None]
        partial_pnl = sum(t.partial_pnl for t in partial_trades)
        
        cooldown_trades = [t for t in trade_results if t.cooldown_active]
        non_cooldown_trades = [t for t in trade_results if not t.cooldown_active]
        
        high_conf_trades = [t for t in trade_results if t.zone_confidence >= 0.75]
        low_conf_trades = [t for t in trade_results if t.zone_confidence < 0.75]
        
        return {
            'break_even': {
                'triggered_count': len(be_triggered),
                'wins_preserved': be_wins_preserved,
                'avg_pnl_with_be': round(np.mean([t.total_pnl for t in be_triggered]), 2) if be_triggered else 0,
                'avg_pnl_without_be': round(np.mean([t.total_pnl for t in be_not_triggered]), 2) if be_not_triggered else 0
            },
            'partial_profits': {
                'trades_with_partial': len(partial_trades),
                'partial_pnl_captured': round(partial_pnl, 2),
                'avg_partial_pnl': round(partial_pnl / len(partial_trades), 2) if partial_trades else 0
            },
            'cooldown': {
                'trades_during_cooldown': len(cooldown_trades),
                'trades_normal': len(non_cooldown_trades),
                'pnl_during_cooldown': round(sum(t.total_pnl for t in cooldown_trades), 2),
                'pnl_normal': round(sum(t.total_pnl for t in non_cooldown_trades), 2)
            },
            'zone_confidence': {
                'high_conf_trades': len(high_conf_trades),
                'high_conf_win_rate': round(len([t for t in high_conf_trades if t.total_pnl > 0]) / len(high_conf_trades), 4) if high_conf_trades else 0,
                'low_conf_trades': len(low_conf_trades),
                'low_conf_win_rate': round(len([t for t in low_conf_trades if t.total_pnl > 0]) / len(low_conf_trades), 4) if low_conf_trades else 0
            }
        }
    
    def generate_trade_summary(self, trade_results: List) -> str:
        metrics = self.calculate_metrics(trade_results)
        
        lines = [
            "=" * 60,
            "BACKTEST SUMMARY REPORT",
            "=" * 60,
            "",
            "OVERALL PERFORMANCE",
            "-" * 40,
            f"Total Trades:        {metrics['total_trades']}",
            f"Win Rate:            {metrics['win_rate']:.1%}",
            f"Total P&L:           ${metrics['total_pnl']:,.2f}",
            f"Avg P&L per Trade:   ${metrics['avg_pnl_per_trade']:,.2f}",
            f"Profit Factor:       {metrics['profit_factor']:.2f}",
            f"Max Drawdown:        ${metrics['max_drawdown']:,.2f}",
            "",
            "TRADE BREAKDOWN",
            "-" * 40,
            f"Winning Trades:      {metrics['winning_trades']}",
            f"Losing Trades:       {metrics['losing_trades']}",
            f"Breakeven Trades:    {metrics['breakeven_trades']}",
            f"Avg Win:             ${metrics['avg_win']:,.2f}",
            f"Avg Loss:            ${metrics['avg_loss']:,.2f}",
            "",
            "DIRECTION BREAKDOWN",
            "-" * 40,
            f"Long Trades:         {metrics['long_trades']} (${metrics['long_pnl']:,.2f})",
            f"Short Trades:        {metrics['short_trades']} (${metrics['short_pnl']:,.2f})",
            "",
            "SESSION BREAKDOWN",
            "-" * 40
        ]
        
        for session, stats in metrics.get('session_breakdown', {}).items():
            lines.append(
                f"{session:15} {stats['trades']:3} trades, "
                f"${stats['pnl']:>8,.2f}, {stats['win_rate']:.1%} win rate"
            )
        
        lines.extend([
            "",
            "ENHANCEMENT IMPACT",
            "-" * 40
        ])
        
        ei = metrics.get('enhancement_impact', {})
        
        be = ei.get('break_even', {})
        lines.append(f"Break-Even Triggers: {be.get('triggered_count', 0)} (preserved {be.get('wins_preserved', 0)} wins)")
        
        pp = ei.get('partial_profits', {})
        lines.append(f"Partial Profits:     {pp.get('trades_with_partial', 0)} trades, ${pp.get('partial_pnl_captured', 0):,.2f} captured")
        
        zc = ei.get('zone_confidence', {})
        lines.append(f"High Conf Zones:     {zc.get('high_conf_trades', 0)} trades, {zc.get('high_conf_win_rate', 0):.1%} win rate")
        lines.append(f"Low Conf Zones:      {zc.get('low_conf_trades', 0)} trades, {zc.get('low_conf_win_rate', 0):.1%} win rate")
        
        lines.extend([
            "",
            "=" * 60
        ])
        
        return "\n".join(lines)
    
    def _empty_metrics(self) -> dict:
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'breakeven_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'gross_profit': 0,
            'gross_loss': 0,
            'avg_pnl_per_trade': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'profit_factor': 0,
            'max_drawdown': 0,
            'max_drawdown_trades': 0,
            'long_trades': 0,
            'short_trades': 0,
            'long_pnl': 0,
            'short_pnl': 0,
            'session_breakdown': {},
            'hour_breakdown': {},
            'enhancement_impact': {}
        }

