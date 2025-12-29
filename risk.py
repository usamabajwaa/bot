import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict
from enum import Enum


class PositionStatus(Enum):
    OPEN = 'open'
    PARTIAL_CLOSED = 'partial_closed'
    CLOSED = 'closed'


@dataclass
class Position:
    trade_id: int
    side: str
    entry_price: float
    entry_time: pd.Timestamp
    entry_index: int
    initial_stop_loss: float
    current_stop_loss: float
    take_profit: float
    contracts: int
    remaining_contracts: int
    session: str
    zone_confidence: float
    confirmation_type: str
    risk_ticks: float
    reward_ticks: float
    
    status: PositionStatus = PositionStatus.OPEN
    break_even_triggered: bool = False
    partial_exit_done: bool = False
    partial_exit_time: Optional[pd.Timestamp] = None
    partial_exit_price: Optional[float] = None
    partial_pnl: float = 0.0
    final_exit_time: Optional[pd.Timestamp] = None
    final_exit_price: Optional[float] = None
    final_pnl: float = 0.0
    total_pnl: float = 0.0
    exit_reason: str = ''
    structure_levels: List[float] = field(default_factory=list)
    last_broken_level: Optional[float] = None


@dataclass
class PendingLimitOrder:
    order_id: int
    side: str
    limit_price: float
    stop_loss: float
    take_profit: float
    session: str
    zone_confidence: float
    confirmation_type: str
    risk_ticks: float
    reward_ticks: float
    structure_levels: List[float]
    created_bar: int
    max_wait_bars: int
    timestamp: pd.Timestamp


@dataclass
class TradeResult:
    trade_id: int
    side: str
    session: str
    entry_time: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    zone_confidence: float
    confirmation_type: str
    partial_exit_time: Optional[pd.Timestamp]
    partial_exit_price: Optional[float]
    partial_pnl: float
    final_exit_time: pd.Timestamp
    final_exit_price: float
    final_pnl: float
    total_pnl: float
    result_ticks: float
    break_even_triggered: bool
    exit_reason: str
    cooldown_active: bool


