import pandas as pd
import numpy as np
from typing import List, Callable, Dict
from dataclasses import dataclass


@dataclass
class WalkForwardResults:
    train_trades: int
    test_trades: int
    train_pnl: float
    test_pnl: float
    train_win_rate: float
    test_win_rate: float
    train_avg_pnl: float
    test_avg_pnl: float
    train_profit_factor: float
    test_profit_factor: float
    train_max_drawdown: float
    test_max_drawdown: float
    performance_degradation: float
    is_robust: bool
    robustness_threshold: float
    
    def to_dict(self) -> dict:
        return {
            'train': {
                'trades': self.train_trades,
                'total_pnl': self.train_pnl,
                'win_rate': self.train_win_rate,
                'avg_pnl': self.train_avg_pnl,
                'profit_factor': self.train_profit_factor,
                'max_drawdown': self.train_max_drawdown
            },
            'test': {
                'trades': self.test_trades,
                'total_pnl': self.test_pnl,
                'win_rate': self.test_win_rate,
                'avg_pnl': self.test_avg_pnl,
                'profit_factor': self.test_profit_factor,
                'max_drawdown': self.test_max_drawdown
            },
            'comparison': {
                'performance_degradation': self.performance_degradation,
                'is_robust': self.is_robust,
                'robustness_threshold': self.robustness_threshold
            }
        }


class WalkForwardValidator:
    def __init__(self, config: dict):
        self.config = config
        
        wf_config = config.get('walk_forward', {})
        self.train_pct = wf_config.get('train_pct', 0.70)
        self.test_pct = wf_config.get('test_pct', 0.30)
        self.robustness_threshold = 0.20
        
    def validate(
        self,
        data: pd.DataFrame,
        backtest_func: Callable[[pd.DataFrame], List]
    ) -> dict:
        data = data.copy()
        data = data.sort_values('timestamp').reset_index(drop=True)
        
        split_idx = int(len(data) * self.train_pct)
        
        train_data = data.iloc[:split_idx].copy()
        test_data = data.iloc[split_idx:].copy()
        
        train_results = backtest_func(train_data)
        test_results = backtest_func(test_data)
        
        train_metrics = self._calculate_metrics(train_results)
        test_metrics = self._calculate_metrics(test_results)
        
        degradation = self._calculate_degradation(train_metrics, test_metrics)
        is_robust = abs(degradation) < self.robustness_threshold
        
        results = WalkForwardResults(
            train_trades=train_metrics['trades'],
            test_trades=test_metrics['trades'],
            train_pnl=train_metrics['total_pnl'],
            test_pnl=test_metrics['total_pnl'],
            train_win_rate=train_metrics['win_rate'],
            test_win_rate=test_metrics['win_rate'],
            train_avg_pnl=train_metrics['avg_pnl'],
            test_avg_pnl=test_metrics['avg_pnl'],
            train_profit_factor=train_metrics['profit_factor'],
            test_profit_factor=test_metrics['profit_factor'],
            train_max_drawdown=train_metrics['max_drawdown'],
            test_max_drawdown=test_metrics['max_drawdown'],
            performance_degradation=degradation,
            is_robust=is_robust,
            robustness_threshold=self.robustness_threshold
        )
        
        return results.to_dict()
    
    def rolling_walk_forward(
        self,
        data: pd.DataFrame,
        backtest_func: Callable[[pd.DataFrame], List],
        num_folds: int = 5
    ) -> List[dict]:
        data = data.copy()
        data = data.sort_values('timestamp').reset_index(drop=True)
        
        fold_size = len(data) // num_folds
        results = []
        
        for i in range(num_folds - 1):
            train_end = (i + 1) * fold_size
            test_start = train_end
            test_end = min((i + 2) * fold_size, len(data))
            
            train_data = data.iloc[:train_end].copy()
            test_data = data.iloc[test_start:test_end].copy()
            
            train_results = backtest_func(train_data)
            test_results = backtest_func(test_data)
            
            train_metrics = self._calculate_metrics(train_results)
            test_metrics = self._calculate_metrics(test_results)
            
            fold_result = {
                'fold': i + 1,
                'train_period': {
                    'start': str(train_data['timestamp'].iloc[0]),
                    'end': str(train_data['timestamp'].iloc[-1]),
                    'bars': len(train_data)
                },
                'test_period': {
                    'start': str(test_data['timestamp'].iloc[0]),
                    'end': str(test_data['timestamp'].iloc[-1]),
                    'bars': len(test_data)
                },
                'train_metrics': train_metrics,
                'test_metrics': test_metrics,
                'degradation': self._calculate_degradation(train_metrics, test_metrics)
            }
            
            results.append(fold_result)
        
        return results
    
    def anchored_walk_forward(
        self,
        data: pd.DataFrame,
        backtest_func: Callable[[pd.DataFrame], List],
        test_periods: int = 4
    ) -> List[dict]:
        data = data.copy()
        data = data.sort_values('timestamp').reset_index(drop=True)
        
        initial_train_size = int(len(data) * 0.5)
        remaining_size = len(data) - initial_train_size
        test_size = remaining_size // test_periods
        
        results = []
        
        for i in range(test_periods):
            train_end = initial_train_size + i * test_size
            test_start = train_end
            test_end = min(test_start + test_size, len(data))
            
            if test_end <= test_start:
                break
            
            train_data = data.iloc[:train_end].copy()
            test_data = data.iloc[test_start:test_end].copy()
            
            train_results = backtest_func(train_data)
            test_results = backtest_func(test_data)
            
            train_metrics = self._calculate_metrics(train_results)
            test_metrics = self._calculate_metrics(test_results)
            
            fold_result = {
                'period': i + 1,
                'train_size': len(train_data),
                'test_size': len(test_data),
                'train_metrics': train_metrics,
                'test_metrics': test_metrics,
                'degradation': self._calculate_degradation(train_metrics, test_metrics)
            }
            
            results.append(fold_result)
        
        return results
    
    def _calculate_metrics(self, trade_results: List) -> dict:
        if not trade_results:
            return {
                'trades': 0,
                'total_pnl': 0.0,
                'win_rate': 0.0,
                'avg_pnl': 0.0,
                'profit_factor': 0.0,
                'max_drawdown': 0.0
            }
        
        pnls = [t.total_pnl for t in trade_results]
        total_pnl = sum(pnls)
        
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        
        win_rate = len(wins) / len(pnls) if pnls else 0.0
        avg_pnl = total_pnl / len(pnls) if pnls else 0.0
        
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0.0
        
        equity = np.cumsum(pnls)
        running_max = np.maximum.accumulate(np.concatenate([[0], equity]))
        drawdown = running_max[1:] - equity
        max_dd = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0
        
        return {
            'trades': len(trade_results),
            'total_pnl': float(total_pnl),
            'win_rate': float(win_rate),
            'avg_pnl': float(avg_pnl),
            'profit_factor': float(min(profit_factor, 100.0)),
            'max_drawdown': max_dd
        }
    
    def _calculate_degradation(
        self,
        train_metrics: dict,
        test_metrics: dict
    ) -> float:
        if train_metrics['avg_pnl'] == 0:
            return 0.0
        
        train_avg = train_metrics['avg_pnl']
        test_avg = test_metrics['avg_pnl']
        
        degradation = (train_avg - test_avg) / abs(train_avg) if train_avg != 0 else 0.0
        
        return float(degradation)

