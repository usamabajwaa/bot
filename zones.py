import pandas as pd
import json
import numpy as np
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime, timedelta
import pytz


class ZoneType(Enum):
    DEMAND = 'demand'
    SUPPLY = 'supply'


@dataclass
class Zone:
    zone_id: int
    zone_type: ZoneType
    low: float
    high: float
    pivot_price: float
    created_index: int
    created_time: pd.Timestamp
    confidence: float = 1.0
    touch_count: int = 0
    is_active: bool = True
    last_touch_index: Optional[int] = None
    # NEW: Zone age and quality metrics
    volume_at_zone: float = 0.0  # Volume when zone was created
    move_away_strength: float = 0.0  # How fast price moved away (imbalance indicator)
    bars_in_zone: int = 0  # How many bars price spent in zone (consolidation)
    quality_score: float = 1.0  # Combined quality score (0-1)
    
    def center(self) -> float:
        return (self.low + self.high) / 2
    
    def width(self) -> float:
        return self.high - self.low
    
    def age_hours(self, current_time: pd.Timestamp = None) -> float:
        """Calculate zone age in hours"""
        if current_time is None:
            current_time = pd.Timestamp.now(tz=pytz.UTC)
        if self.created_time.tzinfo is None:
            created = pytz.UTC.localize(self.created_time)
        else:
            created = self.created_time.astimezone(pytz.UTC)
        if current_time.tzinfo is None:
            current_time = pytz.UTC.localize(current_time)
        delta = current_time - created
        return delta.total_seconds() / 3600
    
    def age_weight(self, current_time: pd.Timestamp = None, decay_hours: float = 48) -> float:
        """Calculate age-based weight (fresher zones get higher weight)"""
        age = self.age_hours(current_time)
        # Exponential decay: zones older than decay_hours get significantly lower weight
        # Fresh zones (< 24 hours) get weight close to 1.0
        weight = np.exp(-age / decay_hours)
        return max(0.1, min(1.0, weight))  # Clamp between 0.1 and 1.0
    
    def combined_score(self, current_time: pd.Timestamp = None, 
                      recent_touch_penalty: bool = True) -> float:
        """Combined score considering age, confidence, quality, and touch recency"""
        age_w = self.age_weight(current_time)
        
        # Penalize recently touched zones (liquidity already taken)
        touch_penalty = 1.0
        if recent_touch_penalty and self.last_touch_index is not None:
            # FIXED: Correct calculation - touch happens AFTER creation
            if hasattr(self, 'created_index'):
                bars_since_touch = self.last_touch_index - self.created_index
                # Assuming 3-minute bars: 20 bars = 60 minutes
                if bars_since_touch >= 0 and bars_since_touch < 20:
                    touch_penalty = 0.7  # Reduce score for recently touched zones
            else:
                # Fallback: use time-based calculation if created_index not available
                if current_time and hasattr(self, 'created_time'):
                    time_since_touch_hours = (current_time - self.created_time).total_seconds() / 3600
                    if time_since_touch_hours < 1:  # Less than 1 hour
                        touch_penalty = 0.7
        
        # Weights: 35% age, 25% confidence, 25% quality, 15% touch recency
        return (0.35 * age_w + 
                0.25 * self.confidence + 
                0.25 * self.quality_score + 
                0.15 * touch_penalty)