class RiskManager:
    def __init__(self, config: dict):
        self.config = config
        self.tick_size = config.get('tick_size', 0.10)
        self.tick_value = config.get('tick_value', 1.00)
        self.commission_per_contract = config.get('commission_per_contract', 0.62)
        self.slippage_ticks = config.get('slippage_ticks', 1)
        self.position_size = config.get('position_size_contracts', 1)
        
        be_config = config.get('break_even', {})
        self.break_even_enabled = be_config.get('enabled', True)
        self.break_even_trigger_r = be_config.get('trigger_r', 1.0)
        self.early_be_enabled = be_config.get('early_be_enabled', False)
        self.early_be_ticks = be_config.get('early_be_ticks', 20)
        
        partial_config = config.get('partial_profit', {})
        self.partial_enabled = partial_config.get('enabled', True)
        self.partial_exit_r = partial_config.get('first_exit_r', 0.8)
        self.partial_exit_pct = partial_config.get('first_exit_pct', 0.5)
        self.trail_to_r = partial_config.get('trail_remaining_to_r', 3.0)
        self.structure_based_partial = partial_config.get('structure_based', False)
        self.structure_buffer_ticks = partial_config.get('structure_buffer_ticks', 3)
        # Larger buffer for SL placement to avoid liquidity sweeps
        self.liquidity_sweep_buffer_ticks = partial_config.get('liquidity_sweep_buffer_ticks', 10)
        # How much profit to lock after partial exit (0.5R gives more room for retest)
        self.post_partial_sl_lock_r = partial_config.get('post_partial_sl_lock_r', 0.5)
        
        trailing_config = config.get('trailing_stop', {})
        self.trailing_enabled = trailing_config.get('enabled', False)
        self.trailing_activation_r = trailing_config.get('activation_r', 0.8)
        self.trailing_distance_r = trailing_config.get('trail_distance_r', 0.25)
        
        cooldown_config = config.get('cooldown', {})
        self.cooldown_enabled = cooldown_config.get('enabled', True)
        self.cooldown_trigger_losses = cooldown_config.get('consecutive_losses_trigger', 2)
        self.cooldown_bars = cooldown_config.get('pause_bars', 20)
        
        self.max_trades_per_day = config.get('max_trades_per_day', 6)
        self.daily_loss_limit = config.get('daily_loss_limit', -800)
        
        self.trade_counter = 0
        self.order_counter = 0
        self.current_position: Optional[Position] = None
        self.trade_results: List[TradeResult] = []
        self.pending_orders: Dict[int, PendingLimitOrder] = {}
        
        self.daily_trades: dict = {}
        self.daily_pnl: dict = {}
        
        self.consecutive_losses = 0
        self.cooldown_bars_remaining = 0
        
        # Limit order retest config
        limit_config = config.get('limit_order_retest', {})
        self.limit_order_enabled = limit_config.get('enabled', False)
        self.limit_max_wait_bars = limit_config.get('max_wait_bars', 4)
        self.limit_entry_offset_ticks = limit_config.get('entry_offset_ticks', 1)
        
    def create_pending_order(
        self,
        side: str,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
        session: str,
        zone_confidence: float,
        confirmation_type: str,
        risk_ticks: float,
        reward_ticks: float,
        structure_levels: List[float],
        bar_index: int,
        timestamp: pd.Timestamp
    ) -> int:
        """Create a pending limit order."""
        self.order_counter += 1
        
        order = PendingLimitOrder(
            order_id=self.order_counter,
            side=side,
            limit_price=limit_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            session=session,
            zone_confidence=zone_confidence,
            confirmation_type=confirmation_type,
            risk_ticks=risk_ticks,
            reward_ticks=reward_ticks,
            structure_levels=structure_levels,
            created_bar=bar_index,
            max_wait_bars=self.limit_max_wait_bars,
            timestamp=timestamp
        )
        
        self.pending_orders[self.order_counter] = order
        return self.order_counter
    
    def check_pending_orders(
        self,
        bar: pd.Series,
        bar_index: int,
        date
    ) -> Optional[Position]:
        """Check if any pending limit orders should be filled or cancelled."""
        if not self.pending_orders:
            return None
        
        high = bar['high']
        low = bar['low']
        timestamp = pd.Timestamp(bar['timestamp'])
        
        orders_to_remove = []
        filled_position = None
        
        for order_id, order in self.pending_orders.items():
            # Check if order has expired
            bars_elapsed = bar_index - order.created_bar
            if bars_elapsed > order.max_wait_bars:
                orders_to_remove.append(order_id)
                continue
            
            # Check if limit price was touched
            filled = False
            if order.side == 'long':
                # For long, price needs to come down to our limit
                if low <= order.limit_price:
                    filled = True
            else:  # short
                # For short, price needs to come up to our limit
                if high >= order.limit_price:
                    filled = True
            
            if filled:
                # Open position from the limit order
                filled_position = self.open_position(
                    side=order.side,
                    entry_price=order.limit_price,
                    entry_time=timestamp,
                    entry_index=bar_index,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    session=order.session,
                    zone_confidence=order.zone_confidence,
                    confirmation_type=order.confirmation_type,
                    risk_ticks=order.risk_ticks,
                    reward_ticks=order.reward_ticks,
                    structure_levels=order.structure_levels
                )
                orders_to_remove.append(order_id)
                # Cancel all other pending orders when one fills
                orders_to_remove.extend([oid for oid in self.pending_orders.keys() if oid != order_id])
                break
        
        # Remove filled/expired orders
        for order_id in orders_to_remove:
            if order_id in self.pending_orders:
                del self.pending_orders[order_id]
        
        return filled_position
    
    def has_pending_orders(self) -> bool:
        return len(self.pending_orders) > 0

    def get_daily_trades(self, date) -> int:
        return self.daily_trades.get(date, 0)
    
    def get_daily_pnl(self, date) -> float:
        return self.daily_pnl.get(date, 0.0)
    
    def is_in_cooldown(self) -> bool:
        return self.cooldown_bars_remaining > 0
    
    def tick_cooldown(self) -> None:
        if self.cooldown_bars_remaining > 0:
            self.cooldown_bars_remaining -= 1
    
    def can_trade(self, date) -> Tuple[bool, str]:
        if self.current_position is not None:
            return False, "position_open"
        
        if self.get_daily_trades(date) >= self.max_trades_per_day:
            return False, "max_trades_reached"
        
        if self.get_daily_pnl(date) <= self.daily_loss_limit:
            return False, "daily_loss_limit"
        
        if self.is_in_cooldown():
            return False, "cooldown"
        
        return True, ""
    
    def get_unrealized_pnl(self, current_price: float) -> float:
        if self.current_position is None:
            return 0.0
        
        pos = self.current_position
        
        if pos.side == 'long':
            pnl_ticks = (current_price - pos.entry_price) / self.tick_size
        else:
            pnl_ticks = (pos.entry_price - current_price) / self.tick_size
        
        gross_pnl = pnl_ticks * self.tick_value * pos.remaining_contracts
        commission = self.commission_per_contract * pos.remaining_contracts * 2
        
        return gross_pnl - commission + pos.partial_pnl
    
    def should_force_exit(self, date, current_price: float) -> bool:
        if self.current_position is None:
            return False
        
        realized_pnl = self.get_daily_pnl(date)
        unrealized_pnl = self.get_unrealized_pnl(current_price)
        
        total_daily_pnl = realized_pnl + unrealized_pnl
        
        return total_daily_pnl <= self.daily_loss_limit
    
    def force_close_position(
        self,
        exit_price: float,
        timestamp: pd.Timestamp,
        bar_index: int
    ) -> Optional[TradeResult]:
        if self.current_position is None:
            return None
        
        pos = self.current_position
        
        slippage = self.slippage_ticks * self.tick_size
        if pos.side == 'long':
            actual_exit = exit_price - slippage
        else:
            actual_exit = exit_price + slippage
        
        if pos.side == 'long':
            pnl_ticks = (actual_exit - pos.entry_price) / self.tick_size
        else:
            pnl_ticks = (pos.entry_price - actual_exit) / self.tick_size
        
        gross_pnl = pnl_ticks * self.tick_value * pos.remaining_contracts
        commission = self.commission_per_contract * pos.remaining_contracts * 2
        net_pnl = gross_pnl - commission
        
        pos.final_exit_time = timestamp
        pos.final_exit_price = actual_exit
        pos.final_pnl = net_pnl
        pos.total_pnl = pos.partial_pnl + pos.final_pnl
        pos.exit_reason = 'hard_daily_stop'
        pos.status = PositionStatus.CLOSED
        
        total_ticks = (pos.total_pnl + self.commission_per_contract * pos.contracts * 2) / (self.tick_value * pos.contracts)
        
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
            final_exit_time=pos.final_exit_time,
            final_exit_price=pos.final_exit_price,
            final_pnl=pos.final_pnl,
            total_pnl=pos.total_pnl,
            result_ticks=total_ticks,
            break_even_triggered=pos.break_even_triggered,
            exit_reason='hard_daily_stop',
            cooldown_active=self.is_in_cooldown()
        )
        
        self.trade_results.append(result)
        
        date = pos.entry_time.date()
        self.daily_pnl[date] = self.daily_pnl.get(date, 0.0) + pos.total_pnl
        
        self.current_position = None
        
        return result
    
    def open_position(
        self,
        side: str,
        entry_price: float,
        entry_time: pd.Timestamp,
        entry_index: int,
        stop_loss: float,
        take_profit: float,
        session: str,
        zone_confidence: float,
        confirmation_type: str,
        risk_ticks: float,
        reward_ticks: float,
        structure_levels: List[float] = None
    ) -> Position:
        self.trade_counter += 1
        
        position = Position(
            trade_id=self.trade_counter,
            side=side,
            entry_price=entry_price,
            entry_time=entry_time,
            entry_index=entry_index,
            initial_stop_loss=stop_loss,
            current_stop_loss=stop_loss,
            take_profit=take_profit,
            contracts=self.position_size,
            remaining_contracts=self.position_size,
            session=session,
            zone_confidence=zone_confidence,
            confirmation_type=confirmation_type,
            risk_ticks=risk_ticks,
            reward_ticks=reward_ticks,
            structure_levels=structure_levels or []
        )
        
        self.current_position = position
        
        date = entry_time.date()
        self.daily_trades[date] = self.daily_trades.get(date, 0) + 1
        
        return position
    
    def update_position(
        self,
        bar: pd.Series,
        bar_index: int
    ) -> Tuple[Optional[TradeResult], bool]:
        if self.current_position is None:
            return None, False
        
        pos = self.current_position
        high = bar['high']
        low = bar['low']
        close = bar['close']
        timestamp = pd.Timestamp(bar['timestamp'])
        
        self._check_break_even(pos, high, low)
        
        partial_result = self._check_partial_profit(pos, high, low, timestamp)
        
        # Check if price broke through a structure level - move SL behind it
        self._check_structure_level_break(pos, high, low)
        
        self._update_trailing_stop(pos, high, low)
        
        exit_result = self._check_exit(pos, high, low, close, timestamp, bar_index)
        
        if exit_result is not None:
            return exit_result, True
        
        return None, partial_result
    
    def _check_structure_level_break(
        self,
        pos: Position,
        high: float,
        low: float
    ) -> None:
        """
        If price breaks through a structure level (resistance becomes support or vice versa),
        move the stop loss behind that level with extra buffer for liquidity sweeps.
        """
        if not self.structure_based_partial or not pos.structure_levels:
            return
        
        # Use larger buffer to avoid liquidity sweep stop-outs
        buffer = self.liquidity_sweep_buffer_ticks * self.tick_size
        
        if pos.side == 'long':
            # Check if we broke through any supply zones (resistance becomes support)
            for level in pos.structure_levels:
                if level > pos.entry_price:
                    # Price closed above this resistance level
                    if low > level:  # Clean break - candle low is above level
                        new_sl = level - buffer
                        if new_sl > pos.current_stop_loss:
                            pos.current_stop_loss = new_sl
                            pos.last_broken_level = level
                            # Remove this level from structure_levels
                            pos.structure_levels = [l for l in pos.structure_levels if l != level]
                        break  # Only check one level at a time
        else:  # short
            # Check if we broke through any demand zones (support becomes resistance)
            for level in pos.structure_levels:
                if level < pos.entry_price:
                    # Price closed below this support level
                    if high < level:  # Clean break - candle high is below level
                        new_sl = level + buffer
                        if new_sl < pos.current_stop_loss:
                            pos.current_stop_loss = new_sl
                            pos.last_broken_level = level
                            # Remove this level from structure_levels
                            pos.structure_levels = [l for l in pos.structure_levels if l != level]
                        break  # Only check one level at a time

    def _update_trailing_stop(
        self,
        pos: Position,
        high: float,
        low: float
    ) -> None:
        if not self.trailing_enabled:
            return
        
        risk = abs(pos.entry_price - pos.initial_stop_loss)
        activation_distance = self.trailing_activation_r * risk
        trail_distance = self.trailing_distance_r * risk
        
        if pos.side == 'long':
            current_profit = high - pos.entry_price
            if current_profit >= activation_distance:
                new_sl = high - trail_distance
                if new_sl > pos.current_stop_loss:
                    pos.current_stop_loss = new_sl
        else:
            current_profit = pos.entry_price - low
            if current_profit >= activation_distance:
                new_sl = low + trail_distance
                if new_sl < pos.current_stop_loss:
                    pos.current_stop_loss = new_sl
    
    def _check_break_even(
        self,
        pos: Position,
        high: float,
        low: float
    ) -> None:
        if not self.break_even_enabled:
            return
        
        if pos.break_even_triggered:
            return
        
        should_move = False
        
        # Early BE based on ticks (moves to BE when trade goes X ticks in profit)
        if self.early_be_enabled:
            early_be_distance = self.early_be_ticks * self.tick_size
            
            if pos.side == 'long':
                # Check if we've reached the early BE threshold
                if high >= pos.entry_price + early_be_distance:
                    if pos.current_stop_loss < pos.entry_price:
                        should_move = True
            else:  # short
                # Check if we've reached the early BE threshold
                if low <= pos.entry_price - early_be_distance:
                    if pos.current_stop_loss > pos.entry_price:
                        should_move = True
        
        # R-based BE (original logic) - only if early BE didn't trigger
        if not should_move:
            risk = abs(pos.entry_price - pos.initial_stop_loss)
            trigger_price_distance = self.break_even_trigger_r * risk
            
            if pos.side == 'long':
                trigger_price = pos.entry_price + trigger_price_distance
                if high >= trigger_price:
                    if pos.current_stop_loss < pos.entry_price:
                        should_move = True
            else:
                trigger_price = pos.entry_price - trigger_price_distance
                if low <= trigger_price:
                    if pos.current_stop_loss > pos.entry_price:
                        should_move = True
        
        if should_move:
            pos.current_stop_loss = pos.entry_price
            pos.break_even_triggered = True
    
    def _check_partial_profit(
        self,
        pos: Position,
        high: float,
        low: float,
        timestamp: pd.Timestamp
    ) -> bool:
        if not self.partial_enabled:
            return False
        
        if pos.partial_exit_done:
            return False
        
        risk = abs(pos.entry_price - pos.initial_stop_loss)
        
        # Structure-based partial: exit just before the next structure level
        if self.structure_based_partial and pos.structure_levels:
            buffer = self.structure_buffer_ticks * self.tick_size
            # Use 2x buffer to exit well before the structure level (more aggressive)
            aggressive_buffer = buffer * 2
            
            if pos.side == 'long':
                # Find the closest structure level (supply zone) ahead
                next_level = None
                for level in pos.structure_levels:
                    if level > pos.entry_price:
                        next_level = level
                        break
                
                if next_level:
                    partial_price = next_level - aggressive_buffer
                    if high >= partial_price:
                        self._execute_partial_exit(pos, partial_price, timestamp)
                        return True
            else:
                # Find the closest structure level (demand zone) ahead
                next_level = None
                for level in pos.structure_levels:
                    if level < pos.entry_price:
                        next_level = level
                        break
                
                if next_level:
                    partial_price = next_level + aggressive_buffer
                    if low <= partial_price:
                        self._execute_partial_exit(pos, partial_price, timestamp)
                        return True
        
        # Fallback to R-based partial if no structure levels
        partial_trigger_distance = self.partial_exit_r * risk
        
        if pos.side == 'long':
            partial_price = pos.entry_price + partial_trigger_distance
            if high >= partial_price:
                self._execute_partial_exit(pos, partial_price, timestamp)
                return True
        else:
            partial_price = pos.entry_price - partial_trigger_distance
            if low <= partial_price:
                self._execute_partial_exit(pos, partial_price, timestamp)
                return True
        
        return False
    
    def _execute_partial_exit(
        self,
        pos: Position,
        exit_price: float,
        timestamp: pd.Timestamp
    ) -> None:
        slippage = self.slippage_ticks * self.tick_size
        if pos.side == 'long':
            actual_exit = exit_price - slippage
        else:
            actual_exit = exit_price + slippage
        
        contracts_to_close = int(pos.contracts * self.partial_exit_pct)
        if contracts_to_close < 1:
            contracts_to_close = 1
        
        if pos.side == 'long':
            pnl_ticks = (actual_exit - pos.entry_price) / self.tick_size
        else:
            pnl_ticks = (pos.entry_price - actual_exit) / self.tick_size
        
        gross_pnl = pnl_ticks * self.tick_value * contracts_to_close
        commission = self.commission_per_contract * contracts_to_close * 2
        net_pnl = gross_pnl - commission
        
        pos.partial_exit_done = True
        pos.partial_exit_time = timestamp
        pos.partial_exit_price = actual_exit
        pos.partial_pnl = net_pnl
        pos.remaining_contracts = pos.contracts - contracts_to_close
        pos.status = PositionStatus.PARTIAL_CLOSED
        
        # Move SL to lock partial profit (configurable, default 0.5R gives room for retest)
        risk = abs(pos.entry_price - pos.initial_stop_loss)
        trailing_distance = self.post_partial_sl_lock_r * risk
        
        if pos.side == 'long':
            new_sl = pos.entry_price + trailing_distance
            pos.current_stop_loss = max(pos.current_stop_loss, new_sl)
        else:
            new_sl = pos.entry_price - trailing_distance
            pos.current_stop_loss = min(pos.current_stop_loss, new_sl)
    
    def _check_exit(
        self,
        pos: Position,
        high: float,
        low: float,
        close: float,
        timestamp: pd.Timestamp,
        bar_index: int
    ) -> Optional[TradeResult]:
        sl_hit = False
        tp_hit = False
        
        if pos.side == 'long':
            sl_hit = low <= pos.current_stop_loss
            tp_hit = high >= pos.take_profit
        else:
            sl_hit = high >= pos.current_stop_loss
            tp_hit = low <= pos.take_profit
        
        if sl_hit and tp_hit:
            exit_price = pos.current_stop_loss
            exit_reason = 'stop_loss'
        elif sl_hit:
            exit_price = pos.current_stop_loss
            exit_reason = 'stop_loss'
        elif tp_hit:
            exit_price = pos.take_profit
            exit_reason = 'take_profit'
        else:
            return None
        
        slippage = self.slippage_ticks * self.tick_size
        if pos.side == 'long':
            actual_exit = exit_price - slippage if exit_reason == 'stop_loss' else exit_price - slippage
        else:
            actual_exit = exit_price + slippage if exit_reason == 'stop_loss' else exit_price + slippage
        
        if pos.side == 'long':
            pnl_ticks = (actual_exit - pos.entry_price) / self.tick_size
        else:
            pnl_ticks = (pos.entry_price - actual_exit) / self.tick_size
        
        gross_pnl = pnl_ticks * self.tick_value * pos.remaining_contracts
        commission = self.commission_per_contract * pos.remaining_contracts * 2
        net_pnl = gross_pnl - commission
        
        pos.final_exit_time = timestamp
        pos.final_exit_price = actual_exit
        pos.final_pnl = net_pnl
        pos.total_pnl = pos.partial_pnl + pos.final_pnl
        pos.exit_reason = exit_reason
        pos.status = PositionStatus.CLOSED
        
        total_ticks = (pos.total_pnl + self.commission_per_contract * pos.contracts * 2) / (self.tick_value * pos.contracts)
        
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
            final_exit_time=pos.final_exit_time,
            final_exit_price=pos.final_exit_price,
            final_pnl=pos.final_pnl,
            total_pnl=pos.total_pnl,
            result_ticks=total_ticks,
            break_even_triggered=pos.break_even_triggered,
            exit_reason=exit_reason,
            cooldown_active=self.is_in_cooldown()
        )
        
        self.trade_results.append(result)
        
        date = pos.entry_time.date()
        self.daily_pnl[date] = self.daily_pnl.get(date, 0.0) + pos.total_pnl
        
        self._update_cooldown(pos.total_pnl)
        
        self.current_position = None
        
        return result
    
    def _update_cooldown(self, pnl: float) -> None:
        if not self.cooldown_enabled:
            return
        
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cooldown_trigger_losses:
                self.cooldown_bars_remaining = self.cooldown_bars
        else:
            self.consecutive_losses = 0
    
    def get_all_results(self) -> List[TradeResult]:
        return self.trade_results
    
    def get_equity_curve(self) -> List[float]:
        equity = [0.0]
        for result in self.trade_results:
            equity.append(equity[-1] + result.total_pnl)
        return equity
    
    def reset(self) -> None:
        self.trade_counter = 0
        self.order_counter = 0
        self.current_position = None
        self.trade_results = []
        self.pending_orders = {}
        self.daily_trades = {}
        self.daily_pnl = {}
        self.consecutive_losses = 0
        self.cooldown_bars_remaining = 0

