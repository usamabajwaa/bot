import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List
import pandas as pd
import pytz

from broker import TopstepXClient, OrderSide, OrderType, OrderStatus, PositionType, Contract
from broker.signalr_client import SignalRClient, Quote, UserOrder, UserPosition, UserTrade, SIGNALR_AVAILABLE
from strategy import Strategy, SignalType
from alerts import AlertManager, load_alert_config
from zones import ZoneType


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('live_trading.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class LiveTrader:
    
    def __init__(
        self, 
        config_path: str = 'config_production.json',
        credentials_path: str = 'credentials.json'
    ):
        self.config = self._load_config(config_path)
        self.credentials = self._load_credentials(credentials_path)
        
        base_url = self.credentials.get('base_url')
        rtc_url = self.credentials.get('rtc_url')
        
        self.client = TopstepXClient(
            username=self.credentials['username'],
            api_key=self.credentials['api_key'],
            base_url=base_url,
            rtc_url=rtc_url
        )
        
        self.signalr: Optional[SignalRClient] = None
        self.last_heartbeat_time = None
        self.heartbeat_interval = 60  # Log heartbeat every 60 seconds
        
        self.strategy = Strategy(self.config)
        
        alert_config = load_alert_config('alerts_config.json')
        self.alerts = AlertManager(alert_config)
        
        self.timezone = pytz.timezone(self.config.get('timezone', 'America/Chicago'))
        self.position_size = self.config.get('position_size_contracts', 5)
        self.daily_loss_limit = self.config.get('daily_loss_limit', -1500)
        self.max_trades_per_day = self.config.get('max_trades_per_day', 4)
        self.tick_size = self.config.get('tick_size', 0.10)
        self.tick_value = self.config.get('tick_value', 1.0)
        
        trailing_config = self.config.get('trailing_stop', {})
        self.trailing_enabled = trailing_config.get('enabled', False)
        self.trailing_activation_r = trailing_config.get('activation_r', 1.0)
        self.trailing_distance_r = trailing_config.get('trail_distance_r', 0.4)
        
        break_even_config = self.config.get('break_even', {})
        self.early_be_enabled = break_even_config.get('early_be_enabled', False)
        self.early_be_ticks = break_even_config.get('early_be_ticks', 40)
        
        partial_config = self.config.get('partial_profit', {})
        self.partial_enabled = partial_config.get('enabled', True)
        self.partial_exit_r = partial_config.get('first_exit_r', 1.0)
        self.partial_exit_pct = partial_config.get('first_exit_pct', 0.5)
        self.structure_based_partial = partial_config.get('structure_based', False)
        self.structure_buffer_ticks = partial_config.get('structure_buffer_ticks', 3)
        # Larger buffer for SL placement to avoid liquidity sweeps
        self.liquidity_sweep_buffer_ticks = partial_config.get('liquidity_sweep_buffer_ticks', 10)
        # How much profit to lock after partial exit (0.5R gives more room for retest)
        self.post_partial_sl_lock_r = partial_config.get('post_partial_sl_lock_r', 0.5)
        
        # Limit order retest config
        limit_config = self.config.get('limit_order_retest', {})
        self.limit_order_enabled = limit_config.get('enabled', False)
        self.limit_max_wait_bars = limit_config.get('max_wait_bars', 4)
        self.limit_entry_offset_ticks = limit_config.get('entry_offset_ticks', 1)
        
        self.contract: Optional[Contract] = None
        self.current_position: Optional[Dict] = None
        self.pending_orders: Dict[int, Dict] = {}
        self.pending_limit_order: Optional[Dict] = None  # For limit order retest
        self._executing_entry = False  # Lock to prevent concurrent entry execution
        
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.last_trade_date: Optional[datetime] = None
        
        self.last_quote: Optional[Quote] = None
        self.quote_count = 0  # Track quote reception
        self.last_quote_log_time = None  # For periodic logging
        self.last_heartbeat_time = None  # For heartbeat logging
        self.heartbeat_interval = 60  # Log heartbeat every 60 seconds
        self.running = False
        self.daily_limit_triggered = False
        self.highest_price = 0.0
        self.lowest_price = float('inf')
        
        cooldown_config = self.config.get('cooldown', {})
        self.cooldown_enabled = cooldown_config.get('enabled', True)
        self.cooldown_trigger_losses = cooldown_config.get('consecutive_losses_trigger', 2)
        # Convert pause_bars to minutes based on trading interval (3-minute bars)
        bar_interval_minutes = 3  # Match trading data interval
        pause_bars = cooldown_config.get('pause_bars', 20)
        self.cooldown_minutes = pause_bars * bar_interval_minutes
        self.consecutive_losses = 0
        self.cooldown_until: Optional[datetime] = None
        
    def _load_config(self, path: str) -> dict:
        with open(path, 'r') as f:
            return json.load(f)
    
    def _load_credentials(self, path: str) -> dict:
        cred_path = Path(path)
        if not cred_path.exists():
            logger.error(f"Credentials file not found: {path}")
            logger.error("Create credentials.json with: username, api_key")
            raise FileNotFoundError(f"Missing {path}")
        with open(path, 'r') as f:
            return json.load(f)
    
    def connect(self) -> bool:
        logger.info("=" * 50)
        logger.info("Connecting to ProjectX Gateway API...")
        logger.info("=" * 50)
        
        if not self.client.authenticate():
            logger.error("Authentication failed. Check username and API key.")
            return False
        
        logger.info("OK Authentication successful")
        
        accounts = self.client.get_accounts(only_active=True)
        if not accounts:
            logger.error("No active accounts found")
            return False
        
        logger.info(f"Found {len(accounts)} account(s):")
        for acc in accounts:
            tradable = "OK" if acc.can_trade else "NO"
            logger.info(f"  [{tradable}] ID: {acc.id} | Name: {acc.name} | Balance: ${acc.balance:.2f}")
        
        configured_account = self.credentials.get('account_id')
        account_suffix = self.credentials.get('account_suffix')  # Support account suffix matching
        
        if configured_account:
            self.client.set_account(configured_account)
            logger.info(f"OK Using configured account: {configured_account}")
        elif account_suffix:
            # Find account ending with specified suffix (by ID or name)
            # First try tradable accounts
            matching_accounts = [a for a in accounts if a.can_trade and (str(a.id).endswith(str(account_suffix)) or str(a.name).endswith(str(account_suffix)))]
            
            # If not found, try any account (regardless of tradable status)
            if not matching_accounts:
                matching_accounts = [a for a in accounts if str(a.id).endswith(str(account_suffix)) or str(a.name).endswith(str(account_suffix))]
            
            if matching_accounts:
                account = matching_accounts[0]
                self.client.set_account(account.id)
                if not account.can_trade:
                    logger.warning(f"Using account ending with '{account_suffix}': {account.id} (marked as NOT TRADABLE, but proceeding anyway)")
                else:
                    logger.info(f"OK Using account ending with '{account_suffix}': {account.id}")
            else:
                logger.error(f"No account found ending with '{account_suffix}'")
                tradable_accounts = [a for a in accounts if a.can_trade]
                if tradable_accounts:
                    self.client.set_account(tradable_accounts[0].id)
                    logger.warning(f"Falling back to first tradable account: {tradable_accounts[0].id}")
                else:
                    logger.error("No tradable accounts available")
                    return False
        else:
            tradable_accounts = [a for a in accounts if a.can_trade]
            if not tradable_accounts:
                logger.error("No tradable accounts available")
                return False
            
            self.client.set_account(tradable_accounts[0].id)
            logger.info(f"OK Using first tradable account: {tradable_accounts[0].id}")
            logger.info("  TIP: Add 'account_id' or 'account_suffix' to credentials.json to specify account")
        
        self.contract = self.client.find_mgc_contract()
        if not self.contract:
            logger.error("MGC contract not found. Available contracts:")
            contracts = self.client.get_available_contracts()
            for c in contracts[:10]:
                logger.info(f"  {c.id} - {c.name} - {c.description}")
            return False
        
        logger.info(f"OK Found contract: {self.contract.id}")
        logger.info(f"  Name: {self.contract.name}")
        logger.info(f"  Description: {self.contract.description}")
        logger.info(f"  Tick Size: {self.contract.tick_size}")
        logger.info(f"  Tick Value: ${self.contract.tick_value}")
        
        if SIGNALR_AVAILABLE:
            try:
                rtc_url = self.credentials.get('rtc_url', self.client.DEMO_RTC_URL)
                self.signalr = SignalRClient(self.client.token, rtc_url)
                
                self.signalr.on_quote = self._on_quote
                self.signalr.on_order = self._on_order
                self.signalr.on_position = self._on_position
                self.signalr.on_trade = self._on_trade
                
                self.signalr.connect_user_hub(self.client.account_id)
                self.signalr.connect_market_hub([self.contract.id])
                
                logger.info("OK Real-time streaming connected")
            except Exception as e:
                logger.warning(f"SignalR not available: {e}")
                logger.warning("  Falling back to REST polling")
        else:
            logger.warning("signalrcore not installed - using REST polling")
            logger.warning("  Install with: pip install signalrcore")
        
        logger.info("=" * 50)
        logger.info("CONNECTION SUCCESSFUL")
        logger.info("=" * 50)
        
        # Load persisted zones and fetch extended historical data for zone building
        self._initialize_zones()
        
        # Reconcile positions on startup
        self._reconcile_positions()
        
        return True
    
    def _reconcile_positions(self) -> None:
        """Check broker for existing positions and sync internal state"""
        try:
            logger.info("Reconciling positions on startup...")
            positions = self.client.get_positions()
            
            for pos in positions:
                if pos.contract_id == self.contract.id and pos.size != 0:
                    if self.current_position is None:
                        logger.warning(f"Found orphaned position on startup: {pos.size} contracts @ ${pos.average_price:.2f}")
                        # Create a UserPosition-like object for syncing
                        from broker.signalr_client import UserPosition
                        broker_pos = UserPosition(
                            id=0,
                            account_id=self.client.account_id,
                            contract_id=pos.contract_id,
                            position_type=1 if pos.size > 0 else 2,
                            size=pos.size,
                            average_price=pos.average_price
                        )
                        self._sync_position_from_broker(broker_pos)
                    else:
                        logger.info(f"Position already tracked: {pos.size} contracts")
            logger.info("Position reconciliation complete")
        except Exception as e:
            logger.error(f"Failed to reconcile positions: {e}")
    
    def _initialize_zones(self) -> None:
        """Initialize zones by loading persisted zones and fetching extended historical data"""
        try:
            zones_file = Path('zones.json')
            
            # Try to load persisted zones
            if zones_file.exists():
                logger.info("Loading persisted zones from zones.json...")
                if self.strategy.zone_manager.load_zones('zones.json'):
                    stats = self.strategy.zone_manager.get_zone_stats()
                    logger.info(f"Loaded {stats['total_zones']} zones ({stats['active_demand']} demand, {stats['active_supply']} supply)")
                else:
                    logger.warning("Failed to load persisted zones")
            else:
                logger.info("No persisted zones found, will build from historical data")
            
            # Fetch extended historical data to build comprehensive zone map
            logger.info("Fetching extended historical data for zone initialization...")
            extended_df = self._fetch_extended_bars(days=30)  # 30 days of data
            
            if not extended_df.empty:
                logger.info(f"Fetched {len(extended_df)} bars for zone initialization")
                # Prepare data and merge zones (preserves any loaded zones)
                self.strategy.prepare_data(extended_df, merge_zones=True)
                
                # Save zones after building from historical data
                if self.strategy.zone_manager.save_zones('zones.json'):
                    stats = self.strategy.zone_manager.get_zone_stats()
                    logger.info(f"Zone initialization complete: {stats['total_zones']} total zones ({stats['active_demand']} demand, {stats['active_supply']} supply)")
                    logger.info(f"Zones saved to zones.json")
                else:
                    logger.warning("Failed to save zones")
            else:
                logger.warning("No extended historical data available for zone initialization")
                
        except Exception as e:
            logger.error(f"Failed to initialize zones: {e}")
            logger.warning("Continuing with empty zone map - performance may differ from backtest")
    
    def _fetch_extended_bars(self, days: int = 30) -> pd.DataFrame:
        """Fetch extended historical data for zone initialization"""
        try:
            from datetime import timezone
            
            now = datetime.now(timezone.utc)
            start_time = now - timedelta(days=days)
            
            logger.info(f"Fetching {days} days of historical data for zone building...")
            
            # Fetch in chunks to avoid API limits
            all_bars = []
            chunk_days = 7  # Fetch 7 days at a time
            current_start = start_time
            
            while current_start < now:
                current_end = min(current_start + timedelta(days=chunk_days), now)
                
                bars = self.client.get_historical_bars(
                    contract_id=self.contract.id,
                    interval=3,  # 3-minute bars
                    start_time=current_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    end_time=current_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    count=10000,  # Large count to get all bars in chunk
                    live=False,
                    unit=2
                )
                
                if bars:
                    all_bars.extend(bars)
                    logger.info(f"  Fetched {len(bars)} bars for {current_start.strftime('%Y-%m-%d')} to {current_end.strftime('%Y-%m-%d')}")
                
                current_start = current_end
                
                # Small delay to respect rate limits
                time.sleep(0.5)
            
            if not all_bars:
                logger.warning("No bars returned from extended fetch")
                return pd.DataFrame()
            
            df = pd.DataFrame(all_bars)
            
            if 't' in df.columns:
                df = df.rename(columns={'t': 'timestamp', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
            
            # TopStep API returns UTC timestamps - parse with UTC timezone
            df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
            df = df.sort_values('timestamp').drop_duplicates(subset=['timestamp']).reset_index(drop=True)
            
            logger.info(f"Extended data fetch complete: {len(df)} total bars")
            return df
            
        except Exception as e:
            logger.error(f"Failed to fetch extended bars: {e}")
            return pd.DataFrame()
    
    def _on_quote(self, quote: Quote):
        self.last_quote = quote
        self.quote_count += 1
        
        # Heartbeat: Log every N seconds to show connection is alive
        now = datetime.now(self.timezone)
        if self.last_heartbeat_time is None or (now - self.last_heartbeat_time).total_seconds() >= self.heartbeat_interval:
            logger.info(f"[HEARTBEAT] SignalR connection alive - Quotes received: {self.quote_count}, Last price: ${quote.last_price:.2f}")
            self.last_heartbeat_time = now
        
        # Log quotes periodically (every 10 seconds when position exists, 30 seconds otherwise)
        should_log = False
        
        if self.current_position is not None:
            # Log every 10 seconds when we have a position (for monitoring partial profits)
            if self.last_quote_log_time is None or (now - self.last_quote_log_time).total_seconds() >= 10:
                should_log = True
                self.last_quote_log_time = now
        elif self.last_quote_log_time is None or (now - self.last_quote_log_time).total_seconds() >= 30:
            # Log every 30 seconds when no position
            should_log = True
            self.last_quote_log_time = now
        
        if should_log:
            logger.info(f"Quote received: ${quote.last_price:.2f} (Bid: ${quote.best_bid:.2f}, Ask: ${quote.best_ask:.2f}) [Total quotes: {self.quote_count}]")
            if self.current_position:
                pos = self.current_position
                entry = pos['entry_price']
                side = pos['side']
                unrealized_pnl = self._calculate_unrealized_pnl(quote.last_price)
                logger.info(f"  Position: {side.upper()} @ ${entry:.2f}, Current: ${quote.last_price:.2f}, Unrealized P&L: ${unrealized_pnl:.2f}")
        
        if self.current_position is not None and not self.daily_limit_triggered:
            self._check_partial_profit(quote.last_price)
            self._check_structure_level_break(quote.last_price)
            self._update_trailing_stop(quote.last_price)
            self._check_realtime_pnl(quote.last_price)
        
        # Check pending limit orders for fill
        if self.pending_limit_order is not None and self.current_position is None:
            self._check_pending_limit_order(quote.last_price)
    
    def _on_order(self, order: UserOrder):
        logger.info(f"Order update: #{order.id} Status: {OrderStatus(order.status).name}")
        
        if order.status == OrderStatus.FILLED:
            logger.info(f"  Order FILLED at {order.filled_price}")
        elif order.status == OrderStatus.REJECTED:
            logger.error(f"  Order REJECTED")
            self.alerts.error(f"Order #{order.id} was rejected")
    
    def _on_position(self, position: UserPosition):
        logger.info(f"Position update: {position.contract_id} Size: {position.size}")
        
        if position.size == 0 and self.current_position:
            logger.info("Position closed")
            self.current_position = None
    
    def _on_trade(self, trade: UserTrade):
        logger.info(f"Trade: {trade.size} @ {trade.price} P&L: ${trade.pnl:.2f}")
        
        self.daily_pnl += trade.pnl
        
        if trade.pnl != 0:
            if trade.pnl < 0:
                self.consecutive_losses += 1
                if self.cooldown_enabled and self.consecutive_losses >= self.cooldown_trigger_losses:
                    self.cooldown_until = datetime.now(self.timezone) + timedelta(minutes=self.cooldown_minutes)
                    logger.warning(f"Cooldown triggered after {self.consecutive_losses} consecutive losses. Pausing until {self.cooldown_until.strftime('%H:%M')}")
                    self.alerts.error(f"Cooldown: {self.consecutive_losses} losses. Pausing {self.cooldown_minutes} min")
            else:
                self.consecutive_losses = 0
            
            side = "LONG" if trade.side == 0 else "SHORT"
            self.alerts.trade_exit(
                side=side,
                entry_price=self.current_position.get('entry_price', 0) if self.current_position else 0,
                exit_price=trade.price,
                pnl=trade.pnl,
                exit_reason="position_closed"
            )
    
    def _reset_daily_counters(self) -> None:
        today = datetime.now(self.timezone).date()
        
        if self.last_trade_date != today:
            if self.daily_trades > 0:
                logger.info(f"Daily summary - Trades: {self.daily_trades}, P&L: ${self.daily_pnl:.2f}")
            
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.daily_limit_triggered = False
            self.consecutive_losses = 0
            self.cooldown_until = None
            self.pending_limit_order = None  # Clear pending limit orders
            self.last_trade_date = today
            logger.info(f"New trading day: {today}")
    
    def _can_trade(self) -> tuple:
        self._reset_daily_counters()
        
        if self.daily_limit_triggered:
            return False, "daily_limit_triggered"
        
        if self.daily_trades >= self.max_trades_per_day:
            return False, "max_trades_reached"
        
        if self.daily_pnl <= self.daily_loss_limit:
            self.daily_limit_triggered = True
            self.alerts.daily_limit_reached(self.daily_pnl, self.daily_loss_limit)
            return False, "daily_loss_limit"
        
        if self.cooldown_until is not None:
            if datetime.now(self.timezone) < self.cooldown_until:
                return False, "cooldown"
            else:
                self.cooldown_until = None
                self.consecutive_losses = 0
                logger.info("Cooldown ended. Resuming trading.")
        
        now = datetime.now(self.timezone)
        day_name = now.strftime('%A')
        
        blocked_days = self.config.get('blocked_days', [])
        if day_name in blocked_days:
            return False, f"blocked_day_{day_name}"
        
        return True, ""
    
    def _get_current_price(self) -> Optional[float]:
        if self.last_quote:
            return self.last_quote.last_price
        
        try:
            from datetime import timezone
            
            now = datetime.now(timezone.utc)
            start_time = now - timedelta(minutes=5)
            
            bars = self.client.get_historical_bars(
                contract_id=self.contract.id,
                interval=1,
                start_time=start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                count=5,
                live=False,
                unit=2
            )
            if bars:
                return bars[-1].get('c', bars[-1].get('close'))
        except Exception as e:
            logger.error(f"Failed to get price: {e}")
        
        return None
    
    def _fetch_recent_bars(self, count: int = 100) -> pd.DataFrame:
        try:
            from datetime import timezone
            
            # Bar interval in minutes (matches backtest data interval)
            bar_interval_minutes = 3
            bar_interval_seconds = bar_interval_minutes * 60  # 180 seconds
            
            # Stale threshold: 3 bar intervals + buffer for API delays
            # This allows for normal delays while still detecting truly stale data
            stale_threshold_seconds = bar_interval_seconds * 3  # 9 minutes (3 bar intervals)
            
            now = datetime.now(timezone.utc)
            start_time = now - timedelta(days=2)
            
            logger.info(f"Fetching recent bars (last {count} bars, {bar_interval_minutes}-minute interval)...")
            
            bars = self.client.get_historical_bars(
                contract_id=self.contract.id,
                interval=bar_interval_minutes,  # Match backtest data interval (3-minute bars)
                start_time=start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                count=count,
                live=False,
                unit=2
            )
            
            if not bars:
                logger.warning("No bars returned from API")
                return pd.DataFrame()
            
            df = pd.DataFrame(bars)
            
            if 't' in df.columns:
                df = df.rename(columns={'t': 'timestamp', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
            
            # Parse timestamps - TopStep API returns UTC timestamps (ISO format with Z or Unix milliseconds)
            df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
            df = df.sort_values('timestamp').reset_index(drop=True)
            
            # Validate data freshness
            if len(df) > 0:
                last_bar_time = pd.to_datetime(df['timestamp'].iloc[-1])
                # Ensure UTC timezone
                if last_bar_time.tzinfo is None:
                    last_bar_time = pytz.UTC.localize(last_bar_time)
                else:
                    last_bar_time = last_bar_time.astimezone(pytz.UTC)
                
                time_diff = (now - last_bar_time).total_seconds()
                logger.info(f"Data refresh: {len(df)} bars fetched, last bar: {last_bar_time.strftime('%Y-%m-%d %H:%M:%S UTC')} ({time_diff:.0f}s ago)")
                
                if time_diff > stale_threshold_seconds:
                    logger.warning(f"Data is stale: {time_diff:.0f} seconds old (>{stale_threshold_seconds}s threshold, {stale_threshold_seconds/bar_interval_seconds:.1f} bar intervals)")
                elif time_diff < 0:
                    logger.warning(f"Data timestamp is in the future: {abs(time_diff):.0f} seconds ahead")
                else:
                    logger.info(f"Data is fresh: {time_diff:.0f} seconds old ({time_diff/bar_interval_seconds:.1f} bar intervals)")
            else:
                logger.warning("Data refresh: No bars in DataFrame")
            
            return df
            
        except Exception as e:
            logger.error(f"Failed to fetch bars: {e}")
            return pd.DataFrame()
    
    def _check_for_signal(self, df: pd.DataFrame) -> Optional[Dict]:
        if len(df) < 20:
            return None
        
        # Note: df should already be prepared with zones merged in run_once()
        # But if called directly, prepare it here
        if 'atr' not in df.columns:
            df = self.strategy.prepare_data(df, merge_zones=True)
        
        bar_index = len(df) - 1
        last_bar = df.iloc[bar_index]
        timestamp = pd.Timestamp(last_bar['timestamp'])
        price = last_bar['close']
        
        # Log signal check attempt with UTC time for clarity
        if timestamp.tzinfo is None:
            timestamp_utc = pytz.UTC.localize(timestamp)
        else:
            timestamp_utc = timestamp.astimezone(pytz.UTC)
        
        # Use current time for session detection to avoid stale bar timestamp issues
        current_time_utc = pd.Timestamp.now(tz=pytz.UTC)
        logger.info(f"Signal check: Bar time={timestamp_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}, Current time={current_time_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}, Price=${price:.2f}")
        
        session = self.strategy.session_manager.get_active_session(current_time_utc)
        if session:
            logger.info(f"  Session: {session}")
        else:
            utc_hour = timestamp_utc.hour
            # Get all enabled sessions for logging
            enabled_sessions = []
            for sess_name, sess_config in self.strategy.session_manager.sessions.items():
                if sess_config.get('enabled', True):
                    enabled_sessions.append(f"{sess_name}: {sess_config['start']}-{sess_config['end']} UTC")
            sessions_str = ", ".join(enabled_sessions) if enabled_sessions else "none"
            logger.info(f"  -> No active session (Current UTC hour: {utc_hour:02d}:00, enabled sessions: {sessions_str})")
            return None
        
        # Check zone availability
        active_demand = len([z for z in self.strategy.zone_manager.zones if z.is_active and z.zone_type == ZoneType.DEMAND])
        active_supply = len([z for z in self.strategy.zone_manager.zones if z.is_active and z.zone_type == ZoneType.SUPPLY])
        logger.info(f"  Active zones: {active_demand} demand, {active_supply} supply")
        
        # Check for zones near current price
        bar_low = last_bar['low']
        bar_high = last_bar['high']
        vwap = last_bar.get('vwap', 0)
        logger.info(f"  Price range: ${bar_low:.2f}-${bar_high:.2f}, VWAP=${vwap:.2f}")
        
        touched_demand = self.strategy.zone_manager.find_touched_zones(bar_low, bar_high, bar_index, ZoneType.DEMAND)
        touched_supply = self.strategy.zone_manager.find_touched_zones(bar_low, bar_high, bar_index, ZoneType.SUPPLY)
        
        if touched_demand:
            logger.info(f"  -> Found {len(touched_demand)} demand zones touched")
            for z in touched_demand[:3]:  # Show first 3
                conf_status = "HIGH" if z.confidence >= self.strategy.zone_manager.min_confidence else "LOW"
                logger.info(f"      Demand @ ${z.pivot_price:.2f} (${z.low:.2f}-${z.high:.2f}), conf={z.confidence:.2f} [{conf_status}]")
        else:
            logger.info(f"  -> No demand zones touched")
            
        if touched_supply:
            logger.info(f"  -> Found {len(touched_supply)} supply zones touched")
            for z in touched_supply[:3]:  # Show first 3
                conf_status = "HIGH" if z.confidence >= self.strategy.zone_manager.min_confidence else "LOW"
                logger.info(f"      Supply @ ${z.pivot_price:.2f} (${z.low:.2f}-${z.high:.2f}), conf={z.confidence:.2f} [{conf_status}]")
        else:
            logger.info(f"  -> No supply zones touched")
        
        signal = self.strategy.generate_signal(
            df=df,
            bar_index=bar_index,
            daily_trades=self.daily_trades,
            daily_pnl=self.daily_pnl,
            in_cooldown=self.cooldown_until is not None and datetime.now(self.timezone) < self.cooldown_until,
            debug_log=True  # Enable detailed filter logging
        )
        
        if signal is None or signal.signal_type == SignalType.NONE:
            logger.info(f"  -> No signal generated (filters: VWAP, HTF, chop, volume, confirmation, zones, R:R)")
            return None
        
        rr_ratio = signal.reward_ticks / signal.risk_ticks if signal.risk_ticks > 0 else 0
        logger.info(f"SIGNAL GENERATED: {signal.signal_type.value.upper()} @ ${signal.entry_price:.2f}, SL=${signal.stop_loss:.2f}, TP=${signal.take_profit:.2f}, R:R={rr_ratio:.2f}")
        return {
            'type': 'long' if signal.signal_type == SignalType.LONG else 'short',
            'entry_price': signal.entry_price,
            'stop_loss': signal.stop_loss,
            'take_profit': signal.take_profit,
            'session': signal.session,
            'risk_ticks': signal.risk_ticks,
            'reward_ticks': signal.reward_ticks,
            'structure_levels': signal.structure_levels or [],
            'zone': signal.zone
        }
    
    def _create_pending_limit_order(self, signal: Dict) -> None:
        """Create a pending limit order at the zone edge for retest entry."""
        side = signal['type']
        zone = signal.get('zone')
        
        if zone is None:
            logger.warning("No zone info for limit order, falling back to market entry")
            self._execute_entry(signal)
            return
        
        # Calculate limit price at zone edge
        if side == 'long':
            limit_price = zone.high + (self.limit_entry_offset_ticks * self.tick_size)
        else:
            limit_price = zone.low - (self.limit_entry_offset_ticks * self.tick_size)
        
        # Recalculate risk/reward with limit price
        if side == 'long':
            risk = limit_price - signal['stop_loss']
            reward = signal['take_profit'] - limit_price
        else:
            risk = signal['stop_loss'] - limit_price
            reward = limit_price - signal['take_profit']
        
        if risk <= 0 or reward <= 0:
            logger.warning("Invalid risk/reward for limit order, falling back to market entry")
            self._execute_entry(signal)
            return
        
        risk_ticks = risk / self.tick_size
        reward_ticks = reward / self.tick_size
        
        self.pending_limit_order = {
            'side': side,
            'limit_price': limit_price,
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'session': signal['session'],
            'risk_ticks': risk_ticks,
            'reward_ticks': reward_ticks,
            'structure_levels': signal.get('structure_levels', []),
            'created_time': datetime.now(self.timezone)
        }
        
        logger.info("=" * 40)
        logger.info(f"PENDING LIMIT ORDER - {side.upper()}")
        logger.info("=" * 40)
        logger.info(f"  Limit Price: ${limit_price:.2f}")
        logger.info(f"  Stop Loss:   ${signal['stop_loss']:.2f}")
        logger.info(f"  Take Profit: ${signal['take_profit']:.2f}")
        logger.info(f"  Max Wait:    {self.limit_max_wait_bars} bars (15-min)")
        
        self.alerts.signal_detected(
            signal_type=f"{side} (LIMIT)",
            entry_price=limit_price,
            stop_loss=signal['stop_loss'],
            take_profit=signal['take_profit'],
            session=signal['session']
        )

    def _execute_entry(self, signal: Dict) -> bool:
        # Execution lock should already be set by run_once() before calling this
        # But verify it's set as a safety check
        if not self._executing_entry:
            logger.warning(f"Entry blocked: Execution lock not set (race condition?)")
            return False
        
        # Prevent duplicate orders - check if position already exists (double-check)
        # Allow pending positions (set by run_once before calling this)
        if self.current_position is not None and not self.current_position.get('pending'):
            logger.warning(f"Entry blocked: Position already exists ({self.current_position['side']} @ ${self.current_position['entry_price']:.2f})")
            self._executing_entry = False
            return False
        
        # Also check for pending limit orders
        if self.pending_limit_order is not None:
            logger.warning(f"Entry blocked: Pending limit order exists")
            self._executing_entry = False
            return False
        
        side = OrderSide.BID if signal['type'] == 'long' else OrderSide.ASK
        
        logger.info("=" * 40)
        logger.info(f"EXECUTING {signal['type'].upper()} ENTRY")
        logger.info("=" * 40)
        logger.info(f"  Entry Price: ${signal['entry_price']:.2f}")
        logger.info(f"  Stop Loss:   ${signal['stop_loss']:.2f}")
        logger.info(f"  Take Profit: ${signal['take_profit']:.2f}")
        logger.info(f"  Risk:        {signal['risk_ticks']:.0f} ticks")
        logger.info(f"  Reward:      {signal['reward_ticks']:.0f} ticks")
        logger.info(f"  Size:        {self.position_size} contracts")
        
        self.alerts.signal_detected(
            signal_type=signal['type'],
            entry_price=signal['entry_price'],
            stop_loss=signal['stop_loss'],
            take_profit=signal['take_profit'],
            session=signal['session']
        )
        
        # Set position IMMEDIATELY before placing order to prevent race condition
        # Use a temporary flag to mark that we're entering
        self.current_position = {
            'side': signal['type'],
            'entry_price': signal['entry_price'],
            'stop_loss': signal['stop_loss'],
            'initial_stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'quantity': self.position_size,
            'entry_time': datetime.now(self.timezone),
            'order_id': None,  # Will be set after order is placed
            'structure_levels': signal.get('structure_levels', []),
            'last_broken_level': None,
            'pending': True  # Flag to indicate order is pending
        }
        
        try:
            sl_ticks = int(abs(signal['risk_ticks']))
            tp_ticks = int(abs(signal['reward_ticks']))
            
            result = self.client.place_bracket_order(
                contract_id=self.contract.id,
                side=side,
                size=self.position_size,
                stop_loss_ticks=sl_ticks,
                take_profit_ticks=tp_ticks
            )
            
            if not result.get('success'):
                error = result.get('errorMessage', 'Unknown error')
                logger.error(f"Order failed: {error}")
                self.alerts.error(f"Order failed: {error}")
                # Clear position on failure
                self.current_position = None
                self._executing_entry = False  # Release lock
                return False
            
            order_id = result.get('orderId')
            
            # Update position with order ID and remove pending flag
            self.current_position['order_id'] = order_id
            self.current_position.pop('pending', None)
            
            self.highest_price = signal['entry_price']
            self.lowest_price = signal['entry_price']
            
            self.daily_trades += 1
            
            logger.info(f"OK Order placed successfully. Order ID: {order_id}")
            
            self.alerts.trade_entry(
                side=signal['type'],
                entry_price=signal['entry_price'],
                quantity=self.position_size,
                stop_loss=signal['stop_loss'],
                take_profit=signal['take_profit']
            )
            
            self._executing_entry = False  # Release lock after successful order
            return True
            
        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            self.alerts.error(f"Order execution failed: {e}")
            # Clear position and release lock on exception
            self.current_position = None
            self._executing_entry = False
            return False
    
    def _check_position_status(self) -> None:
        if self.current_position is None:
            return
        
        try:
            positions = self.client.get_positions()
            
            has_position = False
            broker_position = None
            for pos in positions:
                if pos.contract_id == self.contract.id and pos.size != 0:
                    has_position = True
                    broker_position = pos
                    break
            
            if not has_position:
                logger.info("Position closed (detected via REST)")
                self.current_position = None
            elif broker_position:
                # Sync position details from broker
                if self.current_position.get('quantity') != abs(broker_position.size):
                    logger.info(f"Position size synced: {self.current_position.get('quantity')} -> {abs(broker_position.size)}")
                    self.current_position['quantity'] = abs(broker_position.size)
                
        except Exception as e:
            logger.error(f"Failed to check position: {e}")
    
    def _sync_position_from_broker(self, position: UserPosition) -> None:
        """Sync position state from broker when we detect orphaned position"""
        try:
            logger.warning(f"Syncing orphaned position: {position.size} contracts @ ${position.average_price:.2f}")
            # Get current price for P&L calculation
            current_price = self._get_current_price()
            if current_price is None:
                logger.error("Cannot sync position - no current price available")
                return
            
            # Create position tracking entry
            side = 'long' if position.size > 0 else 'short'
            self.current_position = {
                'side': side,
                'entry_price': position.average_price,
                'stop_loss': position.average_price,  # Will need to query actual stop
                'initial_stop_loss': position.average_price,
                'take_profit': position.average_price,  # Will need to query actual TP
                'quantity': abs(position.size),
                'entry_time': datetime.now(self.timezone),
                'order_id': None,
                'structure_levels': [],
                'last_broken_level': None,
                'break_even_set': False,
                'partial_exit_done': False
            }
            
            # Try to get actual stop/tp from open orders
            try:
                open_orders = self.client.get_open_orders()
                for order in open_orders:
                    if order.get('contractId') == self.contract.id:
                        if order.get('type') == 4:  # STOP order
                            self.current_position['stop_loss'] = order.get('stopPrice', position.average_price)
                        elif order.get('type') == 1:  # LIMIT order (could be TP)
                            if (side == 'long' and order.get('limitPrice', 0) > position.average_price) or \
                               (side == 'short' and order.get('limitPrice', 0) < position.average_price):
                                self.current_position['take_profit'] = order.get('limitPrice', position.average_price)
            except:
                pass
            
            logger.info(f"Position synced: {side} {abs(position.size)} @ ${position.average_price:.2f}")
            self.alerts.error(f"Orphaned position detected and synced: {side} {abs(position.size)} contracts")
            
        except Exception as e:
            logger.error(f"Failed to sync position from broker: {e}")
    
    def _check_break_even(self) -> None:
        if self.current_position is None:
            return
        
        be_config = self.config.get('break_even', {})
        if not be_config.get('enabled', True):
            return
        
        if self.current_position.get('break_even_set'):
            return
        
        current_price = self._get_current_price()
        if current_price is None:
            return
        
        entry = self.current_position['entry_price']
        stop = self.current_position['stop_loss']
        side = self.current_position['side']
        risk = abs(entry - stop)
        trigger_r = be_config.get('trigger_r', 1.0)
        
        should_move = False
        reason = ""
        
        # Early BE based on ticks (moves to BE when trade goes X ticks in profit)
        if self.early_be_enabled:
            early_be_distance = self.early_be_ticks * self.tick_size
            
            if side == 'long':
                profit_distance = current_price - entry
                if profit_distance >= early_be_distance and stop < entry:
                    should_move = True
                    reason = f"Early BE: {profit_distance/self.tick_size:.0f} ticks profit (threshold: {self.early_be_ticks} ticks)"
            else:  # short
                profit_distance = entry - current_price
                if profit_distance >= early_be_distance and stop > entry:
                    should_move = True
                    reason = f"Early BE: {profit_distance/self.tick_size:.0f} ticks profit (threshold: {self.early_be_ticks} ticks)"
        
        # R-based BE (original logic)
        if not should_move:
            if side == 'long':
                if current_price >= entry + (risk * trigger_r):
                    if stop < entry:
                        should_move = True
                        reason = f"R-based BE: {trigger_r}R profit"
            else:
                if current_price <= entry - (risk * trigger_r):
                    if stop > entry:
                        should_move = True
                        reason = f"R-based BE: {trigger_r}R profit"
        
        if should_move:
            logger.info(f"Moving stop to break-even: ${entry:.2f} ({reason})")
            self.current_position['break_even_set'] = True
            self.current_position['stop_loss'] = entry
            self._update_stop_order(entry)  # Update broker stop order
            self.alerts.stop_moved_to_breakeven(entry)
    
    def _calculate_unrealized_pnl(self, current_price: float) -> float:
        if self.current_position is None:
            return 0.0
        
        entry = self.current_position['entry_price']
        side = self.current_position['side']
        
        if side == 'long':
            return ((current_price - entry) / self.tick_size) * self.tick_value * self.position_size
        else:
            return ((entry - current_price) / self.tick_size) * self.tick_value * self.position_size
    
    def _check_partial_profit(self, current_price: float) -> None:
        if not self.partial_enabled:
            logger.debug("Partial profit disabled")
            return
            
        if self.current_position is None:
            logger.debug("No position for partial profit check")
            return
        
        if self.current_position.get('partial_exit_done'):
            logger.debug("Partial exit already done")
            return
        
        pos = self.current_position
        entry = pos['entry_price']
        initial_sl = pos.get('initial_stop_loss', pos['stop_loss'])
        side = pos['side']
        
        risk = abs(entry - initial_sl)
        buffer = self.structure_buffer_ticks * self.tick_size
        
        logger.debug(f"Checking partial profit: entry=${entry:.2f}, price=${current_price:.2f}, side={side}, risk=${risk:.2f}")
        
        # Structure-based partial: exit just before the next structure level
        if self.structure_based_partial and pos.get('structure_levels'):
            structure_levels = pos['structure_levels']
            logger.info(f"Structure-based partial: {len(structure_levels)} levels = {structure_levels}")
            
            # Use 2x buffer to exit well before the structure level (more aggressive)
            aggressive_buffer = buffer * 2
            
            if side == 'long':
                # Find the closest structure level (supply zone) ahead
                for level in structure_levels:
                    if level > entry:
                        partial_price = level - aggressive_buffer
                        logger.info(f"Long: checking level ${level:.2f}, partial_price=${partial_price:.2f} (2x buffer), current=${current_price:.2f}")
                        if current_price >= partial_price:
                            logger.info(f"Structure-based partial trigger at ${partial_price:.2f} (before level ${level:.2f})")
                            self._execute_partial_exit(current_price)
                            return
                        break
            else:  # short
                # Find the closest structure level (demand zone) ahead
                for level in structure_levels:
                    if level < entry:
                        partial_price = level + aggressive_buffer
                        logger.info(f"Short: checking level ${level:.2f}, partial_price=${partial_price:.2f} (2x buffer), current=${current_price:.2f}")
                        if current_price <= partial_price:
                            logger.info(f"Structure-based partial trigger at ${partial_price:.2f} (before level ${level:.2f})")
                            self._execute_partial_exit(current_price)
                            return
                        break
            logger.info("Structure-based partial: No valid structure level found, falling back to R-based")
        
        # Fallback to R-based partial
        trigger_distance = self.partial_exit_r * risk
        logger.info(f"R-based partial: trigger_distance=${trigger_distance:.2f} ({self.partial_exit_r}R), risk=${risk:.2f}")
        
        should_exit = False
        if side == 'long':
            trigger_price = entry + trigger_distance
            logger.info(f"Long partial: entry=${entry:.2f}, trigger=${trigger_price:.2f}, current=${current_price:.2f}")
            if current_price >= trigger_price:
                should_exit = True
            else:
                # Log progress toward partial profit (every 10% of distance)
                progress = ((current_price - entry) / trigger_distance) * 100 if trigger_distance > 0 else 0
                if progress > 0 and int(progress) % 10 == 0 and progress < 100:
                    logger.info(f"Partial profit progress: {progress:.0f}% (${current_price:.2f} / ${trigger_price:.2f} target)")
        else:  # short
            trigger_price = entry - trigger_distance
            logger.info(f"Short partial: entry=${entry:.2f}, trigger=${trigger_price:.2f}, current=${current_price:.2f}")
            if current_price <= trigger_price:
                should_exit = True
            else:
                # Log progress toward partial profit (every 10% of distance)
                progress = ((entry - current_price) / trigger_distance) * 100 if trigger_distance > 0 else 0
                if progress > 0 and int(progress) % 10 == 0 and progress < 100:
                    logger.info(f"Partial profit progress: {progress:.0f}% (${current_price:.2f} / ${trigger_price:.2f} target)")
        
        if should_exit:
            logger.info(f"*** PARTIAL PROFIT TRIGGER HIT! *** Price: ${current_price:.2f}, Target: ${trigger_price:.2f}")
            self._execute_partial_exit(current_price)
    
    def _check_structure_level_break(self, current_price: float) -> None:
        """
        If price breaks through a structure level (resistance becomes support or vice versa),
        move the stop loss behind that level with extra buffer for liquidity sweeps.
        """
        if not self.structure_based_partial or self.current_position is None:
            return
        
        pos = self.current_position
        if not pos.get('structure_levels'):
            return
        
        # Use smaller buffer for detection, larger for SL placement (liquidity sweep protection)
        detect_buffer = self.structure_buffer_ticks * self.tick_size
        sl_buffer = self.liquidity_sweep_buffer_ticks * self.tick_size
        side = pos['side']
        
        if side == 'long':
            # Check if we broke through any supply zones (resistance becomes support)
            for level in pos['structure_levels'][:]:
                if level > pos['entry_price']:
                    # Price is clearly above this level - it's been broken
                    if current_price > level + detect_buffer:
                        new_sl = level - sl_buffer  # Larger buffer for liquidity sweeps
                        if new_sl > pos['stop_loss']:
                            logger.info(f"Structure level ${level:.2f} broken! Moving SL to ${new_sl:.2f} (with ${sl_buffer:.2f} liquidity buffer)")
                            pos['stop_loss'] = new_sl
                            pos['last_broken_level'] = level
                            pos['structure_levels'].remove(level)
                            self._update_stop_order(new_sl)
                        break
        else:  # short
            # Check if we broke through any demand zones (support becomes resistance)
            for level in pos['structure_levels'][:]:
                if level < pos['entry_price']:
                    # Price is clearly below this level - it's been broken
                    if current_price < level - detect_buffer:
                        new_sl = level + sl_buffer  # Larger buffer for liquidity sweeps
                        if new_sl < pos['stop_loss']:
                            logger.info(f"Structure level ${level:.2f} broken! Moving SL to ${new_sl:.2f} (with ${sl_buffer:.2f} liquidity buffer)")
                            pos['stop_loss'] = new_sl
                            pos['last_broken_level'] = level
                            pos['structure_levels'].remove(level)
                            self._update_stop_order(new_sl)
                        break

    def _check_pending_limit_order(self, current_price: float) -> None:
        """Check if pending limit order should be filled or cancelled."""
        if self.pending_limit_order is None:
            return
        
        order = self.pending_limit_order
        side = order['side']
        limit_price = order['limit_price']
        
        # Check if order expired (using 3-minute bars to match trading interval)
        bars_elapsed = (datetime.now(self.timezone) - order['created_time']).total_seconds() / 180  # 3-min bars
        if bars_elapsed > self.limit_max_wait_bars:
            logger.info(f"Limit order expired after {bars_elapsed:.1f} bars. Cancelling.")
            self.pending_limit_order = None
            return
        
        # Check if limit price was touched
        filled = False
        if side == 'long':
            # For long, price needs to come down to our limit
            if current_price <= limit_price:
                filled = True
        else:  # short
            # For short, price needs to come up to our limit
            if current_price >= limit_price:
                filled = True
        
        if filled:
            logger.info(f"Limit order filled! Entry at ${limit_price:.2f}")
            # Execute the entry
            self._execute_limit_entry(order)
            self.pending_limit_order = None

    def _execute_limit_entry(self, order: Dict) -> bool:
        """Execute entry from a filled limit order."""
        # Prevent duplicate orders - check if we're already executing
        if self._executing_entry:
            logger.warning(f"Limit entry blocked: Already executing an entry order")
            return False
        
        # Prevent duplicate orders - check if position already exists
        if self.current_position is not None:
            logger.warning(f"Limit entry blocked: Position already exists ({self.current_position['side']} @ ${self.current_position['entry_price']:.2f})")
            return False
        
        # Set execution lock IMMEDIATELY
        self._executing_entry = True
        
        side = OrderSide.BID if order['side'] == 'long' else OrderSide.ASK
        
        logger.info("=" * 40)
        logger.info(f"LIMIT ORDER FILLED - {order['side'].upper()} ENTRY")
        logger.info("=" * 40)
        logger.info(f"  Limit Price: ${order['limit_price']:.2f}")
        logger.info(f"  Stop Loss:   ${order['stop_loss']:.2f}")
        logger.info(f"  Take Profit: ${order['take_profit']:.2f}")
        
        try:
            sl_ticks = int(abs(order['risk_ticks']))
            tp_ticks = int(abs(order['reward_ticks']))
            
            result = self.client.place_bracket_order(
                contract_id=self.contract.id,
                side=side,
                size=self.position_size,
                stop_loss_ticks=sl_ticks,
                take_profit_ticks=tp_ticks
            )
            
            if not result.get('success'):
                error = result.get('errorMessage', 'Unknown error')
                logger.error(f"Limit entry failed: {error}")
                self._executing_entry = False  # Release lock
                return False
            
            order_id = result.get('orderId')
            
            self.current_position = {
                'side': order['side'],
                'entry_price': order['limit_price'],
                'stop_loss': order['stop_loss'],
                'initial_stop_loss': order['stop_loss'],
                'take_profit': order['take_profit'],
                'quantity': self.position_size,
                'entry_time': datetime.now(self.timezone),
                'order_id': order_id,
                'structure_levels': order.get('structure_levels', []),
                'last_broken_level': None
            }
            
            self.highest_price = order['limit_price']
            self.lowest_price = order['limit_price']
            self.daily_trades += 1
            
            logger.info(f"OK Limit entry executed. Order ID: {order_id}")
            
            self.alerts.trade_entry(
                side=order['side'],
                entry_price=order['limit_price'],
                quantity=self.position_size,
                stop_loss=order['stop_loss'],
                take_profit=order['take_profit']
            )
            
            self._executing_entry = False  # Release lock after successful order
            return True
            
        except Exception as e:
            logger.error(f"Limit entry execution failed: {e}")
            # Clear position and release lock on exception
            self.current_position = None
            self._executing_entry = False
            return False

    def _execute_partial_exit(self, current_price: float) -> None:
        if self.current_position is None:
            return
        
        pos = self.current_position
        side = pos['side']
        current_qty = pos.get('quantity', self.position_size)
        
        exit_qty = max(1, int(current_qty * self.partial_exit_pct))
        
        if exit_qty >= current_qty:
            return
        
        logger.info("=" * 40)
        logger.info(f"PARTIAL PROFIT EXIT - {exit_qty} contracts")
        logger.info("=" * 40)
        
        try:
            result = self.client.partial_close_position(
                contract_id=self.contract.id,
                size=exit_qty
            )
            
            if result.get('success'):
                pos['partial_exit_done'] = True
                pos['quantity'] = current_qty - exit_qty
                
                entry = pos['entry_price']
                initial_sl = pos.get('initial_stop_loss', pos['stop_loss'])
                risk = abs(entry - initial_sl)
                
                # Use configurable profit lock (0.5R gives room for retest)
                sl_move = self.post_partial_sl_lock_r * risk
                
                if side == 'long':
                    new_sl = entry + sl_move
                else:
                    new_sl = entry - sl_move
                
                pos['stop_loss'] = new_sl
                
                logger.info(f"OK Partial exit: {exit_qty} contracts at ${current_price:.2f}")
                logger.info(f"OK Remaining: {pos['quantity']} contracts")
                logger.info(f"OK Stop moved to: ${new_sl:.2f} ({self.post_partial_sl_lock_r}R profit locked)")
                
                self._update_stop_order(new_sl)
                
                self.alerts.error(f"Partial profit: {exit_qty} contracts at ${current_price:.2f}. Stop to ${new_sl:.2f}")
            else:
                logger.error(f"Partial close failed: {result.get('errorMessage')}")
                
        except Exception as e:
            logger.error(f"Failed to execute partial exit: {e}")
    
    def _update_trailing_stop(self, current_price: float) -> None:
        if not self.trailing_enabled or self.current_position is None:
            return
        
        pos = self.current_position
        entry = pos['entry_price']
        initial_sl = pos.get('initial_stop_loss', pos['stop_loss'])
        side = pos['side']
        
        risk = abs(entry - initial_sl)
        activation_distance = self.trailing_activation_r * risk
        trail_distance = self.trailing_distance_r * risk
        
        if side == 'long':
            if current_price > self.highest_price:
                self.highest_price = current_price
            
            current_profit = self.highest_price - entry
            if current_profit >= activation_distance:
                new_sl = self.highest_price - trail_distance
                if new_sl > pos['stop_loss']:
                    old_sl = pos['stop_loss']
                    pos['stop_loss'] = new_sl
                    logger.info(f"Trailing stop updated: ${old_sl:.2f} → ${new_sl:.2f} (High: ${self.highest_price:.2f})")
                    self._update_stop_order(new_sl)
        else:
            if current_price < self.lowest_price:
                self.lowest_price = current_price
            
            current_profit = entry - self.lowest_price
            if current_profit >= activation_distance:
                new_sl = self.lowest_price + trail_distance
                if new_sl < pos['stop_loss']:
                    old_sl = pos['stop_loss']
                    pos['stop_loss'] = new_sl
                    logger.info(f"Trailing stop updated: ${old_sl:.2f} → ${new_sl:.2f} (Low: ${self.lowest_price:.2f})")
                    self._update_stop_order(new_sl)
    
    def _update_stop_order(self, new_stop_price: float) -> None:
        try:
            if 'stop_order_id' in self.current_position and self.current_position['stop_order_id']:
                result = self.client.modify_order(
                    order_id=self.current_position['stop_order_id'],
                    stop_price=new_stop_price
                )
                if result.get('success'):
                    logger.info(f"Stop order modified: #{self.current_position['stop_order_id']} to ${new_stop_price:.2f}")
                    return
                else:
                    logger.warning(f"Modify failed, placing new order: {result.get('errorMessage')}")
            
            side = self.current_position['side']
            stop_side = OrderSide.ASK if side == 'long' else OrderSide.BID
            remaining_qty = self.current_position.get('quantity', self.position_size)
            
            order = self.client.place_order(
                contract_id=self.contract.id,
                order_type=OrderType.STOP,
                side=stop_side,
                size=remaining_qty,
                stop_price=new_stop_price
            )
            
            if order and order.get('orderId'):
                self.current_position['stop_order_id'] = order.get('orderId')
                logger.info(f"New stop order placed: #{order.get('orderId')} at ${new_stop_price:.2f}")
        except Exception as e:
            logger.error(f"Failed to update stop order: {e}")
    
    def _check_realtime_pnl(self, current_price: float) -> None:
        if self.current_position is None or self.daily_limit_triggered:
            return
        
        unrealized_pnl = self._calculate_unrealized_pnl(current_price)
        total_daily_pnl = self.daily_pnl + unrealized_pnl
        
        if total_daily_pnl <= self.daily_loss_limit:
            self._force_close_position(current_price, total_daily_pnl, unrealized_pnl)
    
    def _force_close_position(self, current_price: float, total_pnl: float, unrealized_pnl: float) -> bool:
        if self.current_position is None or self.daily_limit_triggered:
            return False
        
        self.daily_limit_triggered = True
        
        logger.warning("=" * 60)
        logger.warning("WARNING: DAILY LOSS LIMIT HIT - EMERGENCY EXIT")
        logger.warning("=" * 60)
        logger.warning(f"  Current Price: ${current_price:.2f}")
        logger.warning(f"  Unrealized P&L: ${unrealized_pnl:.2f}")
        logger.warning(f"  Realized P&L: ${self.daily_pnl:.2f}")
        logger.warning(f"  Total Daily P&L: ${total_pnl:.2f}")
        logger.warning(f"  Daily Limit: ${self.daily_loss_limit}")
        logger.warning("=" * 60)
        
        self.alerts.error(
            f"🚨 DAILY LOSS LIMIT HIT!\n"
            f"Total P&L: ${total_pnl:.2f}\n"
            f"Forcing position close at ${current_price:.2f}"
        )
        
        side = self.current_position['side']
        
        try:
            close_side = OrderSide.ASK if side == 'long' else OrderSide.BID
            
            order = self.client.place_order(
                contract_id=self.contract.id,
                order_type=OrderType.MARKET,
                side=close_side,
                size=self.position_size
            )
            
            if order:
                logger.info(f"OK Force exit order placed: #{order.id}")
                self.daily_pnl += unrealized_pnl
                self.current_position = None
                
                self.alerts.error(f"Position closed. No more trades today. Final P&L: ${self.daily_pnl:.2f}")
                return True
                
        except Exception as e:
            logger.error(f"CRITICAL: Failed to force close position: {e}")
            self.alerts.error(f"CRITICAL: Failed to force close position: {e}")
        
        return False
    
    def _check_daily_loss_force_exit(self) -> bool:
        if self.current_position is None or self.daily_limit_triggered:
            return False
        
        current_price = self._get_current_price()
        if current_price is None:
            return False
        
        unrealized_pnl = self._calculate_unrealized_pnl(current_price)
        total_daily_pnl = self.daily_pnl + unrealized_pnl
        
        if total_daily_pnl <= self.daily_loss_limit:
            return self._force_close_position(current_price, total_daily_pnl, unrealized_pnl)
        
        return False
    
    def run_once(self) -> None:
        self._reset_daily_counters()
        
        # CRITICAL: Check execution lock FIRST to prevent duplicate orders
        if self._executing_entry:
            logger.debug("Skipping run_once: Entry execution in progress")
            return
        
        if self.current_position is not None:
            self._check_position_status()
            
            if self.current_position is not None:
                if self._check_daily_loss_force_exit():
                    return
                self._check_break_even()
            return
        
        can_trade, reason = self._can_trade()
        if not can_trade:
            if reason not in ['blocked_day_Tuesday', 'blocked_day_Sunday', 'blocked_day_Friday']:
                logger.debug(f"Cannot trade: {reason}")
            return
        
        df = self._fetch_recent_bars(count=100)
        if df.empty:
            logger.warning("No bar data available - skipping signal check")
            return
        
        # Prepare data and merge zones (don't replace existing zones)
        df = self.strategy.prepare_data(df, merge_zones=True)
        
        # Check for broken zones and convert them (role reversal)
        if len(df) > 0:
            last_bar = df.iloc[-1]
            bar_index = len(df) - 1
            converted_zones = self.strategy.zone_manager.invalidate_broken_zones(
                last_bar['close'], bar_index
            )
            if converted_zones:
                logger.info(f"Zone role reversal: {len(converted_zones)} zones converted (resistance<->support)")
                
                # Check if any converted zones are being retested on the current bar
                # This handles immediate retest after break (supply->demand for longs, demand->supply for shorts)
                bar_low = last_bar['low']
                bar_high = last_bar['high']
                
                for zone in converted_zones:
                    # Check if the bar overlaps the converted zone (retest)
                    overlaps = bar_low <= zone.high and bar_high >= zone.low
                    if overlaps:
                        if zone.zone_type == ZoneType.DEMAND:
                            logger.info(f"  -> Converted demand zone @ ${zone.pivot_price:.2f} is being retested (potential long)")
                        elif zone.zone_type == ZoneType.SUPPLY:
                            logger.info(f"  -> Converted supply zone @ ${zone.pivot_price:.2f} is being retested (potential short)")
                
                # Save updated zones
                self.strategy.zone_manager.save_zones('zones.json')
        
        signal = self._check_for_signal(df)
        
        if signal:
            # CRITICAL: Triple-check we don't have a position or pending execution
            # This prevents race conditions when run_once() is called multiple times quickly
            # Check execution lock FIRST (most important)
            if self._executing_entry:
                logger.warning(f"Signal generated but entry already in progress - skipping duplicate entry")
                return
            
            if self.current_position is not None:
                logger.warning(f"Signal generated but position already exists ({self.current_position['side']} @ ${self.current_position['entry_price']:.2f}) - skipping entry")
                return
            
            if self.pending_limit_order is not None:
                logger.warning(f"Signal generated but pending limit order exists - skipping duplicate entry")
                return
            
            # Set execution lock IMMEDIATELY before calling _execute_entry
            # This prevents another run_once() call from getting past the checks
            self._executing_entry = True
            
            try:
                if self.limit_order_enabled:
                    # Create pending limit order instead of entering immediately
                    self._create_pending_limit_order(signal)
                else:
                    # Immediate market order entry
                    self._execute_entry(signal)
            except Exception as e:
                # Release lock on exception
                logger.error(f"Error executing entry: {e}")
                self._executing_entry = False
                raise
    
    def _check_connection_health(self) -> bool:
        """Check if SignalR and API connections are healthy"""
        try:
            # Check API connection
            if not self.client.token or time.time() > self.client.token_expiry:
                logger.warning("API token expired or missing")
                return False
            
            # Check SignalR if available
            if self.signalr and SIGNALR_AVAILABLE:
                # SignalR connection status is handled internally
                # We can still use REST polling if SignalR is down
                pass
            
            return True
        except Exception as e:
            logger.error(f"Connection health check failed: {e}")
            return False
    
    def run(self, interval_seconds: int = 30) -> None:
        if not self.connect():
            logger.error("Failed to connect. Exiting.")
            return
        
        self.running = True
        
        logger.info("")
        logger.info("=" * 50)
        logger.info("LIVE TRADING STARTED")
        logger.info("=" * 50)
        logger.info(f"  Checking every {interval_seconds} seconds (optimized for 3-minute bars)")
        logger.info(f"  Position size: {self.position_size} contracts")
        logger.info(f"  Max trades/day: {self.max_trades_per_day}")
        logger.info(f"  Daily loss limit: ${self.daily_loss_limit}")
        logger.info(f"  Blocked days: {self.config.get('blocked_days', [])}")
        logger.info("=" * 50)
        logger.info("")
        
        try:
            while self.running:
                try:
                    self.run_once()
                except Exception as e:
                    logger.error(f"Error in trading loop: {e}")
                    self.alerts.error(f"Trading loop error: {e}")
                
                time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("\nShutting down...")
            self.stop()
    
    def stop(self) -> None:
        self.running = False
        
        if self.signalr:
            self.signalr.disconnect()
        
        logger.info("Trading stopped")
    
    def emergency_close(self) -> None:
        logger.warning("=" * 50)
        logger.warning("EMERGENCY CLOSE - Closing all positions")
        logger.warning("=" * 50)
        
        results = self.client.close_all_positions()
        for r in results:
            logger.info(f"Close result: {r}")
        
        self.current_position = None
        self.alerts.error("EMERGENCY CLOSE executed")
    
    def get_status(self) -> Dict:
        positions = []
        try:
            positions = self.client.get_positions()
        except Exception as e:
            logger.error(f"Failed to get positions in get_status: {e}")
            positions = []
        
        return {
            'connected': self.client.token is not None,
            'account_id': self.client.account_id,
            'contract': self.contract.id if self.contract else None,
            'positions': [
                {
                    'contract': p.contract_id,
                    'side': PositionType(p.position_type).name,
                    'size': p.size,
                    'avg_price': p.average_price
                }
                for p in positions
            ],
            'daily_trades': self.daily_trades,
            'daily_pnl': self.daily_pnl,
            'current_position': self.current_position,
            'last_quote': {
                'price': self.last_quote.last_price,
                'bid': self.last_quote.best_bid,
                'ask': self.last_quote.best_ask
            } if self.last_quote else None
        }


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='MGC Live Trading Engine')
    parser.add_argument('--config', type=str, default='config_production.json',
                        help='Path to config file')
    parser.add_argument('--credentials', type=str, default='credentials.json',
                        help='Path to credentials file')
    parser.add_argument('--interval', type=int, default=60,
                        help='Check interval in seconds')
    parser.add_argument('--test', action='store_true',
                        help='Test connection only, no trading')
    
    args = parser.parse_args()
    
    trader = LiveTrader(
        config_path=args.config,
        credentials_path=args.credentials
    )
    
    if args.test:
        logger.info("TEST MODE - Connection test only")
        if trader.connect():
            status = trader.get_status()
            logger.info("\nConnection Status:")
            logger.info(json.dumps(status, indent=2, default=str))
            trader.stop()
        return
    
    trader.run(interval_seconds=args.interval)


if __name__ == '__main__':
    main()
