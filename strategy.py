import pandas as pd
from datetime import datetime, time, timedelta
from typing import Optional, Tuple, List, Dict
from dataclasses import dataclass
from enum import Enum
import pytz

from indicators import Indicators
from zones import ZoneManager, ZoneType, Zone


class SignalType(Enum):
    LONG = 'long'
    SHORT = 'short'
    NONE = 'none'


@dataclass
class Signal:
    signal_type: SignalType
    entry_price: float
    stop_loss: float
    take_profit: float
    zone: Zone
    session: str
    bar_index: int
    timestamp: pd.Timestamp
    risk_ticks: float
    reward_ticks: float
    rr_ratio: float
    confirmation_type: str
    zone_confidence: float
    structure_levels: List[float] = None


class SessionManager:
    def __init__(self, config: dict):
        self.config = config
        self.tz = pytz.timezone(config.get('timezone', 'America/Chicago'))
        self.sessions = config.get('sessions', {})
        self.buffer_minutes = config.get('session_boundary_buffer_minutes', 5)
    
    def parse_time(self, time_str: str) -> time:
        parts = time_str.split(':')
        return time(int(parts[0]), int(parts[1]))
    
    def get_active_session(self, timestamp: pd.Timestamp) -> Optional[str]:
        # Session times are in UTC, so convert timestamp to UTC
        import pytz
        utc = pytz.UTC
        if timestamp.tzinfo is None:
            # Assume UTC if no timezone info
            timestamp = utc.localize(timestamp)
        else:
            timestamp = timestamp.astimezone(utc)
        
        current_time = timestamp.time()
        
        # Check sessions in priority order (us, asia, london) to handle overlaps correctly
        # During 18:00-21:00 UTC, both US and Asia are active - prioritize US
        priority_order = ['us', 'asia', 'london', 'london_early']
        
        for session_name in priority_order:
            if session_name not in self.sessions:
                continue
            session_config = self.sessions[session_name]
            if not session_config.get('enabled', True):
                continue
            
            start = self.parse_time(session_config['start'])
            end = self.parse_time(session_config['end'])
            
            if start <= end:
                if start <= current_time <= end:
                    return session_name
            else:
                if current_time >= start or current_time <= end:
                    return session_name
        
        return None
    
    def is_within_boundary_buffer(
        self,
        timestamp: pd.Timestamp,
        session_name: str
    ) -> bool:
        # Keep timestamp in UTC (session times are defined as UTC)
        if timestamp.tzinfo is None:
            timestamp = pytz.UTC.localize(timestamp)
        else:
            timestamp = timestamp.astimezone(pytz.UTC)
        
        session_config = self.sessions.get(session_name)
        if not session_config:
            return False
        
        start = self.parse_time(session_config['start'])
        end = self.parse_time(session_config['end'])
        current_time = timestamp.time()  # Already UTC
        
        # All datetime operations in UTC
        start_dt = datetime.combine(timestamp.date(), start, tzinfo=pytz.UTC)
        end_dt = datetime.combine(timestamp.date(), end, tzinfo=pytz.UTC)
        current_dt = datetime.combine(timestamp.date(), current_time, tzinfo=pytz.UTC)
        
        buffer = timedelta(minutes=self.buffer_minutes)
        
        near_start = abs((current_dt - start_dt).total_seconds()) < buffer.total_seconds()
        near_end = abs((current_dt - end_dt).total_seconds()) < buffer.total_seconds()
        
        return near_start or near_end
    
    def get_session_params(self, session_name: str) -> dict:
        return self.sessions.get(session_name, {})


