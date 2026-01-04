# CRITICAL: Duplicate Order Analysis - 48 SL/TP Orders for 8 Contracts

## Executive Summary

**Problem:** System created up to 48 Stop Loss and Take Profit orders for a single 8-contract position.

**Root Cause:** Multiple cascading failures in order verification and placement logic, combined with aggressive watchdog behavior that runs on every loop iteration.

**Severity:** CRITICAL - This can cause:
- Massive order bloat (48 orders for 8 contracts = 6x multiplier)
- Potential margin/leverage issues
- Confusion in order management
- Risk of unintended fills if orders aren't properly cancelled

---

## Detailed Root Cause Analysis

### 1. Primary Issue: Aggressive Watchdog Function

**Location:** `live_trader.py:1627`
```python
# FIX 1: Enhanced watchdog - runs on EVERY position check (not just when qty changes)
# Checks for missing orders AND size mismatches, fixes them automatically
self._ensure_protective_orders_exist(broker_position)
```

**Problem:** This function is called on **EVERY loop iteration** when a position exists, regardless of whether orders are already placed.

**Flow:**
1. `run_once()` is called every 30-60 seconds
2. `_reconcile_position_with_broker()` is called (line 3904)
3. If position exists, `_ensure_protective_orders_exist()` is called (line 1627)
4. Function checks if orders exist
5. If verification fails (even temporarily), it places NEW orders
6. Next iteration: same check, same failure, MORE new orders
7. **Result: Exponential order growth**

### 2. Verification Failure Cascade

**Location:** `live_trader.py:1843-1866`

**The Verification Logic:**
```python
def _verify_order_placement(self, order_id: int, expected_size: int, 
                           order_type: str, max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        time.sleep(0.5)  # Wait for order to register
        open_orders = self.client.get_open_orders()
        for order in open_orders:
            if order.get('id') == order_id:
                order_size = abs(order.get('size', 0))
                if order_size == expected_size:  # STRICT: Must match exactly
                    return True
                else:
                    return False  # FAILS if size doesn't match
    return False  # FAILS if order not found
```

**Why Verification Fails:**

1. **API Timing Issues:**
   - Order placed successfully
   - API hasn't registered it yet (0.5s wait may not be enough)
   - Verification fails → thinks order doesn't exist
   - Places new order

2. **Size Mismatch After Partial Exits:**
   - Original order: 8 contracts
   - Partial exit: 4 contracts remain
   - Order still shows 8 contracts (not updated yet)
   - Verification fails (8 != 4)
   - Places new order for 4 contracts
   - **Now have 2 orders: old (8) + new (4)**

3. **Network Latency:**
   - `get_open_orders()` call fails or times out
   - Verification fails
   - Places new order

4. **Order State Transitions:**
   - Order is in "pending" state
   - Not yet in "open" orders list
   - Verification fails
   - Places duplicate

### 3. Multiple Order Placement Paths

**Path 1: Initial Entry (`_execute_entry`)**
- Lines 1259-1341: Places SL with retry logic (5 attempts)
- Lines 1343-1408: Places TP with retry logic (5 attempts)
- **Each retry can create a new order if previous one "failed"**

**Path 2: Watchdog Function (`_ensure_protective_orders_exist`)**
- Lines 2426-2543: Places missing SL (5 attempts)
- Lines 2547-2662: Places missing TP (5 attempts)
- **Called EVERY loop iteration**

**Path 3: Stop Order Updates (`_update_stop_order`)**
- Lines 3471-3700: Updates stop order using "place-first" pattern
- Can create new orders if modify fails
- **Called on break-even, trailing stops, partial exits**

**Path 4: Partial Exit Logic**
- Lines 3200-3450: Places new TP after partial exit
- Can create duplicates if verification fails

**Problem:** No coordination between these paths. Each can independently place orders.

### 4. The Critical Bug: No Tracking of "In-Progress" Orders

**Before Fix:**
```python
# In _ensure_protective_orders_exist()
if not has_stop:
    # Place new order
    # But what if order was just placed 1 second ago?
    # What if verification is still in progress?
    # NO CHECK - just places another one!
```

**The Fix Applied:**
```python
# Check if we already have a tracked order ID
tracked_sl_order_id = self.current_position.get('stop_order_id')
if tracked_sl_order_id and not has_stop:
    # Verify the tracked order actually exists
    # Only place new if tracked order doesn't exist
```

### 5. Race Condition: Concurrent Loop Iterations

**Scenario:**
1. Loop iteration 1: Checks orders → verification fails → starts placing order
2. Loop iteration 2 (30s later): Checks orders → verification still failing → places another order
3. Loop iteration 3: Same thing → another order
4. **Result: Multiple orders placed before any verification completes**

**No Locking:** The `_ensure_protective_orders_exist()` function has no lock to prevent concurrent execution.

### 6. Size Mismatch Detection Logic Flaw

**Location:** `live_trader.py:2264-2289`

**The Logic:**
```python
if order_size == quantity:
    has_stop = True  # Only sets has_stop if EXACT match
else:
    # Order exists but size doesn't match
    # has_stop stays False
    # Will try to place new order!
```

**Problem:** If an order exists with wrong size, the code tries to fix it (lines 2293-2349), but if that fix fails or verification fails, `has_stop` remains `False`, triggering a new order placement.

---

## Evidence from Logs

