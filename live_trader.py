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

# Add exactly one file handler and one stream handler
file_handler = logging.FileHandler('live_trading.log')
file_handler.setFormatter(formatter)
root_logger.addHandler(file_handler)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
root_logger.addHandler(stream_handler)

# Set level
root_logger.setLevel(logging.INFO)

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
        self.daily_loss_limit = self.config.get('daily_loss_limit', -2500)
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
        self._trading_locked = False  # Lock to prevent new signals/trades while position is open
        
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
        
        # Replay mode: save bars when signals are generated
        self.save_replay_data = True  # Configurable flag
        self.replay_data_dir = Path('replay_data')  # Separate folder for replay files
        self.replay_data_dir.mkdir(exist_ok=True)
        self.max_replay_files = 200  # Keep only last 200 replay files
        
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
            # Release trading lock when position closes
            self._trading_locked = False
            logger.info("Trading lock released - new signals allowed")
    
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
            
            # Exclude last bar if potentially incomplete (less than 1.5 bar intervals old)
            if len(df) > 0:
                last_bar_time = pd.to_datetime(df['timestamp'].iloc[-1])
                if last_bar_time.tzinfo is None:
                    last_bar_time = pytz.UTC.localize(last_bar_time)
                else:
                    last_bar_time = last_bar_time.astimezone(pytz.UTC)
                
                time_since_last_bar = (now - last_bar_time).total_seconds()
                min_bar_age = bar_interval_seconds * 1.5  # 270 seconds for 3-min bars
                
                if time_since_last_bar < min_bar_age:
                    logger.info(f"Excluding potentially incomplete bar: {time_since_last_bar:.0f}s old (< {min_bar_age:.0f}s threshold)")
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
        
        # Check if bar is stale (use bar timestamp, not current time, for session detection)
        now_utc = pd.Timestamp.now(tz=pytz.UTC)
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
                self._executing_entry = False  # Release execution lock
                self._trading_locked = False  # Release trading lock
                return False
            
            order_id = result.get('orderId')
            
            # Update position with order ID and remove pending flag
            self.current_position['order_id'] = order_id
            self.current_position.pop('pending', None)
            
            self.highest_price = signal['entry_price']
            self.lowest_price = signal['entry_price']
            
            self.daily_trades += 1
            
            logger.info(f"OK Order placed successfully. Order ID: {order_id}")
            
            # Send alert (non-blocking - wrap in try/except to prevent hanging)
            try:
                self.alerts.trade_entry(
                    side=signal['type'],
                    entry_price=signal['entry_price'],
                    quantity=self.position_size,
                    stop_loss=signal['stop_loss'],
                    take_profit=signal['take_profit']
                )
            except Exception as e:
                logger.warning(f"Alert failed (non-critical): {e}")
            
            logger.info("Entry execution completed - releasing locks")
            self._executing_entry = False  # Release lock after successful order
            return True
            
        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            self.alerts.error(f"Order execution failed: {e}")
            # Clear position and release locks on exception
            self.current_position = None
            self._executing_entry = False  # Release execution lock
            self._trading_locked = False  # Release trading lock
            return False
    
    def _check_position_status(self) -> None:
        if self.current_position is None:
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
                logger.info("Position closed (detected via REST)")
                self.current_position = None
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
                else:
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
                
        except Exception as e:
            logger.error(f"Failed to check position: {e}")
            import traceback
            logger.error(f"Position check traceback: {traceback.format_exc()}")
    
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
                            self.current_position['stop_order_id'] = order.get('id')  # Store stop order ID
                            logger.info(f"Found existing stop order: ID {order.get('id')} at ${order.get('stopPrice', 0):.2f}")
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
                
                # Set flag IMMEDIATELY to prevent other threads from processing
                self.current_position['break_even_set'] = True
                logger.info(f"Moving stop to break-even: ${entry:.2f} ({reason})")
                self.current_position['stop_loss'] = entry
                self._update_stop_order(entry)  # Update broker stop order
                self.alerts.stop_moved_to_breakeven(entry)
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
            if self.current_position is None:
                return
            
            # Prevent concurrent calls with a simple lock
            if hasattr(self, '_updating_stop_order') and self._updating_stop_order:
                logger.debug("Stop order update already in progress, skipping duplicate call")
                return
            
            self._updating_stop_order = True
            
            try:
                side = self.current_position['side']
                current_price = self._get_current_price()
                
                # Round to tick size first
                new_stop_price = round(new_stop_price / self.tick_size) * self.tick_size
                
                # Validate stop price
                if current_price is not None:
                    if side == 'long':
                        # For LONG: stop must be below current price (or it would trigger immediately)
                        if new_stop_price >= current_price:
                            logger.warning(f"Invalid stop price for LONG: ${new_stop_price:.2f} >= current ${current_price:.2f}. Adjusting to ${current_price - self.tick_size:.2f}")
                            new_stop_price = round((current_price - self.tick_size) / self.tick_size) * self.tick_size  # Place 1 tick below current
                    else:  # short
                        # For SHORT: stop must be above current price
                        if new_stop_price <= current_price:
                            logger.warning(f"Invalid stop price for SHORT: ${new_stop_price:.2f} <= current ${current_price:.2f}. Adjusting to ${current_price + self.tick_size:.2f}")
                            new_stop_price = round((current_price + self.tick_size) / self.tick_size) * self.tick_size  # Place 1 tick above current
                
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
                    # Update stored ID
                    self.current_position['stop_order_id'] = existing_stop_order_id
                    
                    # Get current position quantity to update order size
                    current_qty = self.current_position.get('quantity')
                    if current_qty is None or current_qty <= 0:
                        try:
                            positions = self.client.get_positions()
                            for pos in positions:
                                if pos.contract_id == self.contract.id:
                                    current_qty = abs(pos.size)
                                    self.current_position['quantity'] = current_qty
                                    break
                        except Exception as e:
                            logger.debug(f"Could not sync quantity: {e}")
                    
                    # Use current quantity or fallback to position_size
                    if current_qty is None or current_qty <= 0:
                        current_qty = self.position_size
                    
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
                
                # If modify failed or no existing order, cancel ALL existing stop orders first
                for stop_id in all_stop_order_ids:
                    try:
                        logger.info(f"Cancelling existing stop order: ID {stop_id}")
                        self.client.cancel_order(stop_id)
                    except Exception as e:
                        logger.warning(f"Failed to cancel stop order {stop_id}: {e}")
                
                # Now place new stop order
                stop_side = OrderSide.ASK if side == 'long' else OrderSide.BID
                # ALWAYS use the actual position quantity, sync from broker if needed
                remaining_qty = self.current_position.get('quantity')
                if remaining_qty is None or remaining_qty <= 0:
                    # Try to get actual quantity from broker
                    try:
                        positions = self.client.get_positions()
                        for pos in positions:
                            if pos.contract_id == self.contract.id:
                                remaining_qty = abs(pos.size)
                                self.current_position['quantity'] = remaining_qty
                                logger.info(f"Synced position quantity from broker: {remaining_qty} contracts")
                                break
                    except Exception as e:
                        logger.warning(f"Could not sync quantity from broker: {e}")
                
                # Fallback to position_size if still not set
                if remaining_qty is None or remaining_qty <= 0:
                    remaining_qty = self.position_size
                    logger.warning(f"Using default position size: {remaining_qty} contracts (position quantity not set)")
                
                logger.info(f"Placing stop order: {stop_side.name} {remaining_qty} contracts @ ${new_stop_price:.2f}")
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
                elif order and not order.get('success'):
                    error_msg = order.get('errorMessage', 'Unknown error')
                    logger.error(f"Failed to place stop order: {error_msg}")
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
                
                if self.current_position is not None:
                    if self._check_daily_loss_force_exit():
                        return
                    self._check_break_even()
                    # Check trailing stop and partial profit using current price
                    current_price = self._get_current_price()
                    if current_price:
                        self._update_trailing_stop(current_price)
                        self._check_partial_profit(current_price)
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
            
            # Set BOTH locks IMMEDIATELY before calling _execute_entry
            # This prevents another run_once() call from getting past the checks
            self._executing_entry = True
            self._trading_locked = True  # Lock trading until position closes
            
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
            loop_count = 0
            while self.running:
                try:
                    loop_count += 1
                    # Log every 10 loops (every ~5-10 minutes depending on interval) to show it's alive
                    if loop_count % 10 == 0:
                        logger.info(f"[HEARTBEAT] Trading loop active - iteration {loop_count}, position: {'OPEN' if self.current_position else 'NONE'}, locked: {self._trading_locked}")
                    
                    self.run_once()
                except Exception as e:
                    logger.error(f"Error in trading loop: {e}")
                    import traceback
                    logger.error(f"Traceback: {traceback.format_exc()}")
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
            
            # Place bracket orders (stop loss and take profit)
            logger.info("Placing bracket orders...")
            
            # Place stop loss order
            stop_side = OrderSide.ASK if side == 'long' else OrderSide.BID
            stop_order = self.client.place_order(
                contract_id=self.contract.id,
                side=stop_side,
                order_type=OrderType.STOP,
                size=actual_qty,
                stop_price=stop_loss
            )
            
            if stop_order and stop_order.get('success'):
                stop_order_id = stop_order.get('orderId')
                if stop_order_id:
                    logger.info(f"Stop loss order placed: ID {stop_order_id} @ ${stop_loss:.2f}")
                    self.pending_orders[stop_order_id] = {
                        'type': 'stop_loss',
                        'price': stop_loss
                    }
                else:
                    logger.warning("Stop loss order placed but no order ID returned")
            else:
                error_msg = stop_order.get('errorMessage', 'Unknown error') if stop_order else 'No response'
                logger.warning(f"Failed to place stop loss order: {error_msg}")
            
            # Place take profit order (partial exit at 1R, then move SL to BE)
            # For test: exit 50% at 1R, keep 50% running
            partial_qty = max(1, actual_qty // 2)  # At least 1 contract
            remaining_qty = actual_qty - partial_qty
            
            tp_side = OrderSide.ASK if side == 'long' else OrderSide.BID
            tp_order = self.client.place_order(
                contract_id=self.contract.id,
                side=tp_side,
                order_type=OrderType.LIMIT,
                size=partial_qty,
                limit_price=take_profit
            )
            
            if tp_order and tp_order.get('success'):
                tp_order_id = tp_order.get('orderId')
                if tp_order_id:
                    logger.info(f"Take profit order placed: ID {tp_order_id} @ ${take_profit:.2f} ({partial_qty} contracts)")
                    self.pending_orders[tp_order_id] = {
                        'type': 'take_profit_partial',
                        'price': take_profit,
                        'quantity': partial_qty
                    }
                else:
                    logger.warning("Take profit order placed but no order ID returned")
            else:
                error_msg = tp_order.get('errorMessage', 'Unknown error') if tp_order else 'No response'
                logger.warning(f"Failed to place take profit order: {error_msg}")
            
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
