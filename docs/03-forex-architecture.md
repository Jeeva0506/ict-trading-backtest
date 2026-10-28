# 3. Forex / Indices Bot: Technical Architecture

[← Back to README](../README.md)

## Process model

```
watchdog.py  (long-lived supervisor, one per account)
   │  spawns / restarts
   ▼
run_live.py --account X --strategy orb
   │  1. parse --account → set ACCOUNT_PROFILE env var
   │  2. import config_fundednext → loads .env.<X> (or .env)
   │  3. acquire live/logs/<label>/bot.lock
   │  4. build trader:  orb → LiveTrader   | orb15 → ORB15Trader | smc → SMCTrader
   ▼
LiveTrader.initialize()  →  LiveTrader.run()   (live/mt5_connector.py)
```

`ACCOUNT_PROFILE` **must** be set before `config_fundednext` is imported, because the config reads `.env` at import time. That's why `run_live.py` reads `--account` with a pre-parser before any other import.

## Main classes (live/mt5_connector.py)

| Class | Responsibility |
|---|---|
| `MT5Connector` | Thin wrapper around the `MetaTrader5` API: `connect`, `get_account_info`, `get_current_price`, `get_ohlc_data`, `place_order`, `place_limit_order`, `modify_position`, `close_position`, `get_open_positions` |
| `DrawdownProtection` | Tracks daily and overall DD from equity, gives adaptive risk % |
| `TradingState` | In-memory day state: trades today, traded symbols, flags. Saved to disk so a restart doesn't trade the same symbol twice |
| `LiveTrader` | The bot: main loop, signal generation, pre-trade checks, execution, position management, reporting, Telegram callbacks |

## The main loop: `LiveTrader.run()`

It runs about every 10 seconds, forever:

```
while not shutdown:
    write heartbeat                       → watchdog freeze detection
    check MT5 connection                  → reconnect; verify positions after reconnect
    daily reset (new trading day)         → reset counters, DD baseline
    check AutoTrading button              → Telegram alert if OFF
    scheduled reports                     → daily / weekly summaries
    account + drawdown update
        MAX DD breached   → close everything, stop
        daily DD breached → no new trades today (keep managing open ones)
    market open/close notifications
    for symbol in active (session-open) symbols:
        can, reason = _can_trade(symbol)          # gates, see below
        if can:
            sig = _generate_signal(symbol)        # strategy
            if sig: _execute_signal(symbol, sig)  # sizing + order
    _manage_positions()                   # BE, trailing, force-close, journal
    sleep
```

## Signal pipeline: `_generate_signal(symbol)`

1. Fetch the last 500 **M1** bars from MT5.
2. Work out today's session in **broker time**. The index's local open/close (for example 09:30–16:00 `America/New_York`) is converted using the broker's measured UTC offset, so DST is handled. If that fails, the static `mt5_open_hour` values are used instead.
3. Slice the session bars and get the **HTF trend** (EMA 50/200 on H1).
4. Call `StrategyICTClean.generate_signals(session_data, htf_trend=...)`.
5. Take the first signal (1 trade per day) and reject it if:
   - it's **stale**: more than 15 bars old
   - **price drift** is too large: the current price is more than 1R away from the signal price
6. Return the **signal dict** (the contract every strategy must meet):

```python
{
  "direction":   "bullish" | "bearish",   # REQUIRED
  "entry_price": float,                   # REQUIRED  expected entry
  "stop_loss":   float,                   # REQUIRED  absolute price
  "tp":          float,                   # REQUIRED  absolute price
  "risk_points": float,                   # REQUIRED  |entry - stop_loss|
  "entry_time":  timestamp,
  "signal_time": str, "signal_price": float, "breakout_time": str,
  "htf_trend":   str, "orb_range": float,
  "session_end": str,                     # used for force-close
}
```

