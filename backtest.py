import json
import pandas as pd
from pathlib import Path
from typing import Optional, List
import argparse

from strategy import Strategy, SignalType
from risk import RiskManager, TradeResult
from reporting import ReportGenerator
from monte_carlo import MonteCarloSimulator
from walk_forward import WalkForwardValidator


class BacktestEngine:
    def __init__(self, config_path: str = 'config.json'):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        
        self.strategy = Strategy(self.config)
        self.risk_manager = RiskManager(self.config)
        self.report_generator = ReportGenerator(self.config)
        
        self.data: Optional[pd.DataFrame] = None
        self.results: List[TradeResult] = []
        
    def _load_config(self) -> dict:
        with open(self.config_path, 'r') as f:
            return json.load(f)
    
    def load_data(self, data_path: str = 'data.csv') -> pd.DataFrame:
        df = pd.read_csv(data_path, comment='#')  # Skip comment lines
        
        required_cols = ['timestamp', 'open', 'high', 'low', 'close']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Missing required column: {col}")
        
        # Validate that data came from TopStep API
        # Check for contract column or metadata that indicates TopStep source
        has_contract_info = 'contract' in df.columns
        
        if has_contract_info:
            # Data has contract info - verify it's TopStep MGC
            topstep_contracts = df['contract'].unique()
            if not any('CON.F.US.MGC' in str(c) for c in topstep_contracts):
                raise ValueError("Data does not appear to be from TopStep MGC contracts. Please use fetch_extended_data.py to fetch TopStep data.")
        else:
            # No contract column - check if data looks valid for MGC
            if len(df) > 0:
                # Check price range - MGC should be around $4000-5000 range
                avg_price = df['close'].mean()
                
                if avg_price < 1000 or avg_price > 10000:
                    raise ValueError(
                        f"Data price range (${avg_price:.2f}) doesn't match MGC. "
                        "Please fetch data from TopStep using: python fetch_extended_data.py --days 90 --interval 3"
                    )
                
                # Warn if no contract info
                import warnings
                warnings.warn(
                    "Data file missing 'contract' column. "
                    "For best results, use fetch_extended_data.py to fetch TopStep data.",
                    UserWarning
                )
        
        if 'volume' not in df.columns:
            df['volume'] = 1
        
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp').reset_index(drop=True)
        
        self.data = df
        return df
    
    def load_blackout_dates(self, blackout_path: str = 'blackout_dates.csv') -> None:
        if Path(blackout_path).exists():
            self.strategy.load_blackout_dates(blackout_path)
    
    def run(self) -> List[TradeResult]:
        if self.data is None:
            raise ValueError("No data loaded. Call load_data() first.")
        
        df = self.strategy.prepare_data(self.data)
        
        # Check if limit order retest is enabled
        limit_order_enabled = self.config.get('limit_order_retest', {}).get('enabled', False)
        entry_offset_ticks = self.config.get('limit_order_retest', {}).get('entry_offset_ticks', 1)
        tick_size = self.config.get('tick_size', 0.10)
        
        for i in range(len(df)):
            bar = df.iloc[i]
            date = pd.Timestamp(bar['timestamp']).date()
            
            # Check for broken zones and convert them (role reversal) if enabled
            role_reversal_enabled = self.config.get('zone_role_reversal', {}).get('enabled', True)
            if role_reversal_enabled:
                converted_zones = self.strategy.zone_manager.invalidate_broken_zones(
                    bar['close'], i
                )
            
            self.risk_manager.tick_cooldown()
            
            # Check pending limit orders first
            if self.risk_manager.has_pending_orders() and self.risk_manager.current_position is None:
                filled_pos = self.risk_manager.check_pending_orders(bar, i, date)
                if filled_pos is not None:
                    # Position was just opened via limit order, continue to next bar
                    continue
            
            if self.risk_manager.current_position is not None:
                result, _ = self.risk_manager.update_position(bar, i)
                if result is not None:
                    self.results.append(result)
                continue
            
            can_trade, reason = self.risk_manager.can_trade(date)
            if not can_trade:
                continue
            
            signal = self.strategy.generate_signal(
                df=df,
                bar_index=i,
                daily_trades=self.risk_manager.get_daily_trades(date),
                daily_pnl=self.risk_manager.get_daily_pnl(date),
                in_cooldown=self.risk_manager.is_in_cooldown()
            )
            
            if signal is None:
                continue
            
            if signal.signal_type == SignalType.NONE:
                continue
            
            side = 'long' if signal.signal_type == SignalType.LONG else 'short'
            
            if limit_order_enabled:
                # Create pending limit order instead of entering immediately
                # For long: place limit at zone high + offset (better price)
                # For short: place limit at zone low - offset (better price)
                if side == 'long':
                    limit_price = signal.zone.high + (entry_offset_ticks * tick_size)
                else:
                    limit_price = signal.zone.low - (entry_offset_ticks * tick_size)
                
                # Recalculate risk/reward with limit price
                if side == 'long':
                    risk = limit_price - signal.stop_loss
                    reward = signal.take_profit - limit_price
                else:
                    risk = signal.stop_loss - limit_price
                    reward = limit_price - signal.take_profit
                
                if risk > 0 and reward > 0:
                    risk_ticks = risk / tick_size
                    reward_ticks = reward / tick_size
                    
                    self.risk_manager.create_pending_order(
                        side=side,
                        limit_price=limit_price,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                        session=signal.session,
                        zone_confidence=signal.zone_confidence,
                        confirmation_type=signal.confirmation_type,
                        risk_ticks=risk_ticks,
                        reward_ticks=reward_ticks,
                        structure_levels=signal.structure_levels or [],
                        bar_index=i,
                        timestamp=signal.timestamp
                    )
            else:
                # Immediate market order entry
                self.risk_manager.open_position(
                    side=side,
                    entry_price=signal.entry_price,
                    entry_time=signal.timestamp,
                    entry_index=i,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    session=signal.session,
                    zone_confidence=signal.zone_confidence,
                    confirmation_type=signal.confirmation_type,
                    risk_ticks=signal.risk_ticks,
                    reward_ticks=signal.reward_ticks,
                    structure_levels=signal.structure_levels
                )
        
        if self.risk_manager.current_position is not None:
            last_bar = df.iloc[-1]
            pos = self.risk_manager.current_position
            
            exit_price = last_bar['close']
            timestamp = pd.Timestamp(last_bar['timestamp'])
            
            tick_size = self.config.get('tick_size', 0.10)
            tick_value = self.config.get('tick_value', 1.00)
            commission = self.config.get('commission_per_contract', 0.62)
            
            if pos.side == 'long':
                pnl_ticks = (exit_price - pos.entry_price) / tick_size
            else:
                pnl_ticks = (pos.entry_price - exit_price) / tick_size
            
            gross_pnl = pnl_ticks * tick_value * pos.remaining_contracts
            net_pnl = gross_pnl - commission * pos.remaining_contracts * 2
            
            result = TradeResult(
                trade_id=pos.trade_id,
                side=pos.side,
                session=pos.session,
                entry_time=pos.entry_time,
                entry_price=pos.entry_price,
                stop_loss=pos.initial_stop_loss,
                take_profit=pos.take_profit,
                zone_confidence=pos.zone_confidence,
                confirmation_type=pos.confirmation_type,
                partial_exit_time=pos.partial_exit_time,
                partial_exit_price=pos.partial_exit_price,
                partial_pnl=pos.partial_pnl,
                final_exit_time=timestamp,
                final_exit_price=exit_price,
                final_pnl=net_pnl,
                total_pnl=pos.partial_pnl + net_pnl,
                result_ticks=pnl_ticks,
                break_even_triggered=pos.break_even_triggered,
                exit_reason='end_of_data',
                cooldown_active=False
            )
            
            self.results.append(result)
            self.risk_manager.current_position = None
        
        return self.results
    
    def generate_reports(self, output_dir: str = '.') -> dict:
        output_path = Path(output_dir)
        
        trades_df = self.report_generator.results_to_dataframe(self.results)
        trades_path = output_path / 'trades.csv'
        trades_df.to_csv(trades_path, index=False)
        
        metrics = self.report_generator.calculate_metrics(self.results)
        
        mc_config = self.config.get('monte_carlo', {})
        if mc_config.get('enabled', True):
            mc_simulator = MonteCarloSimulator(self.config)
            mc_results = mc_simulator.run_simulation(self.results)
            metrics['monte_carlo'] = mc_results
        
        wf_config = self.config.get('walk_forward', {})
        if wf_config.get('enabled', True) and self.data is not None:
            wf_validator = WalkForwardValidator(self.config)
            wf_results = wf_validator.validate(self.data, self._run_backtest_on_data)
            metrics['walk_forward'] = wf_results
        
        results_path = output_path / 'results.json'
        with open(results_path, 'w') as f:
            json.dump(metrics, f, indent=2, default=str)
        
        return metrics
    
    def _run_backtest_on_data(self, data: pd.DataFrame) -> List[TradeResult]:
        temp_strategy = Strategy(self.config)
        temp_risk = RiskManager(self.config)
        
        df = temp_strategy.prepare_data(data)
        results = []
        
        for i in range(len(df)):
            bar = df.iloc[i]
            date = pd.Timestamp(bar['timestamp']).date()
            
            temp_risk.tick_cooldown()
            
            if temp_risk.current_position is not None:
                current_price = bar['close']
                if temp_risk.should_force_exit(date, current_price):
                    result = temp_risk.force_close_position(
                        exit_price=current_price,
                        timestamp=pd.Timestamp(bar['timestamp']),
                        bar_index=i
                    )
                    if result is not None:
                        results.append(result)
                    continue
                
                result, _ = temp_risk.update_position(bar, i)
                if result is not None:
                    results.append(result)
                continue
            
            can_trade, _ = temp_risk.can_trade(date)
            if not can_trade:
                continue
            
            signal = temp_strategy.generate_signal(
                df=df,
                bar_index=i,
                daily_trades=temp_risk.get_daily_trades(date),
                daily_pnl=temp_risk.get_daily_pnl(date),
                in_cooldown=temp_risk.is_in_cooldown()
            )
            
            if signal is None or signal.signal_type == SignalType.NONE:
                continue
            
            side = 'long' if signal.signal_type == SignalType.LONG else 'short'
            
            temp_risk.open_position(
                side=side,
                entry_price=signal.entry_price,
                entry_time=signal.timestamp,
                entry_index=i,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                session=signal.session,
                zone_confidence=signal.zone_confidence,
                confirmation_type=signal.confirmation_type,
                risk_ticks=signal.risk_ticks,
                reward_ticks=signal.reward_ticks,
                structure_levels=signal.structure_levels
            )
        
        return results
    
    def reset(self) -> None:
        self.strategy.reset()
        self.risk_manager.reset()
        self.results = []


