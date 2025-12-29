import numpy as np
from typing import List, Dict
from dataclasses import dataclass


@dataclass
class MonteCarloResults:
    iterations: int
    pnl_mean: float
    pnl_std: float
    pnl_5th_percentile: float
    pnl_25th_percentile: float
    pnl_50th_percentile: float
    pnl_75th_percentile: float
    pnl_95th_percentile: float
    max_drawdown_mean: float
    max_drawdown_std: float
    max_drawdown_95th: float
    probability_of_profit: float
    probability_of_ruin: float
    ruin_threshold: float
    sharpe_ratio_mean: float
    
    def to_dict(self) -> dict:
        return {
            'iterations': self.iterations,
            'pnl_mean': self.pnl_mean,
            'pnl_std': self.pnl_std,
            'pnl_percentiles': {
                '5th': self.pnl_5th_percentile,
                '25th': self.pnl_25th_percentile,
                '50th': self.pnl_50th_percentile,
                '75th': self.pnl_75th_percentile,
                '95th': self.pnl_95th_percentile
            },
            'max_drawdown': {
                'mean': self.max_drawdown_mean,
                'std': self.max_drawdown_std,
                '95th_percentile': self.max_drawdown_95th
            },
            'probability_of_profit': self.probability_of_profit,
            'probability_of_ruin': self.probability_of_ruin,
            'ruin_threshold': self.ruin_threshold,
            'sharpe_ratio_mean': self.sharpe_ratio_mean
        }


class MonteCarloSimulator:
    def __init__(self, config: dict):
        self.config = config
        
        mc_config = config.get('monte_carlo', {})
        self.iterations = mc_config.get('iterations', 1000)
        self.confidence_level = mc_config.get('confidence_level', 0.95)
        self.ruin_threshold = config.get('daily_loss_limit', -800) * 5
        
    def run_simulation(self, trade_results: List) -> dict:
        if not trade_results:
            return self._empty_results()
        
        pnls = np.array([t.total_pnl for t in trade_results])
        
        final_pnls = []
        max_drawdowns = []
        equity_curves = []
        
        np.random.seed(42)
        
        for _ in range(self.iterations):
            shuffled_pnls = np.random.permutation(pnls)
            
            equity_curve = np.cumsum(shuffled_pnls)
            equity_curves.append(equity_curve)
            
            final_pnls.append(equity_curve[-1])
            
            running_max = np.maximum.accumulate(np.concatenate([[0], equity_curve]))
            drawdown = running_max[1:] - equity_curve
            max_dd = np.max(drawdown) if len(drawdown) > 0 else 0
            max_drawdowns.append(max_dd)
        
        final_pnls = np.array(final_pnls)
        max_drawdowns = np.array(max_drawdowns)
        
        pnl_mean = np.mean(final_pnls)
        pnl_std = np.std(final_pnls)
        
        pnl_percentiles = np.percentile(final_pnls, [5, 25, 50, 75, 95])
        
        dd_mean = np.mean(max_drawdowns)
        dd_std = np.std(max_drawdowns)
        dd_95 = np.percentile(max_drawdowns, 95)
        
        prob_profit = np.mean(final_pnls > 0)
        prob_ruin = np.mean(final_pnls < self.ruin_threshold)
        
        returns = pnls / 1000
        if len(returns) > 1 and np.std(returns) > 0:
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
        else:
            sharpe = 0.0
        
        results = MonteCarloResults(
            iterations=self.iterations,
            pnl_mean=float(pnl_mean),
            pnl_std=float(pnl_std),
            pnl_5th_percentile=float(pnl_percentiles[0]),
            pnl_25th_percentile=float(pnl_percentiles[1]),
            pnl_50th_percentile=float(pnl_percentiles[2]),
            pnl_75th_percentile=float(pnl_percentiles[3]),
            pnl_95th_percentile=float(pnl_percentiles[4]),
            max_drawdown_mean=float(dd_mean),
            max_drawdown_std=float(dd_std),
            max_drawdown_95th=float(dd_95),
            probability_of_profit=float(prob_profit),
            probability_of_ruin=float(prob_ruin),
            ruin_threshold=float(self.ruin_threshold),
            sharpe_ratio_mean=float(sharpe)
        )
        
        return results.to_dict()
    
    def run_bootstrap_analysis(
        self,
        trade_results: List,
        sample_size: int = None
    ) -> dict:
        if not trade_results:
            return {}
        
        pnls = np.array([t.total_pnl for t in trade_results])
        n_trades = len(pnls)
        
        if sample_size is None:
            sample_size = n_trades
        
        bootstrap_means = []
        bootstrap_win_rates = []
        
        for _ in range(self.iterations):
            sample_indices = np.random.choice(n_trades, size=sample_size, replace=True)
            sample_pnls = pnls[sample_indices]
            
            bootstrap_means.append(np.mean(sample_pnls))
            bootstrap_win_rates.append(np.mean(sample_pnls > 0))
        
        bootstrap_means = np.array(bootstrap_means)
        bootstrap_win_rates = np.array(bootstrap_win_rates)
        
        alpha = 1 - self.confidence_level
        
        return {
            'mean_pnl': {
                'estimate': float(np.mean(bootstrap_means)),
                'std_error': float(np.std(bootstrap_means)),
                'ci_lower': float(np.percentile(bootstrap_means, alpha/2 * 100)),
                'ci_upper': float(np.percentile(bootstrap_means, (1 - alpha/2) * 100))
            },
            'win_rate': {
                'estimate': float(np.mean(bootstrap_win_rates)),
                'std_error': float(np.std(bootstrap_win_rates)),
                'ci_lower': float(np.percentile(bootstrap_win_rates, alpha/2 * 100)),
                'ci_upper': float(np.percentile(bootstrap_win_rates, (1 - alpha/2) * 100))
            }
        }
    
    def get_equity_curve_distribution(
        self,
        trade_results: List,
        num_paths: int = 100
    ) -> Dict[str, List]:
        if not trade_results:
            return {'paths': [], 'percentiles': {}}
        
        pnls = np.array([t.total_pnl for t in trade_results])
        n_trades = len(pnls)
        
        all_paths = []
        
        for _ in range(num_paths):
            shuffled = np.random.permutation(pnls)
            path = np.concatenate([[0], np.cumsum(shuffled)])
            all_paths.append(path.tolist())
        
        paths_array = np.array(all_paths)
        
        percentiles = {
            '5th': np.percentile(paths_array, 5, axis=0).tolist(),
            '25th': np.percentile(paths_array, 25, axis=0).tolist(),
            '50th': np.percentile(paths_array, 50, axis=0).tolist(),
            '75th': np.percentile(paths_array, 75, axis=0).tolist(),
            '95th': np.percentile(paths_array, 95, axis=0).tolist()
        }
        
        return {
            'paths': all_paths[:10],
            'percentiles': percentiles
        }
    
    def _empty_results(self) -> dict:
        return MonteCarloResults(
            iterations=0,
            pnl_mean=0.0,
            pnl_std=0.0,
            pnl_5th_percentile=0.0,
            pnl_25th_percentile=0.0,
            pnl_50th_percentile=0.0,
            pnl_75th_percentile=0.0,
            pnl_95th_percentile=0.0,
            max_drawdown_mean=0.0,
            max_drawdown_std=0.0,
            max_drawdown_95th=0.0,
            probability_of_profit=0.0,
            probability_of_ruin=0.0,
            ruin_threshold=0.0,
            sharpe_ratio_mean=0.0
        ).to_dict()