### The ORB strategy (`strategy/strategy_ict_clean.py`)
- **Opening range** = high/low of the first `orb_minutes` (15) after the open.
- **Breakout**: a candle **closes** beyond the range within `breakout_window_minutes` (60), and must pass these filters:
  - displacement ≥ `disp_mult` × the average range
  - body ≥ `body_min` of the candle
  - close in the outer `close_strength` part of the candle
  - close beyond the level by `breakout_margin` × the range
  - minimum range size: `orb_range_min_mult`
- **HTF filter**: trade only in the H1 EMA trend direction.
- **SL** = candle extreme ± `sl_buffer`, which must be between `min_sl_points` and `max_sl_points`.
- **Exits**: TP at 2.5R. SL moves to break-even at 1R, then trails 0.5R behind. Force-close before the session ends.

All these parameters are set per symbol in `PROFITABLE_INDICES` in `config_fundednext.py`.

## Pre-trade gates: `_can_trade(symbol)`

A signal is only acted on if **all** of these pass:

| Gate | Blocks when |
|---|---|
| trading disabled / paused | `/pause` was sent, or the bot disabled itself |
| SL protection pause | a stop-loss integrity problem is being resolved |
| market closed | outside the symbol's session |
| daily limit hit | `DAILY_DRAWDOWN_STOP` reached |
| already traded today | 1 trade per symbol per day |
| max daily trades | `max_trades_per_day` (2) |
| DD limit | overall or daily DD over the limit |
| edge guard | rolling PF / win rate fell below thresholds (if enabled) |
| max open positions | `max_open_positions` (2) |
| position already open | on this symbol |
| news | high-impact event within the buffer (ForexFactory calendar) |

If the news calendar can't be fetched, the bot **blocks trading** (fail-safe) and retries.

## Execution: `_execute_signal(symbol, sig)`

1. **Risk %**: from `ADAPTIVE_RISK` (tiered by drawdown) or `RISK_PER_TRADE`.
2. **Lot size** = (balance × risk%) ÷ (`risk_points` × `point_value`), rounded to the broker's volume step and capped.
3. **Spread guard**: skip if the spread is too wide compared to R.
4. **Order type**: market, limit (`USE_LIMIT_ORDERS`) or marketable-limit (`USE_MARKETABLE_LIMIT`, slippage capped at `MARKETABLE_CAP_R`).
5. SL and TP are sent **with the order** (broker-side). Partial fills are detected.
6. The trade is recorded in the state, journal and Telegram.

## Position management: `_manage_positions()`
- **Closed externally** (TP, SL or manual): reads the real exit from deal history, journals it, and sends a Telegram alert.
- **Break-even** at 1R profit, then **trail** the SL 0.5R behind the best price.
- **Force close** 15 minutes before the session ends. This works out session end in DST-proof broker time.
- `sl_protection.py` re-checks that every position still has its correct SL and fixes or alerts if not.

## Supporting modules

| Module | Key behaviour |
|---|---|
| `news_filter.py` | Downloads the ForexFactory high-impact calendar, caches it to disk, and maps currencies to indices (USD→US indices, EUR→GER30/FRA40, …) |
| `edge_guard.py` | Rolling metrics over the last N trades. Pauses if the edge degrades. Records missed trades with reasons. |
| `trade_journal.py` | Writes `trade_journal.xlsx` and sends daily/weekly reports |
| `telegram_commands.py` | Polls Telegram, checks the chat ID, and dispatches to `LiveTrader._on_*` callbacks |
| `watchdog.py` | Process supervision, MT5 auto-start, CPU/RAM alerts, trading-window scheduling, PID lock |
| `config_guard.py` | Checks that the two config copies match (see doc 1) |

## Persistence & restart safety
- Day state is saved to disk on every change, so a restart mid-day doesn't re-enter a symbol that already traded.
- On reconnect, open positions are re-verified against the broker.
- The heartbeat file goes stale when the bot is stopped. That alone isn't an error.
