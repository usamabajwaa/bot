================================================================================
CONFIGURATION FILES USAGE
================================================================================

CONFIG FILES:
-------------

1. config.json
   - Used for: BACKTESTING
   - Loaded by: backtest.py
   - Settings: All 3 sessions enabled, 10 contracts, unlimited trades

2. config_production.json
   - Used for: LIVE TRADING
   - Loaded by: live_trader.py
   - Settings: All 3 sessions enabled, 10 contracts, unlimited trades

CURRENT SETTINGS (Both Configs):
---------------------------------
- Position Size: 10 contracts
- Max Trades/Day: 9999 (unlimited)
- Daily Loss Limit: -$1500
- Sessions:
  * Asia: 18:00-02:00 UTC (enabled)
  * London: 08:00-16:00 UTC (enabled, corrected)
  * US: 14:30-21:00 UTC (enabled, optimized hours)

IMPORTANT:
----------
- config.json = Backtesting configuration
- config_production.json = Live trading configuration
- Both should be kept in sync for consistent results

================================================================================

