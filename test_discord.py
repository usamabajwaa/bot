#!/usr/bin/env python3
"""Test Discord webhook notification"""
import requests
import json
from datetime import datetime

def test_discord_webhook():
    webhook_url = "https://discord.com/api/webhooks/1455059615376216105/XuXhUCcqe6M4w9gOFb-4ZYiBK7bGOUTzo4pZ99ipaNSL3ZcuSm9TMew5pJlBS3igSl7B"
    
    # Create a test embed message
    embed = {
        "title": "✅ Discord Notification Test",
        "description": "This is a test message to confirm Discord notifications are working!",
        "color": 0x2ecc71,  # Green color
        "timestamp": datetime.now().isoformat(),
        "fields": [
            {
                "name": "Status",
                "value": "✅ Connected and Ready",
                "inline": True
            },
            {
                "name": "Bot Name",
                "value": "MGC Trading Bot",
                "inline": True
            }
        ],
        "footer": {
            "text": "MGC Scalping Engine - Test Notification"
        }
    }
    
    payload = {
        "embeds": [embed],
        "content": "🔔 **Test Notification** - Discord webhook is working correctly!"
    }
    
    try:
        print("Sending test message to Discord...")
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        print("OK Test message sent successfully!")
        print(f"Response status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to send test message: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}")
        return False

if __name__ == '__main__':
    test_discord_webhook()

