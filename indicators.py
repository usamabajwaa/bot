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
        
        # Add market regime detection
        df['market_regime'] = self.detect_market_regime(df)
        
        return df
    
    # ========================================
    # NEW: Volume Profile Analysis
    # ========================================
    
    def compute_volume_profile(
        self,
        df: pd.DataFrame,
        num_bins: int = 50,
        lookback_bars: int = None
    ) -> dict:
        """
        Compute volume profile to identify High Volume Nodes (HVN) and Low Volume Nodes (LVN).
        
        Returns dict with:
        - 'poc': Point of Control (price with highest volume)
        - 'hvn_levels': List of high volume node prices
        - 'lvn_levels': List of low volume node prices
        - 'value_area_high': Upper bound of value area (70% of volume)
        - 'value_area_low': Lower bound of value area
        """
        if lookback_bars is not None:
            df_slice = df.tail(lookback_bars).copy()
        else:
            df_slice = df.copy()
        
        if len(df_slice) < 10:
            return {
                'poc': None,
                'hvn_levels': [],
                'lvn_levels': [],
                'value_area_high': None,
                'value_area_low': None
            }
        
        price_low = df_slice['low'].min()
        price_high = df_slice['high'].max()
        
        # Create price bins
        bin_size = (price_high - price_low) / num_bins
        if bin_size <= 0:
            return {'poc': None, 'hvn_levels': [], 'lvn_levels': [], 'value_area_high': None, 'value_area_low': None}
        
        bins = np.linspace(price_low, price_high, num_bins + 1)
        bin_centers = (bins[:-1] + bins[1:]) / 2
        
        # Accumulate volume at each price level
        volume_at_price = np.zeros(num_bins)
        
        for _, row in df_slice.iterrows():
            vol = row.get('volume', 1)
            if pd.isna(vol) or vol <= 0:
                vol = 1
            
            # Distribute volume across the bar's range
            bar_low = row['low']
            bar_high = row['high']
            
            for i, (bin_low, bin_high) in enumerate(zip(bins[:-1], bins[1:])):
                # Check if bin overlaps with bar range
                if bar_low <= bin_high and bar_high >= bin_low:
                    overlap_low = max(bar_low, bin_low)
                    overlap_high = min(bar_high, bin_high)
                    overlap_pct = (overlap_high - overlap_low) / (bar_high - bar_low) if bar_high > bar_low else 1
                    volume_at_price[i] += vol * overlap_pct
        
        total_volume = volume_at_price.sum()
        if total_volume <= 0:
            return {'poc': None, 'hvn_levels': [], 'lvn_levels': [], 'value_area_high': None, 'value_area_low': None}
        
        # Point of Control (highest volume price)
        poc_idx = np.argmax(volume_at_price)
        poc = bin_centers[poc_idx]
        
        # Calculate Value Area (70% of volume around POC)
        target_volume = total_volume * 0.70
        accumulated = volume_at_price[poc_idx]
        va_low_idx = poc_idx
        va_high_idx = poc_idx
        
        while accumulated < target_volume and (va_low_idx > 0 or va_high_idx < num_bins - 1):
            vol_below = volume_at_price[va_low_idx - 1] if va_low_idx > 0 else 0
            vol_above = volume_at_price[va_high_idx + 1] if va_high_idx < num_bins - 1 else 0
            
            if vol_below >= vol_above and va_low_idx > 0:
                va_low_idx -= 1
                accumulated += vol_below
            elif va_high_idx < num_bins - 1:
                va_high_idx += 1
                accumulated += vol_above
            else:
                va_low_idx -= 1
                accumulated += vol_below
        
        value_area_high = bin_centers[va_high_idx]
        value_area_low = bin_centers[va_low_idx]
        
        # Identify HVN (top 30% by volume) and LVN (bottom 30% by volume)
        volume_threshold_high = np.percentile(volume_at_price, 70)
        volume_threshold_low = np.percentile(volume_at_price, 30)
        
        hvn_levels = [bin_centers[i] for i in range(num_bins) if volume_at_price[i] >= volume_threshold_high]
        lvn_levels = [bin_centers[i] for i in range(num_bins) if 0 < volume_at_price[i] <= volume_threshold_low]
        
        return {
            'poc': poc,
            'hvn_levels': hvn_levels,
            'lvn_levels': lvn_levels,
            'value_area_high': value_area_high,
            'value_area_low': value_area_low,
            'volume_by_price': dict(zip(bin_centers, volume_at_price))
        }
    
    def is_near_hvn(self, price: float, volume_profile: dict, tolerance_ticks: int = 5) -> bool:
        """Check if price is near a High Volume Node"""
        if not volume_profile or not volume_profile.get('hvn_levels'):
            return False
        tolerance = tolerance_ticks * self.tick_size
        return any(abs(price - hvn) <= tolerance for hvn in volume_profile['hvn_levels'])
    
    def is_near_lvn(self, price: float, volume_profile: dict, tolerance_ticks: int = 5) -> bool:
        """Check if price is near a Low Volume Node (good for entries)"""
        if not volume_profile or not volume_profile.get('lvn_levels'):
            return False
        tolerance = tolerance_ticks * self.tick_size
        return any(abs(price - lvn) <= tolerance for lvn in volume_profile['lvn_levels'])
    
    # ========================================
    # NEW: Market Regime Detection
    # ========================================
    
    def detect_market_regime(
        self,
        df: pd.DataFrame,
        atr_lookback: int = 20,
        trend_lookback: int = 50
    ) -> pd.Series:
        """
        Detect market regime: 'trending_up', 'trending_down', 'ranging', 'volatile'
        
        Uses combination of:
        - ADX-like measure (trend strength)
        - ATR relative to price range (volatility)
        - Price position relative to moving average
        """
        regimes = pd.Series(index=df.index, dtype=str)
        regimes[:] = 'unknown'
        
        if len(df) < trend_lookback:
            return regimes
        
        close = df['close'].values
        high = df['high'].values
        low = df['low'].values
        
        # Calculate trend strength using directional movement
        ema_short = pd.Series(close).ewm(span=10, adjust=False).mean().values
        ema_long = pd.Series(close).ewm(span=30, adjust=False).mean().values
        
        # ATR for volatility measure
        atr = self.compute_atr(df, atr_lookback).values
        
        # Average price range
        avg_range = pd.Series(high - low).rolling(window=atr_lookback).mean().values
        
        for i in range(trend_lookback, len(df)):
            # Trend direction
            trend_diff = ema_short[i] - ema_long[i]
            price_range = high[i-trend_lookback:i].max() - low[i-trend_lookback:i].min()
            
            if price_range <= 0:
                regimes.iloc[i] = 'ranging'
                continue
            
            # Trend strength: how much price moved directionally vs total range
            net_move = close[i] - close[i-trend_lookback]
            trend_strength = abs(net_move) / price_range if price_range > 0 else 0
            
            # Volatility: current ATR vs average range
            volatility_ratio = atr[i] / avg_range[i] if avg_range[i] > 0 else 1.0
            
            # Classify regime
            if volatility_ratio > 1.5:
                regimes.iloc[i] = 'volatile'
            elif trend_strength > 0.5:
                regimes.iloc[i] = 'trending_up' if net_move > 0 else 'trending_down'
            else:
                regimes.iloc[i] = 'ranging'
        
        return regimes
    
    def get_current_regime(self, df: pd.DataFrame) -> str:
        """Get the current market regime"""
        if 'market_regime' not in df.columns:
            regime_series = self.detect_market_regime(df)
            return regime_series.iloc[-1] if len(regime_series) > 0 else 'unknown'
        return df['market_regime'].iloc[-1] if len(df) > 0 else 'unknown'
    
    # ========================================
    # NEW: Order Flow Analysis (Simulated)
    # ========================================
    
    def compute_order_flow_imbalance(
        self,
        df: pd.DataFrame,
        lookback_bars: int = 10
    ) -> pd.Series:
        """
        Estimate order flow imbalance from price action.
        
        Positive = buying pressure (bullish)
        Negative = selling pressure (bearish)
        
        Uses:
        - Close position within bar range
        - Volume-weighted close position
        - Up/down bar sequences
        """
        if len(df) < lookback_bars:
            return pd.Series(index=df.index, data=0.0)
        
        imbalance = pd.Series(index=df.index, dtype=float)
        imbalance[:] = 0.0
        
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        open_p = df['open'].values
        volume = df['volume'].values if 'volume' in df.columns else np.ones(len(df))
        
        for i in range(lookback_bars, len(df)):
            # Calculate close position within bar (0 = at low, 1 = at high)
            bar_range = high[i] - low[i]
            if bar_range > 0:
                close_position = (close[i] - low[i]) / bar_range
            else:
                close_position = 0.5
            
            # Weight by volume
            vol = volume[i] if not pd.isna(volume[i]) and volume[i] > 0 else 1
            
            # Direction: bullish bar = 1, bearish = -1
            direction = 1 if close[i] > open_p[i] else -1 if close[i] < open_p[i] else 0
            
            # Combine: close position and direction
            bar_imbalance = (close_position - 0.5) * 2 * direction  # -1 to +1
            
            # Look at recent bars for trend
            recent_closes = close[i-lookback_bars:i+1]
            recent_up = sum(1 for j in range(1, len(recent_closes)) if recent_closes[j] > recent_closes[j-1])
            recent_down = lookback_bars - recent_up
            trend_bias = (recent_up - recent_down) / lookback_bars  # -1 to +1
            
            # Combined imbalance (weighted average)
            imbalance.iloc[i] = 0.6 * bar_imbalance + 0.4 * trend_bias
        
        return imbalance
    
    def check_order_flow_alignment(
        self,
        df: pd.DataFrame,
        side: str,
        bar_index: int = None,
        min_imbalance: float = 0.2
    ) -> bool:
        """
        Check if order flow aligns with trade direction.
        
        For longs: require positive imbalance (buying pressure)
        For shorts: require negative imbalance (selling pressure)
        """
        if bar_index is None:
            bar_index = len(df) - 1
        
        imbalance = self.compute_order_flow_imbalance(df)
        
        if bar_index >= len(imbalance):
            return True  # No data, allow trade
        
        current_imbalance = imbalance.iloc[bar_index]
        
        if side == 'long':
            return current_imbalance >= min_imbalance
        else:  # short
            return current_imbalance <= -min_imbalance
    
    # ========================================
    # NEW: Consolidation Detection
    # ========================================
    
    def is_consolidating(
        self,
        df: pd.DataFrame,
        bar_index: int = None,
        lookback_bars: int = 5,
        max_range_atr_mult: float = 1.0
    ) -> bool:
        """
        Check if price is consolidating (good for zone entries).
        
        Returns True if price range over lookback is less than max_range_atr_mult * ATR
        """
        if bar_index is None:
            bar_index = len(df) - 1
        
        if bar_index < lookback_bars:
            return False
        
        start_idx = max(0, bar_index - lookback_bars)
        
        recent_high = df['high'].iloc[start_idx:bar_index+1].max()
        recent_low = df['low'].iloc[start_idx:bar_index+1].min()
        price_range = recent_high - recent_low
        
        atr = df['atr'].iloc[bar_index] if 'atr' in df.columns else self.compute_atr(df).iloc[bar_index]
        
        return price_range <= max_range_atr_mult * atr
    
    def count_bars_in_zone(
        self,
        df: pd.DataFrame,
        zone_low: float,
        zone_high: float,
        bar_index: int = None,
        lookback_bars: int = 10
    ) -> int:
        """Count how many recent bars have been inside the zone"""
        if bar_index is None:
            bar_index = len(df) - 1
        
        start_idx = max(0, bar_index - lookback_bars)
        count = 0
        
        for i in range(start_idx, bar_index + 1):
            bar = df.iloc[i]
            # Bar is in zone if it overlaps
            if bar['low'] <= zone_high and bar['high'] >= zone_low:
                count += 1
        
        return count