class ZoneManager:
    def __init__(self, config: dict):
        self.config = config
        self.zones: List[Zone] = []
        self.zone_counter = 0
        
        decay_config = config.get('zone_decay', {})
        self.decay_enabled = decay_config.get('enabled', True)
        self.max_touches = decay_config.get('max_touches', 3)
        self.decay_per_touch = decay_config.get('confidence_decay_per_touch', 0.25)
        self.min_confidence = decay_config.get('min_confidence', 0.5)
        
        self.tick_size = config.get('tick_size', 0.10)
        
        # NEW: Zone age and quality settings
        zone_age_config = config.get('zone_age', {})
        self.age_decay_hours = zone_age_config.get('decay_hours', 48)
        self.min_age_weight = zone_age_config.get('min_weight', 0.1)
        self.fresh_zone_hours = zone_age_config.get('fresh_hours', 24)
        
        # NEW: Zone clustering settings
        cluster_config = config.get('zone_clustering', {})
        self.clustering_enabled = cluster_config.get('enabled', True)
        self.cluster_threshold_atr = cluster_config.get('threshold_atr_mult', 0.5)
        
        # NEW: Volume profile settings
        vp_config = config.get('volume_profile', {})
        self.volume_weighting_enabled = vp_config.get('enabled', False)
        self.hvn_threshold = vp_config.get('hvn_threshold', 0.7)  # Top 30% by volume
        
        # Track last zone build time for smart rebuilding
        self.last_build_time: Optional[pd.Timestamp] = None
        self.zones_built_today: bool = False
        
        # Zone lookup cache for performance optimization
        self._zone_spatial_index = {}  # Cache key -> list of zones
        self._cache_valid_until_index = 0
    
    def _invalidate_spatial_cache(self):
        """Invalidate zone lookup cache (call when zones are updated)"""
        self._zone_spatial_index = {}
    
    def create_zone_from_pivot(
        self,
        pivot_type: str,
        pivot_price: float,
        atr: float,
        zone_atr_mult: float,
        pivot_index: int,
        pivot_time: pd.Timestamp,
        volume: float = 0.0,
        move_away_strength: float = 0.0,
        bars_in_zone: int = 0
    ) -> Zone:
        zone_half_width = zone_atr_mult * atr
        
        if pivot_type == 'low':
            zone_type = ZoneType.DEMAND
            zone_low = pivot_price - zone_half_width
            zone_high = pivot_price + zone_half_width
        else:
            zone_type = ZoneType.SUPPLY
            zone_low = pivot_price - zone_half_width
            zone_high = pivot_price + zone_half_width
        
        self.zone_counter += 1
        
        # Calculate quality score based on move away strength and volume
        quality_score = self._calculate_zone_quality(volume, move_away_strength, bars_in_zone, atr)
        
        zone = Zone(
            zone_id=self.zone_counter,
            zone_type=zone_type,
            low=zone_low,
            high=zone_high,
            pivot_price=pivot_price,
            created_index=pivot_index,
            created_time=pivot_time,
            volume_at_zone=volume,
            move_away_strength=move_away_strength,
            bars_in_zone=bars_in_zone,
            quality_score=quality_score
        )
        
        self.zones.append(zone)
        return zone
    
    def _calculate_zone_quality(
        self,
        volume: float,
        move_away_strength: float,
        bars_in_zone: int,
        atr: float
    ) -> float:
        """Calculate zone quality based on various factors"""
        quality = 1.0
        
        # Fast move away = strong zone (imbalance)
        if atr > 0 and move_away_strength > 0:
            move_factor = min(move_away_strength / atr, 2.0) / 2.0  # Normalize to 0-1
            quality *= (0.5 + 0.5 * move_factor)  # Weight 0.5-1.0
        
        # Less time in zone = stronger zone (supply/demand imbalance was clear)
        if bars_in_zone > 0:
            # 1-2 bars is ideal, more than 5 is weak
            time_factor = max(0, 1 - (bars_in_zone - 1) * 0.15)
            quality *= max(0.3, time_factor)
        
        return min(1.0, max(0.1, quality))
    
    def update_zones_from_pivots(
        self,
        pivot_highs: list,
        pivot_lows: list,
        atr_series: pd.Series,
        zone_atr_mult: float
    ) -> None:
        existing_pivot_prices = {z.pivot_price for z in self.zones}
        
        for pivot in pivot_lows:
            if pivot.price not in existing_pivot_prices:
                atr_at_pivot = atr_series.iloc[pivot.index] if pivot.index < len(atr_series) else atr_series.iloc[-1]
                self.create_zone_from_pivot(
                    pivot_type='low',
                    pivot_price=pivot.price,
                    atr=atr_at_pivot,
                    zone_atr_mult=zone_atr_mult,
                    pivot_index=pivot.index,
                    pivot_time=pivot.timestamp
                )
        
        for pivot in pivot_highs:
            if pivot.price not in existing_pivot_prices:
                atr_at_pivot = atr_series.iloc[pivot.index] if pivot.index < len(atr_series) else atr_series.iloc[-1]
                self.create_zone_from_pivot(
                    pivot_type='high',
                    pivot_price=pivot.price,
                    atr=atr_at_pivot,
                    zone_atr_mult=zone_atr_mult,
                    pivot_index=pivot.index,
                    pivot_time=pivot.timestamp
                )
    
    def find_touched_zones(
        self,
        bar_low: float,
        bar_high: float,
        bar_index: int,
        zone_type: Optional[ZoneType] = None,
        use_cache: bool = True
    ) -> List[Zone]:
        # Rebuild cache if zones changed or cache expired
        if use_cache and (bar_index > self._cache_valid_until_index):
            self._invalidate_spatial_cache()
            self._cache_valid_until_index = bar_index + 10  # Cache for 10 bars
        
        # Use cached lookup if available
        if use_cache:
            cache_key = (round(bar_low, 2), round(bar_high, 2), zone_type)
            if cache_key in self._zone_spatial_index:
                return self._zone_spatial_index[cache_key]
        
        # Original lookup
        touched = []
        
        for zone in self.zones:
            if not zone.is_active:
                continue
            
            if zone_type is not None and zone.zone_type != zone_type:
                continue
            
            if zone.created_index >= bar_index:
                continue
            
            overlaps = bar_low <= zone.high and bar_high >= zone.low
            
            if overlaps:
                touched.append(zone)
        
        return touched
    
    def record_zone_touch(self, zone: Zone, bar_index: int) -> None:
        zone.touch_count += 1
        zone.last_touch_index = bar_index
        
        if self.decay_enabled:
            zone.confidence -= self.decay_per_touch
            
            if zone.touch_count >= self.max_touches or zone.confidence <= 0:
                zone.is_active = False
    
    def get_nearest_zone(
        self,
        price: float,
        zone_type: ZoneType,
        current_index: int
    ) -> Optional[Zone]:
        active_zones = [
            z for z in self.zones 
            if z.is_active and z.zone_type == zone_type and z.created_index < current_index
        ]
        
        if not active_zones:
            return None
        
        if zone_type == ZoneType.DEMAND:
            below_price = [z for z in active_zones if z.high <= price]
            if below_price:
                return max(below_price, key=lambda z: z.high)
        else:
            above_price = [z for z in active_zones if z.low >= price]
            if above_price:
                return min(above_price, key=lambda z: z.low)
        
        return None
    
    def get_opposing_zone_target(
        self,
        entry_price: float,
        side: str,
        current_index: int,
        min_distance: float = 0
    ) -> Optional[float]:
        if side == 'long':
            supply_zones = [
                z for z in self.zones
                if z.is_active 
                and z.zone_type == ZoneType.SUPPLY 
                and z.low > entry_price + min_distance
                and z.created_index < current_index
            ]
            
            if supply_zones:
                nearest = min(supply_zones, key=lambda z: z.low)
                return nearest.low
        else:
            demand_zones = [
                z for z in self.zones
                if z.is_active 
                and z.zone_type == ZoneType.DEMAND 
                and z.high < entry_price - min_distance
                and z.created_index < current_index
            ]
            
            if demand_zones:
                nearest = max(demand_zones, key=lambda z: z.high)
                return nearest.high
        
        return None
    
    def get_active_zones(self, zone_type: Optional[ZoneType] = None) -> List[Zone]:
        zones = [z for z in self.zones if z.is_active]
        
        if zone_type is not None:
            zones = [z for z in zones if z.zone_type == zone_type]
        
        return zones
    
    def get_high_confidence_zones(
        self,
        zone_type: Optional[ZoneType] = None
    ) -> List[Zone]:
        zones = self.get_active_zones(zone_type)
        return [z for z in zones if z.confidence >= self.min_confidence]
    
    def invalidate_broken_zones(
        self,
        bar_close: float,
        bar_index: int
    ) -> List[Zone]:
        """
        Check for broken zones and convert them to opposite type (role reversal).
        When resistance (Supply) is broken upward, it becomes support (Demand).
        When support (Demand) is broken downward, it becomes resistance (Supply).
        """
        converted = []
        
        # Check if role reversal is enabled (default: True)
        role_reversal_enabled = self.config.get('zone_role_reversal', {}).get('enabled', True)
        
        demand_to_supply = 0
        supply_to_demand = 0
        
        for zone in self.zones:
            if not zone.is_active:
                continue
            
            # Check if Demand zone (support) is broken downward
            if zone.zone_type == ZoneType.DEMAND:
                if bar_close < zone.low:
                    if role_reversal_enabled:
                        # Support broken - convert to Supply (resistance)
                        zone.zone_type = ZoneType.SUPPLY
                        zone.touch_count = 0  # Reset touch count for new role
                        zone.confidence = 1.0  # Reset confidence for new role
                        zone.last_touch_index = None
                        converted.append(zone)
                        demand_to_supply += 1
                    else:
                        # Old behavior: just invalidate
                        zone.is_active = False
                        converted.append(zone)
            
            # Check if Supply zone (resistance) is broken upward
            elif zone.zone_type == ZoneType.SUPPLY:
                if bar_close > zone.high:
                    if role_reversal_enabled:
                        # Resistance broken - convert to Demand (support)
                        zone.zone_type = ZoneType.DEMAND
                        zone.touch_count = 0  # Reset touch count for new role
                        zone.confidence = 1.0  # Reset confidence for new role
                        zone.last_touch_index = None
                        converted.append(zone)
                        supply_to_demand += 1
                    else:
                        # Old behavior: just invalidate
                        zone.is_active = False
                        converted.append(zone)
        
        # Log conversion statistics if any conversions occurred
        if converted and (demand_to_supply > 0 or supply_to_demand > 0):
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Zone role reversal: {supply_to_demand} supply->demand, {demand_to_supply} demand->supply")
        
        # Invalidate cache when zones are converted
        if converted:
            self._invalidate_spatial_cache()
        
        return converted
    
    def get_most_recent_zone(
        self,
        zones: List[Zone]
    ) -> Optional[Zone]:
        if not zones:
            return None
        return max(zones, key=lambda z: z.created_index)
    
    def reset(self) -> None:
        # Invalidate cache on reset
        self._invalidate_spatial_cache()
        self.zones = []
        self.zone_counter = 0
    
    def get_zone_stats(self) -> dict:
        active_demand = len([z for z in self.zones if z.is_active and z.zone_type == ZoneType.DEMAND])
        active_supply = len([z for z in self.zones if z.is_active and z.zone_type == ZoneType.SUPPLY])
        
        return {
            'total_zones': len(self.zones),
            'active_demand': active_demand,
            'active_supply': active_supply,
            'invalidated': len(self.zones) - active_demand - active_supply
        }
    
    def cluster_nearby_zones(self, atr: float) -> int:
        """Merge nearby zones of the same type into single stronger zones"""
        if not self.clustering_enabled or atr <= 0:
            return 0
        
        threshold = self.cluster_threshold_atr * atr
        merged_count = 0
        
        for zone_type in [ZoneType.DEMAND, ZoneType.SUPPLY]:
            active_zones = [z for z in self.zones if z.is_active and z.zone_type == zone_type]
            
            # Sort by center price
            active_zones.sort(key=lambda z: z.center())
            
            i = 0
            while i < len(active_zones) - 1:
                zone1 = active_zones[i]
                zone2 = active_zones[i + 1]
                
                # Check if zones are close enough to merge
                distance = abs(zone1.center() - zone2.center())
                if distance < threshold:
                    # Merge: keep the higher quality zone, expand its range
                    if zone1.combined_score() >= zone2.combined_score():
                        keeper, remove = zone1, zone2
                    else:
                        keeper, remove = zone2, zone1
                    
                    # Expand keeper to encompass both zones
                    keeper.low = min(zone1.low, zone2.low)
                    keeper.high = max(zone1.high, zone2.high)
                    keeper.pivot_price = (zone1.pivot_price + zone2.pivot_price) / 2
                    keeper.volume_at_zone += remove.volume_at_zone
                    keeper.quality_score = max(zone1.quality_score, zone2.quality_score)
                    
                    # Deactivate the removed zone
                    remove.is_active = False
                    merged_count += 1
                    
                    # Update list and continue from same position
                    active_zones.remove(remove)
                else:
                    i += 1
        
        return merged_count
    
    def get_fresh_zones(
        self,
        zone_type: Optional[ZoneType] = None,
        current_time: pd.Timestamp = None
    ) -> List[Zone]:
        """Get zones created within fresh_zone_hours (default 24 hours)"""
        if current_time is None:
            current_time = pd.Timestamp.now(tz=pytz.UTC)
        
        zones = self.get_active_zones(zone_type)
        return [z for z in zones if z.age_hours(current_time) <= self.fresh_zone_hours]
    
    def get_high_quality_zones(
        self,
        zone_type: Optional[ZoneType] = None,
        min_score: float = 0.6,
        current_time: pd.Timestamp = None
    ) -> List[Zone]:
        """Get zones with combined score above threshold"""
        zones = self.get_active_zones(zone_type)
        return [z for z in zones if z.combined_score(current_time) >= min_score]
    
    def should_rebuild_zones(self, current_time: pd.Timestamp = None) -> bool:
        """Check if zones should be rebuilt (once per day or never built)"""
        if self.last_build_time is None:
            return True
        
        if current_time is None:
            current_time = pd.Timestamp.now(tz=pytz.UTC)
        
        # Rebuild if it's a new day
        if current_time.date() != self.last_build_time.date():
            self.zones_built_today = False
            return True
        
        return False
    
    def mark_zones_built(self, current_time: pd.Timestamp = None):
        """Mark that zones have been built for today"""
        if current_time is None:
            current_time = pd.Timestamp.now(tz=pytz.UTC)
        self.last_build_time = current_time
        self.zones_built_today = True
    
    def save_zones(self, filepath: str = 'zones.json') -> bool:
        """Save zones to disk for persistence"""
        try:
            zones_data = []
            for zone in self.zones:
                zone_dict = {
                    'zone_id': zone.zone_id,
                    'zone_type': zone.zone_type.value,
                    'low': zone.low,
                    'high': zone.high,
                    'pivot_price': zone.pivot_price,
                    'created_index': zone.created_index,
                    'created_time': zone.created_time.isoformat(),
                    'confidence': zone.confidence,
                    'touch_count': zone.touch_count,
                    'is_active': zone.is_active,
                    'last_touch_index': zone.last_touch_index
                }
                zones_data.append(zone_dict)
            
            data = {
                'zones': zones_data,
                'zone_counter': self.zone_counter
            }
            
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
            
            return True
        except Exception as e:
            print(f"Failed to save zones: {e}")
            return False
    
    def load_zones(self, filepath: str = 'zones.json') -> bool:
        """Load zones from disk"""
        try:
            path = Path(filepath)
            if not path.exists():
                return False
            
            with open(filepath, 'r') as f:
                data = json.load(f)
            
            self.zones = []
            for zone_dict in data.get('zones', []):
                zone = Zone(
                    zone_id=zone_dict['zone_id'],
                    zone_type=ZoneType(zone_dict['zone_type']),
                    low=zone_dict['low'],
                    high=zone_dict['high'],
                    pivot_price=zone_dict['pivot_price'],
                    created_index=zone_dict['created_index'],
                    created_time=pd.to_datetime(zone_dict['created_time']),
                    confidence=zone_dict.get('confidence', 1.0),
                    touch_count=zone_dict.get('touch_count', 0),
                    is_active=zone_dict.get('is_active', True),
                    last_touch_index=zone_dict.get('last_touch_index')
                )
                self.zones.append(zone)
            
            self.zone_counter = data.get('zone_counter', len(self.zones))
            return True
        except Exception as e:
            print(f"Failed to load zones: {e}")
            return False
    
    def merge_zones(self, other_zones: List[Zone], current_max_index: int = 0) -> None:
        """Merge zones from another source, avoiding duplicates using tolerance + range signature"""
        
        def get_zone_signature(zone: Zone) -> tuple:
            """Create a signature for zone deduplication using tolerance + range"""
            # Round to tick_size buckets to handle float precision issues
            pivot_bucket = round(zone.pivot_price / self.tick_size)
            low_bucket = round(zone.low / self.tick_size)
            high_bucket = round(zone.high / self.tick_size)
            return (zone.zone_type, pivot_bucket, low_bucket, high_bucket)
        
        # Build signature map of existing zones
        existing_signatures = {}
        for zone in self.zones:
            sig = get_zone_signature(zone)
            if sig not in existing_signatures:
                existing_signatures[sig] = zone
            else:
                # If duplicate signature, keep zone with higher confidence or more recent
                existing = existing_signatures[sig]
                if (zone.confidence > existing.confidence or 
                    (zone.confidence == existing.confidence and zone.created_index > existing.created_index)):
                    existing_signatures[sig] = zone
        
        # Merge new zones
        for zone in other_zones:
            sig = get_zone_signature(zone)
            
            if sig not in existing_signatures:
                # New zone - add it
                self.zones.append(zone)
                existing_signatures[sig] = zone
            else:
                # Duplicate signature - keep zone with higher confidence or more recent
                existing = existing_signatures[sig]
                if (zone.confidence > existing.confidence or 
                    (zone.confidence == existing.confidence and zone.created_index > existing.created_index)):
                    # Replace existing zone with better one
                    if existing in self.zones:
                        self.zones.remove(existing)
                    self.zones.append(zone)
                    existing_signatures[sig] = zone
        
        # Update zone_counter to avoid ID conflicts
        if self.zones:
            max_id = max(z.zone_id for z in self.zones)
            self.zone_counter = max(self.zone_counter, max_id + 1)

