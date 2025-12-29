import numpy as np
import pandas as pd
from typing import Tuple, List, Optional
from dataclasses import dataclass


@dataclass
class PivotPoint:
    index: int
    timestamp: pd.Timestamp
    price: float
    pivot_type: str


class Indicators:
    def __init__(self, config: dict):
        self.config = config
        self.tick_size = config.get('tick_size', 0.10)
        
    def compute_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        atr = tr.ewm(span=period, adjust=False).mean()
        
        return atr
    
    def compute_vwap(self, df: pd.DataFrame, reset_daily: bool = True) -> pd.Series:
        df = df.copy()
        
        if 'volume' not in df.columns or df['volume'].isna().all():
            df['volume'] = 1
        
        df['volume'] = df['volume'].fillna(1).replace(0, 1)
        
        df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
        df['tp_volume'] = df['typical_price'] * df['volume']
        
        if reset_daily:
            df['date'] = pd.to_datetime(df['timestamp']).dt.date
            df['cum_tp_vol'] = df.groupby('date')['tp_volume'].cumsum()
            df['cum_vol'] = df.groupby('date')['volume'].cumsum()
        else:
            df['cum_tp_vol'] = df['tp_volume'].cumsum()
            df['cum_vol'] = df['volume'].cumsum()
        
        vwap = df['cum_tp_vol'] / df['cum_vol']
        
        return vwap
    
    def compute_vwap_bands(
        self,
        df: pd.DataFrame,
        vwap: pd.Series,
        atr: pd.Series,
        band_atr_mult: float = 0.20,
        min_band_ticks: int = 2
    ) -> Tuple[pd.Series, pd.Series]:
        min_band = min_band_ticks * self.tick_size
        band_width = np.maximum(band_atr_mult * atr, min_band)
        
        upper_band = vwap + band_width
        lower_band = vwap - band_width
        
        return upper_band, lower_band
    
    def detect_pivot_highs(
        self,
        df: pd.DataFrame,
        strength: int = 2
    ) -> List[PivotPoint]:
        pivots = []
        highs = df['high'].values
        timestamps = df['timestamp'].values
        
        for i in range(strength, len(df) - strength):
            is_pivot = True
            
            for j in range(1, strength + 1):
                if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                    is_pivot = False
                    break
            
            if is_pivot:
                pivots.append(PivotPoint(
                    index=i,
                    timestamp=pd.Timestamp(timestamps[i]),
                    price=highs[i],
                    pivot_type='high'
                ))
        
        return pivots
    
    def detect_pivot_lows(
        self,
        df: pd.DataFrame,
        strength: int = 2
    ) -> List[PivotPoint]:
        pivots = []
        lows = df['low'].values
        timestamps = df['timestamp'].values
        
        for i in range(strength, len(df) - strength):
            is_pivot = True
            
            for j in range(1, strength + 1):
                if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                    is_pivot = False
                    break
            
            if is_pivot:
                pivots.append(PivotPoint(
                    index=i,
                    timestamp=pd.Timestamp(timestamps[i]),
                    price=lows[i],
                    pivot_type='low'
                ))
        
        return pivots
    
    def detect_all_pivots(
        self,
        df: pd.DataFrame,
        strength: int = 2
    ) -> Tuple[List[PivotPoint], List[PivotPoint]]:
        pivot_highs = self.detect_pivot_highs(df, strength)
        pivot_lows = self.detect_pivot_lows(df, strength)
        
        return pivot_highs, pivot_lows
    
    def compute_higher_tf_data(
        self,
        df: pd.DataFrame,
        timeframe_minutes: int = 15,
        ema_period: int = 20
    ) -> pd.DataFrame:
        df = df.copy()
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.set_index('timestamp')
        
        rule = f'{timeframe_minutes}min'
        
        htf_df = df.resample(rule).agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        htf_df['ema'] = htf_df['close'].ewm(span=ema_period, adjust=False).mean()
        
        htf_df = htf_df.reset_index()
        
        return htf_df
    
    def get_higher_tf_trend(
        self,
        df: pd.DataFrame,
        current_time: pd.Timestamp,
        htf_data: pd.DataFrame
    ) -> Optional[str]:
        if htf_data.empty:
            return None
            
        htf_before = htf_data[htf_data['timestamp'] <= current_time]
        
        if htf_before.empty:
            return None
        
        last_htf = htf_before.iloc[-1]
        
        if last_htf['close'] > last_htf['ema']:
            return 'bullish'
        elif last_htf['close'] < last_htf['ema']:
            return 'bearish'
        
        return 'neutral'
    
    def count_vwap_crosses(
        self,
        df: pd.DataFrame,
        vwap: pd.Series,
        lookback_bars: int = 30,
        current_idx: int = None
    ) -> int:
        if current_idx is None:
            current_idx = len(df) - 1
        
        start_idx = max(0, current_idx - lookback_bars)
        
        close_slice = df['close'].iloc[start_idx:current_idx + 1].values
        vwap_slice = vwap.iloc[start_idx:current_idx + 1].values
        
        diff = close_slice - vwap_slice
        
        sign_changes = np.sum(np.diff(np.sign(diff)) != 0)
        
        return int(sign_changes)
    
    def is_rejection_candle(
        self,
        bar: pd.Series,
        zone_low: float,
        zone_high: float,
        side: str
    ) -> bool:
        if side == 'long':
            wicks_into_zone = bar['low'] <= zone_high
            closes_above = bar['close'] > zone_high
            return wicks_into_zone and closes_above
        else:
            wicks_into_zone = bar['high'] >= zone_low
            closes_below = bar['close'] < zone_low
            return wicks_into_zone and closes_below
    
    def is_engulfing_candle(
        self,
        current_bar: pd.Series,
        prev_bar: pd.Series,
        side: str
    ) -> bool:
        curr_body_high = max(current_bar['open'], current_bar['close'])
        curr_body_low = min(current_bar['open'], current_bar['close'])
        prev_body_high = max(prev_bar['open'], prev_bar['close'])
        prev_body_low = min(prev_bar['open'], prev_bar['close'])
        
        curr_bullish = current_bar['close'] > current_bar['open']
        curr_bearish = current_bar['close'] < current_bar['open']
        prev_bullish = prev_bar['close'] > prev_bar['open']
        prev_bearish = prev_bar['close'] < prev_bar['open']
        
        if side == 'long':
            is_bullish_engulf = (
                prev_bearish and 
                curr_bullish and 
                curr_body_low <= prev_body_low and 
                curr_body_high >= prev_body_high
            )
            return is_bullish_engulf
        else:
            is_bearish_engulf = (
                prev_bullish and 
                curr_bearish and 
                curr_body_high >= prev_body_high and 
                curr_body_low <= prev_body_low
            )
            return is_bearish_engulf
    
    def add_indicators_to_df(
        self,
        df: pd.DataFrame,
        atr_period: int = 14
    ) -> pd.DataFrame:
        df = df.copy()
        
        df['atr'] = self.compute_atr(df, atr_period)
        df['vwap'] = self.compute_vwap(df)
        
        vwap_config = self.config.get('vwap', {})
        band_mult = vwap_config.get('vwap_band_atr_mult', 0.20)
        min_ticks = vwap_config.get('vwap_min_band_ticks', 2)
        
        df['vwap_upper'], df['vwap_lower'] = self.compute_vwap_bands(
            df, df['vwap'], df['atr'], band_mult, min_ticks
        )
        
        long_trend_config = self.config.get('long_trend_filter', {})
        ema_period = long_trend_config.get('ema_period', 20)
        df[f'ema_{ema_period}'] = df['close'].ewm(span=ema_period, adjust=False).mean()
        
        return df