### Pattern 1: Repeated Verification Failures
```
2026-01-02 12:11:43 - ERROR - FAILED Order 2170442799 verification failed after 2 attempts
2026-01-02 12:11:45 - ERROR - FAILED Order 2170442953 verification failed after 2 attempts
2026-01-02 12:12:51 - ERROR - FAILED Order 2170448294 verification failed after 2 attempts
... (continues every ~60 seconds)
```

### Pattern 2: Multiple Placement Attempts
```
2026-01-02 09:15:42 - INFO - Placing stop loss (attempt 1/5): BID 8 @ $4356.40
2026-01-02 09:15:44 - INFO - Placing stop loss (attempt 2/5): BID 8 @ $4356.50
2026-01-02 09:15:46 - INFO - Placing stop loss (attempt 3/5): BID 8 @ $4356.60
2026-01-02 09:15:48 - INFO - Placing stop loss (attempt 4/5): BID 8 @ $4356.70
2026-01-02 09:15:50 - INFO - Placing stop loss (attempt 5/5): BID 8 @ $4356.80
```

**Each attempt creates a new order if previous one "failed" verification!**

### Pattern 3: Watchdog Triggering Repeatedly
```
# Every 30-60 seconds:
- Position check
- _ensure_protective_orders_exist() called
- Verification fails
- Places new order
- Next iteration: same thing
```

---

## The Fix Applied

### Fix 1: Check Tracked Orders Before Placement

**Location:** `live_trader.py:2426-2443` (SL) and `2547-2564` (TP)

**What It Does:**
1. Before placing a new order, check if we already have a tracked order ID
2. Verify that tracked order actually exists in open orders
3. Only place new order if tracked order doesn't exist
4. This prevents duplicates when verification fails but order actually exists

**Code:**
```python
# CRITICAL FIX: Check if we already have a stop order ID tracked
tracked_sl_order_id = self.current_position.get('stop_order_id')
if tracked_sl_order_id and not has_stop:
    # Verify the tracked order ID exists in open orders
    order_exists = False
    for order in open_orders:
        if order.get('id') == tracked_sl_order_id:
            order_exists = True
            has_stop = True  # Mark as existing
            break
    
    if not order_exists:
        # Clear tracked ID so we can place new one
        self.current_position['stop_order_id'] = None
```

---

## Additional Issues Found

### Issue 1: No Rate Limiting
- Function can place orders every 30-60 seconds
- No cooldown period after failed verification
- **Recommendation:** Add 5-minute cooldown after failed verification

### Issue 2: No Deduplication Logic
- Doesn't check for duplicate orders before placing
- **Recommendation:** Count existing orders of same type before placing

### Issue 3: Verification Too Strict
- Requires exact size match
- Fails if order exists but size is different
- **Recommendation:** Accept order if it exists, even if size differs (fix size separately)

### Issue 4: No Locking
- Multiple loop iterations can call function concurrently
- **Recommendation:** Add execution lock to prevent concurrent placement

### Issue 5: Retry Logic Creates Duplicates
- Each retry attempt creates a new order
- Should cancel previous order before retrying
- **Recommendation:** Cancel previous order before retry

---

## Recommendations for Additional Fixes

### 1. Add Cooldown Period
```python
# Track last placement attempt
if hasattr(self, '_last_order_placement_attempt'):
    time_since_last = time.time() - self._last_order_placement_attempt
    if time_since_last < 300:  # 5 minutes
        logger.debug("Order placement cooldown active - skipping")
        return
```

### 2. Count Existing Orders
```python
# Before placing, count existing orders
existing_sl_count = sum(1 for o in open_orders 
                       if o.get('type') == 4 and o.get('contractId') == self.contract.id)
if existing_sl_count > 0:
    logger.warning(f"Found {existing_sl_count} existing stop orders - not placing new one")
    return
```

### 3. Add Execution Lock
```python
if hasattr(self, '_placing_protective_orders') and self._placing_protective_orders:
    return
self._placing_protective_orders = True
try:
    # ... placement logic ...
finally:
    self._placing_protective_orders = False
```

### 4. Improve Verification Logic
```python
# Accept order if it exists, even if size differs
if order.get('id') == order_id:
    order_size = abs(order.get('size', 0))
    if order_size == expected_size:
        return True
    else:
        # Order exists but size differs - still return True
        # Size will be fixed by watchdog
        logger.warning(f"Order exists but size differs: {order_size} != {expected_size}")
        return True  # Changed from False
```

### 5. Cancel Previous Order Before Retry
```python
for attempt in range(5):
    if attempt > 0:
        # Cancel previous order before retry
        if previous_order_id:
            try:
                self.client.cancel_order(previous_order_id)
            except:
                pass
    # Place new order
    result = self.client.place_stop_order(...)
    if result.get('success'):
        previous_order_id = result.get('orderId')
```

---

## Testing Recommendations

1. **Monitor order counts:** Log total SL/TP orders after each placement
2. **Add alerts:** Alert if order count exceeds position size
3. **Test verification failures:** Simulate API delays to test behavior
4. **Test partial exits:** Verify behavior when position size changes
5. **Load testing:** Run with high-frequency loop iterations

---

## Conclusion

The duplicate order issue was caused by:
1. **Aggressive watchdog** running every iteration
2. **Strict verification** failing due to timing/size mismatches
3. **No tracking** of in-progress orders
4. **No deduplication** before placement
5. **Retry logic** creating duplicates instead of replacing

The fix applied addresses the primary issue (checking tracked orders), but additional safeguards are recommended for production use.

