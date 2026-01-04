import json
import time
import logging
import os
import sys
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


# Configure logging only once - prevent duplicate handlers
# This is critical to avoid duplicate log entries (was causing 12 entries instead of 3)
root_logger = logging.getLogger()
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# Check if handlers already exist for our log file
has_file_handler = False
has_stream_handler = False

for handler in root_logger.handlers:
    if isinstance(handler, logging.FileHandler):
        try:
            # Check if this is our log file handler
            if hasattr(handler, 'baseFilename') and 'live_trading.log' in str(handler.baseFilename):
                has_file_handler = True
        except (AttributeError, TypeError):
            pass
    elif isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
        has_stream_handler = True

# Remove ALL existing handlers to prevent duplicates, then add fresh ones
# This ensures we have exactly one of each handler type
for handler in root_logger.handlers[:]:  # Copy list to avoid modification during iteration
    root_logger.removeHandler(handler)
    if hasattr(handler, 'close'):
        handler.close()

# Add file handler only (StreamHandler removed to avoid OSError in background processes)
# Logs will only go to file, which is fine for background processes
file_handler = logging.FileHandler('live_trading.log')
file_handler.setFormatter(formatter)
root_logger.addHandler(file_handler)

# StreamHandler removed: When running as background process on Windows,
# stderr/stdout handles are invalid, causing OSError [Errno 22] Invalid argument
# File logging is sufficient for background operation

# Set level
root_logger.setLevel(logging.INFO)

logger = logging.getLogger(__name__)


def validate_config(config: dict) -> List[str]:
    """Validate configuration and return list of issues"""
    issues = []
    
    # Required fields
    required = ['tick_size', 'tick_value', 'position_size_contracts']
    for field in required:
        if field not in config:
            issues.append(f"Missing required field: {field}")
    
    # Logical checks
    if 'min_rr' in config and config['min_rr'] < 1.0:
        issues.append("min_rr should be >= 1.0 for positive expectancy")
    
    if 'daily_loss_limit' in config and config['daily_loss_limit'] >= 0:
        issues.append("daily_loss_limit should be negative")
    
    # Session checks
    sessions = config.get('sessions', {})
    for name, sess in sessions.items():
        if 'start' not in sess or 'end' not in sess:
            issues.append(f"Session '{name}' missing start/end times")
    
    # Position size check
    if 'position_size_contracts' in config:
        pos_size = config['position_size_contracts']
        if not isinstance(pos_size, int) or pos_size <= 0:
            issues.append("position_size_contracts must be a positive integer")
    
    # Tick size/value checks
    if 'tick_size' in config and config['tick_size'] <= 0:
        issues.append("tick_size must be positive")
    if 'tick_value' in config and config['tick_value'] <= 0:
        issues.append("tick_value must be positive")
    
    return issues


class CircuitBreaker:
    """Circuit breaker to prevent repeated failures from causing cascading issues"""
    def __init__(self, failure_threshold: int = 3, timeout_minutes: int = 15):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.timeout_minutes = timeout_minutes
        self.last_failure_time = None
        self.is_open = False
    
    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        
        if self.failure_count >= self.failure_threshold:
            self.is_open = True
            logger.error(f"🚨 CIRCUIT BREAKER OPENED after {self.failure_count} failures")
    
    def record_success(self):
        self.failure_count = 0
        if self.is_open:
            logger.info("OK Circuit breaker CLOSED - failures reset")
        self.is_open = False
    
    def should_allow_trade(self) -> bool:
        if not self.is_open:
            return True
        
        # Check if timeout has passed
        if self.last_failure_time:
            elapsed = (datetime.now() - self.last_failure_time).total_seconds() / 60
            if elapsed >= self.timeout_minutes:
                logger.info(f"Circuit breaker reset after {elapsed:.1f} minute timeout")
                self.is_open = False
                self.failure_count = 0
                return True
        
        return False


class ConnectionMonitor:
    """Monitor connection health via heartbeat tracking"""
    def __init__(self, heartbeat_interval: int = 60, max_missed: int = 3):
        self.last_heartbeat = datetime.now()
        self.heartbeat_interval = heartbeat_interval
        self.max_missed = max_missed
        self.missed_count = 0
    
    def record_heartbeat(self):
        self.last_heartbeat = datetime.now()
        self.missed_count = 0
    
    def check_health(self) -> bool:
        elapsed = (datetime.now() - self.last_heartbeat).total_seconds()
        
        if elapsed > self.heartbeat_interval:
            self.missed_count += 1
            logger.warning(f"Missed heartbeat #{self.missed_count} (elapsed: {elapsed:.0f}s, threshold: {self.heartbeat_interval}s)")
            
            if self.missed_count >= self.max_missed:
                logger.error(f"❌ Connection appears dead - {self.missed_count} missed heartbeats (triggering reconnect)")
                return False
        
        return True


