import requests
import json
import time
import logging
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from enum import IntEnum


logger = logging.getLogger(__name__)


class OrderSide(IntEnum):
    BID = 0   # Buy
    ASK = 1   # Sell


class OrderType(IntEnum):
    UNKNOWN = 0
    LIMIT = 1
    MARKET = 2
    STOP_LIMIT = 3
    STOP = 4
    TRAILING_STOP = 5
    JOIN_BID = 6
    JOIN_ASK = 7


class OrderStatus(IntEnum):
    NONE = 0
    OPEN = 1
    FILLED = 2
    CANCELLED = 3
    EXPIRED = 4
    REJECTED = 5
    PENDING = 6


class PositionType(IntEnum):
    UNDEFINED = 0
    LONG = 1
    SHORT = 2


@dataclass
class Position:
    id: int
    account_id: int
    contract_id: str
    position_type: PositionType
    size: int
    average_price: float
    creation_timestamp: str


@dataclass
class AccountInfo:
    id: int
    name: str
    balance: float
    can_trade: bool
    is_visible: bool
    simulated: bool


@dataclass 
class Contract:
    id: str
    name: str
    description: str
    tick_size: float
    tick_value: float
    active: bool
    symbol_id: str


class TopstepXClient:
    
    TOPSTEPX_URL = "https://api.topstepx.com"
    DEMO_BASE_URL = "https://gateway-api-demo.s2f.projectx.com"
    DEMO_RTC_URL = "https://gateway-rtc-demo.s2f.projectx.com"
    
    def __init__(
        self, 
        username: str, 
        api_key: str,
        base_url: Optional[str] = None,
        rtc_url: Optional[str] = None
    ):
        self.username = username
        self.api_key = api_key
        self.base_url = base_url or self.TOPSTEPX_URL
        self.rtc_url = rtc_url
        
        self.session = requests.Session()
        self.token: Optional[str] = None
        self.token_expiry: float = 0
        self.account_id: Optional[int] = None
        
        self._rate_limit_remaining = 200
        self._rate_limit_reset = time.time() + 60
        
        # History endpoint has stricter limits: 50 requests / 30 seconds
        self._history_rate_limit_remaining = 50
        self._history_rate_limit_reset = time.time() + 30
        
    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/plain"
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers
    
    def _handle_rate_limit(self, is_history_endpoint: bool = False) -> None:
        """Handle rate limiting for API endpoints.
        
        Args:
            is_history_endpoint: If True, uses history endpoint limits (50/30s)
                                Otherwise uses standard limits (200/60s)
        """
        if is_history_endpoint:
            # History endpoint: 50 requests / 30 seconds
            if time.time() > self._history_rate_limit_reset:
                self._history_rate_limit_remaining = 50
                self._history_rate_limit_reset = time.time() + 30
            
            if self._history_rate_limit_remaining <= 5:
                sleep_time = self._history_rate_limit_reset - time.time()
                if sleep_time > 0:
                    logger.warning(f"History API rate limit approaching, sleeping {sleep_time:.1f}s")
                    time.sleep(sleep_time)
                    self._history_rate_limit_remaining = 50
                    self._history_rate_limit_reset = time.time() + 30
        else:
            # Standard endpoints: 200 requests / 60 seconds
            if time.time() > self._rate_limit_reset:
                self._rate_limit_remaining = 200
                self._rate_limit_reset = time.time() + 60
            
            if self._rate_limit_remaining <= 5:
                sleep_time = self._rate_limit_reset - time.time()
                if sleep_time > 0:
                    logger.warning(f"Rate limit approaching, sleeping {sleep_time:.1f}s")
                    time.sleep(sleep_time)
                    self._rate_limit_remaining = 200
                    self._rate_limit_reset = time.time() + 60
    
    def _request(
        self, 
        method: str, 
        endpoint: str, 
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        skip_auth_check: bool = False,
        skip_rate_limit: bool = False
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        
        # Only handle rate limit if not skipped (history endpoint handles it separately)
        if not skip_rate_limit:
            self._handle_rate_limit(is_history_endpoint=False)
        
        if not skip_auth_check and self.token and time.time() > self.token_expiry - 3600:
            self.validate_token()
        
        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=self._get_headers(),
                json=data,
                params=params,
                timeout=30
            )
            
            self._rate_limit_remaining -= 1
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 30))
                logger.warning(f"Rate limited, waiting {retry_after}s")
                time.sleep(retry_after)
                return self._request(method, endpoint, data, params, skip_auth_check)
            
            if response.status_code == 401 and not skip_auth_check:
                logger.info("Token expired, refreshing...")
                if self.validate_token():
                    return self._request(method, endpoint, data, params, True)
            
            response.raise_for_status()
            
            if response.text:
                result = response.json()
                if not result.get('success', True):
                    error_msg = result.get('errorMessage', 'Unknown error')
                    error_code = result.get('errorCode', -1)
                    logger.error(f"API error {error_code}: {error_msg}")
                return result
            return {}
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            raise
    
    def authenticate(self) -> bool:
        data = {
            "userName": self.username,
            "apiKey": self.api_key
        }
        
        try:
            response = self._request(
                "POST", 
                "/api/Auth/loginKey", 
                data=data,
                skip_auth_check=True
            )
            
            if response.get('success') and response.get('token'):
                self.token = response['token']
                self.token_expiry = time.time() + 24 * 3600
                logger.info("Authentication successful")
                return True
            
            error_msg = response.get('errorMessage', 'Unknown error')
            logger.error(f"Authentication failed: {error_msg}")
            return False
            
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return False
    
    def validate_token(self) -> bool:
        try:
            response = self._request(
                "POST",
                "/api/Auth/validate",
                skip_auth_check=True
            )
            
            if response.get('success') and response.get('newToken'):
                self.token = response['newToken']
                self.token_expiry = time.time() + 24 * 3600
                logger.info("Token refreshed successfully")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return False
    
    def get_accounts(self, only_active: bool = True) -> List[AccountInfo]:
        data = {"onlyActiveAccounts": only_active}
        response = self._request("POST", "/api/Account/search", data=data)
        
        accounts = []
        for acc in response.get('accounts', []):
            accounts.append(AccountInfo(
                id=acc.get('id'),
                name=acc.get('name', ''),
                balance=acc.get('balance', 0),
                can_trade=acc.get('canTrade', False),
                is_visible=acc.get('isVisible', True),
                simulated=acc.get('simulated', False)
            ))
        return accounts
    
    def set_account(self, account_id: int) -> None:
        self.account_id = account_id
        logger.info(f"Account set to: {account_id}")
    
    def get_available_contracts(self, live: bool = False) -> List[Contract]:
        data = {"live": live}
        response = self._request("POST", "/api/Contract/available", data=data)
        
        contracts = []
        for c in response.get('contracts', []):
            contracts.append(Contract(
                id=c.get('id', ''),
                name=c.get('name', ''),
                description=c.get('description', ''),
                tick_size=c.get('tickSize', 0.01),
                tick_value=c.get('tickValue', 1.0),
                active=c.get('activeContract', False),
                symbol_id=c.get('symbolId', '')
            ))
        return contracts
    
    def search_contracts(self, text: str) -> List[Contract]:
        data = {"searchText": text, "live": True}
        response = self._request("POST", "/api/Contract/search", data=data)
        
        contracts = []
        for c in response.get('contracts', []):
            contracts.append(Contract(
                id=c.get('id', ''),
                name=c.get('name', ''),
                description=c.get('description', ''),
                tick_size=c.get('tickSize', 0.01),
                tick_value=c.get('tickValue', 1.0),
                active=c.get('activeContract', False),
                symbol_id=c.get('symbolId', '')
            ))
        return contracts
    
    def find_mgc_contract(self) -> Optional[Contract]:
        contracts = self.get_available_contracts(live=False)
        
        for c in contracts:
            if 'MGC' in c.id.upper() or 'MGC' in c.name.upper():
                if c.active:
                    logger.info(f"Found MGC contract: {c.id} - {c.description}")
                    return c
        
        for c in contracts:
            if 'MGC' in c.id.upper() or 'MGC' in c.name.upper():
                logger.info(f"Found MGC contract (inactive): {c.id} - {c.description}")
                return c
        
        logger.warning("MGC contract not found")
        return None
    
    def place_order(
        self,
        contract_id: str,
        side: OrderSide,
        order_type: OrderType,
        size: int,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        trail_price: Optional[float] = None,
        stop_loss_ticks: Optional[int] = None,
        take_profit_ticks: Optional[int] = None,
        stop_loss_type: int = 4,
        take_profit_type: int = 1,
        custom_tag: Optional[str] = None
    ) -> Dict:
        if not self.account_id:
            raise ValueError("Account ID not set. Call set_account() first.")
        
        data = {
            "accountId": self.account_id,
            "contractId": contract_id,
            "type": int(order_type),
            "side": int(side),
            "size": size
        }
        
        if limit_price is not None:
            data["limitPrice"] = limit_price
        
        if stop_price is not None:
            data["stopPrice"] = stop_price
        
        if trail_price is not None:
            data["trailPrice"] = trail_price
        
        if stop_loss_ticks is not None:
            data["stopLossBracket"] = {
                "ticks": stop_loss_ticks,
                "type": stop_loss_type
            }
        
        if take_profit_ticks is not None:
            data["takeProfitBracket"] = {
                "ticks": take_profit_ticks,
                "type": take_profit_type
            }
        
        if custom_tag:
            data["customTag"] = custom_tag
        
        logger.info(f"Placing order: {side.name} {size} {contract_id} @ {order_type.name}")
        response = self._request("POST", "/api/Order/place", data=data)
        
        if response.get('success'):
            logger.info(f"Order placed: ID {response.get('orderId')}")
        else:
            logger.error(f"Order failed: {response.get('errorMessage')}")
        
        return response
    
    def place_market_order(
        self, 
        contract_id: str, 
        side: OrderSide, 
        size: int,
        stop_loss_ticks: Optional[int] = None,
        take_profit_ticks: Optional[int] = None
    ) -> Dict:
        return self.place_order(
            contract_id=contract_id,
            side=side,
            order_type=OrderType.MARKET,
            size=size,
            stop_loss_ticks=stop_loss_ticks,
            take_profit_ticks=take_profit_ticks
        )
    
    def place_limit_order(
        self, 
        contract_id: str, 
        side: OrderSide, 
        size: int,
        limit_price: float,
        stop_loss_ticks: Optional[int] = None,
        take_profit_ticks: Optional[int] = None
    ) -> Dict:
        return self.place_order(
            contract_id=contract_id,
            side=side,
            order_type=OrderType.LIMIT,
            size=size,
            limit_price=limit_price,
            stop_loss_ticks=stop_loss_ticks,
            take_profit_ticks=take_profit_ticks
        )
    
    def place_stop_order(
        self, 
        contract_id: str, 
        side: OrderSide, 
        size: int,
        stop_price: float
    ) -> Dict:
        return self.place_order(
            contract_id=contract_id,
            side=side,
            order_type=OrderType.STOP,
            size=size,
            stop_price=stop_price
        )
    
    def place_bracket_order(
        self,
        contract_id: str,
        side: OrderSide,
        size: int,
        stop_loss_ticks: int,
        take_profit_ticks: int
    ) -> Dict:
        """
        Place bracket order with stop loss and take profit.
        
        TopStep API expects:
        - For LONG (BID): stop_loss_ticks < 0 (below entry), take_profit_ticks > 0 (above entry)
        - For SHORT (ASK): stop_loss_ticks > 0 (above entry), take_profit_ticks < 0 (below entry)
        
        This function converts absolute tick values to signed values based on side.
        """
        # Convert absolute ticks to signed ticks based on side
        if side == OrderSide.BID:  # LONG
            # Stop loss is below entry (negative), take profit is above entry (positive)
            sl_ticks_signed = -abs(stop_loss_ticks)
            tp_ticks_signed = abs(take_profit_ticks)
        else:  # SHORT (ASK)
            # Stop loss is above entry (positive), take profit is below entry (negative)
            sl_ticks_signed = abs(stop_loss_ticks)
            tp_ticks_signed = -abs(take_profit_ticks)
        
        return self.place_market_order(
            contract_id=contract_id,
            side=side,
            size=size,
            stop_loss_ticks=sl_ticks_signed,
            take_profit_ticks=tp_ticks_signed
        )
    
    def cancel_order(self, order_id: int) -> Dict:
        if not self.account_id:
            raise ValueError("Account ID not set")
        
        data = {
            "accountId": self.account_id,
            "orderId": order_id
        }
        response = self._request("POST", "/api/Order/cancel", data=data)
        
        if response.get('success'):
            logger.info(f"Order {order_id} cancelled")
        else:
            logger.error(f"Cancel failed: {response.get('errorMessage')}")
        
        return response
    
    def modify_order(
        self, 
        order_id: int, 
        size: Optional[int] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        trail_price: Optional[float] = None
    ) -> Dict:
        if not self.account_id:
            raise ValueError("Account ID not set")
        
        data = {
            "accountId": self.account_id,
            "orderId": order_id
        }
        
        if size is not None:
            data["size"] = size
        if limit_price is not None:
            data["limitPrice"] = limit_price
        if stop_price is not None:
            data["stopPrice"] = stop_price
        if trail_price is not None:
            data["trailPrice"] = trail_price
        
        response = self._request("POST", "/api/Order/modify", data=data)
        return response
    
    def partial_close_position(
        self,
        contract_id: str,
        size: int
    ) -> Dict:
        if not self.account_id:
            raise ValueError("Account ID not set")
        
        data = {
            "accountId": self.account_id,
            "contractId": contract_id,
            "size": size
        }
        
        logger.info(f"Partial close: {size} contracts of {contract_id}")
        response = self._request("POST", "/api/Position/partialCloseContract", data=data)
        
        if response.get('success'):
            logger.info(f"Partial close successful")
        else:
            logger.error(f"Partial close failed: {response.get('errorMessage')}")
        
        return response
    
    def get_open_orders(self) -> List[Dict]:
        if not self.account_id:
            raise ValueError("Account ID not set")
        
        try:
            data = {"accountId": self.account_id}
            response = self._request("POST", "/api/Order/searchOpen", data=data)
            return response.get('orders', [])
        except Exception as e:
            logger.debug(f"Could not fetch open orders: {e}")
            return []
    
    def search_orders(
        self,
        start_timestamp: str,
        end_timestamp: Optional[str] = None
    ) -> List[Dict]:
        if not self.account_id:
            raise ValueError("Account ID not set")
        
        data = {
            "accountId": self.account_id,
            "startTimestamp": start_timestamp
        }
        
        if end_timestamp:
            data["endTimestamp"] = end_timestamp
        
        try:
            response = self._request("POST", "/api/Order/search", data=data)
            return response.get('orders', [])
        except Exception as e:
            logger.debug(f"Could not search orders: {e}")
            return []
    
    def get_positions(self) -> List[Position]:
        if not self.account_id:
            raise ValueError("Account ID not set")
        
        try:
            data = {"accountId": self.account_id}
            response = self._request("POST", "/api/Position/searchOpen", data=data)
            
            positions = []
            for p in response.get('positions', []):
                if p.get('size', 0) != 0:
                    positions.append(Position(
                        id=p.get('id', 0),
                        account_id=p.get('accountId', self.account_id),
                        contract_id=p.get('contractId', ''),
                        position_type=PositionType.LONG if p.get('positionType', 0) == 1 else PositionType.SHORT,
                        size=p.get('size', 0),
                        average_price=p.get('averagePrice', 0),
                        creation_timestamp=p.get('creationTimestamp', '')
                    ))
            return positions
        except Exception as e:
            logger.debug(f"Could not fetch positions via Position API: {e}")
            return []
    
    def close_position(self, contract_id: str, size: Optional[int] = None) -> Dict:
        positions = self.get_positions()
        
        for pos in positions:
            if pos.contract_id == contract_id:
                close_size = size or abs(pos.size)
                
                if pos.size > 0:
                    side = OrderSide.ASK
                else:
                    side = OrderSide.BID
                
                return self.place_market_order(contract_id, side, close_size)
        
        return {"success": False, "errorMessage": "Position not found"}
    
    def close_all_positions(self) -> List[Dict]:
        positions = self.get_positions()
        results = []
        
        for pos in positions:
            if pos.size != 0:
                result = self.close_position(pos.contract_id)
                results.append(result)
        
        return results
    
    def get_historical_bars(
        self, 
        contract_id: str, 
        interval: int = 15,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        count: int = 100,
        live: bool = False,
        unit: int = 2,
        include_partial: bool = False
    ) -> List[Dict]:
        from datetime import datetime, timedelta, timezone
        
        now = datetime.now(timezone.utc)
        if not end_time:
            end_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if not start_time:
            start_dt = now - timedelta(days=1)
            start_time = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        data = {
            "contractId": contract_id,
            "live": live,
            "startTime": start_time,
            "endTime": end_time,
            "unit": unit,
            "unitNumber": int(interval) if isinstance(interval, str) else interval,
            "limit": count,
            "includePartialBar": include_partial
        }
        
        # Handle history endpoint rate limiting (50 requests / 30 seconds)
        self._handle_rate_limit(is_history_endpoint=True)
        
        try:
            response = self._request("POST", "/api/History/retrieveBars", data=data, skip_auth_check=False, skip_rate_limit=True)
            # Decrement history rate limit counter (already handled above)
            return response.get('bars', [])
        except Exception as e:
            logger.warning(f"History API failed: {e}")
            return []
    
    def get_user_hub_url(self) -> str:
        return f"{self.rtc_url}/hubs/user?access_token={self.token}"
    
    def get_market_hub_url(self) -> str:
        return f"{self.rtc_url}/hubs/market?access_token={self.token}"
