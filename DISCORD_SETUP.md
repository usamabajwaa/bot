# Discord Notifications Setup Guide

## How to Get Your Discord Webhook URL

1. **Open Discord** and go to your server (or create a new one)

2. **Go to Server Settings**:
   - Right-click on your server name
   - Select "Server Settings"

3. **Navigate to Integrations**:
   - Click on "Integrations" in the left sidebar
   - Click "Webhooks" at the top
   - Click "New Webhook"

4. **Configure the Webhook**:
   - Give it a name (e.g., "MGC Trading Bot")
   - Choose which channel to send messages to
   - Optionally add an avatar/icon
   - Click "Copy Webhook URL"

5. **Update alerts_config.json**:
   - Open `alerts_config.json`
   - Replace `YOUR_DISCORD_WEBHOOK_URL_HERE` with your copied webhook URL
   - Save the file

6. **Restart the Live Trader**:
   ```powershell
   pm2 restart mgc-live-trader
   ```

## What Notifications You'll Receive

✅ **Trade Entry**: When a new position is opened
   - Side (LONG/SHORT)
   - Entry price
   - Quantity (contracts)
   - Stop loss
   - Take profit

✅ **Trade Exit**: When a position is closed
   - Side (LONG/SHORT)
   - Entry and exit prices
   - P&L (profit/loss)
   - Exit reason

✅ **Signal Detected**: When a trading signal is found (before entry)
   - Signal type
   - Entry, stop loss, take profit
   - Risk/reward ratio
   - Session

✅ **Stop Moved to Break-Even**: When stop is moved to entry price

✅ **Daily Loss Limit**: When daily loss limit is reached

✅ **Errors**: When trading errors occur

## Example Webhook URL Format

```
https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ
```

## Security Note

⚠️ **Keep your webhook URL private!** Don't share it publicly or commit it to public repositories.

