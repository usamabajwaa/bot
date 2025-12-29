import requests
import json
import logging
from typing import Optional, Dict
from datetime import datetime
from dataclasses import dataclass
from enum import Enum


logger = logging.getLogger(__name__)


class AlertType(Enum):
    SIGNAL = "signal"
    ENTRY = "entry"
    EXIT = "exit"
    STOP_MOVED = "stop_moved"
    DAILY_LIMIT = "daily_limit"
    ERROR = "error"
    STATUS = "status"


@dataclass
class Alert:
    alert_type: AlertType
    title: str
    message: str
    timestamp: datetime = None
    data: Optional[Dict] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class DiscordNotifier:
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
    
    def send(self, alert: Alert) -> bool:
        color_map = {
            AlertType.SIGNAL: 0x3498db,
            AlertType.ENTRY: 0x2ecc71,
            AlertType.EXIT: 0xf1c40f,
            AlertType.STOP_MOVED: 0x9b59b6,
            AlertType.DAILY_LIMIT: 0xe74c3c,
            AlertType.ERROR: 0xe74c3c,
            AlertType.STATUS: 0x95a5a6,
        }
        
        embed = {
            "title": f"🔔 {alert.title}",
            "description": alert.message,
            "color": color_map.get(alert.alert_type, 0x3498db),
            "timestamp": alert.timestamp.isoformat(),
            "footer": {"text": "MGC Scalping Engine"}
        }
        
        if alert.data:
            fields = []
            for key, value in alert.data.items():
                fields.append({
                    "name": key.replace('_', ' ').title(),
                    "value": str(value),
                    "inline": True
                })
            embed["fields"] = fields
        
        payload = {"embeds": [embed]}
        
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Discord notification failed: {e}")
            return False


class TelegramNotifier:
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
    
    def send(self, alert: Alert) -> bool:
        emoji_map = {
            AlertType.SIGNAL: "📊",
            AlertType.ENTRY: "✅",
            AlertType.EXIT: "🔔",
            AlertType.STOP_MOVED: "🔄",
            AlertType.DAILY_LIMIT: "⛔",
            AlertType.ERROR: "❌",
            AlertType.STATUS: "ℹ️",
        }
        
        emoji = emoji_map.get(alert.alert_type, "📌")
        
        text = f"{emoji} *{alert.title}*\n\n{alert.message}"
        
        if alert.data:
            text += "\n\n"
            for key, value in alert.data.items():
                text += f"• {key.replace('_', ' ').title()}: `{value}`\n"
        
        text += f"\n_{alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')}_"
        
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Telegram notification failed: {e}")
            return False


class AlertManager:
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.notifiers = []
        
        self._setup_notifiers()
    
    def _setup_notifiers(self) -> None:
        discord_config = self.config.get('discord', {})
        if discord_config.get('enabled') and discord_config.get('webhook_url'):
            self.notifiers.append(DiscordNotifier(discord_config['webhook_url']))
            logger.info("Discord notifications enabled")
        
        telegram_config = self.config.get('telegram', {})
        if telegram_config.get('enabled') and telegram_config.get('bot_token'):
            self.notifiers.append(TelegramNotifier(
                bot_token=telegram_config['bot_token'],
                chat_id=telegram_config['chat_id']
            ))
            logger.info("Telegram notifications enabled")
    
    def send_alert(self, alert: Alert) -> None:
        for notifier in self.notifiers:
            try:
                notifier.send(alert)
            except Exception as e:
                logger.error(f"Failed to send alert via {type(notifier).__name__}: {e}")
    
    def signal_detected(
        self, 
        signal_type: str, 
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        session: str
    ) -> None:
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        rr_ratio = reward / risk if risk > 0 else 0
        
        alert = Alert(
            alert_type=AlertType.SIGNAL,
            title=f"{signal_type.upper()} Signal Detected",
            message=f"New trading signal in {session} session",
            data={
                "entry": f"${entry_price:.2f}",
                "stop_loss": f"${stop_loss:.2f}",
                "take_profit": f"${take_profit:.2f}",
                "risk_reward": f"{rr_ratio:.1f}R",
                "session": session
            }
        )
        self.send_alert(alert)
    
    def trade_entry(
        self,
        side: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        take_profit: float
    ) -> None:
        alert = Alert(
            alert_type=AlertType.ENTRY,
            title=f"Trade Entered: {side.upper()}",
            message=f"Position opened with {quantity} contracts",
            data={
                "side": side.upper(),
                "entry": f"${entry_price:.2f}",
                "quantity": quantity,
                "stop_loss": f"${stop_loss:.2f}",
                "take_profit": f"${take_profit:.2f}"
            }
        )
        self.send_alert(alert)
    
    def trade_exit(
        self,
        side: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        exit_reason: str
    ) -> None:
        emoji = "✅" if pnl > 0 else "❌"
        
        alert = Alert(
            alert_type=AlertType.EXIT,
            title=f"Trade Closed: {emoji} ${pnl:+.2f}",
            message=f"Position closed via {exit_reason}",
            data={
                "side": side.upper(),
                "entry": f"${entry_price:.2f}",
                "exit": f"${exit_price:.2f}",
                "pnl": f"${pnl:+.2f}",
                "reason": exit_reason
            }
        )
        self.send_alert(alert)
    
    def stop_moved_to_breakeven(self, new_stop: float) -> None:
        alert = Alert(
            alert_type=AlertType.STOP_MOVED,
            title="Stop Moved to Break-Even",
            message=f"Stop loss moved to entry price: ${new_stop:.2f}",
            data={
                "new_stop": f"${new_stop:.2f}",
                "status": "Risk-free trade"
            }
        )
        self.send_alert(alert)
    
    def daily_limit_reached(self, daily_pnl: float, limit: float) -> None:
        alert = Alert(
            alert_type=AlertType.DAILY_LIMIT,
            title="Daily Loss Limit Reached",
            message="Trading halted for the day",
            data={
                "daily_pnl": f"${daily_pnl:.2f}",
                "limit": f"${limit:.2f}"
            }
        )
        self.send_alert(alert)
    
    def error(self, error_message: str) -> None:
        alert = Alert(
            alert_type=AlertType.ERROR,
            title="Trading Error",
            message=error_message
        )
        self.send_alert(alert)
    
    def daily_summary(
        self,
        trades: int,
        pnl: float,
        win_rate: float
    ) -> None:
        emoji = "📈" if pnl > 0 else "📉"
        
        alert = Alert(
            alert_type=AlertType.STATUS,
            title=f"Daily Summary {emoji}",
            message=f"End of day trading report",
            data={
                "total_trades": trades,
                "daily_pnl": f"${pnl:+.2f}",
                "win_rate": f"{win_rate:.1f}%"
            }
        )
        self.send_alert(alert)


def load_alert_config(path: str = 'alerts_config.json') -> Dict:
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

