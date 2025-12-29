import asyncio
import json
import logging
from typing import Optional, Dict, Callable, List
from dataclasses import dataclass


logger = logging.getLogger(__name__)


try:
    from signalrcore.hub_connection_builder import HubConnectionBuilder
    SIGNALR_AVAILABLE = True
except ImportError:
    SIGNALR_AVAILABLE = False
    logger.warning("signalrcore not installed. Real-time features disabled. Install with: pip install signalrcore")


@dataclass
class Quote:
    contract_id: str
    last_price: float
    best_bid: float
    best_ask: float
    high: float
    low: float
    volume: int
    timestamp: str


@dataclass
class UserOrder:
    id: int
    account_id: int
    contract_id: str
    status: int
    order_type: int
    side: int
    size: int
    limit_price: Optional[float]
    stop_price: Optional[float]
    fill_volume: int
    filled_price: Optional[float]


@dataclass
class UserPosition:
    id: int
    account_id: int
    contract_id: str
    position_type: int
    size: int
    average_price: float


@dataclass
class UserTrade:
    id: int
    account_id: int
    contract_id: str
    price: float
    pnl: float
    fees: float
    side: int
    size: int
    order_id: int


class SignalRClient:
    
    def __init__(self, token: str, rtc_base_url: str = "https://gateway-rtc-demo.s2f.projectx.com"):
        if not SIGNALR_AVAILABLE:
            raise ImportError("signalrcore is required for real-time features")
        
        self.token = token
        self.rtc_base_url = rtc_base_url
        
        self.user_hub = None
        self.market_hub = None
        
        self.on_quote: Optional[Callable[[Quote], None]] = None
        self.on_order: Optional[Callable[[UserOrder], None]] = None
        self.on_position: Optional[Callable[[UserPosition], None]] = None
        self.on_trade: Optional[Callable[[UserTrade], None]] = None
        self.on_account: Optional[Callable[[Dict], None]] = None
        
        self._subscribed_contracts: List[str] = []
        self._subscribed_account: Optional[int] = None
    
    def _build_hub(self, hub_path: str):
        url = f"{self.rtc_base_url}/hubs/{hub_path}?access_token={self.token}"
        
        hub = HubConnectionBuilder() \
            .with_url(url) \
            .with_automatic_reconnect({
                "type": "raw",
                "keep_alive_interval": 10,
                "reconnect_interval": 5,
                "max_attempts": 5
            }) \
            .build()
        
        return hub
    
    def _handle_quote(self, args):
        try:
            if len(args) >= 2:
                contract_id = args[0]
                data = args[1]
                
                quote = Quote(
                    contract_id=contract_id,
                    last_price=data.get('lastPrice', 0),
                    best_bid=data.get('bestBid', 0),
                    best_ask=data.get('bestAsk', 0),
                    high=data.get('high', 0),
                    low=data.get('low', 0),
                    volume=data.get('volume', 0),
                    timestamp=data.get('timestamp', '')
                )
                
                # Debug logging to verify quotes are received
                logger.debug(f"[SignalR] Quote received for {contract_id}: ${quote.last_price:.2f}")
                
                if self.on_quote:
                    self.on_quote(quote)
                else:
                    logger.warning(f"[SignalR] Quote received but on_quote callback not set!")
            else:
                logger.warning(f"[SignalR] Quote handler received unexpected args: {args}")
        except Exception as e:
            logger.error(f"[SignalR] Error handling quote: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    def _handle_order(self, args):
        if len(args) >= 1:
            data = args[0]
            
            order = UserOrder(
                id=data.get('id'),
                account_id=data.get('accountId'),
                contract_id=data.get('contractId', ''),
                status=data.get('status', 0),
                order_type=data.get('type', 0),
                side=data.get('side', 0),
                size=data.get('size', 0),
                limit_price=data.get('limitPrice'),
                stop_price=data.get('stopPrice'),
                fill_volume=data.get('fillVolume', 0),
                filled_price=data.get('filledPrice')
            )
            
            if self.on_order:
                self.on_order(order)
    
    def _handle_position(self, args):
        if len(args) >= 1:
            data = args[0]
            
            position = UserPosition(
                id=data.get('id'),
                account_id=data.get('accountId'),
                contract_id=data.get('contractId', ''),
                position_type=data.get('type', 0),
                size=data.get('size', 0),
                average_price=data.get('averagePrice', 0)
            )
            
            if self.on_position:
                self.on_position(position)
    
    def _handle_trade(self, args):
        if len(args) >= 1:
            data = args[0]
            
            trade = UserTrade(
                id=data.get('id'),
                account_id=data.get('accountId'),
                contract_id=data.get('contractId', ''),
                price=data.get('price', 0),
                pnl=data.get('profitAndLoss', 0),
                fees=data.get('fees', 0),
                side=data.get('side', 0),
                size=data.get('size', 0),
                order_id=data.get('orderId', 0)
            )
            
            if self.on_trade:
                self.on_trade(trade)
    
    def _handle_account(self, args):
        if len(args) >= 1:
            data = args[0]
            if self.on_account:
                self.on_account(data)
    
    def connect_user_hub(self, account_id: int) -> bool:
        try:
            self.user_hub = self._build_hub("user")
            
            self.user_hub.on("GatewayUserAccount", self._handle_account)
            self.user_hub.on("GatewayUserOrder", self._handle_order)
            self.user_hub.on("GatewayUserPosition", self._handle_position)
            self.user_hub.on("GatewayUserTrade", self._handle_trade)
            
            def on_open():
                logger.info("User hub connected")
                self._subscribe_user(account_id)
            
            def on_reconnect():
                logger.info("User hub reconnected")
                self._subscribe_user(account_id)
            
            def on_close():
                logger.warning("User hub disconnected")
            
            def on_error(error):
                logger.error(f"User hub error: {error}")
            
            self.user_hub.on_open(on_open)
            self.user_hub.on_reconnect(on_reconnect)
            self.user_hub.on_close(on_close)
            self.user_hub.on_error(on_error)
            
            self.user_hub.start()
            self._subscribed_account = account_id
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect user hub: {e}")
            return False
    
    def _subscribe_user(self, account_id: int):
        if self.user_hub:
            self.user_hub.send("SubscribeAccounts", [])
            self.user_hub.send("SubscribeOrders", [account_id])
            self.user_hub.send("SubscribePositions", [account_id])
            self.user_hub.send("SubscribeTrades", [account_id])
            logger.info(f"Subscribed to user events for account {account_id}")
    
    def connect_market_hub(self, contract_ids: List[str]) -> bool:
        try:
            self.market_hub = self._build_hub("market")
            
            self.market_hub.on("GatewayQuote", self._handle_quote)
            
            def on_open():
                logger.info("Market hub connected")
                self._subscribe_market(contract_ids)
                logger.info("[SignalR] Market hub ready - waiting for quotes...")
            
            def on_reconnect():
                logger.info("Market hub reconnected")
                self._subscribe_market(contract_ids)
                logger.info("[SignalR] Market hub reconnected - resubscribed to quotes")
            
            def on_close():
                logger.warning("Market hub disconnected - quotes will stop")
            
            def on_error(error):
                logger.error(f"Market hub error: {error}")
                logger.error("[SignalR] Connection error - quotes may not be received")
            
            self.market_hub.on_open(on_open)
            self.market_hub.on_reconnect(on_reconnect)
            self.market_hub.on_close(on_close)
            self.market_hub.on_error(on_error)
            
            self.market_hub.start()
            self._subscribed_contracts = contract_ids
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect market hub: {e}")
            return False
    
    def _subscribe_market(self, contract_ids: List[str]):
        if self.market_hub:
            for contract_id in contract_ids:
                try:
                    self.market_hub.send("SubscribeContractQuotes", [contract_id])
                    logger.info(f"Subscribed to quotes for {contract_id}")
                    logger.info(f"[SignalR] Subscription sent - waiting for GatewayQuote events...")
                except Exception as e:
                    logger.error(f"Failed to subscribe to quotes for {contract_id}: {e}")
    
    def disconnect(self):
        if self.user_hub:
            try:
                if self._subscribed_account:
                    self.user_hub.send("UnsubscribeAccounts", [])
                    self.user_hub.send("UnsubscribeOrders", [self._subscribed_account])
                    self.user_hub.send("UnsubscribePositions", [self._subscribed_account])
                    self.user_hub.send("UnsubscribeTrades", [self._subscribed_account])
                self.user_hub.stop()
            except:
                pass
        
        if self.market_hub:
            try:
                for contract_id in self._subscribed_contracts:
                    self.market_hub.send("UnsubscribeContractQuotes", [contract_id])
                self.market_hub.stop()
            except:
                pass
        
        logger.info("SignalR disconnected")

