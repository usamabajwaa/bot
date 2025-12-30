import json
import pandas as pd
from pathlib import Path
from typing import List, Optional
from strategy import Strategy, SignalType
from risk import RiskManager, TradeResult
from reporting import ReportGenerator


class ReplayEngine:
    """Replay saved live bars through strategy for realistic backtesting"""
    
    def __init__(self, config_path: str = 'config.json'):
        self.config = self._load_config(config_path)
        self.strategy = Strategy(self.config)
        self.risk_manager = RiskManager(self.config)
        self.report_generator = ReportGenerator(self.config)
        self.results: List[TradeResult] = []
    
    def _load_config(self, path: str) -> dict:
        with open(path, 'r') as f:
            return json.load(f)
    
    def load_replay_data(self, replay_file: str) -> pd.DataFrame:
        """Load bars saved from live trading"""
        df = pd.read_csv(replay_file)
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        return df.sort_values('timestamp').reset_index(drop=True)
    
    def replay(self, replay_file: str) -> List[TradeResult]:
        """Replay saved bars through strategy - more realistic than backtest"""
        df = self.load_replay_data(replay_file)
        df = self.strategy.prepare_data(df)
        
        # Same logic as backtest but uses exact live bars
        limit_order_enabled = self.config.get('limit_order_retest', {}).get('enabled', False)
        entry_offset_ticks = self.config.get('limit_order_retest', {}).get('entry_offset_ticks', 1)
        tick_size = self.config.get('tick_size', 0.10)
        
        for i in range(len(df)):
            bar = df.iloc[i]
            date = pd.Timestamp(bar['timestamp']).date()
            
            self.risk_manager.tick_cooldown()
            
            # Check pending limit orders first
            if self.risk_manager.has_pending_orders() and self.risk_manager.current_position is None:
                filled_pos = self.risk_manager.check_pending_orders(bar, i, date)
                if filled_pos is not None:
                    continue
            
            if self.risk_manager.current_position is not None:
                result, _ = self.risk_manager.update_position(bar, i)
                if result is not None:
                    self.results.append(result)
                continue
            
            can_trade, _ = self.risk_manager.can_trade(date)
            if not can_trade:
                continue
            
            signal = self.strategy.generate_signal(
                df=df,
                bar_index=i,
                daily_trades=self.risk_manager.get_daily_trades(date),
                daily_pnl=self.risk_manager.get_daily_pnl(date),
                in_cooldown=self.risk_manager.is_in_cooldown()
            )
            
            if signal is None or signal.signal_type == SignalType.NONE:
                continue
            
            side = 'long' if signal.signal_type == SignalType.LONG else 'short'
            
            if limit_order_enabled:
                # Create pending limit order instead of entering immediately
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
                # Get indicator values and filter results from signal if available
                indicator_values = getattr(signal, 'indicator_values', None)
                filter_results = getattr(signal, 'filter_results', None)
                
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
                    structure_levels=signal.structure_levels,
                    indicator_values=indicator_values,
                    filter_results=filter_results
                )
        
        # Close any remaining position at end of data
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
            
            from risk import TradeResult
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
        """Generate reports similar to backtest"""
        output_path = Path(output_dir)
        trades_df = self.report_generator.results_to_dataframe(self.results)
        trades_path = output_path / 'replay_trades.csv'
        trades_df.to_csv(trades_path, index=False)
        
        metrics = self.report_generator.calculate_metrics(self.results)
        
        results_path = output_path / 'replay_results.json'
        with open(results_path, 'w') as f:
            json.dump(metrics, f, indent=2, default=str)
        
        return metrics


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Replay saved live bars through strategy')
    parser.add_argument('--replay-file', type=str, required=True, help='Path to saved replay CSV')
    parser.add_argument('--config', type=str, default='config.json', help='Config file path')
    parser.add_argument('--output', type=str, default='.', help='Output directory')
    
    args = parser.parse_args()
    
    engine = ReplayEngine(config_path=args.config)
    results = engine.replay(args.replay_file)
    metrics = engine.generate_reports(output_dir=args.output)
    
    print("\n" + "="*50)
    print("REPLAY RESULTS")
    print("="*50)
    print(f"Total Trades: {metrics.get('total_trades', 0)}")
    print(f"Win Rate: {metrics.get('win_rate', 0):.1%}")
    print(f"Total P&L: ${metrics.get('total_pnl', 0):.2f}")
    print(f"Avg P&L/Trade: ${metrics.get('avg_pnl_per_trade', 0):.2f}")
    print(f"Profit Factor: {metrics.get('profit_factor', 0):.2f}")
    print(f"Max Drawdown: ${metrics.get('max_drawdown', 0):.2f}")
    print("="*50)
    print(f"\nResults saved to {args.output}/")


if __name__ == '__main__':
    main()