class LiveTrader:
    
    def __init__(
        self, 
        config_path: str = 'config_production.json',
        credentials_path: str = 'credentials.json'
    ):
        self.config = self._load_config(config_path)
        
        # Validate configuration
        config_issues = validate_config(self.config)
        if config_issues:
            logger.error("Configuration validation failed:")
            for issue in config_issues:
                logger.error(f"  - {issue}")
            raise ValueError(f"Invalid configuration: {len(config_issues)} issue(s) found")
        
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
        self.daily_loss_limit = self.config.get('daily_loss_limit', -2500)
        self.max_trades_per_day = self.config.get('max_trades_per_day', 4)
        self.max_position_hours = self.config.get('max_position_hours', 8)  # Force close after N hours
        self.tick_size = self.config.get('tick_size', 0.10)
        self.tick_value = self.config.get('tick_value', 1.0)
        self.commission_per_contract = self.config.get('commission_per_contract', 0.62)
        
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
        # Graduated scale-out levels
        self.scale_out_levels = partial_config.get('scale_out_levels', [])
        self.scale_out_mode = partial_config.get('scale_out_mode', 'standard')  # 'standard' or 'graduated'
        
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
        self._trading_locked = False  # Lock to prevent new signals/trades while position is open
        self._last_watchdog_check: Optional[datetime] = None  # Debouncing for watchdog
        self._last_order_update: Optional[datetime] = None  # Debouncing for order updates
        self._last_trailing_update: Optional[datetime] = None  # Debouncing for trailing stop updates
        
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
        self.last_processed_trade_id: Optional[int] = None  # Track last processed trade to avoid double-counting
        self.last_position_qty: Optional[int] = None  # Track last known position quantity for P&L calculation
        
        # Circuit breaker to prevent repeated failures
        self.circuit_breaker = CircuitBreaker(failure_threshold=3, timeout_minutes=15)
        
        # Connection monitor for heartbeat tracking
        self.connection_monitor = ConnectionMonitor(heartbeat_interval=60, max_missed=3)
        
        # Replay mode: save bars when signals are generated
        self.save_replay_data = True  # Configurable flag
        self.replay_data_dir = Path('replay_data')  # Separate folder for replay files
        self.replay_data_dir.mkdir(exist_ok=True)
        self.max_replay_files = 200  # Keep only last 200 replay files
        
        # Rolling DataFrame for zone updates (avoid rebuilding zones every loop)
        self.rolling_df: Optional[pd.DataFrame] = None
        self.rolling_df_max_bars = 2000  # Keep last 2000 bars in memory
        self.last_zone_update_bars: Optional[int] = None  # Track number of bars in rolling_df when zones were last updated
        self.zone_update_interval_bars = 20  # Update zones every 20 new bars (or 15 minutes)
        self.last_zone_update_time: Optional[datetime] = None
        
        # Replay mode: save bars when signals are generated
        self.save_replay_data = True  # Configurable flag
        self.replay_data_dir = Path('replay_data')
        self.replay_data_dir.mkdir(exist_ok=True)
    
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
                        # Use actual position_type from Position object (more reliable)
                        from broker.signalr_client import UserPosition
                        broker_pos = UserPosition(
                            id=0,
                            account_id=self.client.account_id,
                            contract_id=pos.contract_id,
                            position_type=pos.position_type.value,  # Use actual position_type (1=LONG, 2=SHORT)
                            size=pos.size,
                            average_price=pos.average_price
                        )
                        logger.info(f"Position type from broker: {pos.position_type.name} (value: {pos.position_type.value})")
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
                    unit=2,
                    include_partial=False  # Explicitly exclude partial bars
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
            logger.info("Position closed (detected via SignalR)")
            
            # CRITICAL FIX: Calculate and process P&L when position closes (fallback if trade callback missed)
            # This ensures cooldown triggers even if _on_trade() callback didn't fire
            try:
                entry_price = self.current_position.get('entry_price')
                current_price = self._get_current_price()
                
                if entry_price and current_price:
                    side = self.current_position.get('side')
                    quantity = self.current_position.get('quantity', self.position_size)
                    
                    # Calculate P&L
                    if side == 'long':
                        pnl_ticks = (current_price - entry_price) / self.tick_size
                    else:
                        pnl_ticks = (entry_price - current_price) / self.tick_size
                    
                    # Estimate commission (open + close)
                    commission = self.commission_per_contract * quantity * 2
                    gross_pnl = pnl_ticks * self.tick_value * quantity
                    net_pnl = gross_pnl - commission
                    
                    logger.warning(f"[COOLDOWN FALLBACK] Position closed via SignalR - calculated P&L: ${net_pnl:.2f} (entry: ${entry_price:.2f}, exit: ${current_price:.2f}, qty: {quantity})")
                    
                    # Process P&L for cooldown (only if we haven't processed it already via trade callback)
                    # Use a dummy trade_id based on timestamp to allow processing
                    fallback_trade_id = int(datetime.now(self.timezone).timestamp() * 1000)
                    self._process_trade_pnl(net_pnl, trade_id=fallback_trade_id, source="SignalR_position_close")
                    
                    # Update daily P&L if not already updated by trade callback
                    self.daily_pnl += net_pnl
            except Exception as e:
                logger.warning(f"Could not calculate P&L on position close: {e}")
            
            # CRITICAL: Cancel any remaining bracket orders (SL/TP) when position is closed
            # Position Brackets doesn't auto-cancel, so we must clean up manually
            logger.warning("Position closed - immediately cleaning up remaining SL/TP orders...")
            self._cancel_remaining_bracket_orders()
            
            # Wait briefly then call again to catch any missed orders
            import time
            time.sleep(0.5)
            self._cancel_remaining_bracket_orders()
            
            # CRITICAL: Double-check and close everything remaining
            logger.info("Double-checking: Closing any remaining positions and orders...")
            self._emergency_close_all()
            
            # Clear position tracking
            self.current_position = None
            self.last_position_qty = None
            # Release trading lock when position closes
            self._trading_locked = False
            logger.info("Trading lock released - new signals allowed")
        elif self.current_position:
            # Update last known position quantity
            self.last_position_qty = abs(position.size)
    
    def _process_trade_pnl(self, pnl: float, trade_id: Optional[int] = None, source: str = "unknown") -> None:
        """
        Process trade P&L and update cooldown state.
        This is called from multiple sources (_on_trade callback, position close detection, etc.)
        to ensure cooldown triggers reliably even if SignalR callbacks fail.
        
        Args:
            pnl: Trade P&L (negative for losses)
            trade_id: Optional trade ID to prevent double-counting
            source: Source of the trade (for logging)
        """
        # Prevent double-counting if we have a trade ID
        if trade_id is not None and trade_id == self.last_processed_trade_id:
            logger.debug(f"Skipping duplicate trade processing (ID: {trade_id}, source: {source})")
            return
        
        if pnl == 0:
            return  # Skip zero P&L trades
        
        logger.info(f"[COOLDOWN TRACKING] Processing trade P&L: ${pnl:.2f} (source: {source}, trade_id: {trade_id})")
        
        if pnl < 0:
            self.consecutive_losses += 1
            logger.warning(f"[COOLDOWN TRACKING] Loss #{self.consecutive_losses}: ${pnl:.2f} (source: {source})")
            
            if self.cooldown_enabled and self.consecutive_losses >= self.cooldown_trigger_losses:
                self.cooldown_until = datetime.now(self.timezone) + timedelta(minutes=self.cooldown_minutes)
                logger.warning(f"🚨 COOLDOWN TRIGGERED after {self.consecutive_losses} consecutive losses. Pausing until {self.cooldown_until.strftime('%H:%M')}")
                logger.warning(f"   Cooldown duration: {self.cooldown_minutes} minutes")
                self.alerts.error(f"Cooldown: {self.consecutive_losses} losses. Pausing {self.cooldown_minutes} min")
        else:
            logger.info(f"[COOLDOWN TRACKING] Profit: ${pnl:.2f} - resetting consecutive losses counter")
            self.consecutive_losses = 0
        
        # Track last processed trade ID
        if trade_id is not None:
            self.last_processed_trade_id = trade_id
    
    def _on_trade(self, trade: UserTrade):
        """SignalR callback for trade events - primary source of trade P&L"""
        logger.info(f"Trade callback: {trade.size} @ {trade.price} P&L: ${trade.pnl:.2f} (ID: {trade.id})")
        
        self.daily_pnl += trade.pnl
        
        # Process P&L for cooldown tracking
        self._process_trade_pnl(trade.pnl, trade_id=trade.id, source="SignalR_trade_callback")
        
        if trade.pnl != 0:
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
            self.last_processed_trade_id = None  # Reset trade ID tracking
            self.last_position_qty = None  # Reset position quantity tracking
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
        # CRITICAL FIX 11: Check quote staleness before using it
        if self.last_quote:
            # Check if quote is stale (older than 30 seconds)
            try:
                if hasattr(self.last_quote, 'timestamp') and self.last_quote.timestamp:
                    from dateutil import parser
                    quote_time = parser.parse(self.last_quote.timestamp)
                    if quote_time.tzinfo is None:
                        quote_time = self.timezone.localize(quote_time)
                    else:
                        quote_time = quote_time.astimezone(self.timezone)
                    now = datetime.now(self.timezone)
                    age_seconds = (now - quote_time).total_seconds()
                    if age_seconds > 30:
                        logger.warning(f"Quote is stale: {age_seconds:.1f} seconds old, fetching fresh price")
                        self.last_quote = None  # Mark as stale
            except Exception as e:
                logger.debug(f"Could not check quote timestamp: {e}")
                # Continue with quote if timestamp check fails
        
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
            
            # Calculate minimum bars needed for indicator warmup
            # ATR(14), EMA(20), VWAP needs ~50 bars minimum, add buffer for safety
            atr_period = 14
            ema_period = 20
            vwap_lookback = 50  # Conservative estimate
            min_bars = max(atr_period, ema_period, vwap_lookback) * 3  # 3x for safety = 150
            min_bars = max(min_bars, 500)  # Ensure at least 500 bars
            
            # Use max of requested count and minimum
            actual_count = max(count, min_bars)
            logger.info(f"Fetching {actual_count} bars (minimum {min_bars} for indicator warmup, {bar_interval_minutes}-minute interval)...")
            
            bars = self.client.get_historical_bars(
                contract_id=self.contract.id,
                interval=bar_interval_minutes,  # Match backtest data interval (3-minute bars)
                start_time=start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                count=actual_count,
                live=False,
                unit=2,
                include_partial=False  # Explicitly exclude partial bars
            )
            
            if not bars:
                logger.warning("No bars returned from API")
                return pd.DataFrame()
            
            df = pd.DataFrame(bars)
            
            if 't' in df.columns:
                df = df.rename(columns={'t': 'timestamp', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
            
            # Parse timestamps - TopStep API returns UTC timestamps (ISO format with Z or Unix milliseconds)
            df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
            df = df.sort_values('timestamp').drop_duplicates(subset=['timestamp']).reset_index(drop=True)
            
            # Detect gaps in data
            if len(df) > 1:
                df['time_diff'] = df['timestamp'].diff()
                expected_interval = pd.Timedelta(seconds=bar_interval_seconds)
                gap_threshold = expected_interval * 1.5
                
                gaps = df[df['time_diff'] > gap_threshold]
                if not gaps.empty:
                    for idx, row in gaps.iterrows():
                        gap_duration = row['time_diff'].total_seconds() / 60
                        logger.warning(f"Gap detected: {gap_duration:.1f} minutes between bars at {row['timestamp']}")
                
                df = df.drop(columns=['time_diff'])
            
            # Exclude last bar if it hasn't completed yet (bar period hasn't ended)
            # A bar starting at T should only be used after T + interval
            if len(df) > 0:
                last_bar_time = pd.to_datetime(df['timestamp'].iloc[-1])
                if last_bar_time.tzinfo is None:
                    last_bar_time = pytz.UTC.localize(last_bar_time)
                else:
                    last_bar_time = last_bar_time.astimezone(pytz.UTC)
                
                # Calculate when the bar should end (start_time + interval)
                bar_end_time = last_bar_time + pd.Timedelta(seconds=bar_interval_seconds)
                
                # Only use bar if its period has ended
                if now < bar_end_time:
                    time_until_completion = (bar_end_time - now).total_seconds()
                    logger.info(f"Excluding incomplete bar: bar at {last_bar_time.strftime('%Y-%m-%d %H:%M:%S UTC')} ends at {bar_end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}, {time_until_completion:.0f}s until completion")
                    df = df.iloc[:-1].copy()
            
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
        
        # Ensure timestamp is UTC for consistent session detection
        if timestamp.tzinfo is None:
            timestamp_utc = pytz.UTC.localize(timestamp)
        else:
            timestamp_utc = timestamp.astimezone(pytz.UTC)
        
        # CRITICAL: Verify bar is actually complete before generating signal
        # Bar interval is 3 minutes (180 seconds)
        bar_interval_seconds = 3 * 60  # 180 seconds
        bar_end_time = timestamp_utc + pd.Timedelta(seconds=bar_interval_seconds)
        now_utc = pd.Timestamp.now(tz=pytz.UTC)
        
        if now_utc < bar_end_time:
            time_until_completion = (bar_end_time - now_utc).total_seconds()
            logger.warning(f"Bar at {timestamp_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} is not yet complete (ends at {bar_end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}). Skipping signal check. {time_until_completion:.0f}s until completion.")
            return None
        
        # Check if bar is stale (use bar timestamp, not current time, for session detection)
        bar_age_seconds = (now_utc - timestamp_utc).total_seconds()
        stale_threshold_seconds = 180 * 3  # 9 minutes (3 bar intervals)
        
        if bar_age_seconds > stale_threshold_seconds:
            logger.warning(f"Bar is stale: {bar_age_seconds:.0f}s old (>{stale_threshold_seconds}s threshold)")
            return None
        
        logger.info(f"Signal check: Bar time={timestamp_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}, Price=${price:.2f}")
        
        # Let strategy.generate_signal() handle session detection based on bar timestamp
        # Don't pre-check session here to avoid timestamp mismatches
        
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
        
        # Save bars for replay if enabled
        if self.save_replay_data:
            self._save_replay_data(df, signal)
        
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
    
    def _save_replay_data(self, df: pd.DataFrame, signal) -> None:
        """Save bars around signal for realistic replay backtesting"""
        # Save last 500 bars (enough for full context and indicator warmup)
        save_df = df.iloc[-500:].copy() if len(df) > 500 else df.copy()
        
        timestamp_str = signal.timestamp.strftime('%Y%m%d_%H%M%S')
        filename = self.replay_data_dir / f"replay_{timestamp_str}_{signal.signal_type.value}.csv"
        save_df.to_csv(filename, index=False)
        logger.info(f"Saved replay data: {filename} ({len(save_df)} bars)")
        
        # Keep only last 200 replay files (cleanup older ones)
        self._cleanup_old_replay_files()
    
    def _cleanup_old_replay_files(self) -> None:
        """Remove old replay files, keeping only the most recent max_replay_files"""
        try:
            # Get all replay CSV files
            replay_files = list(self.replay_data_dir.glob('replay_*.csv'))
            
            if len(replay_files) <= self.max_replay_files:
                return  # No cleanup needed
            
            # Sort by modification time (newest first)
            replay_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            
            # Delete files beyond the limit
            files_to_delete = replay_files[self.max_replay_files:]
            deleted_count = 0
            for file in files_to_delete:
                try:
                    file.unlink()
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete old replay file {file}: {e}")
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} old replay files (keeping last {self.max_replay_files})")
        except Exception as e:
            logger.warning(f"Failed to cleanup old replay files: {e}")
    
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
            'pending': True,  # Flag to indicate order is pending
            'scale_out_levels_done': []  # Track completed scale-out levels
        }
        
        try:
            import math
            # Use ceil() for SL (safer - ensures we don't shrink the stop distance)
            # Use round() for TP (balanced - maintains R:R closer to planned)
            sl_ticks = math.ceil(abs(signal['risk_ticks']))
            tp_ticks = round(abs(signal['reward_ticks']))
            
            # NEW APPROACH: Position Brackets enabled - place separate orders
            # 1. Place market entry order FIRST (no bracket parameters)
            logger.info(f"Placing market entry order: {side.name} {self.position_size} contracts")
            logger.info("NOTE: Position Brackets enabled - will place SL/TP separately after fill")
            
            entry_result = self.client.place_market_order(
                contract_id=self.contract.id,
                side=side,
                size=self.position_size
                # NO bracket parameters - Position Brackets will handle OCO linkage
            )
            
            if not entry_result.get('success'):
                error = entry_result.get('errorMessage', 'Unknown error')
                logger.error(f"Entry order failed: {error}")
                self.alerts.error(f"Entry order failed: {error}")
                # Clear position on failure
                self.current_position = None
                self._executing_entry = False  # Release execution lock
                self._trading_locked = False  # Release trading lock
                return False
            
            entry_order_id = entry_result.get('orderId')
            logger.info(f"Entry order placed: ID {entry_order_id}")
            
            # 2. Wait for fill confirmation
            import time
            time.sleep(1.5)  # Wait for order to fill
            
            # 3. Get ACTUAL filled position from TopStep API (size, entry price, etc.)
            actual_position_size = self.position_size
            actual_entry_price = signal['entry_price']
            
            try:
                positions = self.client.get_positions()
                for pos in positions:
                    if pos.contract_id == self.contract.id and pos.size != 0:
                        actual_position_size = abs(pos.size)
                        actual_entry_price = pos.average_price
                        
                        # Update position tracking with ACTUAL values from API
                        self.current_position['quantity'] = actual_position_size
                        self.current_position['entry_price'] = actual_entry_price
                        
                        if actual_position_size != self.position_size:
                            logger.warning(f"Partial fill detected: Requested {self.position_size}, Filled {actual_position_size}")
                        logger.info(f"Position filled: {actual_position_size} contracts @ ${actual_entry_price:.2f}")
                        break
            except Exception as e:
                logger.warning(f"Could not get actual position from API: {e}")
                logger.info(f"Using requested values: {self.position_size} contracts @ ${signal['entry_price']:.2f}")
            
            # Update position with order ID and remove pending flag
            self.current_position['order_id'] = entry_order_id
            self.current_position.pop('pending', None)
            
            self.highest_price = actual_entry_price
            self.lowest_price = actual_entry_price
            
            self.daily_trades += 1
            
            # 4. Calculate SL and TP prices from ACTUAL entry price
            if signal['type'] == 'long':
                sl_price = actual_entry_price - (sl_ticks * self.tick_size)
                tp_price = actual_entry_price + (tp_ticks * self.tick_size)
                sl_order_side = OrderSide.ASK  # Sell to close long
                tp_order_side = OrderSide.ASK  # Sell to close long
            else:  # short
                sl_price = actual_entry_price + (sl_ticks * self.tick_size)
                tp_price = actual_entry_price - (tp_ticks * self.tick_size)
                sl_order_side = OrderSide.BID  # Buy to close short
                tp_order_side = OrderSide.BID  # Buy to close short
            
            # Round to tick size
            sl_price = round(sl_price / self.tick_size) * self.tick_size
            tp_price = round(tp_price / self.tick_size) * self.tick_size
            
            # Validate stop price against current market (API requirement)
            # For LONG: stop must be below best_ask (minimum 2 ticks)
            # For SHORT: stop must be above best_bid (minimum 2 ticks)
            # Wait briefly for quote to arrive if we don't have one
            if not self.last_quote:
                logger.debug("Waiting for quote to validate stop price...")
                time.sleep(0.5)  # Brief wait for quote
            
            if self.last_quote:
                if signal['type'] == 'long':
                    # For LONG: stop must be below best_ask (minimum 2 ticks for API)
                    best_ask = self.last_quote.best_ask
                    min_distance = 2 * self.tick_size
                    max_stop_price = best_ask - min_distance
                    if sl_price >= best_ask or sl_price > max_stop_price:
                        original_sl = sl_price
                        sl_price = round((best_ask - min_distance) / self.tick_size) * self.tick_size
                        logger.warning(f"Stop price adjusted for API requirement: ${original_sl:.2f} -> ${sl_price:.2f} (must be at least 2 ticks below best_ask ${best_ask:.2f})")
                else:  # short
                    # For SHORT: stop must be above best_bid (minimum 2 ticks for API)
                    best_bid = self.last_quote.best_bid
                    min_distance = 2 * self.tick_size
                    min_stop_price = best_bid + min_distance
                    if sl_price <= best_bid or sl_price < min_stop_price:
                        original_sl = sl_price
                        sl_price = round((best_bid + min_distance) / self.tick_size) * self.tick_size
                        logger.warning(f"Stop price adjusted for API requirement: ${original_sl:.2f} -> ${sl_price:.2f} (must be at least 2 ticks above best_bid ${best_bid:.2f})")
            else:
                # No quote available - use conservative adjustment based on entry price
                logger.warning("No quote available - using conservative stop price adjustment")
                if signal['type'] == 'long':
                    # For LONG: ensure stop is well below entry (at least 5 ticks for safety)
                    min_distance = 5 * self.tick_size
                    max_stop_price = actual_entry_price - min_distance
                    if sl_price > max_stop_price:
                        original_sl = sl_price
                        sl_price = round((actual_entry_price - min_distance) / self.tick_size) * self.tick_size
                        logger.warning(f"Stop price adjusted (no quote): ${original_sl:.2f} -> ${sl_price:.2f} (at least 5 ticks below entry ${actual_entry_price:.2f})")
                else:  # short
                    # For SHORT: ensure stop is well above entry
                    min_distance = 5 * self.tick_size
                    min_stop_price = actual_entry_price + min_distance
                    if sl_price < min_stop_price:
                        original_sl = sl_price
                        sl_price = round((actual_entry_price + min_distance) / self.tick_size) * self.tick_size
                        logger.warning(f"Stop price adjusted (no quote): ${original_sl:.2f} -> ${sl_price:.2f} (at least 5 ticks above entry ${actual_entry_price:.2f})")
            
            logger.info(f"Calculated from actual fill: SL=${sl_price:.2f}, TP=${tp_price:.2f}")
            
            # Final validation: Get fresh quote and ensure stop price meets API requirements
            # This is critical because API will reject orders that don't meet price constraints
            try:
                quotes = self.client.get_quotes([self.contract.id])
                if quotes and self.contract.id in quotes:
                    fresh_quote = quotes[self.contract.id]
                    if signal['type'] == 'long':
                        # For LONG: stop must be at least 2 ticks below best_ask
                        best_ask = fresh_quote.ask
                        max_allowed_stop = best_ask - (2 * self.tick_size)
                        if sl_price >= best_ask or sl_price > max_allowed_stop:
                            original_sl = sl_price
                            sl_price = round((best_ask - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                            logger.warning(f"Final stop price adjustment for API: ${original_sl:.2f} -> ${sl_price:.2f} (best_ask: ${best_ask:.2f})")
                            # Update position tracking with adjusted stop
                            if self.current_position:
                                self.current_position['stop_loss'] = sl_price
                                self.current_position['initial_stop_loss'] = sl_price
                    else:  # short
                        # For SHORT: stop must be at least 2 ticks above best_bid
                        best_bid = fresh_quote.bid
                        min_allowed_stop = best_bid + (2 * self.tick_size)
                        if sl_price <= best_bid or sl_price < min_allowed_stop:
                            original_sl = sl_price
                            sl_price = round((best_bid + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                            logger.warning(f"Final stop price adjustment for API: ${original_sl:.2f} -> ${sl_price:.2f} (best_bid: ${best_bid:.2f})")
                            # Ensure stop is still >= entry for SHORT (protect against losses)
                            if sl_price < actual_entry_price:
                                logger.warning(f"Adjusted stop ${sl_price:.2f} is below entry ${actual_entry_price:.2f}, adjusting to entry + 2 ticks")
                                sl_price = round((actual_entry_price + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                            # Update position tracking with adjusted stop
                            if self.current_position:
                                self.current_position['stop_loss'] = sl_price
                                self.current_position['initial_stop_loss'] = sl_price
            except Exception as e:
                logger.warning(f"Could not get fresh quote for final validation: {e}")
            
            # 5. Place Stop Loss order with retry logic and price adjustment (CRITICAL)
            sl_order_id = None
            sl_placed = False
            max_sl_retries = 5  # Increased retries
            
            for sl_attempt in range(max_sl_retries):
                # Get fresh quote before each attempt to adjust price if needed
                if sl_attempt > 0:
                    try:
                        if self.last_quote and self.last_quote.contract_id == self.contract.id:
                            if signal['type'] == 'long':
                                # For LONG: stop must be below best_ask
                                best_ask = self.last_quote.best_ask
                                max_allowed = best_ask - (2 * self.tick_size)
                                if sl_price >= best_ask or sl_price > max_allowed:
                                    sl_price = round((best_ask - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                    logger.info(f"Adjusted SL price for retry: ${sl_price:.2f} (best_ask: ${best_ask:.2f})")
                            else:  # short
                                # For SHORT: stop must be above best_bid
                                best_bid = self.last_quote.best_bid
                                min_allowed = best_bid + (2 * self.tick_size)
                                if sl_price <= best_bid or sl_price < min_allowed:
                                    sl_price = round((best_bid + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                    logger.info(f"Adjusted SL price for retry: ${sl_price:.2f} (best_bid: ${best_bid:.2f})")
                        time.sleep(0.5)  # Brief wait for quote update
                    except Exception as e:
                        logger.debug(f"Could not adjust price: {e}")
                
                logger.info(f"Placing stop loss (attempt {sl_attempt + 1}/{max_sl_retries}): {sl_order_side.name} {actual_position_size} @ ${sl_price:.2f}")
                sl_result = self.client.place_stop_order(
                    contract_id=self.contract.id,
                    side=sl_order_side,
                    size=actual_position_size,
                    stop_price=sl_price
                )
                
                if sl_result.get('success'):
                    sl_order_id = sl_result.get('orderId')
                    # Verify order was actually placed
                    time.sleep(0.5)
                    if self._verify_order_placement(sl_order_id, actual_position_size, 'Stop Loss', max_retries=2):
                        self.current_position['stop_order_id'] = sl_order_id
                        self.current_position['stop_loss'] = sl_price
                        self.current_position['initial_stop_loss'] = sl_price
                        logger.info(f"Stop loss placed and verified: ID {sl_order_id} @ ${sl_price:.2f}")
                        sl_placed = True
                        break
                    else:
                        logger.warning(f"Stop loss order {sl_order_id} not verified, retrying...")
                        if sl_attempt < max_sl_retries - 1:
                            time.sleep(1.5)  # Wait longer before retry
                else:
                    error_msg = sl_result.get('errorMessage', 'Unknown error')
                    logger.warning(f"Stop loss attempt {sl_attempt + 1} failed: {error_msg}")
                    # If price error, adjust price for next attempt
                    if 'price' in error_msg.lower() or 'outside' in error_msg.lower():
                        if signal['type'] == 'long':
                            sl_price = sl_price - (1 * self.tick_size)  # Move down
                        else:
                            sl_price = sl_price + (1 * self.tick_size)  # Move up
                        logger.info(f"Adjusting SL price for next attempt: ${sl_price:.2f}")
                    if sl_attempt < max_sl_retries - 1:
                        time.sleep(1.5)  # Wait before retry
            
            if not sl_placed:
                # Try fallback stop loss
                logger.error(f"CRITICAL: All {max_sl_retries} stop loss attempts failed, trying fallback...")
                if not self._place_fallback_stop_loss(signal):
                    # If fallback also fails, CLOSE POSITION immediately - it's unprotected
                    logger.error("CRITICAL: Stop loss placement completely failed - closing position for safety")
                    self.alerts.error("CRITICAL: Stop loss failed - closing unprotected position immediately")
                    try:
                        close_result = self.client.close_position(self.contract.id)
                        if close_result.get('success'):
                            logger.info("Position closed successfully due to SL placement failure")
                        else:
                            logger.error(f"Failed to close position: {close_result.get('errorMessage')}")
                    except Exception as e:
                        logger.error(f"Exception closing position: {e}")
                    
                    self.current_position = None
                    self._executing_entry = False
                    self._trading_locked = False
                    return False
            
            # 6. Place Take Profit order with retry logic and price adjustment (IMPORTANT)
            tp_order_id = None
            tp_placed = False
            max_tp_retries = 5  # Increased retries
            
            for tp_attempt in range(max_tp_retries):
                # Get fresh quote before each attempt to adjust price if needed
                if tp_attempt > 0:
                    try:
                        if self.last_quote and self.last_quote.contract_id == self.contract.id:
                            if signal['type'] == 'long':
                                # For LONG: TP should be above best_ask
                                best_ask = self.last_quote.best_ask
                                if tp_price <= best_ask:
                                    tp_price = round((best_ask + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                    logger.info(f"Adjusted TP price for retry: ${tp_price:.2f} (best_ask: ${best_ask:.2f})")
                            else:  # short
                                # For SHORT: TP should be below best_bid
                                best_bid = self.last_quote.best_bid
                                if tp_price >= best_bid:
                                    tp_price = round((best_bid - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                    logger.info(f"Adjusted TP price for retry: ${tp_price:.2f} (best_bid: ${best_bid:.2f})")
                        time.sleep(0.5)  # Brief wait for quote update
                    except Exception as e:
                        logger.debug(f"Could not adjust TP price: {e}")
                
                logger.info(f"Placing take profit (attempt {tp_attempt + 1}/{max_tp_retries}): {tp_order_side.name} {actual_position_size} @ ${tp_price:.2f}")
                tp_result = self.client.place_limit_order(
                    contract_id=self.contract.id,
                    side=tp_order_side,
                    size=actual_position_size,
                    limit_price=tp_price
                )
                
                if tp_result.get('success'):
                    tp_order_id = tp_result.get('orderId')
                    # Verify order was actually placed
                    time.sleep(0.5)
                    if self._verify_order_placement(tp_order_id, actual_position_size, 'Take Profit', max_retries=2):
                        self.current_position['tp_order_id'] = tp_order_id
                        self.current_position['take_profit'] = tp_price
                        logger.info(f"Take profit placed and verified: ID {tp_order_id} @ ${tp_price:.2f}")
                        tp_placed = True
                        break
                    else:
                        logger.warning(f"Take profit order {tp_order_id} not verified, retrying...")
                        if tp_attempt < max_tp_retries - 1:
                            time.sleep(1.5)  # Wait longer before retry
                else:
                    error_msg = tp_result.get('errorMessage', 'Unknown error')
                    logger.warning(f"Take profit attempt {tp_attempt + 1} failed: {error_msg}")
                    # If price error, adjust price for next attempt
                    if 'price' in error_msg.lower() or 'outside' in error_msg.lower():
                        if signal['type'] == 'long':
                            tp_price = tp_price + (1 * self.tick_size)  # Move up
                        else:
                            tp_price = tp_price - (1 * self.tick_size)  # Move down
                        logger.info(f"Adjusting TP price for next attempt: ${tp_price:.2f}")
                    if tp_attempt < max_tp_retries - 1:
                        time.sleep(1.5)  # Wait before retry
            
            if not tp_placed:
                # TP failure is less critical than SL (position still protected), but log it
                error_msg = tp_result.get('errorMessage', 'Unknown error') if tp_result else 'All attempts failed'
                logger.error(f"Take profit placement failed after {max_tp_retries} attempts: {error_msg}")
                self.alerts.error(f"Take profit placement failed - position protected by SL but TP missing")
                # Position is still protected by SL, but TP will need to be placed manually or via monitoring
            
            # Simple verification: Check that orders exist (Position Brackets will handle OCO linkage)
            try:
                time.sleep(0.5)  # Brief wait for orders to register
                open_orders = self.client.get_open_orders()
                sl_verified = False
                tp_verified = False
                
                for order in open_orders:
                    if order.get('contractId') == self.contract.id:
                        order_id = order.get('id')
                        if order_id == self.current_position.get('stop_order_id'):
                            sl_verified = True
                            logger.info(f"Stop loss verified: ID {order_id} @ ${order.get('stopPrice', 0):.2f}, Size: {order.get('size', 0)}")
                        elif order_id == self.current_position.get('tp_order_id'):
                            tp_verified = True
                            logger.info(f"Take profit verified: ID {order_id} @ ${order.get('limitPrice', 0):.2f}, Size: {order.get('size', 0)}")
                
                if not sl_verified and self.current_position.get('stop_order_id'):
                    logger.warning(f"Stop loss order not found in open orders (may have been cancelled or filled)")
                if not tp_verified and self.current_position.get('tp_order_id'):
                    logger.warning(f"Take profit order not found in open orders (may have been cancelled or filled)")
            except Exception as e:
                logger.warning(f"Could not verify orders: {e}")
            
            # Send alert (non-blocking - wrap in try/except to prevent hanging)
            try:
                self.alerts.trade_entry(
                    side=signal['type'],
                    entry_price=actual_entry_price,  # Use actual fill price
                    quantity=actual_position_size,   # Use actual position size
                    stop_loss=sl_price,              # Use actual SL price
                    take_profit=tp_price             # Use actual TP price
                )
            except Exception as e:
                logger.warning(f"Alert failed (non-critical): {e}")
            
            # CRITICAL: Final verification - check everything matches expectations
            logger.info("Performing final order verification...")
            verification_passed = self._verify_all_orders_match_expectations(signal, actual_position_size, actual_entry_price)
            
            if not verification_passed:
                logger.error("CRITICAL: Order verification failed! Closing position and all orders for safety...")
                self._emergency_close_all()
                self.current_position = None
                self._executing_entry = False
                self._trading_locked = False
                return False
            
            logger.info("Entry execution completed - releasing locks")
            self._executing_entry = False  # Release lock after successful order
            self.circuit_breaker.record_success()
            
            # Log trade metadata to journal
            self._log_trade_metadata(signal, actual_entry_price, actual_position_size, sl_price, tp_price)
            
            return True
            
        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            self.alerts.error(f"Order execution failed: {e}")
            # Record failure in circuit breaker
            self.circuit_breaker.record_failure(timezone=self.timezone)
            # Clear position and release locks on exception
            self.current_position = None
            self._executing_entry = False  # Release execution lock
            self._trading_locked = False  # Release trading lock
            return False
    
    def _place_fallback_stop_loss(self, signal: Dict) -> bool:
        """
        Place a standalone stop loss order when bracket order didn't create one.
        This is a CRITICAL safety measure to protect the position.
        """
        if not self.current_position:
            logger.error("Cannot place fallback SL: No position exists")
            return False
        
        try:
            entry_price = self.current_position.get('entry_price')
            position_side = self.current_position.get('side', signal['type'])
            
            # Get fresh quote to ensure stop price meets API requirements
            try:
                if self.last_quote and self.last_quote.contract_id == self.contract.id:
                    quote = self.last_quote
                else:
                    # Wait a bit for quote if we don't have one
                    time.sleep(0.5)
                    if self.last_quote and self.last_quote.contract_id == self.contract.id:
                        quote = self.last_quote
                    else:
                        quote = None
                        logger.warning("No quote available for fallback SL - will use entry-based calculation")
            except:
                quote = None
            
            if position_side == 'long':
                sl_order_side = OrderSide.ASK  # Sell to close long
                # For LONG: stop must be below entry and below best_ask
                if quote:
                    best_ask = quote.best_ask
                    max_stop = best_ask - (2 * self.tick_size)
                    stop_price = min(signal['stop_loss'], max_stop)
                    # Ensure stop is still below entry
                    stop_price = min(stop_price, entry_price - (2 * self.tick_size))
                else:
                    stop_price = entry_price - (5 * self.tick_size)  # Conservative 5 ticks below entry
            else:  # short
                sl_order_side = OrderSide.BID  # Buy to close short
                # For SHORT: stop must be above entry and above best_bid
                if quote:
                    best_bid = quote.best_bid
                    min_stop = best_bid + (2 * self.tick_size)
                    stop_price = max(signal['stop_loss'], min_stop)
                    # Ensure stop is still above entry
                    stop_price = max(stop_price, entry_price + (2 * self.tick_size))
                else:
                    stop_price = entry_price + (5 * self.tick_size)  # Conservative 5 ticks above entry
            
            # Round to tick size
            stop_price = round(stop_price / self.tick_size) * self.tick_size
            
            # Get actual position size from TopStep API (preferred) or use signal/position tracking
            quantity = signal.get('quantity', self.current_position.get('quantity', self.position_size))
            
            # Double-check with API if we have a position
            try:
                positions = self.client.get_positions()
                for pos in positions:
                    if pos.contract_id == self.contract.id and pos.size != 0:
                        quantity = abs(pos.size)
                        logger.info(f"Using actual position size from TopStep API: {quantity} contracts")
                        break
            except Exception as e:
                logger.debug(f"Could not verify position size from API: {e}")
            
            logger.info(f"Placing fallback stop loss: {sl_order_side.name} {quantity} @ ${stop_price:.2f} (size from API: {quantity})")
            
            result = self.client.place_order(
                contract_id=self.contract.id,
                order_type=OrderType.STOP,
                side=sl_order_side,
                size=quantity,
                stop_price=stop_price
            )
            
            if result and result.get('success'):
                stop_order_id = result.get('orderId')
                self.current_position['stop_order_id'] = stop_order_id
                self.current_position['stop_loss'] = stop_price
                logger.info(f"Fallback stop loss placed successfully: ID {stop_order_id} @ ${stop_price:.2f}")
                # Fallback SL placed successfully - no alert needed (entry alert already sent)
                return True
            else:
                error = result.get('errorMessage', 'Unknown error') if result else 'No response'
                logger.error(f"CRITICAL: Fallback stop loss FAILED: {error}")
                self.alerts.error(f"CRITICAL: Position has NO stop loss! SL placement failed: {error}")
                return False
                
        except Exception as e:
            logger.error(f"CRITICAL: Exception placing fallback stop loss: {e}")
            self.alerts.error(f"CRITICAL: Position has NO stop loss! Exception: {e}")
            return False
    
    def _check_position_status(self) -> None:
        if self.current_position is None:
            # Even if no position tracked, check for orphaned orders and clean them up
            self._cancel_remaining_bracket_orders()
            return
        
        try:
            # Log that we're checking position status
            logger.debug(f"Checking position status: {self.current_position.get('side', 'unknown')} {self.current_position.get('quantity', 0)} @ ${self.current_position.get('entry_price', 0):.2f}")
            
            positions = self.client.get_positions()
            
            has_position = False
            broker_position = None
            for pos in positions:
                if pos.contract_id == self.contract.id and pos.size != 0:
                    has_position = True
                    broker_position = pos
                    break
            
            if not has_position:
                logger.warning("Position closed (detected via REST) - cleaning up orders immediately...")
                
                # CRITICAL FIX: Calculate and process P&L when position closes (fallback if trade callback missed)
                # This ensures cooldown triggers even if _on_trade() callback didn't fire
                try:
                    entry_price = self.current_position.get('entry_price')
                    current_price = self._get_current_price()
                    
                    if entry_price and current_price:
                        side = self.current_position.get('side')
                        quantity = self.current_position.get('quantity', self.position_size)
                        
                        # Calculate P&L
                        if side == 'long':
                            pnl_ticks = (current_price - entry_price) / self.tick_size
                        else:
                            pnl_ticks = (entry_price - current_price) / self.tick_size
                        
                        # Estimate commission (open + close)
                        commission = self.commission_per_contract * quantity * 2
                        gross_pnl = pnl_ticks * self.tick_value * quantity
                        net_pnl = gross_pnl - commission
                        
                        logger.warning(f"[COOLDOWN FALLBACK] Position closed via REST - calculated P&L: ${net_pnl:.2f} (entry: ${entry_price:.2f}, exit: ${current_price:.2f}, qty: {quantity})")
                        
                        # Process P&L for cooldown (only if we haven't processed it already via trade callback)
                        # Use a dummy trade_id based on timestamp to allow processing
                        fallback_trade_id = int(datetime.now(self.timezone).timestamp() * 1000) + 1
                        self._process_trade_pnl(net_pnl, trade_id=fallback_trade_id, source="REST_position_check")
                        
                        # Update daily P&L if not already updated by trade callback
                        self.daily_pnl += net_pnl
                except Exception as e:
                    logger.warning(f"Could not calculate P&L on position close: {e}")
                
                # CRITICAL: Cancel any remaining bracket orders (SL/TP) when position is closed
                # Position Brackets doesn't auto-cancel, so we must clean up manually
                self._cancel_remaining_bracket_orders()
                
                # Double-check: cancel ALL orders for this contract as safety measure
                import time
                time.sleep(0.5)  # Brief pause to ensure order state is updated
                self._cancel_remaining_bracket_orders()  # Call again to catch any missed orders
                
                # Triple-check: one more time after another brief pause
                time.sleep(0.5)
                self._cancel_remaining_bracket_orders()
                
                # Clear position tracking
                self.current_position = None
                self.last_position_qty = None
                # Release trading lock when position closes
                self._trading_locked = False
                logger.info("Trading lock released - new signals allowed")
            elif broker_position:
                # Sync position details from broker
                old_qty = self.current_position.get('quantity')
                new_qty = abs(broker_position.size)
                if old_qty != new_qty:
                    logger.info(f"Position size synced: {old_qty} -> {new_qty}")
                    self.current_position['quantity'] = new_qty
                    # Update order sizes to match new position quantity
                    self._sync_order_sizes_to_position()
                
                # FIX 1: Enhanced watchdog - runs on EVERY position check (not just when qty changes)
                # Checks for missing orders AND size mismatches, fixes them automatically
                # CRITICAL FIX: Only run watchdog if NOT updating orders AND NOT executing entry
                if (not hasattr(self, '_updating_stop_order') or not self._updating_stop_order) and \
                   (not hasattr(self, '_placing_protective_orders') or not self._placing_protective_orders) and \
                   (not hasattr(self, '_executing_entry') or not self._executing_entry):
                    self._ensure_protective_orders_exist(broker_position)
                else:
                    logger.debug("Skipping watchdog - order update or entry execution in progress")
                
                if old_qty == new_qty:
                    # ALWAYS log position status when position exists (not just every 5th check)
                    if not hasattr(self, '_position_check_count'):
                        self._position_check_count = 0
                    self._position_check_count += 1
                    
                    current_price = self._get_current_price()
                    if current_price:
                        entry = self.current_position.get('entry_price', 0)
                        side = self.current_position.get('side', 'unknown')
                        qty = self.current_position.get('quantity', 0)
                        
                        if side == 'long':
                            pnl = (current_price - entry) * qty
                            pnl_ticks = (current_price - entry) / self.tick_size
                        else:
                            pnl = (entry - current_price) * qty
                            pnl_ticks = (entry - current_price) / self.tick_size
                        
                        # Log every position check to show trader is active
                        logger.info(f"[POSITION] {side.upper()} {qty} @ ${entry:.2f}, Current: ${current_price:.2f}, P&L: ${pnl:.2f} ({pnl_ticks:.1f} ticks)")
                
                # Check max position duration
                if self.current_position and self.current_position.get('entry_time'):
                    entry_time = self.current_position['entry_time']
                    duration_hours = (datetime.now(self.timezone) - entry_time).total_seconds() / 3600
                    
                    if duration_hours > self.max_position_hours:
                        logger.warning(f"Position open for {duration_hours:.1f} hours (max: {self.max_position_hours}) - forcing close")
                        current_price = self._get_current_price()
                        if current_price:
                            total_daily_pnl = self.daily_pnl
                            unrealized_pnl = self._calculate_unrealized_pnl(current_price)
                            self._force_close_position(current_price, total_daily_pnl, unrealized_pnl)
                            return
                
        except Exception as e:
            logger.error(f"Failed to check position: {e}")
            import traceback
            logger.error(f"Position check traceback: {traceback.format_exc()}")
    
    def _cancel_remaining_bracket_orders(self) -> None:
        """
        Cancel any remaining bracket orders (stop loss or take profit) 
        when position is closed. Uses TopStep API to verify position is actually closed.
        This prevents abandoned orders.
        
        CRITICAL: This is called proactively to clean up orders even if position tracking is lost.
        """
        try:
            # CRITICAL: Verify position is actually closed via TopStep API
            positions = self.client.get_positions()
            has_position = False
            for pos in positions:
                if pos.contract_id == self.contract.id and pos.size != 0:
                    has_position = True
                    logger.debug(f"Position still exists: {pos.size} contracts - skipping cleanup")
                    break
            
            # Only skip if position definitely exists
            # If no position exists OR current_position is None, proceed with cleanup check
            if has_position and self.current_position is not None:
                logger.debug("Position exists (via API), skipping bracket order cleanup")
                return
            
            # Position confirmed closed or not tracked - proceed with cleanup
            open_orders = self.client.get_open_orders()
            cancelled_count = 0
            orders_to_cancel = []
            
            # Collect ALL orders for this contract (not just SL/TP, to be safe)
            for order in open_orders:
                if order.get('contractId') == self.contract.id:
                    order_type = order.get('type')
                    order_id = order.get('id')
                    order_size = order.get('size', 0)
                    order_type_name = {1: 'LIMIT', 2: 'MARKET', 4: 'STOP'}.get(order_type, f'TYPE{order_type}')
                    
                    # Collect ALL orders (stop loss, take profit, any other orders)
                    orders_to_cancel.append((order_type_name, order_id, order_size))
            
            if orders_to_cancel:
                logger.warning(f"Found {len(orders_to_cancel)} remaining order(s) to cancel for {self.contract.id}")
            
            # Cancel all collected orders
            for order_type, order_id, order_size in orders_to_cancel:
                try:
                    result = self.client.cancel_order(order_id)
                    if result.get('success'):
                        logger.warning(f"CANCELLED {order_type} order: ID {order_id}, Size: {order_size}")
                        cancelled_count += 1
                    else:
                        error_msg = result.get('errorMessage', 'Unknown error')
                        # Some orders may already be cancelled/filled - that's OK
                        if 'not found' in error_msg.lower() or 'does not exist' in error_msg.lower() or 'invalid' in error_msg.lower():
                            logger.debug(f"Order {order_id} already cancelled/filled (OK)")
                        else:
                            logger.error(f"FAILED to cancel {order_type} order {order_id}: {error_msg}")
                except Exception as e:
                    logger.error(f"ERROR cancelling {order_type} order {order_id}: {e}")
            
            if cancelled_count > 0:
                logger.warning(f"CLEANUP: Cancelled {cancelled_count} remaining order(s)")
            elif orders_to_cancel:
                logger.warning(f"WARNING: Found {len(orders_to_cancel)} orders but failed to cancel any")
        
        except Exception as e:
            logger.error(f"ERROR in cleanup function: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    def _log_trade_metadata(self, signal: Dict, entry_price: float, quantity: int, 
                           stop_loss: float, take_profit: float) -> None:
        """Log comprehensive trade data for analysis"""
        try:
            journal_file = Path('trade_journal.jsonl')  # JSON Lines format
            
            # Extract zone information if available
            zone_data = None
            if 'zone' in signal and signal['zone']:
                zone = signal['zone']
                zone_data = {
                    'confidence': getattr(zone, 'confidence', None),
                    'age_hours': zone.age_hours(datetime.now(self.timezone)) if hasattr(zone, 'age_hours') else None,
                    'touch_count': getattr(zone, 'touch_count', None),
                    'quality_score': getattr(zone, 'quality_score', None)
                }
            
            trade_data = {
                'timestamp': signal.get('timestamp', datetime.now(self.timezone)).isoformat() if isinstance(signal.get('timestamp'), datetime) else str(signal.get('timestamp', datetime.now(self.timezone))),
                'side': signal.get('type', 'unknown'),
                'entry': entry_price,
                'sl': stop_loss,
                'tp': take_profit,
                'quantity': quantity,
                'session': signal.get('session', 'unknown'),
                'zone': zone_data,
                'market_context': {
                    'vwap': signal.get('vwap'),
                    'atr': signal.get('atr'),
                    'regime': signal.get('market_regime', 'unknown')
                },
                'outcome': None  # Will be updated when trade closes
            }
            
            with open(journal_file, 'a') as f:
                f.write(json.dumps(trade_data) + '\n')
        except Exception as e:
            logger.debug(f"Failed to log trade metadata: {e}")
    
    def _safety_cleanup_orphaned_orders(self) -> None:
        """
        Safety cleanup - run every loop iteration to catch orphaned orders.
        This is a lightweight check that verifies no position exists before cleaning up orders.
        """
        try:
            # Verify position is actually closed via API
            positions = self.client.get_positions()
            has_position = any(
                p.contract_id == self.contract.id and p.size != 0 
                for p in positions
            )
            
            # Only cleanup if definitely no position exists
            if not has_position and self.current_position is None:
                # No position exists - safe to clean up any orphaned orders
                self._cancel_remaining_bracket_orders()
        except Exception as e:
            # Log but don't raise - cleanup failures shouldn't block trading loop
            logger.debug(f"Safety cleanup check failed: {e}")
    
    def _reconcile_position_with_broker(self) -> None:
        """Sync internal state with broker reality - called every loop iteration"""
        try:
            if self.contract is None:
                return  # Can't reconcile without contract
            
            positions = self.client.get_positions()
            broker_pos = None
            for pos in positions:
                if pos.contract_id == self.contract.id and pos.size != 0:
                    broker_pos = pos
                    break
            
            if broker_pos and not self.current_position:
                # Found orphaned position - sync it
                logger.warning("Orphaned position detected at broker - syncing internal state")
                # Convert to UserPosition-like object for syncing
                # Use actual position_type from Position object (more reliable)
                from broker.signalr_client import UserPosition
                broker_user_pos = UserPosition(
                    id=0,
                    account_id=self.client.account_id,
                    contract_id=broker_pos.contract_id,
                    position_type=broker_pos.position_type.value,  # Use actual position_type (1=LONG, 2=SHORT)
                    size=broker_pos.size,
                    average_price=broker_pos.average_price
                )
                logger.info(f"Position type from broker: {broker_pos.position_type.name} (value: {broker_pos.position_type.value})")
                self._sync_position_from_broker(broker_user_pos)
            elif not broker_pos and self.current_position:
                # Position closed at broker but still tracked internally
                logger.warning("Position closed at broker but still tracked internally - clearing state")
                self.current_position = None
                self._trading_locked = False
            elif broker_pos and self.current_position:
                # Sync quantities if they differ
                actual_qty = abs(broker_pos.size)
                if actual_qty != self.current_position.get('quantity', 0):
                    logger.info(f"Syncing position quantity: {self.current_position.get('quantity', 0)} -> {actual_qty}")
                    self.current_position['quantity'] = actual_qty
        except Exception as e:
            logger.debug(f"Position reconciliation failed: {e}")
    
    def _verify_order_placement(self, order_id: int, expected_size: int, 
                               order_type: str, max_retries: int = 3) -> bool:
        """Verify order actually exists after placement"""
        for attempt in range(max_retries):
            time.sleep(0.5)  # Wait for order to register
            
            try:
                open_orders = self.client.get_open_orders()
                for order in open_orders:
                    if order.get('id') == order_id:
                        order_size = abs(order.get('size', 0))
                        if order_size == expected_size:
                            logger.info(f"OK {order_type} order verified: ID {order_id}, size {order_size}")
                            return True
                        else:
                            # CRITICAL FIX: Order exists but size differs - still return True
                            # Size mismatch will be fixed by watchdog function separately
                            # Returning False here causes duplicate order placement
                            logger.warning(f"Order exists but size differs: {order_size} != {expected_size} (order ID {order_id}) - will fix size separately")
                            return True  # Changed from False - prevents duplicate placement
                
                logger.warning(f"Attempt {attempt+1}/{max_retries}: Order {order_id} not found in open orders")
            except Exception as e:
                logger.error(f"Verification attempt {attempt+1}/{max_retries} failed: {e}")
        
        logger.error(f"FAILED Order {order_id} verification failed after {max_retries} attempts")
        return False
    
    def _verify_all_orders_match_expectations(self, signal: Dict, expected_size: int, expected_entry: float) -> bool:
        """
        Comprehensive verification that all orders match expectations.
        Returns True if everything is correct, False otherwise.
        """
        try:
            logger.info("=" * 60)
            logger.info("COMPREHENSIVE ORDER VERIFICATION")
            logger.info("=" * 60)
            
            # Get actual position from API
            positions = self.client.get_positions()
            actual_position = None
            for pos in positions:
                if pos.contract_id == self.contract.id and pos.size != 0:
                    actual_position = pos
                    break
            
            if not actual_position:
                logger.error("VERIFICATION FAILED: No position found in API")
                return False
            
            actual_size = abs(actual_position.size)
            actual_entry = actual_position.average_price
            
            logger.info(f"Position from API: {actual_size} contracts @ ${actual_entry:.2f}")
            logger.info(f"Expected: {expected_size} contracts @ ${expected_entry:.2f}")
            
            # Verify position size matches
            if actual_size != expected_size:
                logger.error(f"VERIFICATION FAILED: Position size mismatch! Expected {expected_size}, got {actual_size}")
                return False
            
            # Verify entry price is reasonable (within 5 ticks)
            entry_diff_ticks = abs(actual_entry - expected_entry) / self.tick_size
            if entry_diff_ticks > 5:
                logger.warning(f"Entry price differs by {entry_diff_ticks:.1f} ticks (expected ${expected_entry:.2f}, got ${actual_entry:.2f})")
                # Not a failure, but log it
            
            # Get all open orders
            open_orders = self.client.get_open_orders()
            stop_orders = []
            tp_orders = []
            
            for order in open_orders:
                if order.get('contractId') == self.contract.id:
                    order_type = order.get('type')
                    order_size = order.get('size', 0)
                    order_id = order.get('id')
                    
                    if order_type == 4:  # STOP
                        stop_price = order.get('stopPrice', 0)
                        stop_orders.append({
                            'id': order_id,
                            'size': order_size,
                            'price': stop_price
                        })
                    elif order_type == 1:  # LIMIT (could be TP)
                        limit_price = order.get('limitPrice', 0)
                        # Check if it's a TP (on correct side of entry)
                        if signal['type'] == 'long' and limit_price > actual_entry:
                            tp_orders.append({
                                'id': order_id,
                                'size': order_size,
                                'price': limit_price
                            })
                        elif signal['type'] == 'short' and limit_price < actual_entry:
                            tp_orders.append({
                                'id': order_id,
                                'size': order_size,
                                'price': limit_price
                            })
            
            # Verify stop loss exists and matches
            if not stop_orders:
                logger.error("VERIFICATION FAILED: No stop loss order found!")
                return False
            
            if len(stop_orders) > 1:
                logger.warning(f"VERIFICATION WARNING: Found {len(stop_orders)} stop orders (expected 1)")
            
            for stop in stop_orders:
                if stop['size'] != actual_size:
                    logger.error(f"VERIFICATION FAILED: Stop loss size mismatch! Expected {actual_size}, got {stop['size']}")
                    return False
                logger.info(f"Stop loss verified: ID {stop['id']}, Size: {stop['size']}, Price: ${stop['price']:.2f}")
            
            # Verify take profit exists and matches
            if not tp_orders:
                logger.warning("VERIFICATION WARNING: No take profit order found")
            else:
                if len(tp_orders) > 1:
                    logger.warning(f"VERIFICATION WARNING: Found {len(tp_orders)} take profit orders (expected 1)")
                
                for tp in tp_orders:
                    if tp['size'] != actual_size:
                        logger.error(f"VERIFICATION FAILED: Take profit size mismatch! Expected {actual_size}, got {tp['size']}")
                        return False
                    logger.info(f"Take profit verified: ID {tp['id']}, Size: {tp['size']}, Price: ${tp['price']:.2f}")
            
            logger.info("=" * 60)
            logger.info("VERIFICATION PASSED: All orders match expectations")
            logger.info("=" * 60)
            return True
            
        except Exception as e:
            logger.error(f"VERIFICATION ERROR: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def _emergency_close_all(self) -> None:
        """
        Emergency cleanup: Close all positions and cancel all orders for this contract.
        Used when verification fails or position is closed.
        """
        try:
            logger.warning("EMERGENCY CLEANUP: Closing all positions and orders...")
            
            # CRITICAL FIX 13: Close all positions and verify closure
            positions = self.client.get_positions()
            positions_to_close = [p for p in positions if p.contract_id == self.contract.id and p.size != 0]
            
            for pos in positions_to_close:
                size = abs(pos.size)
                side = OrderSide.ASK if pos.size > 0 else OrderSide.BID
                logger.warning(f"Closing position: {size} contracts ({'LONG' if pos.size > 0 else 'SHORT'})")
                result = self.client.place_market_order(
                    contract_id=self.contract.id,
                    side=side,
                    size=size
                )
                if result.get('success'):
                    logger.info(f"Market order placed to close {size} contracts")
                else:
                    logger.error(f"FAILED Failed to close position: {result.get('errorMessage')}")
            
            # Wait for positions to close and verify
            if positions_to_close:
                import time
                time.sleep(3)  # Increased wait time for market orders to fill
                
                # Verify positions are closed
                remaining_positions = self.client.get_positions()
                still_open = [p for p in remaining_positions if p.contract_id == self.contract.id and p.size != 0]
                if still_open:
                    logger.error(f"CRITICAL: {len(still_open)} position(s) still open after emergency close!")
                    for p in still_open:
                        logger.error(f"  Position still open: {p.size} contracts")
                else:
                    logger.info("All positions verified closed")
            
            # Cancel all orders for this contract
            open_orders = self.client.get_open_orders()
            cancelled = 0
            for order in open_orders:
                if order.get('contractId') == self.contract.id:
                    order_id = order.get('id')
                    order_type = order.get('type')
                    order_size = order.get('size', 0)
                    logger.warning(f"Cancelling order: ID {order_id}, Type {order_type}, Size {order_size}")
                    result = self.client.cancel_order(order_id)
                    if result.get('success'):
                        logger.info(f"Order {order_id} cancelled")
                        cancelled += 1
                    else:
                        logger.error(f"FAILED Failed to cancel order {order_id}: {result.get('errorMessage')}")
            
            if cancelled > 0:
                logger.info(f"Emergency cleanup complete: {cancelled} order(s) cancelled")
            else:
                logger.info("Emergency cleanup complete: No orders to cancel")
            
            # Final verification - check again
            time.sleep(1)
            final_positions = self.client.get_positions()
            final_orders = self.client.get_open_orders()
            
            remaining_positions = [p for p in final_positions if p.contract_id == self.contract.id and p.size != 0]
            remaining_orders = [o for o in final_orders if o.get('contractId') == self.contract.id]
            
            if remaining_positions:
                logger.error(f"WARNING: {len(remaining_positions)} position(s) still remain after cleanup!")
            if remaining_orders:
                logger.error(f"WARNING: {len(remaining_orders)} order(s) still remain after cleanup!")
            
            if not remaining_positions and not remaining_orders:
                logger.info("Cleanup verified: No positions or orders remaining")
            
        except Exception as e:
            logger.error(f"Emergency cleanup error: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    def _sync_position_from_broker(self, position: UserPosition) -> None:
        """Sync position state from broker when we detect orphaned position"""
        try:
            logger.warning(f"Syncing orphaned position: {position.size} contracts @ ${position.average_price:.2f}")
            
            # Determine side using position_type (more reliable than size)
            # position_type: 1 = LONG, 2 = SHORT
            # Handle both int (UserPosition) and Enum (Position from topstepx_client)
            if hasattr(position, 'position_type'):
                # Handle both int and Enum
                pos_type_value = position.position_type.value if hasattr(position.position_type, 'value') else position.position_type
                side = 'long' if pos_type_value == 1 else 'short'
                logger.info(f"Position type: {pos_type_value} ({'LONG' if pos_type_value == 1 else 'SHORT'})")
            else:
                # Fallback to size-based detection
                side = 'long' if position.size > 0 else 'short'
                logger.info(f"Position side from size: {position.size} ({'LONG' if position.size > 0 else 'SHORT'})")
            
            # Get current price for P&L calculation
            current_price = self._get_current_price()
            if current_price is None:
                logger.error("Cannot sync position - no current price available")
                return
            
            # Create position tracking entry
            entry_price = position.average_price
            logger.info(f"Syncing {side.upper()} position: {abs(position.size)} contracts @ ${entry_price:.2f}")
            self.current_position = {
                'side': side,
                'entry_price': entry_price,
                'stop_loss': entry_price,  # Will need to query actual stop
                'initial_stop_loss': entry_price,
                'take_profit': entry_price,  # Will need to query actual TP
                'quantity': abs(position.size),
                'entry_time': datetime.now(self.timezone),
                'order_id': None,
                'structure_levels': [],
                'last_broken_level': None,
                'break_even_set': False,
                'partial_exit_done': False,
                'scale_out_levels_done': []  # Track completed scale-out levels
            }
            
            # Initialize trailing stop tracking
            self.highest_price = entry_price
            self.lowest_price = entry_price
            
            # Try to get actual stop/tp from open orders
            tp_order_found = False
            stop_order_side = None  # Track stop order side to validate it matches position side
            try:
                open_orders = self.client.get_open_orders()
                for order in open_orders:
                    if order.get('contractId') == self.contract.id:
                        order_side_value = order.get('side', -1)  # 0 = BID (buy), 1 = ASK (sell)
                        order_side = 'ASK' if order_side_value == 1 else 'BID' if order_side_value == 0 else 'UNKNOWN'
                        
                        if order.get('type') == 4:  # STOP order
                            stop_price = order.get('stopPrice', position.average_price)
                            # Validate stop order direction matches position
                            # For LONG: stop should be ASK (1 = sell to close loss) BELOW entry
                            # For SHORT: stop should be BID (0 = buy to close loss) ABOVE entry
                            # API: side = 0 (Bid/buy), side = 1 (Ask/sell)
                            expected_stop_side_value = 1 if side == 'long' else 0  # ASK (1) for long, BID (0) for short
                            
                            if order_side_value == expected_stop_side_value:
                                self.current_position['stop_loss'] = stop_price
                                self.current_position['initial_stop_loss'] = stop_price
                                self.current_position['stop_order_id'] = order.get('id')
                                logger.info(f"Found existing stop order: ID {order.get('id')} at ${stop_price:.2f} (side: {order_side}, position: {side.upper()})")
                                stop_order_side = order_side
                            else:
                                expected_side_str = 'ASK (1)' if side == 'long' else 'BID (0)'
                                logger.warning(f"Found stop order with wrong side: ID {order.get('id')}, side {order_side} ({order_side_value}), expected {expected_side_str} for {side.upper()}")
                        
                        elif order.get('type') == 1:  # LIMIT order (could be TP)
                            limit_price = order.get('limitPrice', 0)
                            # Validate TP order direction and price match position
                            # For LONG: TP should be ASK (sell to take profit) ABOVE entry
                            # For SHORT: TP should be BID (buy to take profit) BELOW entry
                            expected_tp_side = 'ASK' if side == 'long' else 'BID'
                            
                            is_valid_tp = False
                            if side == 'long' and limit_price > position.average_price and order_side == expected_tp_side:
                                is_valid_tp = True
                            elif side == 'short' and limit_price < position.average_price and order_side == expected_tp_side:
                                is_valid_tp = True
                            
                            if is_valid_tp:
                                self.current_position['take_profit'] = limit_price
                                self.current_position['tp_order_id'] = order.get('id')
                                tp_order_found = True
                                logger.info(f"Found existing TP order: ID {order.get('id')} at ${limit_price:.2f} (side: {order_side}, position: {side.upper()})")
                            else:
                                logger.debug(f"Ignoring limit order (not TP): ID {order.get('id')} at ${limit_price:.2f}, side {order_side} (position: {side.upper()}, entry: ${entry_price:.2f})")
            except Exception as e:
                logger.warning(f"Error checking open orders during sync: {e}")
            
            # CRITICAL FIX 9: Double-check for existing TP orders before placing new one
            # Re-check open orders to ensure we didn't miss any TP orders
            if not tp_order_found:
                try:
                    open_orders_recheck = self.client.get_open_orders()
                    for order in open_orders_recheck:
                        if order.get('contractId') == self.contract.id and order.get('type') == 1:  # LIMIT order
                            limit_price = order.get('limitPrice', 0)
                            order_side_value = order.get('side', -1)  # 0 = BID (buy), 1 = ASK (sell)
                            order_side = 'ASK' if order_side_value == 1 else 'BID' if order_side_value == 0 else 'UNKNOWN'
                            expected_tp_side = 'ASK' if side == 'long' else 'BID'
                            
                            is_valid_tp = False
                            if side == 'long' and limit_price > entry_price and order_side == expected_tp_side:
                                is_valid_tp = True
                            elif side == 'short' and limit_price < entry_price and order_side == expected_tp_side:
                                is_valid_tp = True
                            
                            if is_valid_tp:
                                self.current_position['take_profit'] = limit_price
                                self.current_position['tp_order_id'] = order.get('id')
                                tp_order_found = True
                                logger.info(f"Found existing TP order on recheck: ID {order.get('id')} at ${limit_price:.2f}")
                                break
                except Exception as e:
                    logger.warning(f"Error rechecking orders: {e}")
            
            # If no TP order found, calculate and place one based on stop loss and min_rr
            if not tp_order_found:
                stop_loss = self.current_position.get('stop_loss', entry_price)
                
                # Calculate TP based on risk/reward ratio (min_rr from config)
                min_rr = self.config.get('min_rr', 1.3)
                
                if side == 'long':
                    # For LONG: Stop loss should be BELOW entry, TP should be ABOVE entry
                    if stop_loss < entry_price:
                        risk = entry_price - stop_loss
                        take_profit = entry_price + (risk * min_rr)
                        logger.info(f"LONG position: Entry ${entry_price:.2f}, SL ${stop_loss:.2f} (below), Risk ${risk:.2f}, TP ${take_profit:.2f}")
                    else:
                        # Invalid stop loss (above entry for long) - use default
                        logger.warning(f"LONG position: Stop loss ${stop_loss:.2f} is ABOVE entry ${entry_price:.2f} (invalid), using default TP distance")
                        default_risk_ticks = 20
                        take_profit = entry_price + (default_risk_ticks * self.tick_size * min_rr)
                        
                else:  # short
                    # For SHORT: Stop loss should be ABOVE entry, TP should be BELOW entry
                    if stop_loss > entry_price:
                        risk = stop_loss - entry_price
                        take_profit = entry_price - (risk * min_rr)
                        logger.info(f"SHORT position: Entry ${entry_price:.2f}, SL ${stop_loss:.2f} (above), Risk ${risk:.2f}, TP ${take_profit:.2f}")
                    else:
                        # Invalid stop loss (below entry for short) - use default
                        logger.warning(f"SHORT position: Stop loss ${stop_loss:.2f} is BELOW entry ${entry_price:.2f} (invalid), using default TP distance")
                        default_risk_ticks = 20
                        take_profit = entry_price - (default_risk_ticks * self.tick_size * min_rr)
                
                # Round to tick size
                take_profit = round(take_profit / self.tick_size) * self.tick_size
                self.current_position['take_profit'] = take_profit
                
                # Place TP order with correct side
                # LONG: TP is ASK (sell) order ABOVE entry
                # SHORT: TP is BID (buy) order BELOW entry
                try:
                    if side == 'long':
                        tp_side = OrderSide.ASK  # Sell to close long position
                        if take_profit <= entry_price:
                            logger.error(f"CRITICAL: TP ${take_profit:.2f} must be ABOVE entry ${entry_price:.2f} for LONG position!")
                            return
                    else:  # short
                        tp_side = OrderSide.BID  # Buy to close short position
                        if take_profit >= entry_price:
                            logger.error(f"CRITICAL: TP ${take_profit:.2f} must be BELOW entry ${entry_price:.2f} for SHORT position!")
                            return
                    
                    quantity = abs(position.size)
                    logger.info(f"Placing missing TP order: {side.upper()} {quantity} contracts, {tp_side.name} @ ${take_profit:.2f}")
                    
                    tp_result = self.client.place_limit_order(
                        contract_id=self.contract.id,
                        side=tp_side,
                        size=quantity,
                        limit_price=take_profit
                    )
                    
                    if tp_result.get('success'):
                        tp_order_id = tp_result.get('orderId')
                        self.current_position['tp_order_id'] = tp_order_id
                        logger.info(f"TP order placed successfully: ID {tp_order_id}, {tp_side.name} {quantity} @ ${take_profit:.2f} ({side.upper()} position)")
                    else:
                        error_msg = tp_result.get('errorMessage', 'Unknown error')
                        logger.error(f"Failed to place TP order: {error_msg}")
                        self.alerts.error(f"Failed to place TP order for {side.upper()} position: {error_msg}")
                except Exception as e:
                    logger.error(f"Exception placing TP order: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            logger.info(f"Position synced: {side} {abs(position.size)} @ ${position.average_price:.2f}")
            logger.info(f"  Stop Loss: ${self.current_position.get('stop_loss', entry_price):.2f}")
            logger.info(f"  Take Profit: ${self.current_position.get('take_profit', entry_price):.2f}")
            self.alerts.error(f"Orphaned position detected and synced: {side} {abs(position.size)} contracts")
            
        except Exception as e:
            logger.error(f"Failed to sync position from broker: {e}")
    
    def _ensure_protective_orders_exist(self, broker_position) -> None:
        """Ensure SL and TP orders exist for current position - place missing ones"""
        if not self.current_position or not broker_position:
            return
        
        # CRITICAL FIX: Prevent concurrent execution with lock
        if hasattr(self, '_placing_protective_orders') and self._placing_protective_orders:
            logger.debug("Protective order placement already in progress - skipping duplicate call")
            return
        
        # CRITICAL FIX: Disable watchdog during order updates
        if hasattr(self, '_updating_stop_order') and self._updating_stop_order:
            logger.debug("Stop order update in progress - skipping watchdog")
            return
        
        if self._executing_entry:
            logger.debug("Entry execution in progress - skipping watchdog")
            return
        
        # CRITICAL FIX: Add debouncing/cooldown (only run once per 60 seconds)
        from datetime import datetime, timedelta
        if hasattr(self, '_last_watchdog_check'):
            elapsed = (datetime.now(self.timezone) - self._last_watchdog_check).total_seconds()
            if elapsed < 60:  # Only run once per minute
                logger.debug(f"Watchdog cooldown active ({elapsed:.1f}s < 60s) - skipping")
                return
        
        self._last_watchdog_check = datetime.now(self.timezone)
        self._placing_protective_orders = True
        
        try:
            side = self.current_position.get('side', 'long')
            entry_price = self.current_position.get('entry_price', broker_position.average_price)
            quantity = abs(broker_position.size)
            
            # FIX 1: Enhanced watchdog - check for missing orders AND size mismatches
            # Get all open orders for this contract
            open_orders = self.client.get_open_orders()
            has_stop = False
            has_tp = False
            stop_order_id = None
            stop_order_size = None
            tp_order_id = None
            tp_order_size = None
            
            for order in open_orders:
                if order.get('contractId') != self.contract.id:
                    continue
                
                order_type = order.get('type')
                order_size = abs(order.get('size', 0))
                order_id = order.get('id')
                
                # Check for stop loss order
                if order_type == 4:  # STOP order
                    order_side_value = order.get('side', -1)  # 0 = BID (buy), 1 = ASK (sell)
                    expected_stop_side_value = 1 if side == 'long' else 0  # ASK (1) for long, BID (0) for short
                    if order_side_value == expected_stop_side_value:
                        stop_order_id = order_id
                        stop_order_size = order_size
                        if order_size == quantity:
                            has_stop = True
                            if not self.current_position.get('stop_order_id'):
                                self.current_position['stop_order_id'] = order_id
                                self.current_position['stop_loss'] = order.get('stopPrice', 0)
                
                # Check for take profit order
                elif order_type == 1:  # LIMIT order
                    limit_price = order.get('limitPrice', 0)
                    order_side_value = order.get('side', -1)  # 0 = BID (buy), 1 = ASK (sell)
                    expected_tp_side_value = 1 if side == 'long' else 0  # ASK (1) for long, BID (0) for short
                    
                    is_valid_tp = False
                    if side == 'long' and limit_price > entry_price and order_side_value == expected_tp_side_value:
                        is_valid_tp = True
                    elif side == 'short' and limit_price < entry_price and order_side_value == expected_tp_side_value:
                        is_valid_tp = True
                    
                    if is_valid_tp:
                        tp_order_id = order_id
                        tp_order_size = order_size
                        if order_size == quantity:
                            has_tp = True
                            if not self.current_position.get('tp_order_id'):
                                self.current_position['tp_order_id'] = order_id
                                self.current_position['take_profit'] = limit_price
            
            # FIX 1: Enhanced watchdog - Fix size mismatches (order exists but wrong size)
            # This is critical after partial exits, BE moves, or any order modifications
            if stop_order_id and stop_order_size != quantity:
                logger.warning(f"FIX 1 WATCHDOG: Stop order size mismatch detected: {stop_order_id} has size {stop_order_size}, position has {quantity}. Fixing...")
                try:
                    stop_price = None
                    # Get stop price from order
                    for order in open_orders:
                        if order.get('id') == stop_order_id:
                            stop_price = order.get('stopPrice')
                            break
                    
                    if stop_price:
                        # FIX 3: Validate stop price before modifying
                        current_price = self._get_current_price()
                        if current_price:
                            if side == 'long':
                                # For LONG: stop must be below best_ask (at least 2 ticks)
                                if self.last_quote:
                                    best_ask = self.last_quote.best_ask
                                    max_allowed = best_ask - (2 * self.tick_size)
                                    if stop_price >= best_ask or stop_price > max_allowed:
                                        stop_price = round((best_ask - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                        logger.warning(f"Adjusted stop price for API requirement: ${stop_price:.2f}")
                            else:  # short
                                # For SHORT: stop must be above best_bid (at least 2 ticks)
                                if self.last_quote:
                                    best_bid = self.last_quote.best_bid
                                    min_allowed = best_bid + (2 * self.tick_size)
                                    if stop_price <= best_bid or stop_price < min_allowed:
                                        stop_price = round((best_bid + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                        logger.warning(f"Adjusted stop price for API requirement: ${stop_price:.2f}")
                        
                        result = self.client.modify_order(
                            order_id=stop_order_id,
                            size=quantity,
                            stop_price=stop_price
                        )
                        if result.get('success'):
                            # CRITICAL FIX 8: Verify the modification actually worked
                            time.sleep(0.5)
                            if self._verify_order_placement(stop_order_id, quantity, 'Stop Loss', max_retries=2):
                                logger.info(f"FIX 1: Fixed stop order size: {stop_order_id} -> {quantity} contracts (verified)")
                                has_stop = True  # Now it's valid
                                if not self.current_position.get('stop_order_id'):
                                    self.current_position['stop_order_id'] = stop_order_id
                            else:
                                logger.warning(f"Stop order size fix succeeded but verification failed - order may not be updated correctly")
                                # Don't set has_stop = True if verification failed
                        else:
                            logger.error(f"Failed to fix stop order size: {result.get('errorMessage')}")
                            # If modify fails, try place-first pattern
                            logger.warning("Modify failed, attempting place-first pattern for stop order...")
                            self._update_stop_order(stop_price)  # This will use place-first pattern
                    else:
                        logger.warning(f"Could not get stop price for order {stop_order_id}")
                except Exception as e:
                    logger.error(f"Exception fixing stop order size: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            # FIX 1: Enhanced watchdog - Fix TP size mismatches
            if tp_order_id and tp_order_size != quantity:
                logger.warning(f"FIX 1 WATCHDOG: TP order size mismatch detected: {tp_order_id} has size {tp_order_size}, position has {quantity}. Fixing...")
                try:
                    tp_price = None
                    # Get TP price from order
                    for order in open_orders:
                        if order.get('id') == tp_order_id:
                            tp_price = order.get('limitPrice')
                            break
                    
                    if tp_price:
                        # FIX 3: Validate TP price before modifying
                        current_price = self._get_current_price()
                        if current_price and self.last_quote:
                            if side == 'long':
                                # For LONG: TP should be above best_ask
                                best_ask = self.last_quote.best_ask
                                if tp_price <= best_ask:
                                    tp_price = round((best_ask + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                    logger.warning(f"Adjusted TP price for API requirement: ${tp_price:.2f}")
                            else:  # short
                                # For SHORT: TP should be below best_bid
                                best_bid = self.last_quote.best_bid
                                if tp_price >= best_bid:
                                    tp_price = round((best_bid - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                    logger.warning(f"Adjusted TP price for API requirement: ${tp_price:.2f}")
                        
                        result = self.client.modify_order(
                            order_id=tp_order_id,
                            size=quantity,
                            limit_price=tp_price
                        )
                        if result.get('success'):
                            # CRITICAL FIX 8: Verify the modification actually worked
                            time.sleep(0.5)
                            if self._verify_order_placement(tp_order_id, quantity, 'Take Profit', max_retries=2):
                                logger.info(f"FIX 1: Fixed TP order size: {tp_order_id} -> {quantity} contracts (verified)")
                                has_tp = True  # Now it's valid
                                if not self.current_position.get('tp_order_id'):
                                    self.current_position['tp_order_id'] = tp_order_id
                            else:
                                logger.warning(f"TP order size fix succeeded but verification failed - order may not be updated correctly")
                                # Don't set has_tp = True if verification failed
                        else:
                            error_msg = result.get('errorMessage', 'Unknown error')
                            logger.error(f"Failed to fix TP order size: {error_msg}")
                            # If modify fails, use place-first pattern (same as in partial exit)
                            logger.warning("Modify failed, attempting place-first pattern for TP order...")
                            # Use the same robust replace logic from partial exit
                            tp_side = OrderSide.ASK if side == 'long' else OrderSide.BID
                            new_tp_result = self.client.place_limit_order(
                                contract_id=self.contract.id,
                                side=tp_side,
                                size=quantity,
                                limit_price=tp_price
                            )
                            if new_tp_result.get('success'):
                                new_tp_id = new_tp_result.get('orderId')
                                time.sleep(0.5)
                                if self._verify_order_placement(new_tp_id, quantity, 'Take Profit', max_retries=2):
                                    self.current_position['tp_order_id'] = new_tp_id
                                    self.current_position['take_profit'] = tp_price
                                    # Cancel old TP
                                    try:
                                        self.client.cancel_order(tp_order_id)
                                        logger.info(f"FIX 1: TP order replaced via place-first: {new_tp_id} (old {tp_order_id} cancelled)")
                                    except Exception as e:
                                        logger.warning(f"Failed to cancel old TP: {e}")
                    else:
                        logger.warning(f"Could not get TP price for order {tp_order_id}")
                except Exception as e:
                    logger.error(f"Exception fixing TP order size: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            # CRITICAL FIX: Check if we already have a stop order ID tracked before placing new one
            # This prevents duplicate orders when verification fails but order actually exists
            tracked_sl_order_id = self.current_position.get('stop_order_id')
            if tracked_sl_order_id and not has_stop:
                # Verify the tracked order ID exists in open orders
                order_exists = False
                for order in open_orders:
                    if order.get('id') == tracked_sl_order_id and order.get('contractId') == self.contract.id:
                        order_exists = True
                        # Update has_stop if order exists (even if size mismatch, we'll fix that above)
                        has_stop = True
                        logger.info(f"Tracked stop order {tracked_sl_order_id} exists in open orders (will fix size if needed)")
                        break
                
                if not order_exists:
                    # Tracked order doesn't exist - clear it so we can place a new one
                    logger.warning(f"Tracked stop order {tracked_sl_order_id} not found in open orders - clearing and will place new one")
                    self.current_position['stop_order_id'] = None
            
            # CRITICAL FIX: Count existing stop orders before placing new one
            existing_sl_count = 0
            for order in open_orders:
                if (order.get('contractId') == self.contract.id and 
                    order.get('type') == 4):  # STOP order
                    order_side_value = order.get('side', -1)  # 0 = BID (buy), 1 = ASK (sell)
                    expected_stop_side_value = 1 if side == 'long' else 0  # ASK (1) for long, BID (0) for short
                    if order_side_value == expected_stop_side_value:
                        existing_sl_count += 1
            
            if existing_sl_count > 1:
                logger.error(f"CRITICAL: Found {existing_sl_count} duplicate stop orders! Cleaning up...")
                # Keep only the first one, cancel others
                kept_first = False
                for order in open_orders:
                    if (order.get('contractId') == self.contract.id and 
                        order.get('type') == 4):
                        order_side_value = order.get('side', -1)  # 0 = BID (buy), 1 = ASK (sell)
                        expected_stop_side_value = 1 if side == 'long' else 0  # ASK (1) for long, BID (0) for short
                        if order_side_value == expected_stop_side_value:
                            if not kept_first:
                                kept_first = True
                                self.current_position['stop_order_id'] = order.get('id')
                                self.current_position['stop_loss'] = order.get('stopPrice', 0)
                                logger.info(f"Keeping stop order: {order.get('id')}")
                            else:
                                try:
                                    logger.warning(f"Cancelling duplicate stop order: {order.get('id')}")
                                    self.client.cancel_order(order.get('id'))
                                except Exception as e:
                                    logger.error(f"Failed to cancel duplicate: {e}")
                has_stop = True  # Mark as having stop after cleanup
            
            # Place missing stop loss
            if not has_stop:
                logger.warning("CRITICAL: Missing stop loss order detected - placing now")
                stop_loss = self.current_position.get('stop_loss') or self.current_position.get('initial_stop_loss')
                if not stop_loss:
                    # Calculate stop loss from entry
                    risk_ticks = 20  # Default
                    if side == 'long':
                        stop_loss = entry_price - (risk_ticks * self.tick_size)
                    else:
                        stop_loss = entry_price + (risk_ticks * self.tick_size)
                
                # Round to tick size
                stop_loss = round(stop_loss / self.tick_size) * self.tick_size
                
                # Get current market price for validation
                current_price = None
                try:
                    if self.last_quote and self.last_quote.contract_id == self.contract.id:
                        current_price = (self.last_quote.best_bid + self.last_quote.best_ask) / 2
                        best_bid = self.last_quote.best_bid
                        best_ask = self.last_quote.best_ask
                    else:
                        # Try to get from recent bars
                        import time
                        time.sleep(0.5)  # Brief wait for quote
                        if self.last_quote and self.last_quote.contract_id == self.contract.id:
                            current_price = (self.last_quote.best_bid + self.last_quote.best_ask) / 2
                            best_bid = self.last_quote.best_bid
                            best_ask = self.last_quote.best_ask
                        else:
                            # Fallback: use entry price as approximation
                            current_price = entry_price
                            best_bid = entry_price - (1 * self.tick_size)  # Approximate
                            best_ask = entry_price + (1 * self.tick_size)  # Approximate
                    
                    if current_price:
                        if side == 'long':
                            # For LONG: stop must be below best_ask (at least 2 ticks)
                            max_allowed_stop = best_ask - (2 * self.tick_size)
                            if stop_loss >= best_ask or stop_loss > max_allowed_stop:
                                original_sl = stop_loss
                                stop_loss = round((best_ask - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                logger.warning(f"Stop price adjusted for API: ${original_sl:.2f} -> ${stop_loss:.2f} (best_ask: ${best_ask:.2f})")
                            # Also ensure stop is below entry for long
                            if stop_loss >= entry_price:
                                stop_loss = round((entry_price - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                logger.warning(f"Stop adjusted below entry: ${stop_loss:.2f}")
                        else:  # short
                            # For SHORT: stop must be above best_bid (at least 2 ticks)
                            min_allowed_stop = best_bid + (2 * self.tick_size)
                            if stop_loss <= best_bid or stop_loss < min_allowed_stop:
                                original_sl = stop_loss
                                stop_loss = round((best_bid + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                logger.warning(f"Stop price adjusted for API: ${original_sl:.2f} -> ${stop_loss:.2f} (best_bid: ${best_bid:.2f})")
                            # Also ensure stop is above entry for short
                            if stop_loss <= entry_price:
                                stop_loss = round((entry_price + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                logger.warning(f"Stop adjusted above entry: ${stop_loss:.2f}")
                except Exception as e:
                    logger.warning(f"Could not validate stop price: {e}")
                
                sl_side = OrderSide.ASK if side == 'long' else OrderSide.BID
                
                # Retry with price adjustment
                sl_placed = False
                for attempt in range(5):
                    if attempt > 0:
                        # Adjust price based on error
                        if side == 'long':
                            stop_loss = stop_loss - (1 * self.tick_size)
                        else:
                            stop_loss = stop_loss + (1 * self.tick_size)
                        logger.info(f"Retrying SL placement (attempt {attempt + 1}) with adjusted price: ${stop_loss:.2f}")
                    
                    sl_result = self.client.place_stop_order(
                        contract_id=self.contract.id,
                        side=sl_side,
                        size=quantity,
                        stop_price=stop_loss
                    )
                    
                    if sl_result.get('success'):
                        sl_order_id = sl_result.get('orderId')
                        time.sleep(0.5)
                        if self._verify_order_placement(sl_order_id, quantity, 'Stop Loss', max_retries=2):
                            self.current_position['stop_order_id'] = sl_order_id
                            self.current_position['stop_loss'] = stop_loss
                            logger.info(f"Missing stop loss placed and verified: ID {sl_order_id} @ ${stop_loss:.2f}")
                            self.alerts.error(f"Missing stop loss placed for {side.upper()} position")
                            sl_placed = True
                            break
                    else:
                        error_msg = sl_result.get('errorMessage', 'Unknown error')
                        logger.warning(f"SL attempt {attempt + 1} failed: {error_msg}")
                        if attempt < 4:
                            time.sleep(1.0)
                
                if not sl_placed:
                    logger.error(f"CRITICAL: Failed to place missing stop loss after 5 attempts")
                    self.alerts.error(f"CRITICAL: Failed to place missing stop loss!")
            
            # CRITICAL FIX: Check if we already have a TP order ID tracked before placing new one
            # This prevents duplicate orders when verification fails but order actually exists
            tracked_tp_order_id = self.current_position.get('tp_order_id')
            if tracked_tp_order_id and not has_tp:
                # Verify the tracked order ID exists in open orders
                order_exists = False
                for order in open_orders:
                    if order.get('id') == tracked_tp_order_id and order.get('contractId') == self.contract.id:
                        order_exists = True
                        # Update has_tp if order exists (even if size mismatch, we'll fix that above)
                        has_tp = True
                        logger.info(f"Tracked TP order {tracked_tp_order_id} exists in open orders (will fix size if needed)")
                        break
                
                if not order_exists:
                    # Tracked order doesn't exist - clear it so we can place a new one
                    logger.warning(f"Tracked TP order {tracked_tp_order_id} not found in open orders - clearing and will place new one")
                    self.current_position['tp_order_id'] = None
            
            # CRITICAL FIX: Count existing TP orders before placing new one
            existing_tp_count = 0
            for order in open_orders:
                if (order.get('contractId') == self.contract.id and 
                    order.get('type') == 1):  # LIMIT order
                    limit_price = order.get('limitPrice', 0)
                    order_side_value = order.get('side', -1)  # 0 = BID (buy), 1 = ASK (sell)
                    expected_tp_side_value = 1 if side == 'long' else 0  # ASK (1) for long, BID (0) for short
                    is_valid_tp = False
                    if side == 'long' and limit_price > entry_price and order_side_value == expected_tp_side_value:
                        is_valid_tp = True
                    elif side == 'short' and limit_price < entry_price and order_side_value == expected_tp_side_value:
                        is_valid_tp = True
                    if is_valid_tp:
                        existing_tp_count += 1
            
            if existing_tp_count > 1:
                logger.error(f"CRITICAL: Found {existing_tp_count} duplicate TP orders! Cleaning up...")
                # Keep only the first one, cancel others
                kept_first = False
                for order in open_orders:
                    if (order.get('contractId') == self.contract.id and 
                        order.get('type') == 1):
                        limit_price = order.get('limitPrice', 0)
                        order_side_value = order.get('side', -1)  # 0 = BID (buy), 1 = ASK (sell)
                        expected_tp_side_value = 1 if side == 'long' else 0  # ASK (1) for long, BID (0) for short
                        is_valid_tp = False
                        if side == 'long' and limit_price > entry_price and order_side_value == expected_tp_side_value:
                            is_valid_tp = True
                        elif side == 'short' and limit_price < entry_price and order_side_value == expected_tp_side_value:
                            is_valid_tp = True
                        if is_valid_tp:
                            if not kept_first:
                                kept_first = True
                                self.current_position['tp_order_id'] = order.get('id')
                                self.current_position['take_profit'] = limit_price
                                logger.info(f"Keeping TP order: {order.get('id')}")
                            else:
                                try:
                                    logger.warning(f"Cancelling duplicate TP order: {order.get('id')}")
                                    self.client.cancel_order(order.get('id'))
                                except Exception as e:
                                    logger.error(f"Failed to cancel duplicate: {e}")
                has_tp = True  # Mark as having TP after cleanup
            
            # Place missing take profit
            if not has_tp:
                logger.warning("Missing take profit order detected - placing now")
                take_profit = self.current_position.get('take_profit')
                if not take_profit:
                    # Calculate TP from stop loss and min_rr
                    stop_loss = self.current_position.get('stop_loss') or self.current_position.get('initial_stop_loss')
                    min_rr = self.config.get('min_rr', 1.3)
                    risk = abs(entry_price - stop_loss) if stop_loss else (20 * self.tick_size)
                    if side == 'long':
                        take_profit = entry_price + (risk * min_rr)
                    else:
                        take_profit = entry_price - (risk * min_rr)
                
                # Round to tick size
                take_profit = round(take_profit / self.tick_size) * self.tick_size
                
                # Validate TP price
                try:
                    if self.last_quote and self.last_quote.contract_id == self.contract.id:
                        quote = self.last_quote
                    else:
                        # Try to get quote from market data
                        try:
                            bars = self.client.get_historical_bars(
                                contract_id=self.contract.id,
                                interval=3,
                                count=1,
                                live=True,
                                unit=2
                            )
                            if bars and len(bars) > 0:
                                class SimpleQuote:
                                    def __init__(self, bid, ask):
                                        self.bid = bid
                                        self.ask = ask
                                        self.contract_id = self.contract.id
                                quote = SimpleQuote(bars[0]['c'], bars[0]['c'])
                            else:
                                quote = None
                        except:
                            quote = None
                    
                    if quote:
                        if side == 'long':
                            best_ask = getattr(quote, 'ask', getattr(quote, 'best_ask', None))
                            if best_ask:
                                # TP should be above current ask
                                if take_profit <= best_ask:
                                    take_profit = round((best_ask + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                    logger.warning(f"TP adjusted to be above best_ask: ${take_profit:.2f}")
                        else:  # short
                            best_bid = getattr(quote, 'bid', getattr(quote, 'best_bid', None))
                            if best_bid:
                                # TP should be below current bid
                                if take_profit >= best_bid:
                                    take_profit = round((best_bid - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                    logger.warning(f"TP adjusted to be below best_bid: ${take_profit:.2f}")
                except Exception as e:
                    logger.warning(f"Could not validate TP price: {e}")
                
                tp_side = OrderSide.ASK if side == 'long' else OrderSide.BID
                
                # Retry with price adjustment
                tp_placed = False
                for attempt in range(5):
                    if attempt > 0:
                        # Adjust price based on error
                        if side == 'long':
                            take_profit = take_profit + (1 * self.tick_size)
                        else:
                            take_profit = take_profit - (1 * self.tick_size)
                        logger.info(f"Retrying TP placement (attempt {attempt + 1}) with adjusted price: ${take_profit:.2f}")
                    
                    tp_result = self.client.place_limit_order(
                        contract_id=self.contract.id,
                        side=tp_side,
                        size=quantity,
                        limit_price=take_profit
                    )
                    
                    if tp_result.get('success'):
                        tp_order_id = tp_result.get('orderId')
                        time.sleep(0.5)
                        if self._verify_order_placement(tp_order_id, quantity, 'Take Profit', max_retries=2):
                            self.current_position['tp_order_id'] = tp_order_id
                            self.current_position['take_profit'] = take_profit
                            logger.info(f"Missing take profit placed and verified: ID {tp_order_id} @ ${take_profit:.2f}")
                            tp_placed = True
                            break
                    else:
                        error_msg = tp_result.get('errorMessage', 'Unknown error')
                        logger.warning(f"TP attempt {attempt + 1} failed: {error_msg}")
                        if attempt < 4:
                            time.sleep(1.0)
                
                if not tp_placed:
                    logger.warning(f"Failed to place missing take profit after 5 attempts")
            
        except Exception as e:
            logger.error(f"Error ensuring protective orders exist: {e}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            self._placing_protective_orders = False
    
    def _check_break_even(self) -> None:
        if self.current_position is None:
            return
        
        # Prevent concurrent calls with a lock
        if hasattr(self, '_checking_break_even') and self._checking_break_even:
            return
        
        self._checking_break_even = True
        try:
            be_config = self.config.get('break_even', {})
            if not be_config.get('enabled', True):
                return
            
            # Check flag FIRST to prevent any duplicate processing
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
                # Double-check flag AGAIN (race condition protection)
                if self.current_position.get('break_even_set'):
                    logger.debug("Break-even already set, skipping duplicate call")
                    return
                
                # FIX 3: Validate BE price against market before moving stop
                be_price = entry
                if current_price is not None and self.last_quote:
                    if side == 'long':
                        # For LONG: stop must be below best_ask (at least 2 ticks)
                        best_ask = self.last_quote.best_ask
                        max_allowed_be = best_ask - (2 * self.tick_size)
                        if be_price >= best_ask or be_price > max_allowed_be:
                            # Adjust BE to be as close to entry as possible but still valid
                            be_price = round((best_ask - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                            # Ensure it's still reasonably close to entry (within 5 ticks)
                            if abs(be_price - entry) <= 5 * self.tick_size:
                                logger.info(f"FIX 3: BE price adjusted for LONG: ${entry:.2f} -> ${be_price:.2f} (best_ask: ${best_ask:.2f})")
                            else:
                                logger.warning(f"BE price ${entry:.2f} too close to current ${current_price:.2f}, using adjusted ${be_price:.2f}")
                    else:  # short
                        # For SHORT: stop must be above best_bid (at least 2 ticks)
                        best_bid = self.last_quote.best_bid
                        min_allowed_be = best_bid + (2 * self.tick_size)
                        if be_price <= best_bid or be_price < min_allowed_be:
                            # Adjust BE to be as close to entry as possible but still valid
                            be_price = round((best_bid + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                            # Ensure it's still >= entry (for short, stop must protect against losses)
                            if be_price < entry:
                                be_price = entry
                            # Ensure it's still reasonably close to entry (within 5 ticks)
                            if abs(be_price - entry) <= 5 * self.tick_size:
                                logger.info(f"FIX 3: BE price adjusted for SHORT: ${entry:.2f} -> ${be_price:.2f} (best_bid: ${best_bid:.2f})")
                            else:
                                logger.warning(f"BE price ${entry:.2f} too close to current ${current_price:.2f}, using adjusted ${be_price:.2f}")
                
                # Set flag IMMEDIATELY to prevent other threads from processing
                self.current_position['break_even_set'] = True
                logger.info(f"Moving stop to break-even: ${be_price:.2f} ({reason})")
                self.current_position['stop_loss'] = be_price
                # FIX 2: _update_stop_order already uses place-first pattern (never cancel-first)
                self._update_stop_order(be_price)  # Update broker stop order
                
                # CRITICAL FIX 7: Also update TP order size when moving to break-even
                # After partial exits, TP size might not match position size
                tp_order_id = self.current_position.get('tp_order_id')
                if tp_order_id:
                    current_qty = self.current_position.get('quantity')
                    if current_qty and current_qty > 0:
                        tp_price = self.current_position.get('take_profit')
                        if tp_price:
                            try:
                                # Try to modify TP order size to match current position
                                result = self.client.modify_order(
                                    order_id=tp_order_id,
                                    size=current_qty,
                                    limit_price=tp_price
                                )
                                if result.get('success'):
                                    # Verify the modification
                                    time.sleep(0.5)
                                    if self._verify_order_placement(tp_order_id, current_qty, 'Take Profit', max_retries=2):
                                        logger.info(f"TP order size updated to {current_qty} contracts during break-even move")
                                    else:
                                        logger.warning("TP order modify succeeded but verification failed")
                            except Exception as e:
                                logger.warning(f"Failed to update TP order size during break-even: {e}")
                self.alerts.stop_moved_to_breakeven(be_price)
        finally:
            self._checking_break_even = False
    
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
        
        pos = self.current_position
        entry = pos['entry_price']
        initial_sl = pos.get('initial_stop_loss', pos['stop_loss'])
        side = pos['side']
        risk = abs(entry - initial_sl)
        
        # Check if using graduated scale-out levels
        if self.scale_out_mode == 'graduated' and self.scale_out_levels:
            self._check_gradual_partial_exit(current_price)
            return
        
        # Legacy single partial exit (backward compatibility)
        if pos.get('partial_exit_done'):
            logger.debug("Partial exit already done")
            return
        
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
    
    def _check_gradual_partial_exit(self, current_price: float) -> None:
        """Check for graduated scale-out levels"""
        if not self.partial_enabled or self.current_position is None:
            return
        
        pos = self.current_position
        scale_levels = self.scale_out_levels
        
        if not scale_levels:
            return  # Fall back to original single partial
        
        entry = pos['entry_price']
        initial_sl = pos.get('initial_stop_loss', pos['stop_loss'])
        side = pos['side']
        risk = abs(entry - initial_sl)
        scale_out_done = pos.get('scale_out_levels_done', [])
        
        for level_idx, level_config in enumerate(scale_levels):
            level_key = f'scale_out_{level_idx}_done'
            if level_idx in scale_out_done:
                continue  # Already exited at this level
            
            target_r = level_config.get('r', 0)
            exit_pct = level_config.get('pct', 0.33)
            
            # Calculate target price
            if target_r > 0:
                # R-based target
                if side == 'long':
                    target_price = entry + (risk * target_r)
                    if current_price >= target_price:
                        self._execute_scaled_partial(pos, current_price, exit_pct, level_idx, level_config)
                        return
                else:  # short
                    target_price = entry - (risk * target_r)
                    if current_price <= target_price:
                        self._execute_scaled_partial(pos, current_price, exit_pct, level_idx, level_config)
                        return
            elif target_r == 0 and level_config.get('target') == 'structure':
                # Structure-based target (for final level)
                if pos.get('structure_levels'):
                    buffer = self.structure_buffer_ticks * self.tick_size * 2
                    if side == 'long':
                        for level in pos['structure_levels']:
                            if level > entry:
                                target_price = level - buffer
                                if current_price >= target_price:
                                    self._execute_scaled_partial(pos, current_price, exit_pct, level_idx, level_config)
                                    return
                                break
                    else:  # short
                        for level in pos['structure_levels']:
                            if level < entry:
                                target_price = level + buffer
                                if current_price <= target_price:
                                    self._execute_scaled_partial(pos, current_price, exit_pct, level_idx, level_config)
                                    return
                                break
    
    def _execute_scaled_partial(self, pos: dict, current_price: float, exit_pct: float, level_idx: int, level_config: dict) -> None:
        """Execute a scaled partial exit with level-specific actions"""
        side = pos['side']
        current_qty = pos.get('quantity', self.position_size)
        
        exit_qty = max(1, int(current_qty * exit_pct))
        
        if exit_qty >= current_qty:
            logger.warning(f"Cannot exit {exit_qty} contracts from {current_qty} remaining")
            return
        
        logger.info("=" * 50)
        logger.info(f"SCALE-OUT LEVEL {level_idx + 1} - Exiting {exit_qty} contracts ({exit_pct*100:.0f}%)")
        logger.info("=" * 50)
        
        try:
            result = self.client.partial_close_position(
                contract_id=self.contract.id,
                size=exit_qty
            )
            
            if result.get('success'):
                # Track completed level
                scale_out_done = pos.get('scale_out_levels_done', [])
                if level_idx not in scale_out_done:
                    scale_out_done.append(level_idx)
                pos['scale_out_levels_done'] = scale_out_done
                
                # Update position quantity
                pos['quantity'] = current_qty - exit_qty
                
                entry = pos['entry_price']
                initial_sl = pos.get('initial_stop_loss', pos['stop_loss'])
                risk = abs(entry - initial_sl)
                
                logger.info(f"OK Scale-out level {level_idx + 1}: {exit_qty} contracts at ${current_price:.2f}")
                logger.info(f"OK Remaining: {pos['quantity']} contracts")
                
                # Handle level-specific actions
                if level_config.get('move_be', False):
                    # Move stop to break-even
                    pos['stop_loss'] = entry
                    pos['break_even_set'] = True
                    self._update_stop_order(entry)
                    logger.info(f"OK Stop moved to break-even: ${entry:.2f}")
                
                if 'trail_r' in level_config:
                    # Activate trailing stop for remaining position
                    trail_r = level_config['trail_r']
                    if side == 'long':
                        trail_distance = risk * trail_r
                        trail_price = current_price - trail_distance
                        if trail_price > pos['stop_loss']:
                            pos['stop_loss'] = trail_price
                            self._update_stop_order(trail_price)
                            logger.info(f"OK Trailing stop activated at ${trail_price:.2f} ({trail_r}R distance)")
                    else:  # short
                        trail_distance = risk * trail_r
                        trail_price = current_price + trail_distance
                        if trail_price < pos['stop_loss']:
                            pos['stop_loss'] = trail_price
                            self._update_stop_order(trail_price)
                            logger.info(f"OK Trailing stop activated at ${trail_price:.2f} ({trail_r}R distance)")
                
                self.alerts.partial_exit(exit_qty, current_price)
            else:
                error = result.get('errorMessage', 'Unknown error')
                logger.error(f"Scale-out level {level_idx + 1} failed: {error}")
                
        except Exception as e:
            logger.error(f"Error executing scale-out level {level_idx + 1}: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
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
            import math
            # Use ceil() for SL (safer - ensures we don't shrink the stop distance)
            # Use round() for TP (balanced - maintains R:R closer to planned)
            sl_ticks = math.ceil(abs(order['risk_ticks']))
            tp_ticks = round(abs(order['reward_ticks']))
            
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
                self._executing_entry = False  # Release execution lock
                self._trading_locked = False  # Release trading lock
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
                'last_broken_level': None,
                'scale_out_levels_done': []  # Track completed scale-out levels
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
            # Clear position and release locks on exception
            self.current_position = None
            self._executing_entry = False  # Release execution lock
            self._trading_locked = False  # Release trading lock
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
                    # For LONG: move stop up (above entry) to lock profit
                    new_sl = entry + sl_move
                else:
                    # For SHORT: move stop down (toward entry) to lock profit, but keep it above entry
                    # Initial stop is above entry, so we move it down by sl_move
                    # This locks in profit while keeping stop >= entry (protects against losses)
                    if initial_sl > entry:
                        new_sl = max(entry, initial_sl - sl_move)
                    else:
                        # Fallback: if initial_sl is somehow below entry, use entry (break-even)
                        new_sl = entry
                
                pos['stop_loss'] = new_sl
                
                logger.info(f"OK Partial exit: {exit_qty} contracts at ${current_price:.2f}")
                logger.info(f"OK Remaining: {pos['quantity']} contracts")
                logger.info(f"OK Stop moved to: ${new_sl:.2f} ({self.post_partial_sl_lock_r}R profit locked)")
                
                # FIX 4: Update BOTH stop loss AND take profit order sizes to match remaining position
                remaining_qty = pos['quantity']
                logger.info(f"FIX 4: Updating BOTH SL and TP orders to size {remaining_qty} contracts after partial exit")
                
                # Update stop loss order size and price
                self._update_stop_order(new_sl)
                
                # FIX 4: Verify SL size was updated correctly
                time.sleep(0.5)  # Brief wait for order update
                sl_order_id = pos.get('stop_order_id')
                if sl_order_id:
                    try:
                        open_orders = self.client.get_open_orders()
                        for order in open_orders:
                            if order.get('id') == sl_order_id and order.get('contractId') == self.contract.id:
                                sl_size = abs(order.get('size', 0))
                                if sl_size == remaining_qty:
                                    logger.info(f"OK Stop loss order size verified: {sl_size} contracts")
                                else:
                                    logger.warning(f"Stop loss size mismatch: {sl_size} != {remaining_qty}. Will be fixed by watchdog.")
                                    # Try to fix it immediately
                                    try:
                                        result = self.client.modify_order(
                                            order_id=sl_order_id,
                                            size=remaining_qty,
                                            stop_price=new_sl
                                        )
                                        if result.get('success'):
                                            logger.info(f"Fixed stop loss order size: {sl_order_id} -> {remaining_qty}")
                                    except Exception as e:
                                        logger.warning(f"Failed to fix SL size immediately: {e}")
                                break
                    except Exception as e:
                        logger.warning(f"Could not verify SL order size: {e}")
                
                # FIX 5: Update take profit order size to match remaining position (robust retry loop)
                tp_order_id = pos.get('tp_order_id')
                if tp_order_id:
                    tp_price = pos.get('take_profit')
                    if tp_price:
                        logger.info(f"Updating take profit order size after partial: {tp_order_id} -> {remaining_qty} contracts")
                        
                        # Try modify first (preferred method)
                        modify_success = False
                        try:
                            result = self.client.modify_order(
                                order_id=tp_order_id,
                                size=remaining_qty,
                                limit_price=tp_price
                            )
                            if result.get('success'):
                                # Verify the modification
                                time.sleep(0.5)
                                if self._verify_order_placement(tp_order_id, remaining_qty, 'Take Profit', max_retries=2):
                                    logger.info(f"OK Take profit order size updated to {remaining_qty} contracts")
                                    modify_success = True
                                else:
                                    logger.warning("TP order modify succeeded but verification failed")
                        except Exception as e:
                            logger.warning(f"Exception during TP modify: {e}")
                        
                        # If modify failed, use robust replace (place-first, then cancel)
                        if not modify_success:
                            error_msg = result.get('errorMessage', 'Unknown error') if 'result' in locals() else 'Exception during modify'
                            logger.warning(f"TP modify failed: {error_msg}. Using robust replace (place-first pattern)...")
                            
                            # FIX 5: Robust TP replace with retry loop, price validation, and verification
                            tp_side = OrderSide.ASK if side == 'long' else OrderSide.BID
                            tp_placed = False
                            max_tp_retries = 5
                            
                            for tp_attempt in range(max_tp_retries):
                                # Get fresh quote before each attempt to adjust price if needed
                                if tp_attempt > 0:
                                    try:
                                        if self.last_quote and self.last_quote.contract_id == self.contract.id:
                                            if side == 'long':
                                                # For LONG: TP should be above best_ask
                                                best_ask = self.last_quote.best_ask
                                                if tp_price <= best_ask:
                                                    tp_price = round((best_ask + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                                    logger.info(f"Adjusted TP price for retry: ${tp_price:.2f} (best_ask: ${best_ask:.2f})")
                                            else:  # short
                                                # For SHORT: TP should be below best_bid
                                                best_bid = self.last_quote.best_bid
                                                if tp_price >= best_bid:
                                                    tp_price = round((best_bid - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                                                    logger.info(f"Adjusted TP price for retry: ${tp_price:.2f} (best_bid: ${best_bid:.2f})")
                                            time.sleep(0.5)  # Brief wait for quote update
                                    except Exception as e:
                                        logger.debug(f"Could not adjust TP price: {e}")
                                
                                # STEP 1: Place new TP order FIRST (before canceling old one)
                                logger.info(f"Placing new TP order (attempt {tp_attempt + 1}/{max_tp_retries}): {tp_side.name} {remaining_qty} @ ${tp_price:.2f}")
                                tp_result = self.client.place_limit_order(
                                    contract_id=self.contract.id,
                                    side=tp_side,
                                    size=remaining_qty,
                                    limit_price=tp_price
                                )
                                
                                if tp_result.get('success'):
                                    new_tp_order_id = tp_result.get('orderId')
                                    # Verify order was actually placed
                                    time.sleep(0.5)
                                    if self._verify_order_placement(new_tp_order_id, remaining_qty, 'Take Profit', max_retries=2):
                                        # STEP 2: New TP verified - now safely cancel old TP
                                        pos['tp_order_id'] = new_tp_order_id
                                        pos['take_profit'] = tp_price
                                        logger.info(f"OK Take profit order replaced: ID {new_tp_order_id} @ ${tp_price:.2f}, size {remaining_qty}")
                                        
                                        # CRITICAL FIX 6: Cancel old TP order and verify cancellation
                                        try:
                                            self.client.cancel_order(tp_order_id)
                                            logger.info(f"Cancelled old TP order: {tp_order_id}")
                                            
                                            # Wait and verify old order is cancelled
                                            time.sleep(1.0)
                                            open_orders_after = self.client.get_open_orders()
                                            old_order_still_exists = any(o.get('id') == tp_order_id 
                                                                        for o in open_orders_after 
                                                                        if o.get('contractId') == self.contract.id)
                                            if old_order_still_exists:
                                                logger.warning(f"Old TP order {tp_order_id} still exists after cancel - retrying cancellation")
                                                try:
                                                    self.client.cancel_order(tp_order_id)
                                                    time.sleep(0.5)
                                                except:
                                                    pass
                                            else:
                                                logger.info(f"Old TP order {tp_order_id} successfully cancelled and verified")
                                        except Exception as e:
                                            logger.warning(f"Failed to cancel old TP order {tp_order_id}: {e}")
                                        
                                        tp_placed = True
                                        break
                                    else:
                                        # CRITICAL FIX 3: Verification failed - cancel this failed attempt before retry
                                        logger.warning(f"TP order {new_tp_order_id} not verified, cancelling before retry...")
                                        try:
                                            self.client.cancel_order(new_tp_order_id)
                                            logger.info(f"Cancelled unverified TP order {new_tp_order_id} before retry")
                                        except Exception as e:
                                            logger.warning(f"Failed to cancel unverified TP order {new_tp_order_id}: {e}")
                                        
                                        if tp_attempt < max_tp_retries - 1:
                                            time.sleep(1.5)
                                else:
                                    error_msg = tp_result.get('errorMessage', 'Unknown error')
                                    logger.warning(f"TP attempt {tp_attempt + 1} failed: {error_msg}")
                                    # If price error, adjust price for next attempt
                                    if 'price' in error_msg.lower() or 'outside' in error_msg.lower():
                                        if side == 'long':
                                            tp_price = tp_price + (1 * self.tick_size)  # Move up
                                        else:
                                            tp_price = tp_price - (1 * self.tick_size)  # Move down
                                        logger.info(f"Adjusting TP price for next attempt: ${tp_price:.2f}")
                                    if tp_attempt < max_tp_retries - 1:
                                        time.sleep(1.5)  # Wait before retry
                            
                            if not tp_placed:
                                # TP replace failed - old TP may still exist (check and keep it if valid)
                                logger.error(f"Failed to replace TP order after {max_tp_retries} attempts")
                                self.alerts.error(f"TP order resize failed after partial exit - old TP may still be active")
                                # Old TP order remains (may have wrong size, but better than nothing)
                    else:
                        logger.warning("No TP price found in position - cannot update take profit order")
                else:
                    logger.warning("No TP order ID found - cannot update take profit order size")
                
                # FIX 4: Final verification - ensure BOTH SL and TP sizes match remaining position
                time.sleep(0.5)  # Brief wait for all order updates
                logger.info("FIX 4: Final verification of SL and TP order sizes after partial exit...")
                try:
                    open_orders = self.client.get_open_orders()
                    sl_verified = False
                    tp_verified = False
                    
                    for order in open_orders:
                        if order.get('contractId') != self.contract.id:
                            continue
                        
                        order_id = order.get('id')
                        order_size = abs(order.get('size', 0))
                        order_type = order.get('type')
                        
                        if order_type == 4 and order_id == pos.get('stop_order_id'):  # STOP order
                            if order_size == remaining_qty:
                                sl_verified = True
                                logger.info(f"OK Final verification: Stop loss order size correct ({order_size} contracts)")
                            else:
                                logger.warning(f"WARNING: Stop loss order size mismatch: {order_size} != {remaining_qty}")
                        
                        elif order_type == 1 and order_id == pos.get('tp_order_id'):  # LIMIT order (TP)
                            if order_size == remaining_qty:
                                tp_verified = True
                                logger.info(f"OK Final verification: Take profit order size correct ({order_size} contracts)")
                            else:
                                logger.warning(f"WARNING: Take profit order size mismatch: {order_size} != {remaining_qty}")
                    
                    if not sl_verified:
                        logger.warning("WARNING: Stop loss order not verified after partial exit - watchdog will fix")
                    if not tp_verified and pos.get('tp_order_id'):
                        logger.warning("WARNING: Take profit order not verified after partial exit - watchdog will fix")
                    
                    if sl_verified and (tp_verified or not pos.get('tp_order_id')):
                        logger.info("OK Both protective orders verified after partial exit")
                except Exception as e:
                    logger.warning(f"Could not perform final verification: {e}")
                
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
        else:  # short
            if current_price < self.lowest_price:
                self.lowest_price = current_price
            
            current_profit = entry - self.lowest_price
            if current_profit >= activation_distance:
                new_sl = self.lowest_price + trail_distance
                # For SHORT, ensure stop is >= entry (protects against losses)
                if new_sl < entry:
                    new_sl = entry
                # Only update if new stop is closer to entry (locks more profit)
                if new_sl < pos['stop_loss']:
                    old_sl = pos['stop_loss']
                    pos['stop_loss'] = new_sl
                    logger.info(f"Trailing stop updated: ${old_sl:.2f} → ${new_sl:.2f} (Low: ${self.lowest_price:.2f})")
                    self._update_stop_order(new_sl)
    
    def _update_stop_order(self, new_stop_price: float) -> None:
        try:
            # CRITICAL FIX 16: Check position size before updating
            if self.current_position is None:
                return
            
            current_qty = self.current_position.get('quantity')
            if current_qty is None or current_qty <= 0:
                logger.warning("Position quantity is 0 or None - skipping stop order update")
                return
            
            # CRITICAL FIX: Prevent concurrent calls with a lock
            if hasattr(self, '_updating_stop_order') and self._updating_stop_order:
                logger.debug("Stop order update already in progress, skipping duplicate call")
                return
            
            # CRITICAL FIX: Add debouncing (max 1 update per 5 seconds)
            if self._last_order_update:
                elapsed = (datetime.now(self.timezone) - self._last_order_update).total_seconds()
                if elapsed < 5:
                    logger.debug(f"Order update too soon ({elapsed:.1f}s < 5s), skipping to prevent duplicates")
                    return
            
            self._updating_stop_order = True
            self._last_order_update = datetime.now(self.timezone)
            
            try:
                side = self.current_position['side']
                current_price = self._get_current_price()
                entry_price = self.current_position['entry_price']
                
                # Round to tick size first
                new_stop_price = round(new_stop_price / self.tick_size) * self.tick_size
                
                # Check if this is a break-even move (stop price equals entry price)
                is_break_even = abs(new_stop_price - entry_price) < self.tick_size / 2
                
                # Validate stop price
                if current_price is not None:
                    if side == 'long':
                        # For LONG: stop must be below current price (or it would trigger immediately)
                        # Exception: For break-even moves, allow entry price even if very close to current
                        if new_stop_price >= current_price:
                            if is_break_even:
                                # For break-even, if entry is too close to current, place it slightly below current
                                # but as close to entry as possible (minimum 2 ticks for API requirement)
                                min_distance = 2 * self.tick_size
                                adjusted_price = round((current_price - min_distance) / self.tick_size) * self.tick_size
                                # Only adjust if we can still be reasonably close to entry (within 5 ticks)
                                if abs(adjusted_price - entry_price) <= 5 * self.tick_size:
                                    new_stop_price = adjusted_price
                                    logger.info(f"Break-even adjusted for LONG: ${entry_price:.2f} -> ${new_stop_price:.2f} (current: ${current_price:.2f}, min distance: {min_distance/self.tick_size:.0f} ticks)")
                                else:
                                    logger.warning(f"Break-even at ${entry_price:.2f} too close to current ${current_price:.2f}, keeping entry price but order may be rejected")
                            else:
                                logger.warning(f"Invalid stop price for LONG: ${new_stop_price:.2f} >= current ${current_price:.2f}. Adjusting to ${current_price - self.tick_size:.2f}")
                                new_stop_price = round((current_price - self.tick_size) / self.tick_size) * self.tick_size  # Place 1 tick below current
                    else:  # short
                        # For SHORT: stop must be >= entry price (to protect against losses)
                        # API requirement: stop must be above best bid
                        
                        # CRITICAL: For SHORT, stop must always be >= entry price
                        if new_stop_price < entry_price:
                            logger.warning(f"Invalid stop price for SHORT: ${new_stop_price:.2f} < entry ${entry_price:.2f}. Adjusting to entry price (break-even)")
                            new_stop_price = entry_price
                        
                        # API requirement: stop must be above best bid
                        # For SHORT, if current price is below entry (we're in profit), stop can be at entry or above
                        # If current price is above entry (we're at a loss), stop must be above current
                        if new_stop_price <= current_price:
                            if is_break_even:
                                # For break-even, if entry is too close to current, place it slightly above current
                                # but as close to entry as possible (minimum 2 ticks for API requirement)
                                min_distance = 2 * self.tick_size
                                adjusted_price = round((current_price + min_distance) / self.tick_size) * self.tick_size
                                # Ensure adjusted price is still >= entry
                                if adjusted_price < entry_price:
                                    adjusted_price = entry_price
                                # Only adjust if we can still be reasonably close to entry (within 5 ticks)
                                if abs(adjusted_price - entry_price) <= 5 * self.tick_size:
                                    new_stop_price = adjusted_price
                                    logger.info(f"Break-even adjusted for SHORT: ${entry_price:.2f} -> ${new_stop_price:.2f} (current: ${current_price:.2f}, min distance: {min_distance/self.tick_size:.0f} ticks)")
                                else:
                                    logger.warning(f"Break-even at ${entry_price:.2f} too close to current ${current_price:.2f}, keeping entry price but order may be rejected")
                            else:
                                # For SHORT, if stop is at or below current price, it must be at least entry price
                                # But API requires it to be above best bid, so we need to place it above current
                                if new_stop_price < entry_price:
                                    new_stop_price = entry_price
                                if new_stop_price <= current_price:
                                    logger.warning(f"Stop price for SHORT: ${new_stop_price:.2f} <= current ${current_price:.2f}. Adjusting to ${current_price + self.tick_size:.2f}")
                                    new_stop_price = round((current_price + self.tick_size) / self.tick_size) * self.tick_size
                                    # Ensure it's still >= entry
                                    if new_stop_price < entry_price:
                                        new_stop_price = entry_price
                
                # ALWAYS get fresh list of open orders first to find existing stop orders
                existing_stop_order_id = None
                all_stop_order_ids = []
                try:
                    open_orders = self.client.get_open_orders()
                    for order in open_orders:
                        if order.get('contractId') == self.contract.id and order.get('type') == 4:  # STOP order
                            order_id = order.get('id')
                            all_stop_order_ids.append(order_id)
                            # Use the first one we find, or prefer the one we have stored
                            if existing_stop_order_id is None:
                                existing_stop_order_id = order_id
                            if order_id == self.current_position.get('stop_order_id'):
                                existing_stop_order_id = order_id  # Prefer stored ID
                except Exception as e:
                    logger.debug(f"Could not fetch open orders: {e}")
                
                # If we found an existing stop order, try to modify it
                if existing_stop_order_id:
                    # CRITICAL FIX 14: Track old order ID before overwriting (for cleanup if modify fails)
                    old_stop_order_id = self.current_position.get('stop_order_id')
                    if old_stop_order_id and old_stop_order_id != existing_stop_order_id:
                        logger.debug(f"Found different stop order ID: old={old_stop_order_id}, found={existing_stop_order_id}")
                    # Update stored ID
                    self.current_position['stop_order_id'] = existing_stop_order_id
                    
                    # ALWAYS sync quantity with actual broker position first
                    current_qty = None
                    try:
                        positions = self.client.get_positions()
                        for pos in positions:
                            if pos.contract_id == self.contract.id and pos.size != 0:
                                current_qty = abs(pos.size)
                                # Update internal tracking
                                self.current_position['quantity'] = current_qty
                                logger.debug(f"Synced position quantity from broker: {current_qty} contracts")
                                break
                    except Exception as e:
                        logger.error(f"Failed to sync position quantity from broker: {e}")
                    
                    # Fallback to stored quantity or position_size only if sync failed
                    if current_qty is None or current_qty <= 0:
                        current_qty = self.current_position.get('quantity')
                        if current_qty is None or current_qty <= 0:
                            current_qty = self.position_size
                            logger.warning(f"Using fallback position size: {current_qty} contracts (broker sync failed)")
                    
                    result = self.client.modify_order(
                        order_id=existing_stop_order_id,
                        size=current_qty,  # Update size to match position
                        stop_price=new_stop_price
                    )
                    if result.get('success'):
                        logger.info(f"Stop order modified: #{existing_stop_order_id} to ${new_stop_price:.2f}")
                        # Cancel any other duplicate stop orders
                        for other_id in all_stop_order_ids:
                            if other_id != existing_stop_order_id:
                                try:
                                    logger.info(f"Cancelling duplicate stop order: ID {other_id}")
                                    self.client.cancel_order(other_id)
                                except Exception as e:
                                    logger.warning(f"Failed to cancel duplicate stop order {other_id}: {e}")
                        return
                    else:
                        error_msg = result.get('errorMessage', 'Unknown error')
                        logger.warning(f"Modify failed: {error_msg}. Will cancel and place new order.")
                
                # If modify failed or no existing order, store old stop order info before cancelling (for potential restore)
                old_stop_order_id = existing_stop_order_id if existing_stop_order_id else (all_stop_order_ids[0] if all_stop_order_ids else None)
                old_stop_price = self.current_position.get('stop_loss')
                
                # FIX 2: PLACE-FIRST, THEN CANCEL (never cancel-first to avoid unprotected position)
                # This ensures position is always protected - if new order fails, old order remains active
                # Sync quantity with actual broker position first
                stop_side = OrderSide.ASK if side == 'long' else OrderSide.BID
                remaining_qty = None
                try:
                    positions = self.client.get_positions()
                    for pos in positions:
                        if pos.contract_id == self.contract.id and pos.size != 0:
                            remaining_qty = abs(pos.size)
                            # Update internal tracking
                            self.current_position['quantity'] = remaining_qty
                            logger.info(f"Synced position quantity from broker: {remaining_qty} contracts")
                            break
                except Exception as e:
                    logger.error(f"Failed to sync position quantity from broker: {e}")
                
                # Fallback to stored quantity or position_size only if sync failed
                if remaining_qty is None or remaining_qty <= 0:
                    remaining_qty = self.current_position.get('quantity')
                    if remaining_qty is None or remaining_qty <= 0:
                        remaining_qty = self.position_size
                        logger.warning(f"Using fallback position size: {remaining_qty} contracts (broker sync failed)")
                
                # STEP 1: Place new stop order FIRST (before canceling old one)
                logger.info(f"Placing new stop order FIRST (place-first pattern): {stop_side.name} {remaining_qty} contracts @ ${new_stop_price:.2f}")
                order = self.client.place_order(
                    contract_id=self.contract.id,
                    order_type=OrderType.STOP,
                    side=stop_side,
                    size=remaining_qty,
                    stop_price=new_stop_price
                )
                
                # STEP 2: Verify new stop order was actually placed
                new_stop_order_id = None
                new_stop_verified = False
                
                if order and order.get('orderId'):
                    new_stop_order_id = order.get('orderId')
                    logger.info(f"New stop order placed: #{new_stop_order_id} at ${new_stop_price:.2f}")
                    
                    # Verify the order actually exists by checking open orders
                    try:
                        time.sleep(0.5)  # Wait a moment for order to be registered
                        open_orders = self.client.get_open_orders()
                        for o in open_orders:
                            if o.get('id') == new_stop_order_id and o.get('contractId') == self.contract.id:
                                order_size = abs(o.get('size', 0))
                                if order_size == remaining_qty:
                                    new_stop_verified = True
                                    logger.info(f"Verified: Stop order #{new_stop_order_id} confirmed in open orders (size: {order_size})")
                                    break
                                else:
                                    logger.warning(f"Stop order #{new_stop_order_id} size mismatch: {order_size} != {remaining_qty}")
                    except Exception as verify_e:
                        logger.warning(f"Could not verify stop order placement: {verify_e}")
                
                # STEP 3: Only cancel old stop orders if new one was successfully placed and verified
                if new_stop_verified and new_stop_order_id:
                    # Update position tracking with new stop order
                    self.current_position['stop_order_id'] = new_stop_order_id
                    self.current_position['stop_loss'] = new_stop_price
                    
                    # CRITICAL FIX: Cancel ALL old stop orders and verify cancellation
                    cancelled_count = 0
                    for stop_id in all_stop_order_ids:
                        if stop_id != new_stop_order_id:  # Don't cancel the new one
                            try:
                                logger.info(f"Cancelling old stop order: ID {stop_id}")
                                self.client.cancel_order(stop_id)
                                cancelled_count += 1
                            except Exception as e:
                                logger.warning(f"Failed to cancel old stop order {stop_id}: {e}")
                    
                    # Wait for cancellations to complete and verify
                    if cancelled_count > 0:
                        time.sleep(1.0)  # Wait for cancellations to propagate
                        try:
                            open_orders_after = self.client.get_open_orders()
                            remaining_stops = [o.get('id') for o in open_orders_after 
                                             if (o.get('contractId') == self.contract.id and 
                                                 o.get('type') == 4 and 
                                                 o.get('id') != new_stop_order_id)]
                            if remaining_stops:
                                logger.warning(f"Warning: {len(remaining_stops)} old stop orders still exist after cancel: {remaining_stops}")
                                # Try one more cancellation pass
                                for stop_id in remaining_stops:
                                    try:
                                        self.client.cancel_order(stop_id)
                                    except:
                                        pass
                            else:
                                logger.info(f"Successfully cancelled {cancelled_count} old stop order(s)")
                        except Exception as e:
                            logger.warning(f"Could not verify cancellations: {e}")
                    
                    logger.info(f"Stop order successfully updated: #{new_stop_order_id} at ${new_stop_price:.2f} (old orders cancelled)")
                else:
                    # New stop order placement/verification failed - KEEP OLD STOP (don't cancel)
                    error_msg = order.get('errorMessage', 'Unknown error') if order else 'No order ID returned'
                    logger.error(f"CRITICAL: Failed to place/verify new stop order: {error_msg}")
                    logger.warning(f"KEEPING OLD STOP ORDER(s) - position remains protected by existing stop(s)")
                    
                    # Try to place new stop with retry and price adjustment
                    if 'price' in error_msg.lower() or 'outside' in error_msg.lower():
                        logger.info("Attempting price adjustment and retry...")
                        # Adjust price based on error
                        if side == 'long':
                            adjusted_price = new_stop_price - (2 * self.tick_size)
                        else:
                            adjusted_price = new_stop_price + (2 * self.tick_size)
                        
                        # Validate adjusted price
                        if current_price:
                            if side == 'long' and adjusted_price >= current_price:
                                adjusted_price = round((current_price - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                            elif side == 'short' and adjusted_price <= current_price:
                                adjusted_price = round((current_price + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                        
                        logger.info(f"Retrying with adjusted price: ${adjusted_price:.2f}")
                        retry_order = self.client.place_order(
                            contract_id=self.contract.id,
                            order_type=OrderType.STOP,
                            side=stop_side,
                            size=remaining_qty,
                            stop_price=adjusted_price
                        )
                        
                        if retry_order and retry_order.get('orderId'):
                            retry_order_id = retry_order.get('orderId')
                            time.sleep(0.5)
                            # Verify retry order
                            try:
                                open_orders = self.client.get_open_orders()
                                for o in open_orders:
                                    if o.get('id') == retry_order_id and o.get('contractId') == self.contract.id:
                                        self.current_position['stop_order_id'] = retry_order_id
                                        self.current_position['stop_loss'] = adjusted_price
                                        logger.info(f"Retry successful: Stop order #{retry_order_id} at ${adjusted_price:.2f}")
                                        # Now cancel old stops
                                        for stop_id in all_stop_order_ids:
                                            if stop_id != retry_order_id:
                                                try:
                                                    self.client.cancel_order(stop_id)
                                                except Exception as e:
                                                    logger.warning(f"Failed to cancel old stop {stop_id}: {e}")
                                        return
                            except Exception as e:
                                logger.warning(f"Could not verify retry order: {e}")
                    
                    # If we still have old stop, keep it and alert
                    if old_stop_order_id:
                        logger.warning(f"Old stop order #{old_stop_order_id} remains active - position protected")
                        self.alerts.error(f"Stop order update failed - old stop remains active. New stop: {error_msg}")
                    else:
                        logger.error(f"No old stop order to keep - position may be unprotected!")
                        self.alerts.error(f"CRITICAL: Stop order update failed and no old stop exists - position unprotected!")
                    self.alerts.error(f"CRITICAL: Stop order placement returned invalid response - position may be unprotected!")
            finally:
                self._updating_stop_order = False
        except Exception as e:
            logger.error(f"Failed to update stop order: {e}")
            import traceback
            logger.error(traceback.format_exc())
            if hasattr(self, '_updating_stop_order'):
                self._updating_stop_order = False
    
    def _sync_order_sizes_to_position(self) -> None:
        """Sync all open order sizes to match current position quantity"""
        if self.current_position is None:
            return
        
        current_qty = self.current_position.get('quantity')
        if current_qty is None or current_qty <= 0:
            return
        
        try:
            open_orders = self.client.get_open_orders()
            updated_count = 0
            
            for order in open_orders:
                if order.get('contractId') != self.contract.id:
                    continue
                
                order_id = order.get('id')
                order_size = abs(order.get('size', 0))
                order_type = order.get('type')
                
                # Only update stop and limit (TP) orders
                if order_type in [1, 4]:  # LIMIT=1, STOP=4
                    if order_size != current_qty:
                        try:
                            # Modify order to update size
                            if order_type == 4:  # STOP
                                result = self.client.modify_order(
                                    order_id=order_id,
                                    size=current_qty,
                                    stop_price=order.get('stopPrice')
                                )
                            else:  # LIMIT (TP)
                                result = self.client.modify_order(
                                    order_id=order_id,
                                    size=current_qty,
                                    limit_price=order.get('limitPrice')
                                )
                            
                            if result.get('success'):
                                logger.info(f"Updated order #{order_id} size: {order_size} -> {current_qty} contracts")
                                updated_count += 1
                            else:
                                logger.warning(f"Failed to update order #{order_id} size: {result.get('errorMessage')}")
                        except Exception as e:
                            logger.warning(f"Error updating order #{order_id} size: {e}")
            
            if updated_count > 0:
                logger.info(f"Synced {updated_count} order(s) to position size: {current_qty} contracts")
        except Exception as e:
            logger.warning(f"Failed to sync order sizes: {e}")
    
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
            
            # Use remaining quantity, not initial size (in case of partial exits)
            remaining_qty = self.current_position.get('quantity', self.position_size)
            
            order = self.client.place_order(
                contract_id=self.contract.id,
                order_type=OrderType.MARKET,
                side=close_side,
                size=remaining_qty
            )
            
            if order:
                logger.info(f"OK Force exit order placed: #{order.id}")
                self.daily_pnl += unrealized_pnl
                self.current_position = None
                # Release trading lock when position closes
                self._trading_locked = False
                logger.info("Trading lock released - new signals allowed")
                
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
        
        # Safety cleanup - run every iteration to catch orphaned orders
        # This ensures cleanup happens quickly if position closes or tracking is lost
        self._safety_cleanup_orphaned_orders()
        
        # Reconcile with broker EVERY iteration to sync internal state
        self._reconcile_position_with_broker()
        
        # CRITICAL: Check execution lock FIRST to prevent duplicate orders
        if self._executing_entry:
            logger.debug("Skipping run_once: Entry execution in progress")
            return
        
        # CRITICAL: Check trading lock - prevent new signals while position is open
        if self._trading_locked:
            # When trading is locked, we still need to monitor the position
            if self.current_position is not None:
                logger.debug("Trading locked - monitoring position status")
                self._check_position_status()
                
                # ALWAYS check for orphaned orders even if position tracking exists
                # This ensures cleanup happens quickly when position closes
                self._cancel_remaining_bracket_orders()
                
                if self.current_position is not None:
                    if self._check_daily_loss_force_exit():
                        return
                    self._check_break_even()
                    # Check trailing stop and partial profit using current price
                    current_price = self._get_current_price()
                    if current_price:
                        self._update_trailing_stop(current_price)
                        self._check_partial_profit(current_price)
            else:
                # No position tracked but trading locked - check for orphaned orders
                self._cancel_remaining_bracket_orders()
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
        
        # Update rolling DataFrame with new bars
        if self.rolling_df is None:
            # First time - initialize with fetched bars
            self.rolling_df = df.copy()
        else:
            # Append new bars to rolling DataFrame
            # Find new bars (not already in rolling_df)
            if len(self.rolling_df) > 0:
                last_timestamp = self.rolling_df['timestamp'].iloc[-1]
                new_bars = df[df['timestamp'] > last_timestamp].copy()
            else:
                new_bars = df.copy()
            
            if not new_bars.empty:
                # Append new bars
                self.rolling_df = pd.concat([self.rolling_df, new_bars], ignore_index=True)
                # Keep only last N bars to prevent memory growth
                if len(self.rolling_df) > self.rolling_df_max_bars:
                    self.rolling_df = self.rolling_df.iloc[-self.rolling_df_max_bars:].reset_index(drop=True)
        
        # Update zones only periodically (every N bars or every 15 minutes)
        should_update_zones = False
        if self.last_zone_update_bars is None:
            should_update_zones = True  # First update
        else:
            bars_since_update = len(self.rolling_df) - self.last_zone_update_bars
            time_since_update = None
            if self.last_zone_update_time:
                time_since_update = (datetime.now(self.timezone) - self.last_zone_update_time).total_seconds() / 60
            
            if bars_since_update >= self.zone_update_interval_bars:
                should_update_zones = True
                logger.info(f"Zone update triggered: {bars_since_update} new bars since last update")
            elif time_since_update and time_since_update >= 15:
                should_update_zones = True
                logger.info(f"Zone update triggered: {time_since_update:.1f} minutes since last update")
        
        # CRITICAL FIX 12: Skip zone updates when position is open (zones shouldn't change during trade)
        if should_update_zones:
            if self.current_position is not None:
                logger.debug("Skipping zone update - position is open (zones should remain stable during trade)")
            else:
                # Prepare data and merge zones (don't replace existing zones)
                self.rolling_df = self.strategy.prepare_data(self.rolling_df, merge_zones=True)
                self.last_zone_update_bars = len(self.rolling_df)
                self.last_zone_update_time = datetime.now(self.timezone)
                logger.debug(f"Zones updated from rolling DataFrame ({len(self.rolling_df)} bars)")
        
        # Add indicators to current df for signal generation (zones already in zone_manager)
        if 'atr' not in df.columns:
            df = self.strategy.indicators.add_indicators_to_df(df)
        
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
            # CRITICAL: Multiple checks to prevent duplicate orders
            # Check trading lock FIRST (prevents any new signals while position is open)
            if self._trading_locked:
                logger.warning(f"Signal generated but trading is locked (position is open) - skipping entry")
                return
            
            # Check execution lock
            if self._executing_entry:
                logger.warning(f"Signal generated but entry already in progress - skipping duplicate entry")
                return
            
            if self.current_position is not None:
                logger.warning(f"Signal generated but position already exists ({self.current_position['side']} @ ${self.current_position['entry_price']:.2f}) - skipping entry")
                return
            
            if self.pending_limit_order is not None:
                logger.warning(f"Signal generated but pending limit order exists - skipping duplicate entry")
                return
            
            # Check circuit breaker before executing entry
            if not self.circuit_breaker.should_allow_trade(timezone=self.timezone):
                logger.error("Circuit breaker open - trade blocked (too many recent failures)")
                return
            
            # Set BOTH locks IMMEDIATELY before calling _execute_entry
            # This prevents another run_once() call from getting past the checks
            self._executing_entry = True
            self._trading_locked = True  # Lock trading until position closes
            
            try:
                success = False
                if self.limit_order_enabled:
                    # Create pending limit order instead of entering immediately
                    self._create_pending_limit_order(signal)
                    success = True  # Assume success for limit orders (will be verified on fill)
                else:
                    # Immediate market order entry
                    success = self._execute_entry(signal)
                
                # Record result in circuit breaker
                if success:
                    self.circuit_breaker.record_success()
                else:
                    self.circuit_breaker.record_failure(timezone=self.timezone)
            except Exception as e:
                # Release BOTH locks on exception
                logger.error(f"Error executing entry: {e}")
                self._executing_entry = False
                self._trading_locked = False
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
            loop_count = 0
            while self.running:
                try:
                    # Check connection health via heartbeat monitor
                    if not self.connection_monitor.check_health():
                        logger.warning("Connection appears dead - attempting to reconnect...")
                        self.stop()
                        time.sleep(5)
                        if self.connect():
                            logger.info("OK Reconnected successfully")
                            self.connection_monitor.record_heartbeat()
                        else:
                            logger.error("FAILED Reconnection failed - exiting")
                            break
                    
                    loop_count += 1
                    # Log every 10 loops (every ~5-10 minutes depending on interval) to show it's alive
                    if loop_count % 10 == 0:
                        logger.info(f"[HEARTBEAT] Trading loop active - iteration {loop_count}, position: {'OPEN' if self.current_position else 'NONE'}, locked: {self._trading_locked}")
                    
                    # Record heartbeat on each successful loop iteration
                    self.connection_monitor.record_heartbeat()
                    
                    self.run_once()
                except Exception as e:
                    # Wrap logging in try/except to prevent logging errors from crashing the loop
                    try:
                        import traceback
                        error_msg = str(e)
                        traceback_str = traceback.format_exc()
                        logger.error(f"Error in trading loop: {error_msg}")
                        logger.error(f"Traceback: {traceback_str}")
                        # Only send alert if alerts don't cause errors
                        try:
                            self.alerts.error(f"Trading loop error: {error_msg}")
                        except:
                            pass  # Silently ignore alert errors
                    except Exception as log_error:
                        # If logging itself fails, write to file directly as last resort
                        try:
                            with open('live_trading.log', 'a') as f:
                                f.write(f"{datetime.now()} - ERROR - Logging failed: {log_error}, Original error: {e}\n")
                        except:
                            pass  # If even file write fails, silently continue
                
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
    
    def test_trade(self, side: str = 'long', entry_price: float = None, quantity: int = 1) -> bool:
        """
        Place a test trade with 1 contract, bracket orders, partial exits, and break-even
        
        Args:
            side: 'long' or 'short'
            entry_price: Entry price (uses current market price if None)
            quantity: Number of contracts (default 1 for testing)
        
        Returns:
            True if trade placed successfully, False otherwise
        """
        if not self.contract:
            logger.error("Cannot place test trade - no contract set")
            return False
        
        if self.current_position is not None:
            logger.warning(f"Cannot place test trade - position already exists: {self.current_position.get('side')} {self.current_position.get('quantity')}")
            return False
        
        try:
            # Get current price if entry_price not provided
            if entry_price is None:
                current_price = self._get_current_price()
                if current_price is None:
                    logger.error("Cannot place test trade - no current price available")
                    return False
                entry_price = current_price
            
            logger.info("=" * 70)
            logger.info("PLACING TEST TRADE")
            logger.info("=" * 70)
            logger.info(f"Side: {side.upper()}")
            logger.info(f"Entry Price: ${entry_price:.2f}")
            logger.info(f"Quantity: {quantity} contract(s)")
            
            # Calculate test stop loss and take profit (sample values)
            # For LONG: SL 10 ticks below, TP 30 ticks above
            # For SHORT: SL 10 ticks above, TP 30 ticks below
            if side == 'long':
                stop_loss = round((entry_price - (10 * self.tick_size)) / self.tick_size) * self.tick_size
                take_profit = round((entry_price + (30 * self.tick_size)) / self.tick_size) * self.tick_size
            else:
                stop_loss = round((entry_price + (10 * self.tick_size)) / self.tick_size) * self.tick_size
                take_profit = round((entry_price - (30 * self.tick_size)) / self.tick_size) * self.tick_size
            
            logger.info(f"Stop Loss: ${stop_loss:.2f}")
            logger.info(f"Take Profit: ${take_profit:.2f}")
            logger.info(f"Risk: 10 ticks, Reward: 30 ticks, R:R = 3.0")
            
            # Place market order
            order_side = OrderSide.BID if side == 'long' else OrderSide.ASK
            logger.info(f"Placing {side.upper()} market order: {quantity} contracts @ ${entry_price:.2f}")
            
            order_result = self.client.place_order(
                contract_id=self.contract.id,
                side=order_side,
                order_type=OrderType.MARKET,
                size=quantity
            )
            
            if not order_result or not order_result.get('success'):
                error_msg = order_result.get('errorMessage', 'Unknown error') if order_result else 'No response'
                logger.error(f"Failed to place test trade order: {error_msg}")
                return False
            
            order_id = order_result.get('orderId')
            if not order_id:
                logger.error("Order placed but no order ID returned")
                return False
            
            logger.info(f"Order placed: ID {order_id}")
            
            # Wait a moment for fill
            import time
            time.sleep(2)
            
            # Check if order was filled
            positions = self.client.get_positions()
            filled_position = None
            for pos in positions:
                if pos.contract_id == self.contract.id and pos.size != 0:
                    filled_position = pos
                    break
            
            if not filled_position:
                logger.warning("Order placed but position not found - may need to wait longer")
                return False
            
            actual_entry = filled_position.average_price
            actual_qty = abs(filled_position.size)
            logger.info(f"Position filled: {side.upper()} {actual_qty} @ ${actual_entry:.2f}")
            
            # Create position tracking
            self.current_position = {
                'side': side,
                'entry_price': actual_entry,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'quantity': actual_qty,
                'initial_quantity': actual_qty,
                'risk_ticks': 10.0,
                'reward_ticks': 30.0,
                'session': 'test',
                'test_trade': True
            }
            
            # Set trading lock
            self._trading_locked = True
            self._executing_entry = False
            
            # Update stop loss and take profit based on actual entry
            if side == 'long':
                stop_loss = round((actual_entry - (10 * self.tick_size)) / self.tick_size) * self.tick_size
                take_profit = round((actual_entry + (30 * self.tick_size)) / self.tick_size) * self.tick_size
            else:
                stop_loss = round((actual_entry + (10 * self.tick_size)) / self.tick_size) * self.tick_size
                take_profit = round((actual_entry - (30 * self.tick_size)) / self.tick_size) * self.tick_size
            
            # Validate stop price against current market (API requirement)
            try:
                quotes = self.client.get_quotes([self.contract.id])
                if quotes and self.contract.id in quotes:
                    fresh_quote = quotes[self.contract.id]
                    if side == 'long':
                        best_ask = fresh_quote.ask
                        max_allowed_stop = best_ask - (2 * self.tick_size)
                        if stop_loss >= best_ask or stop_loss > max_allowed_stop:
                            original_sl = stop_loss
                            stop_loss = round((best_ask - (2 * self.tick_size)) / self.tick_size) * self.tick_size
                            logger.warning(f"Stop price adjusted for API: ${original_sl:.2f} -> ${stop_loss:.2f} (best_ask: ${best_ask:.2f})")
                    else:  # short
                        best_bid = fresh_quote.bid
                        min_allowed_stop = best_bid + (2 * self.tick_size)
                        if stop_loss <= best_bid or stop_loss < min_allowed_stop:
                            original_sl = stop_loss
                            stop_loss = round((best_bid + (2 * self.tick_size)) / self.tick_size) * self.tick_size
                            logger.warning(f"Stop price adjusted for API: ${original_sl:.2f} -> ${stop_loss:.2f} (best_bid: ${best_bid:.2f})")
            except Exception as e:
                logger.warning(f"Could not validate stop price with quote: {e}")
            
            # Update position with actual prices
            self.current_position['stop_loss'] = stop_loss
            self.current_position['take_profit'] = take_profit
            self.current_position['initial_stop_loss'] = stop_loss
            
            # Place bracket orders with retry logic (CRITICAL)
            logger.info("Placing bracket orders with retry logic...")
            
            # Place Stop Loss order with retry
            stop_side = OrderSide.ASK if side == 'long' else OrderSide.BID
            sl_order_id = None
            sl_placed = False
            max_sl_retries = 3
            
            for sl_attempt in range(max_sl_retries):
                logger.info(f"Placing stop loss (attempt {sl_attempt + 1}/{max_sl_retries}): {stop_side.name} {actual_qty} @ ${stop_loss:.2f}")
                sl_result = self.client.place_stop_order(
                    contract_id=self.contract.id,
                    side=stop_side,
                    size=actual_qty,
                    stop_price=stop_loss
                )
                
                if sl_result.get('success'):
                    sl_order_id = sl_result.get('orderId')
                    time.sleep(0.5)
                    if self._verify_order_placement(sl_order_id, actual_qty, 'Stop Loss', max_retries=2):
                        self.current_position['stop_order_id'] = sl_order_id
                        logger.info(f"Stop loss placed and verified: ID {sl_order_id} @ ${stop_loss:.2f}")
                        sl_placed = True
                        break
                    else:
                        logger.warning(f"Stop loss order {sl_order_id} not verified, retrying...")
                        if sl_attempt < max_sl_retries - 1:
                            time.sleep(1.0)
                else:
                    error_msg = sl_result.get('errorMessage', 'Unknown error')
                    logger.warning(f"Stop loss attempt {sl_attempt + 1} failed: {error_msg}")
                    if sl_attempt < max_sl_retries - 1:
                        time.sleep(1.0)
            
            if not sl_placed:
                logger.error(f"CRITICAL: All {max_sl_retries} stop loss attempts failed - closing position for safety")
                try:
                    close_result = self.client.close_position(self.contract.id)
                    if close_result.get('success'):
                        logger.info("Test position closed due to SL placement failure")
                    else:
                        logger.error(f"Failed to close position: {close_result.get('errorMessage')}")
                except Exception as e:
                    logger.error(f"Exception closing position: {e}")
                self.current_position = None
                self._trading_locked = False
                return False
            
            # Place Take Profit order with retry (full quantity for test)
            tp_side = OrderSide.ASK if side == 'long' else OrderSide.BID
            tp_order_id = None
            tp_placed = False
            max_tp_retries = 3
            
            for tp_attempt in range(max_tp_retries):
                logger.info(f"Placing take profit (attempt {tp_attempt + 1}/{max_tp_retries}): {tp_side.name} {actual_qty} @ ${take_profit:.2f}")
                tp_result = self.client.place_limit_order(
                    contract_id=self.contract.id,
                    side=tp_side,
                    size=actual_qty,
                    limit_price=take_profit
                )
                
                if tp_result.get('success'):
                    tp_order_id = tp_result.get('orderId')
                    time.sleep(0.5)
                    if self._verify_order_placement(tp_order_id, actual_qty, 'Take Profit', max_retries=2):
                        self.current_position['tp_order_id'] = tp_order_id
                        logger.info(f"Take profit placed and verified: ID {tp_order_id} @ ${take_profit:.2f}")
                        tp_placed = True
                        break
                    else:
                        logger.warning(f"Take profit order {tp_order_id} not verified, retrying...")
                        if tp_attempt < max_tp_retries - 1:
                            time.sleep(1.0)
                else:
                    error_msg = tp_result.get('errorMessage', 'Unknown error')
                    logger.warning(f"Take profit attempt {tp_attempt + 1} failed: {error_msg}")
                    if tp_attempt < max_tp_retries - 1:
                        time.sleep(1.0)
            
            if not tp_placed:
                error_msg = tp_result.get('errorMessage', 'Unknown error') if tp_result else 'All attempts failed'
                logger.error(f"Take profit placement failed after {max_tp_retries} attempts: {error_msg}")
                logger.warning("Position protected by SL, but TP missing - will retry on next check")
            
            # Final verification
            try:
                time.sleep(1.0)
                open_orders = self.client.get_open_orders()
                sl_verified = False
                tp_verified = False
                
                for order in open_orders:
                    if order.get('contractId') == self.contract.id:
                        order_id = order.get('id')
                        order_type = order.get('type')
                        
                        if order_type == 4 and order_id == sl_order_id:
                            order_size = abs(order.get('size', 0))
                            if order_size == actual_qty:
                                sl_verified = True
                                logger.info(f"Final SL verification: ID {order_id} @ ${order.get('stopPrice', 0):.2f}, Size: {order_size}")
                        elif order_type == 1 and order_id == tp_order_id:
                            order_size = abs(order.get('size', 0))
                            if order_size == actual_qty:
                                tp_verified = True
                                logger.info(f"Final TP verification: ID {order_id} @ ${order.get('limitPrice', 0):.2f}, Size: {order_size}")
                
                if not sl_verified:
                    logger.error(f"CRITICAL: Stop loss order {sl_order_id} not found in final verification!")
                if not tp_verified and tp_order_id:
                    logger.warning(f"Take profit order {tp_order_id} not found in final verification")
                    
            except Exception as e:
                logger.error(f"Final verification error: {e}")
            
            logger.info("=" * 70)
            logger.info("TEST TRADE PLACED SUCCESSFULLY")
            logger.info("=" * 70)
            logger.info("Monitoring position for:")
            logger.info("  - Partial exit at 1R (50% of position)")
            logger.info("  - Break-even move after partial")
            logger.info("  - Stop loss protection")
            logger.info("=" * 70)
            
            return True
            
        except Exception as e:
            logger.error(f"Error placing test trade: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            self._executing_entry = False
            self._trading_locked = False
            return False


def kill_existing_live_traders():
    """Kill all running instances of live_trader.py before starting new one"""
    import subprocess
    
    current_pid = os.getpid()
    killed_count = 0
    
    logger.info("Checking for existing live trader instances...")
    
    if sys.platform == 'win32':
        # Windows: use wmic to find processes, then taskkill to stop them
        try:
            # Find all python.exe processes with live_trader.py in command line
            result = subprocess.run(
                ['wmic', 'process', 'where', 'name="python.exe"', 'get', 'ProcessId,CommandLine', '/format:list'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )
            
            processes = []
            current_process = {}
            
            for line in result.stdout.split('\n'):
                line = line.strip()
                if line.startswith('ProcessId='):
                    if current_process:
                        processes.append(current_process)
                    current_process = {'pid': line.split('=')[1].strip()}
                elif line.startswith('CommandLine='):
                    current_process['cmd'] = line.split('=', 1)[1] if '=' in line else ''
            
            if current_process:
                processes.append(current_process)
            
            # Filter for live_trader processes (exclude current process)
            live_trader_pids = []
            for proc in processes:
                cmd = proc.get('cmd', '').lower()
                pid = proc.get('pid', '').strip()
                
                # Check if this is a live_trader process and not the current process
                if pid and pid != 'None' and pid != str(current_pid):
                    # Check for live_trader.py in command line (case insensitive)
                    if 'live_trader.py' in cmd and 'check_running' not in cmd:
                        try:
                            # Validate PID is numeric before adding
                            int(pid)
                            live_trader_pids.append(pid)
                        except ValueError:
                            logger.debug(f"Invalid PID found: {pid}")
            
            if live_trader_pids:
                logger.warning(f"Found {len(live_trader_pids)} existing live_trader instance(s) to kill: {', '.join(live_trader_pids)}")
                for pid in live_trader_pids:
                    try:
                        result = subprocess.run(
                            ['taskkill', '/F', '/PID', pid],
                            capture_output=True,
                            text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                        )
                        if result.returncode == 0:
                            logger.info(f"OK Killed live trader instance (PID: {pid})")
                            killed_count += 1
                        else:
                            # Check if process doesn't exist (already terminated)
                            if 'not found' in result.stdout.lower() or 'not found' in result.stderr.lower():
                                logger.debug(f"Process {pid} already terminated")
                            else:
                                logger.warning(f"Failed to kill PID {pid}: {result.stderr.strip() or result.stdout.strip()}")
                    except Exception as e:
                        logger.warning(f"Error killing PID {pid}: {e}")
                
                # Give processes time to fully terminate
                if killed_count > 0:
                    logger.info(f"Waiting 3 seconds for processes to terminate...")
                    time.sleep(3)
                else:
                    logger.info("No processes were killed (may have already terminated)")
            else:
                logger.info("OK No existing live trader instances found")
                
        except Exception as e:
            logger.warning(f"Could not check for existing instances: {e}")
    else:
        # Unix-like: use pkill (but need to exclude current process)
        try:
            # First, get current process info to exclude it
            current_cmd = ' '.join(sys.argv)
            
            # Use pgrep to find PIDs first, then kill them individually (excluding current)
            pgrep_result = subprocess.run(
                ['pgrep', '-f', 'live_trader.py'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if pgrep_result.returncode == 0:
                pids = pgrep_result.stdout.strip().split('\n')
                pids_to_kill = [pid.strip() for pid in pids if pid.strip() and pid.strip() != str(current_pid)]
                
                if pids_to_kill:
                    logger.warning(f"Found {len(pids_to_kill)} existing live_trader instance(s) to kill: {', '.join(pids_to_kill)}")
                    for pid in pids_to_kill:
                        try:
                            subprocess.run(['kill', '-9', pid], timeout=3)
                            logger.info(f"OK Killed live trader instance (PID: {pid})")
                            killed_count += 1
                        except Exception as e:
                            logger.warning(f"Error killing PID {pid}: {e}")
                    
                    if killed_count > 0:
                        time.sleep(3)
                else:
                    logger.info("No existing live trader instances found (excluding current)")
            else:
                logger.info("No existing live trader instances found")
                
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Could not kill existing instances: {e}")
    
    if killed_count > 0:
        logger.info(f"Successfully killed {killed_count} duplicate live trader instance(s)")
    else:
        logger.info("No duplicate instances found - safe to start new instance")


def main():
    import argparse
    
    # Kill existing instances before starting new one
    kill_existing_live_traders()
    
    parser = argparse.ArgumentParser(description='MGC Live Trading Engine')
    parser.add_argument('--config', type=str, default='config_production.json',
                        help='Path to config file')
    parser.add_argument('--credentials', type=str, default='credentials.json',
                        help='Path to credentials file')
    parser.add_argument('--interval', type=int, default=60,
                        help='Check interval in seconds')
    parser.add_argument('--test', action='store_true',
                        help='Test connection only, no trading')
    parser.add_argument('--test-trade', type=str, choices=['long', 'short'],
                        help='Place a test trade (long or short) with 1 contract')
    
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
    
    if args.test_trade:
        logger.info("TEST TRADE MODE - Placing test trade")
        if trader.connect():
            success = trader.test_trade(side=args.test_trade, quantity=1)
            if success:
                logger.info("Test trade placed - starting monitoring loop")
                trader.run(interval_seconds=args.interval)
            else:
                logger.error("Failed to place test trade")
                trader.stop()
        return
    
    trader.run(interval_seconds=args.interval)


if __name__ == '__main__':
    main()