class Strategy:
    def __init__(self, config: dict):
        self.config = config
        self.indicators = Indicators(config)
        self.zone_manager = ZoneManager(config)
        self.session_manager = SessionManager(config)
        
        self.tick_size = config.get('tick_size', 0.10)
        self.tick_value = config.get('tick_value', 1.00)
        self.min_sl_ticks = config.get('min_sl_ticks', 2)
        self.min_rr = config.get('min_rr', 2.0)
        self.pivot_strength = config.get('pivot_strength', 2)
        self.slippage_ticks = config.get('slippage_ticks', 1)
        
        vwap_config = config.get('vwap', {})
        self.use_vwap_filter = vwap_config.get('use_vwap_filter', True)
        
        chop_config = config.get('chop_filter', {})
        self.use_chop_filter = chop_config.get('enabled', True)
        self.chop_lookback = chop_config.get('lookback_bars', 30)
        self.chop_max_crosses = chop_config.get('max_crosses', 6)
        
        htf_config = config.get('higher_tf_filter', {})
        self.use_htf_filter = htf_config.get('enabled', True)
        self.htf_timeframe = htf_config.get('timeframe_minutes', 15)
        self.htf_ema_period = htf_config.get('ema_period', 20)
        
        confirm_config = config.get('confirmation', {})
        self.use_rejection = confirm_config.get('use_rejection', True)
        self.use_engulfing = confirm_config.get('use_engulfing', True)
        self.require_both = confirm_config.get('require_both', False)
        
        long_trend_config = config.get('long_trend_filter', {})
        self.long_trend_filter_enabled = long_trend_config.get('enabled', False)
        self.long_trend_ema_period = long_trend_config.get('ema_period', 20)
        
        volume_config = config.get('volume_filter', {})
        self.use_volume_filter = volume_config.get('enabled', False)
        self.volume_lookback = volume_config.get('lookback_bars', 20)
        self.volume_min_mult = volume_config.get('min_volume_mult', 0.8)
        
        vwap_obstruction_config = config.get('vwap_obstruction', {})
        self.vwap_obstruction_enabled = vwap_obstruction_config.get('enabled', False)
        
        self.blackout_dates = set()
        self.htf_data = None
        
    def load_blackout_dates(self, filepath: str) -> None:
        try:
            df = pd.read_csv(filepath)
            self.blackout_dates = set(pd.to_datetime(df['date']).dt.date)
        except Exception:
            self.blackout_dates = set()
    
    def is_blackout_date(self, timestamp: pd.Timestamp) -> bool:
        return timestamp.date() in self.blackout_dates
    
    def is_blocked_day(self, timestamp: pd.Timestamp) -> bool:
        blocked_days = self.config.get('blocked_days', [])
        if not blocked_days:
            return False
        day_name = timestamp.day_name()
        return day_name in blocked_days
    
    def is_blocked_hour(self, timestamp: pd.Timestamp) -> bool:
        blocked_hours = self.config.get('blocked_hours_utc', [])
        if not blocked_hours:
            return False
        # Explicitly normalize to UTC
        if timestamp.tzinfo is None:
            ts_utc = pytz.UTC.localize(timestamp)
        else:
            ts_utc = timestamp.astimezone(pytz.UTC)
        utc_hour = ts_utc.hour
        return utc_hour in blocked_hours
    
    def prepare_data(self, df: pd.DataFrame, merge_zones: bool = False) -> pd.DataFrame:
        """
        Prepare data and create zones from pivots.
        
        Args:
            df: DataFrame with market data
            merge_zones: If True, merge new zones with existing ones instead of replacing
        """
        import sys
        print(f"Preparing data: {len(df)} bars...", file=sys.stderr)
        df = df.copy()
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp').reset_index(drop=True)
        
        print("  Calculating indicators...", file=sys.stderr)
        df = self.indicators.add_indicators_to_df(df)
        
        print("  Detecting pivots...", file=sys.stderr)
        pivot_highs, pivot_lows = self.indicators.detect_all_pivots(df, self.pivot_strength)
        
        # Use global config for zone building instead of always using 'us' session
        # This ensures consistent zones regardless of which session is active
        zone_atr_mult = self.config.get('zone_atr_mult', 0.3)  # Global default
        
        if merge_zones:
            # Create temporary zone manager to get new zones
            temp_zone_manager = ZoneManager(self.config)
            temp_zone_manager.update_zones_from_pivots(
                pivot_highs, pivot_lows, df['atr'], zone_atr_mult
            )
            # Merge new zones with existing ones
            self.zone_manager.merge_zones(temp_zone_manager.zones, current_max_index=len(df))
        else:
            # Original behavior: replace zones
            self.zone_manager.update_zones_from_pivots(
                pivot_highs, pivot_lows, df['atr'], zone_atr_mult
            )
        
        if self.use_htf_filter:
            self.htf_data = self.indicators.compute_higher_tf_data(
                df, self.htf_timeframe, self.htf_ema_period
            )
        
        return df
    
    def check_vwap_filter(
        self,
        bar: pd.Series,
        vwap: float,
        side: str,
        zone: Optional[Zone] = None
    ) -> bool:
        if not self.use_vwap_filter:
            return True
        
        # For zone role reversal trades, relax VWAP requirement
        # A freshly converted zone (touch_count == 0, confidence == 1.0) indicates a reversal
        # For reversals, allow entries even if price is on the "wrong" side of VWAP
        is_reversal_zone = False
        if zone is not None:
            # Check if this is a freshly converted zone (role reversal)
            is_reversal_zone = (zone.touch_count == 0 and zone.confidence == 1.0)
        
        if side == 'long':
            if is_reversal_zone:
                # For reversal longs (supply->demand), allow entry below VWAP
                # This captures the bounce off the newly converted support zone
                return True
            return bar['close'] >= vwap
        else:
            if is_reversal_zone:
                # For reversal shorts (demand->supply), allow entry above VWAP
                # This captures the rejection from the newly converted resistance zone
                return True
            return bar['close'] <= vwap
    
    def check_chop_filter(
        self,
        df: pd.DataFrame,
        vwap: pd.Series,
        bar_index: int,
        max_crosses: Optional[int] = None
    ) -> bool:
        if not self.use_chop_filter:
            return True
        
        effective_max = max_crosses if max_crosses is not None else self.chop_max_crosses
        
        crosses = self.indicators.count_vwap_crosses(
            df, vwap, self.chop_lookback, bar_index
        )
        
        return crosses < effective_max
    
    def check_htf_filter(
        self,
        timestamp: pd.Timestamp,
        side: str
    ) -> bool:
        if not self.use_htf_filter or self.htf_data is None:
            return True
        
        trend = self.indicators.get_higher_tf_trend(
            None, timestamp, self.htf_data
        )
        
        if trend is None or trend == 'neutral':
            return True
        
        if side == 'long' and trend == 'bullish':
            return True
        if side == 'short' and trend == 'bearish':
            return True
        
        return False
    
    def check_volume_filter(
        self,
        df: pd.DataFrame,
        bar_index: int
    ) -> bool:
        if not self.use_volume_filter:
            return True
        
        if 'volume' not in df.columns:
            return True
        
        start_idx = max(0, bar_index - self.volume_lookback)
        avg_volume = df['volume'].iloc[start_idx:bar_index].mean()
        
        if avg_volume <= 0:
            return True
        
        current_volume = df['volume'].iloc[bar_index]
        return current_volume >= avg_volume * self.volume_min_mult
    
    def is_vwap_obstructing(
        self,
        entry_price: float,
        take_profit: float,
        vwap: float,
        side: str
    ) -> bool:
        if not self.vwap_obstruction_enabled:
            return False
        
        if side == 'long':
            return entry_price < vwap < take_profit
        else:
            return take_profit < vwap < entry_price
    
    def find_structure_levels(
        self,
        entry_price: float,
        side: str,
        bar_index: int,
        min_distance: float
    ) -> List[float]:
        levels = []
        
        if side == 'long':
            supply_zones = [
                z for z in self.zone_manager.zones
                if z.is_active 
                and z.zone_type == ZoneType.SUPPLY 
                and z.low > entry_price + min_distance
                and z.created_index < bar_index
            ]
            for z in sorted(supply_zones, key=lambda x: x.low):
                levels.append(z.low)
        else:
            demand_zones = [
                z for z in self.zone_manager.zones
                if z.is_active 
                and z.zone_type == ZoneType.DEMAND 
                and z.high < entry_price - min_distance
                and z.created_index < bar_index
            ]
            for z in sorted(demand_zones, key=lambda x: -x.high):
                levels.append(z.high)
        
        return levels[:3]
    
    def check_confirmation(
        self,
        current_bar: pd.Series,
        prev_bar: pd.Series,
        zone: Zone,
        side: str,
        require_both: Optional[bool] = None
    ) -> Tuple[bool, str]:
        confirmations = []
        
        is_rejection = False
        is_engulfing = False
        
        if self.use_rejection:
            is_rejection = self.indicators.is_rejection_candle(
                current_bar, zone.low, zone.high, side
            )
            if is_rejection:
                confirmations.append('rejection')
        
        if self.use_engulfing:
            is_engulfing = self.indicators.is_engulfing_candle(
                current_bar, prev_bar, side
            )
            if is_engulfing:
                confirmations.append('engulfing')
        
        effective_require_both = require_both if require_both is not None else self.require_both
        
        if effective_require_both:
            if is_rejection and is_engulfing:
                return True, 'rejection+engulfing'
            return False, ''
        
        if confirmations:
            return True, '+'.join(confirmations)
        
        return False, ''
    
    def calculate_sl_tp(
        self,
        entry_price: float,
        zone: Zone,
        side: str,
        atr: float,
        session_params: dict,
        bar_index: int,
        vwap: float,
        min_rr: float = None
    ) -> Tuple[float, float, float, float]:
        sl_buffer_mult = session_params.get('sl_buffer_atr_mult', 0.15)
        buffer = max(sl_buffer_mult * atr, self.min_sl_ticks * self.tick_size)
        
        # Use passed min_rr (session-specific) or fall back to global
        effective_min_rr = min_rr if min_rr is not None else self.min_rr
        
        if side == 'long':
            stop_loss = zone.low - buffer
            risk = entry_price - stop_loss
            
            if risk <= 0:
                return None, None, None, None
            
            min_tp = entry_price + effective_min_rr * risk
            
            structure_levels = self.find_structure_levels(
                entry_price, side, bar_index, min_distance=risk * 0.5
            )
            
            take_profit = min_tp
            
            for level in structure_levels:
                if level >= min_tp:
                    take_profit = level
                    break
            
            if self.check_vwap_obstruction_enabled and entry_price < vwap < take_profit:
                if structure_levels and structure_levels[0] < vwap:
                    take_profit = structure_levels[0]
                else:
                    return None, None, None, None
            
            reward = take_profit - entry_price
            
        else:
            stop_loss = zone.high + buffer
            risk = stop_loss - entry_price
            
            if risk <= 0:
                return None, None, None, None
            
            min_tp = entry_price - effective_min_rr * risk
            
            structure_levels = self.find_structure_levels(
                entry_price, side, bar_index, min_distance=risk * 0.5
            )
            
            take_profit = min_tp
            
            for level in structure_levels:
                if level <= min_tp:
                    take_profit = level
                    break
            
            if self.check_vwap_obstruction_enabled and take_profit < vwap < entry_price:
                if structure_levels and structure_levels[0] > vwap:
                    take_profit = structure_levels[0]
                else:
                    return None, None, None, None
            
            reward = entry_price - take_profit
        
        return stop_loss, take_profit, risk, reward
    
    @property
    def check_vwap_obstruction_enabled(self) -> bool:
        return getattr(self, 'vwap_obstruction_enabled', False)
    
    def generate_signal(
        self,
        df: pd.DataFrame,
        bar_index: int,
        daily_trades: int,
        daily_pnl: float,
        in_cooldown: bool,
        debug_log: bool = False
    ) -> Optional[Signal]:
        if bar_index < 2:
            if debug_log:
                print(f"[SIGNAL DEBUG] Bar index too low: {bar_index}")
            return None
        
        bar = df.iloc[bar_index]
        prev_bar = df.iloc[bar_index - 1]
        timestamp = pd.Timestamp(bar['timestamp'])
        price = bar['close']
        
        if debug_log:
            print(f"[SIGNAL DEBUG] Checking signal at {timestamp}, Price=${price:.2f}")
        
        if self.is_blackout_date(timestamp):
            if debug_log:
                print(f"  -> BLOCKED: Blackout date")
            return None
        
        if self.is_blocked_day(timestamp):
            if debug_log:
                print(f"  -> BLOCKED: Blocked day")
            return None
        
        if self.is_blocked_hour(timestamp):
            if debug_log:
                print(f"  -> BLOCKED: Blocked hour (UTC {timestamp.hour})")
            return None
        
        session = self.session_manager.get_active_session(timestamp)
        if session is None:
            if debug_log:
                print(f"  -> BLOCKED: No active session (asia: 18:00-02:00 UTC, london: 06:00-08:30 UTC)")
            return None
        
        if debug_log:
            print(f"  -> Session: {session}")
        
        if self.session_manager.is_within_boundary_buffer(timestamp, session):
            if debug_log:
                print(f"  -> BLOCKED: Within session boundary buffer")
            return None
        
        max_trades = self.config.get('max_trades_per_day', 6)
        daily_limit = self.config.get('daily_loss_limit', -2500)
        
        if daily_trades >= max_trades:
            if debug_log:
                print(f"  -> BLOCKED: Max trades reached ({daily_trades}/{max_trades})")
            return None
        
        if daily_pnl <= daily_limit:
            if debug_log:
                print(f"  -> BLOCKED: Daily loss limit hit (${daily_pnl:.2f} <= ${daily_limit:.2f})")
            return None
        
        if in_cooldown:
            if debug_log:
                print(f"  -> BLOCKED: In cooldown period")
            return None
        
        session_params = self.session_manager.get_session_params(session)
        vwap = bar['vwap']
        atr = bar['atr']
        
        # Get session-specific filter overrides
        session_filters = session_params.get('filters', {})
        
        # Apply session-specific min R:R if set
        session_min_rr = session_filters.get('min_rr', None)
        effective_min_rr = session_min_rr if session_min_rr is not None else self.min_rr
        
        # Apply session-specific chop filter if set
        session_chop_max = session_filters.get('chop_max_crosses', None)
        effective_chop_max = session_chop_max if session_chop_max is not None else self.chop_max_crosses
        
        # Apply session-specific volume filter requirement
        session_require_volume = session_filters.get('require_volume_filter', None)
        effective_require_volume = session_require_volume if session_require_volume is not None else self.use_volume_filter
        
        # Apply session-specific confirmation requirement
        session_require_both = session_filters.get('require_both_confirmations', None)
        effective_require_both = session_require_both if session_require_both is not None else self.require_both
        
        # Check time-based filters (avoid first/last minutes of session)
        avoid_first = session_filters.get('avoid_first_minutes', 0)
        avoid_last = session_filters.get('avoid_last_minutes', 0)
        if avoid_first > 0 or avoid_last > 0:
            session_start = self.session_manager.parse_time(session_params['start'])
            session_end = self.session_manager.parse_time(session_params['end'])
            current_time = timestamp.time() if timestamp.tzinfo is None else timestamp.astimezone(pytz.UTC).time()
            
            # Calculate minutes from session start/end
            start_dt = datetime.combine(timestamp.date(), session_start)
            end_dt = datetime.combine(timestamp.date(), session_end)
            current_dt = datetime.combine(timestamp.date(), current_time)
            
            if session_start <= session_end:
                minutes_from_start = (current_dt - start_dt).total_seconds() / 60
                minutes_to_end = (end_dt - current_dt).total_seconds() / 60
            else:  # Session spans midnight
                if current_time >= session_start:
                    minutes_from_start = (current_dt - start_dt).total_seconds() / 60
                    minutes_to_end = ((end_dt + timedelta(days=1)) - current_dt).total_seconds() / 60
                else:
                    minutes_from_start = ((current_dt + timedelta(days=1)) - start_dt).total_seconds() / 60
                    minutes_to_end = (end_dt - current_dt).total_seconds() / 60
            
            if (avoid_first > 0 and minutes_from_start < avoid_first) or (avoid_last > 0 and minutes_to_end < avoid_last):
                if debug_log:
                    print(f"  -> BLOCKED: Within avoid period (first {avoid_first}min or last {avoid_last}min)")
                return None
        
        if debug_log:
            print(f"  -> VWAP=${vwap:.2f}, ATR=${atr:.2f}")
            if session_filters:
                print(f"  -> Session-specific filters: min_rr={effective_min_rr}, chop_max={effective_chop_max}, require_volume={effective_require_volume}, require_both={effective_require_both}")
        
        # Track filter results for logging (initialize before checks)
        filter_results = {}
        filter_results['chop_filter'] = self.check_chop_filter(df, df['vwap'], bar_index, max_crosses=effective_chop_max)
        filter_results['volume_filter'] = self.check_volume_filter(df, bar_index) if effective_require_volume else True
        filter_results['htf_filter_long'] = self.check_htf_filter(timestamp, 'long')
        filter_results['htf_filter_short'] = self.check_htf_filter(timestamp, 'short')
        
        # Check chop filter with session-specific threshold
        if not filter_results['chop_filter']:
            if debug_log:
                print(f"  -> BLOCKED: Chop filter failed (too many VWAP crosses, max={effective_chop_max})")
            return None
        
        # Check volume filter with session-specific requirement
        if effective_require_volume and not filter_results['volume_filter']:
            if debug_log:
                print(f"  -> BLOCKED: Volume filter failed (low volume, required for this session)")
            return None
        
        for side, zone_type in [('long', ZoneType.DEMAND), ('short', ZoneType.SUPPLY)]:
            if debug_log:
                print(f"  -> Checking {side.upper()} signals ({zone_type.value} zones)...")
            
            # Check VWAP filter after we have the zone (for reversal exceptions)
            # We'll check it again after zone selection
            
            # Use pre-computed HTF filter result
            htf_passed = filter_results['htf_filter_long'] if side == 'long' else filter_results['htf_filter_short']
            if not htf_passed:
                if debug_log:
                    print(f"    -> HTF filter failed: 15-min trend doesn't align")
                continue
            
            touched_zones = self.zone_manager.find_touched_zones(
                bar['low'], bar['high'], bar_index, zone_type
            )
            
            if debug_log:
                print(f"    -> Found {len(touched_zones)} touched {zone_type.value} zones")
            
            high_conf_zones = [
                z for z in touched_zones 
                if z.confidence >= self.zone_manager.min_confidence
            ]
            
            if debug_log and touched_zones:
                for z in touched_zones:
                    conf_status = "HIGH" if z.confidence >= self.zone_manager.min_confidence else "LOW"
                    print(f"      Zone @ ${z.pivot_price:.2f} (${z.low:.2f}-${z.high:.2f}), confidence={z.confidence:.2f} [{conf_status}]")
            
            if not high_conf_zones:
                if debug_log:
                    print(f"    -> No high-confidence zones (min={self.zone_manager.min_confidence})")
                continue
            
            zone = self.zone_manager.get_most_recent_zone(high_conf_zones)
            if zone is None:
                if debug_log:
                    print(f"    -> No most recent zone selected")
                continue
            
            if debug_log:
                print(f"    -> Selected zone @ ${zone.pivot_price:.2f} (${zone.low:.2f}-${zone.high:.2f})")
            
            # Check VWAP filter with zone context (allows reversal exceptions)
            if not self.check_vwap_filter(bar, vwap, side, zone):
                if debug_log:
                    is_reversal = (zone.touch_count == 0 and zone.confidence == 1.0)
                    reversal_note = " (reversal zone)" if is_reversal else ""
                    print(f"    -> VWAP filter failed: price ${price:.2f} {'<' if side=='long' else '>'} VWAP ${vwap:.2f}{reversal_note}")
                continue
            
            confirmed, confirm_type = self.check_confirmation(
                bar, prev_bar, zone, side
            )
            
            if not confirmed:
                if debug_log:
                    print(f"    -> Confirmation failed: {confirm_type}")
                continue
            
            if debug_log:
                print(f"    -> Confirmation passed: {confirm_type}")
            
            slippage = self.slippage_ticks * self.tick_size
            if side == 'long':
                entry_price = bar['close'] + slippage
            else:
                entry_price = bar['close'] - slippage
            
            sl, tp, risk, reward = self.calculate_sl_tp(
                entry_price, zone, side, atr, session_params, bar_index, vwap, min_rr=effective_min_rr
            )
            
            if sl is None or tp is None:
                if debug_log:
                    print(f"    -> SL/TP calculation failed")
                continue
            
            risk_ticks = abs(risk) / self.tick_size
            reward_ticks = abs(reward) / self.tick_size
            rr_ratio = reward / risk if risk > 0 else 0
            
            if debug_log:
                print(f"    -> Entry=${entry_price:.2f}, SL=${sl:.2f}, TP=${tp:.2f}, R:R={rr_ratio:.2f} (min={self.min_rr})")
            
            if rr_ratio < effective_min_rr:
                if debug_log:
                    print(f"    -> R:R too low: {rr_ratio:.2f} < {effective_min_rr}")
                continue
            
            if side == 'long' and self.long_trend_filter_enabled:
                if bar_index >= self.long_trend_ema_period:
                    ema_col = f'ema_{self.long_trend_ema_period}'
                    if ema_col in df.columns:
                        ema_value = df.iloc[bar_index][ema_col]
                        if bar['close'] < ema_value:
                            continue
            
            self.zone_manager.record_zone_touch(zone, bar_index)
            
            # Get structure levels for structure-based partial profit
            structure_levels = self.find_structure_levels(
                entry_price, side, bar_index, min_distance=risk * 0.3
            )
            
            # Get indicator values and VWAP filter result for logging
            indicator_values = {
                'vwap': vwap,
                'atr': atr,
                'ema': bar.get('ema', 0) if 'ema' in bar else 0
            }
            # Add VWAP filter result (check with zone context)
            filter_results['vwap_filter'] = self.check_vwap_filter(bar, vwap, side, zone)
            
            # Attach indicator values and filter results to signal for logging
            signal = Signal(
                signal_type=SignalType.LONG if side == 'long' else SignalType.SHORT,
                entry_price=entry_price,
                stop_loss=sl,
                take_profit=tp,
                zone=zone,
                session=session,
                bar_index=bar_index,
                timestamp=timestamp,
                risk_ticks=risk_ticks,
                reward_ticks=reward_ticks,
                rr_ratio=rr_ratio,
                confirmation_type=confirm_type,
                zone_confidence=zone.confidence,
                structure_levels=structure_levels
            )
            
            # Store indicator values and filter results as attributes for later use
            signal.indicator_values = indicator_values
            signal.filter_results = filter_results
            
            return signal
        
        return None
    
    def reset(self) -> None:
        self.zone_manager.reset()
        self.htf_data = None