def main():
    parser = argparse.ArgumentParser(description='MGC Scalping Engine Backtest')
    parser.add_argument('--config', type=str, default='config.json',
                        help='Path to config file')
    parser.add_argument('--data', type=str, default='data.csv',
                        help='Path to data file')
    parser.add_argument('--blackout', type=str, default='blackout_dates.csv',
                        help='Path to blackout dates file')
    parser.add_argument('--output', type=str, default='.',
                        help='Output directory')
    parser.add_argument('--visualize', action='store_true',
                        help='Generate visualization charts')
    
    args = parser.parse_args()
    
    engine = BacktestEngine(config_path=args.config)
    
    engine.load_data(args.data)
    engine.load_blackout_dates(args.blackout)
    
    results = engine.run()
    
    metrics = engine.generate_reports(output_dir=args.output)
    
    print("\n" + "="*50)
    print("BACKTEST RESULTS")
    print("="*50)
    print(f"Total Trades: {metrics.get('total_trades', 0)}")
    print(f"Win Rate: {metrics.get('win_rate', 0):.1%}")
    print(f"Total P&L: ${metrics.get('total_pnl', 0):.2f}")
    print(f"Avg P&L/Trade: ${metrics.get('avg_pnl_per_trade', 0):.2f}")
    print(f"Profit Factor: {metrics.get('profit_factor', 0):.2f}")
    print(f"Max Drawdown: ${metrics.get('max_drawdown', 0):.2f}")
    print("="*50)
    
    if 'session_breakdown' in metrics:
        print("\nSession Breakdown:")
        for session, stats in metrics['session_breakdown'].items():
            print(f"  {session}: {stats.get('trades', 0)} trades, "
                  f"${stats.get('pnl', 0):.2f} P&L, "
                  f"{stats.get('win_rate', 0):.1%} win rate")
    
    if args.visualize:
        from visualize import Visualizer
        viz = Visualizer(engine.config)
        viz.plot_all(results, metrics, output_dir=args.output)
        print("\nCharts saved to output directory")
    
    print(f"\nResults saved to {args.output}/")


if __name__ == '__main__':
    main()

