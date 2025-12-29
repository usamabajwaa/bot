import numpy as np
import pandas as pd
from typing import List, Dict
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Patch
import plotly.graph_objects as go
from plotly.subplots import make_subplots


class Visualizer:
    def __init__(self, config: dict):
        self.config = config
        
        plt.style.use('seaborn-v0_8-darkgrid')
        self.colors = {
            'profit': '#2ecc71',
            'loss': '#e74c3c',
            'neutral': '#95a5a6',
            'primary': '#3498db',
            'secondary': '#9b59b6',
            'asia': '#f39c12',
            'london_early': '#1abc9c',
            'us': '#e74c3c'
        }
        
    def plot_all(
        self,
        trade_results: List,
        metrics: dict,
        output_dir: str = '.'
    ) -> None:
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)
        
        self.plot_equity_curve(trade_results, output_path / 'equity_curve.png')
        self.plot_drawdown(trade_results, output_path / 'drawdown.png')
        self.plot_session_comparison(trade_results, metrics, output_path / 'session_comparison.png')
        self.plot_trade_distribution(trade_results, output_path / 'trade_distribution.png')
        self.plot_pnl_distribution(trade_results, output_path / 'pnl_distribution.png')
        
        if 'monte_carlo' in metrics:
            self.plot_monte_carlo(metrics['monte_carlo'], output_path / 'monte_carlo.png')
        
        if 'walk_forward' in metrics:
            self.plot_walk_forward(metrics['walk_forward'], output_path / 'walk_forward.png')
        
        self.plot_zone_confidence_analysis(trade_results, output_path / 'zone_confidence.png')
        self.plot_enhancement_impact(metrics.get('enhancement_impact', {}), output_path / 'enhancement_impact.png')
        
        self.create_interactive_dashboard(trade_results, metrics, output_path / 'dashboard.html')
    
    def plot_equity_curve(
        self,
        trade_results: List,
        output_path: Path
    ) -> None:
        if not trade_results:
            return
        
        fig, ax = plt.subplots(figsize=(14, 7))
        
        pnls = [t.total_pnl for t in trade_results]
        equity = [0] + list(np.cumsum(pnls))
        
        dates = [trade_results[0].entry_time] + [t.final_exit_time for t in trade_results]
        
        for i in range(1, len(equity)):
            color = self.colors['profit'] if pnls[i-1] >= 0 else self.colors['loss']
            ax.plot([i-1, i], [equity[i-1], equity[i]], color=color, linewidth=2)
        
        ax.fill_between(range(len(equity)), equity, 0, 
                       where=[e >= 0 for e in equity], 
                       color=self.colors['profit'], alpha=0.3)
        ax.fill_between(range(len(equity)), equity, 0, 
                       where=[e < 0 for e in equity], 
                       color=self.colors['loss'], alpha=0.3)
        
        ax.axhline(y=0, color='black', linestyle='-', linewidth=0.5)
        
        ax.set_xlabel('Trade Number', fontsize=12)
        ax.set_ylabel('Cumulative P&L ($)', fontsize=12)
        ax.set_title('Equity Curve', fontsize=14, fontweight='bold')
        
        final_pnl = equity[-1]
        ax.annotate(f'Final: ${final_pnl:,.2f}', 
                   xy=(len(equity)-1, final_pnl),
                   xytext=(10, 10), textcoords='offset points',
                   fontsize=10, fontweight='bold')
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
    
    def plot_drawdown(
        self,
        trade_results: List,
        output_path: Path
    ) -> None:
        if not trade_results:
            return
        
        fig, ax = plt.subplots(figsize=(14, 5))
        
        pnls = [t.total_pnl for t in trade_results]
        equity = np.array([0] + list(np.cumsum(pnls)))
        running_max = np.maximum.accumulate(equity)
        drawdown = running_max - equity
        
        ax.fill_between(range(len(drawdown)), -drawdown, 0,
                       color=self.colors['loss'], alpha=0.7)
        ax.plot(range(len(drawdown)), -drawdown, color='darkred', linewidth=1)
        
        max_dd_idx = np.argmax(drawdown)
        max_dd = drawdown[max_dd_idx]
        ax.scatter([max_dd_idx], [-max_dd], color='red', s=100, zorder=5)
        ax.annotate(f'Max DD: ${max_dd:,.2f}',
                   xy=(max_dd_idx, -max_dd),
                   xytext=(10, -20), textcoords='offset points',
                   fontsize=10, fontweight='bold')
        
        ax.set_xlabel('Trade Number', fontsize=12)
        ax.set_ylabel('Drawdown ($)', fontsize=12)
        ax.set_title('Drawdown Chart', fontsize=14, fontweight='bold')
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
    
    def plot_session_comparison(
        self,
        trade_results: List,
        metrics: dict,
        output_path: Path
    ) -> None:
        session_data = metrics.get('session_breakdown', {})
        
        if not session_data:
            return
        
        fig, axes = plt.subplots(1, 3, figsize=(15, 5))
        
        sessions = list(session_data.keys())
        trades = [session_data[s]['trades'] for s in sessions]
        pnls = [session_data[s]['pnl'] for s in sessions]
        win_rates = [session_data[s]['win_rate'] * 100 for s in sessions]
        
        colors = [self.colors.get(s, self.colors['primary']) for s in sessions]
        
        axes[0].bar(sessions, trades, color=colors)
        axes[0].set_ylabel('Number of Trades')
        axes[0].set_title('Trades by Session')
        
        bar_colors = [self.colors['profit'] if p >= 0 else self.colors['loss'] for p in pnls]
        axes[1].bar(sessions, pnls, color=bar_colors)
        axes[1].set_ylabel('P&L ($)')
        axes[1].set_title('P&L by Session')
        axes[1].axhline(y=0, color='black', linestyle='-', linewidth=0.5)
        
        axes[2].bar(sessions, win_rates, color=colors)
        axes[2].set_ylabel('Win Rate (%)')
        axes[2].set_title('Win Rate by Session')
        axes[2].axhline(y=50, color='gray', linestyle='--', linewidth=1)
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
    
    def plot_trade_distribution(
        self,
        trade_results: List,
        output_path: Path
    ) -> None:
        if not trade_results:
            return
        
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        
        hours = [t.entry_time.hour for t in trade_results]
        hour_pnls = {}
        for t in trade_results:
            h = t.entry_time.hour
            if h not in hour_pnls:
                hour_pnls[h] = []
            hour_pnls[h].append(t.total_pnl)
        
        sorted_hours = sorted(hour_pnls.keys())
        hour_totals = [sum(hour_pnls[h]) for h in sorted_hours]
        hour_counts = [len(hour_pnls[h]) for h in sorted_hours]
        
        bar_colors = [self.colors['profit'] if p >= 0 else self.colors['loss'] for p in hour_totals]
        axes[0].bar([str(h) for h in sorted_hours], hour_totals, color=bar_colors)
        axes[0].set_xlabel('Hour of Day')
        axes[0].set_ylabel('Total P&L ($)')
        axes[0].set_title('P&L by Hour')
        axes[0].tick_params(axis='x', rotation=45)
        
        axes[1].bar([str(h) for h in sorted_hours], hour_counts, color=self.colors['primary'])
        axes[1].set_xlabel('Hour of Day')
        axes[1].set_ylabel('Number of Trades')
        axes[1].set_title('Trade Count by Hour')
        axes[1].tick_params(axis='x', rotation=45)
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
    
    def plot_pnl_distribution(
        self,
        trade_results: List,
        output_path: Path
    ) -> None:
        if not trade_results:
            return
        
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        
        pnls = [t.total_pnl for t in trade_results]
        
        colors = [self.colors['profit'] if p >= 0 else self.colors['loss'] for p in pnls]
        n, bins, patches = axes[0].hist(pnls, bins=30, edgecolor='white', linewidth=0.5)
        
        for i, (patch, left_edge) in enumerate(zip(patches, bins[:-1])):
            if left_edge >= 0:
                patch.set_facecolor(self.colors['profit'])
            else:
                patch.set_facecolor(self.colors['loss'])
        
        axes[0].axvline(x=0, color='black', linestyle='-', linewidth=1)
        axes[0].axvline(x=np.mean(pnls), color=self.colors['primary'], linestyle='--', linewidth=2, label=f'Mean: ${np.mean(pnls):.2f}')
        axes[0].set_xlabel('P&L ($)')
        axes[0].set_ylabel('Frequency')
        axes[0].set_title('P&L Distribution')
        axes[0].legend()
        
        long_pnls = [t.total_pnl for t in trade_results if t.side == 'long']
        short_pnls = [t.total_pnl for t in trade_results if t.side == 'short']
        
        data = [long_pnls, short_pnls] if long_pnls and short_pnls else [pnls]
        labels = ['Long', 'Short'] if long_pnls and short_pnls else ['All']
        
        bp = axes[1].boxplot(data, labels=labels, patch_artist=True)
        colors_box = [self.colors['profit'], self.colors['loss']]
        for patch, color in zip(bp['boxes'], colors_box[:len(data)]):
            patch.set_facecolor(color)
            patch.set_alpha(0.7)
        
        axes[1].axhline(y=0, color='black', linestyle='-', linewidth=0.5)
        axes[1].set_ylabel('P&L ($)')
        axes[1].set_title('P&L by Direction')
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
    
    def plot_monte_carlo(
        self,
        mc_results: dict,
        output_path: Path
    ) -> None:
        if not mc_results:
            return
        
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        
        percentiles = mc_results.get('pnl_percentiles', {})
        pctl_values = [
            percentiles.get('5th', 0),
            percentiles.get('25th', 0),
            percentiles.get('50th', 0),
            percentiles.get('75th', 0),
            percentiles.get('95th', 0)
        ]
        pctl_labels = ['5th', '25th', '50th', '75th', '95th']
        
        colors = [self.colors['loss'] if v < 0 else self.colors['profit'] for v in pctl_values]
        axes[0].barh(pctl_labels, pctl_values, color=colors)
        axes[0].axvline(x=0, color='black', linestyle='-', linewidth=1)
        axes[0].set_xlabel('P&L ($)')
        axes[0].set_title('Monte Carlo P&L Percentiles')
        
        dd_data = mc_results.get('max_drawdown', {})
        dd_values = [
            dd_data.get('mean', 0),
            dd_data.get('95th_percentile', 0)
        ]
        dd_labels = ['Mean DD', '95th Percentile DD']
        
        axes[1].barh(dd_labels, dd_values, color=self.colors['loss'])
        axes[1].set_xlabel('Drawdown ($)')
        axes[1].set_title('Monte Carlo Drawdown Analysis')
        
        prob_profit = mc_results.get('probability_of_profit', 0)
        prob_ruin = mc_results.get('probability_of_ruin', 0)
        
        textstr = f'Probability of Profit: {prob_profit:.1%}\nProbability of Ruin: {prob_ruin:.1%}'
        axes[1].text(0.95, 0.95, textstr, transform=axes[1].transAxes, fontsize=10,
                    verticalalignment='top', horizontalalignment='right',
                    bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
    
    def plot_walk_forward(
        self,
        wf_results: dict,
        output_path: Path
    ) -> None:
        if not wf_results:
            return
        
        fig, axes = plt.subplots(1, 3, figsize=(15, 5))
        
        train = wf_results.get('train', {})
        test = wf_results.get('test', {})
        comparison = wf_results.get('comparison', {})
        
        metrics = ['trades', 'total_pnl', 'win_rate']
        labels = ['Trades', 'P&L', 'Win Rate']
        
        x = np.arange(len(metrics))
        width = 0.35
        
        train_values = [train.get(m, 0) for m in metrics]
        test_values = [test.get(m, 0) for m in metrics]
        
        for i, (ax, metric, label) in enumerate(zip(axes, metrics, labels)):
            train_val = train.get(metric, 0)
            test_val = test.get(metric, 0)
            
            bars = ax.bar(['Train', 'Test'], [train_val, test_val],
                         color=[self.colors['primary'], self.colors['secondary']])
            ax.set_ylabel(label)
            ax.set_title(f'{label}: Train vs Test')
            
            for bar, val in zip(bars, [train_val, test_val]):
                if metric == 'win_rate':
                    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height(),
                           f'{val:.1%}', ha='center', va='bottom')
                else:
                    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height(),
                           f'{val:.0f}' if metric == 'trades' else f'${val:,.0f}',
                           ha='center', va='bottom')
        
        degradation = comparison.get('performance_degradation', 0)
        is_robust = comparison.get('is_robust', False)
        
        status = "ROBUST" if is_robust else "NOT ROBUST"
        status_color = self.colors['profit'] if is_robust else self.colors['loss']
        
        fig.suptitle(f'Walk-Forward Validation | Degradation: {degradation:.1%} | Status: {status}',
                    fontsize=12, fontweight='bold', color=status_color)
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
    
    def plot_zone_confidence_analysis(
        self,
        trade_results: List,
        output_path: Path
    ) -> None:
        if not trade_results:
            return
        
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        
        conf_buckets = {'High (>0.75)': [], 'Medium (0.5-0.75)': [], 'Low (<0.5)': []}
        
        for t in trade_results:
            if t.zone_confidence > 0.75:
                conf_buckets['High (>0.75)'].append(t)
            elif t.zone_confidence >= 0.5:
                conf_buckets['Medium (0.5-0.75)'].append(t)
            else:
                conf_buckets['Low (<0.5)'].append(t)
        
        labels = list(conf_buckets.keys())
        win_rates = []
        avg_pnls = []
        
        for label in labels:
            trades = conf_buckets[label]
            if trades:
                wins = len([t for t in trades if t.total_pnl > 0])
                win_rates.append(wins / len(trades) * 100)
                avg_pnls.append(np.mean([t.total_pnl for t in trades]))
            else:
                win_rates.append(0)
                avg_pnls.append(0)
        
        axes[0].bar(labels, win_rates, color=[self.colors['profit'], self.colors['primary'], self.colors['loss']])
        axes[0].set_ylabel('Win Rate (%)')
        axes[0].set_title('Win Rate by Zone Confidence')
        axes[0].axhline(y=50, color='gray', linestyle='--')
        
        bar_colors = [self.colors['profit'] if p >= 0 else self.colors['loss'] for p in avg_pnls]
        axes[1].bar(labels, avg_pnls, color=bar_colors)
        axes[1].set_ylabel('Average P&L ($)')
        axes[1].set_title('Avg P&L by Zone Confidence')
        axes[1].axhline(y=0, color='black', linestyle='-')
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
    
    def plot_enhancement_impact(
        self,
        enhancement_data: dict,
        output_path: Path
    ) -> None:
        if not enhancement_data:
            return
        
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        
        be = enhancement_data.get('break_even', {})
        be_labels = ['With BE', 'Without BE']
        be_values = [be.get('avg_pnl_with_be', 0), be.get('avg_pnl_without_be', 0)]
        colors = [self.colors['profit'] if v >= 0 else self.colors['loss'] for v in be_values]
        axes[0, 0].bar(be_labels, be_values, color=colors)
        axes[0, 0].set_ylabel('Avg P&L ($)')
        axes[0, 0].set_title(f"Break-Even Impact ({be.get('triggered_count', 0)} triggers)")
        axes[0, 0].axhline(y=0, color='black', linestyle='-')
        
        pp = enhancement_data.get('partial_profits', {})
        pp_data = [pp.get('trades_with_partial', 0), pp.get('partial_pnl_captured', 0)]
        axes[0, 1].bar(['Trades w/ Partial', 'P&L Captured ($)'], pp_data, 
                       color=[self.colors['primary'], self.colors['profit']])
        axes[0, 1].set_title('Partial Profit Taking Impact')
        
        cd = enhancement_data.get('cooldown', {})
        cd_labels = ['Normal', 'During Cooldown']
        cd_pnls = [cd.get('pnl_normal', 0), cd.get('pnl_during_cooldown', 0)]
        colors = [self.colors['profit'] if v >= 0 else self.colors['loss'] for v in cd_pnls]
        axes[1, 0].bar(cd_labels, cd_pnls, color=colors)
        axes[1, 0].set_ylabel('Total P&L ($)')
        axes[1, 0].set_title('Cooldown Impact')
        axes[1, 0].axhline(y=0, color='black', linestyle='-')
        
        zc = enhancement_data.get('zone_confidence', {})
        zc_labels = ['High Conf', 'Low Conf']
        zc_win_rates = [zc.get('high_conf_win_rate', 0) * 100, zc.get('low_conf_win_rate', 0) * 100]
        axes[1, 1].bar(zc_labels, zc_win_rates, 
                       color=[self.colors['profit'], self.colors['loss']])
        axes[1, 1].set_ylabel('Win Rate (%)')
        axes[1, 1].set_title('Zone Confidence Win Rates')
        axes[1, 1].axhline(y=50, color='gray', linestyle='--')
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
    
    def create_interactive_dashboard(
        self,
        trade_results: List,
        metrics: dict,
        output_path: Path
    ) -> None:
        if not trade_results:
            return
        
        fig = make_subplots(
            rows=3, cols=2,
            subplot_titles=(
                'Equity Curve', 'Drawdown',
                'P&L Distribution', 'Session Breakdown',
                'Trade by Hour', 'Win Rate by Session'
            ),
            vertical_spacing=0.12,
            horizontal_spacing=0.1
        )
        
        pnls = [t.total_pnl for t in trade_results]
        equity = [0] + list(np.cumsum(pnls))
        
        fig.add_trace(
            go.Scatter(
                y=equity, 
                mode='lines+markers',
                name='Equity',
                line=dict(color='#3498db', width=2),
                marker=dict(size=4)
            ),
            row=1, col=1
        )
        
        equity_arr = np.array(equity)
        running_max = np.maximum.accumulate(equity_arr)
        drawdown = running_max - equity_arr
        
        fig.add_trace(
            go.Scatter(
                y=-drawdown,
                mode='lines',
                fill='tozeroy',
                name='Drawdown',
                line=dict(color='#e74c3c')
            ),
            row=1, col=2
        )
        
        fig.add_trace(
            go.Histogram(
                x=pnls,
                nbinsx=30,
                name='P&L',
                marker_color='#3498db'
            ),
            row=2, col=1
        )
        
        session_data = metrics.get('session_breakdown', {})
        sessions = list(session_data.keys())
        session_pnls = [session_data[s]['pnl'] for s in sessions]
        
        fig.add_trace(
            go.Bar(
                x=sessions,
                y=session_pnls,
                name='Session P&L',
                marker_color=['#2ecc71' if p >= 0 else '#e74c3c' for p in session_pnls]
            ),
            row=2, col=2
        )
        
        hour_data = metrics.get('hour_breakdown', {})
        hours = sorted(hour_data.keys(), key=lambda x: int(x))
        hour_trades = [hour_data[h]['trades'] for h in hours]
        
        fig.add_trace(
            go.Bar(
                x=hours,
                y=hour_trades,
                name='Trades by Hour',
                marker_color='#9b59b6'
            ),
            row=3, col=1
        )
        
        session_win_rates = [session_data[s]['win_rate'] * 100 for s in sessions]
        
        fig.add_trace(
            go.Bar(
                x=sessions,
                y=session_win_rates,
                name='Win Rate',
                marker_color='#1abc9c'
            ),
            row=3, col=2
        )
        
        fig.update_layout(
            title_text='MGC Scalping Engine - Backtest Dashboard',
            title_font_size=20,
            showlegend=False,
            height=1000,
            template='plotly_dark'
        )
        
        fig.write_html(str(output_path))

