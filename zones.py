import pandas as pd
import json
from pathlib import Path
from typing import List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum


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
    
    def center(self) -> float:
        return (self.low + self.high) / 2
    
    def width(self) -> float:
        return self.high - self.low


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
        
    def create_zone_from_pivot(
        self,
        pivot_type: str,
        pivot_price: float,
        atr: float,
        zone_atr_mult: float,
        pivot_index: int,
        pivot_time: pd.Timestamp
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
        
        zone = Zone(
            zone_id=self.zone_counter,
            zone_type=zone_type,
            low=zone_low,
            high=zone_high,
            pivot_price=pivot_price,
            created_index=pivot_index,
            created_time=pivot_time
        )
        
        self.zones.append(zone)
        return zone
    
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
        zone_type: Optional[ZoneType] = None
    ) -> List[Zone]:
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
        
        return converted
    
    def get_most_recent_zone(
        self,
        zones: List[Zone]
    ) -> Optional[Zone]:
        if not zones:
            return None
        return max(zones, key=lambda z: z.created_index)
    
    def reset(self) -> None:
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
        """Merge zones from another source, avoiding duplicates"""
        existing_pivot_prices = {z.pivot_price for z in self.zones}
        
        for zone in other_zones:
            # Only add zones that don't already exist (by pivot price)
            if zone.pivot_price not in existing_pivot_prices:
                # Adjust zone indices if needed (for live trading where indices are relative)
                # Keep original created_index for zones from historical data
                self.zones.append(zone)
                existing_pivot_prices.add(zone.pivot_price)
        
        # Update zone_counter to avoid ID conflicts
        if self.zones:
            max_id = max(z.zone_id for z in self.zones)
            self.zone_counter = max(self.zone_counter, max_id + 1)

