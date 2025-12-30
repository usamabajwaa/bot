# Test Trade Functionality

## What Was Fixed

### 1. Position Logging Issue
**Problem:** After taking a position, logs stopped updating because position status checks weren't being logged frequently enough.

**Solution:**
- Position status is now logged **every check** (every 30-60 seconds) when a position is open
- Logs show: Side, Quantity, Entry Price, Current Price, P&L, and P&L in ticks
- Format: `[POSITION] LONG 3 @ $4378.30, Current: $4380.50, P&L: $6.60 (22.0 ticks)`

### 2. Position Monitoring
**Fixed:** When `_trading_locked` is True, the trader now:
- Still checks position status
- Monitors break-even conditions
- Checks trailing stops
- Monitors partial profit exits
- All while preventing new signal generation

## Test Trade Function

### Purpose
Place a test trade with 1 contract to verify:
- Bracket orders (stop loss + take profit)
- Partial profit exits
- Break-even moves
- Position monitoring

### Usage

#### Option 1: Command Line
```bash
# Place a LONG test trade
python live_trader.py --config config_production.json --test-trade long

# Place a SHORT test trade
python live_trader.py --config config_production.json --test-trade short
```

#### Option 2: Python Script
```python
from live_trader import LiveTrader

trader = LiveTrader(
    config_path='config_production.json',
    credentials_path='credentials.json'
)

if trader.connect():
    # Place test trade
    success = trader.test_trade(side='long', quantity=1)
    
    if success:
        # Start monitoring
        trader.run(interval_seconds=30)
```

### Test Trade Details

**Default Settings:**
- **Quantity:** 1 contract
- **Stop Loss:** 10 ticks from entry
- **Take Profit:** 30 ticks from entry (R:R = 3.0)
- **Partial Exit:** 50% at 1R (10 ticks profit)
- **Break-Even:** Moves to BE after partial exit

**What Happens:**
1. Market order placed for 1 contract
2. Bracket orders placed:
   - Stop loss order (10 ticks risk)
   - Take profit order (30 ticks reward, 50% of position)
3. Position monitoring starts:
   - Logs position status every 30-60 seconds
   - Monitors for partial profit trigger
   - Moves stop to break-even after partial
   - Trails stop if configured

### Monitoring

After placing test trade, you'll see logs like:
```
[POSITION] LONG 1 @ $4378.30, Current: $4380.50, P&L: $2.20 (22.0 ticks)
Partial profit progress: 50% ($4380.50 / $4388.30 target)
*** PARTIAL PROFIT TRIGGER HIT! ***
PARTIAL PROFIT EXIT - 1 contracts
OK Stop moved to: $4378.30 (0.5R profit locked)
```

### Important Notes

1. **Test trades are real trades** - they use real money and real positions
2. **Use with caution** - test on practice account first
3. **Position size is 1 contract** - minimal risk for testing
4. **All features active** - break-even, partials, trailing stops all work

### Stopping Test Trade

To stop monitoring:
- Press `Ctrl+C` if running manually
- Use `pm2 stop mgc-live-trader` if running via PM2
- Position will remain open until stop/target hits

## Verification Checklist

After placing test trade, verify:
- [ ] Position appears in logs: `[POSITION] ...`
- [ ] Stop loss order is placed
- [ ] Take profit order is placed (partial)
- [ ] Position status logs every 30-60 seconds
- [ ] Break-even moves after partial exit
- [ ] Trailing stop works (if enabled)

## Troubleshooting

**No position logs appearing:**
- Check if position actually filled
- Verify trader is running: `pm2 list`
- Check logs: `pm2 logs mgc-live-trader`

**Position not monitoring:**
- Verify `_trading_locked` is True
- Check that `current_position` is set
- Look for errors in logs

**Test trade fails:**
- Check account has sufficient margin
- Verify contract is available
- Check API connection status

