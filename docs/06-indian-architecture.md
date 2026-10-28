# 6. Indian Market Bot: Technical Architecture

[← Back to README](../README.md)

## Components

```
start_paper_trading.py
   └─ ORBSweepTrader(TradingConfig())          orb_sweep/trader.py
         ├─ ZerodhaBroker      broker.py        Kite API: 1m/5m bars, VIX, LTP, option lookup, orders
         ├─ ORBSweepStrategy   strategy.py      ORB levels, regime filter, per-bar signal scan
         ├─ RiskManager        risk_manager.py  daily/weekly loss limits, trades/day
         ├─ Safety layer       safety.py        BrokerAPIGuard, PositionReconciler, kill switch
         ├─ TradeLogger        trade_logger.py  SQLite (data/trades.db)
         ├─ ExcelJournal       excel_journal.py Excel journal (+ Black-Scholes delta)
         └─ Alerts             alerts.py        Telegram
```

## Main loop: `ORBSweepTrader.run()` → `_tick()` every 15 seconds

```
skip weekends
reset day state (on a new date)
kill switch active? → force exit everything, idle
before 9:15  → wait
9:15–9:30   → wait (ORB forming)
at 9:30 (once):
    fetch 5 days of 1m bars
    strategy.compute_orb()        → ORB high/low, range %
    broker.fetch_vix()
    strategy.check_regime()       → range% ≥ min_range_pct AND 5m ATR ≥ min_atr_5m
    if expiry day and filters OK  → _enter_straddle()
straddle open → _manage_straddle()
otherwise:
    fetch 1m bars
    if no position and risk_mgr.can_trade():
        for each post-9:30 bar: signal = strategy.scan_bar(df, idx)
        first signal → _enter_sweep_trade(signal)
    position open → _manage_sweep_position(spot)
hard-exit times → _force_exit_all()
after close → _run_eod()
```

State (open position, flags) is written to `data/state.json` on every change and loaded on start, so a crash or restart mid-trade picks the position back up.

## Strategy: `ORBSweepStrategy` (strategy.py)

**Opening range**: high/low of 9:15–9:30.

**Regime filter**: trade only on days with enough movement. Both of these must hold:
- ORB range % ≥ `min_range_pct` (0.25%)
- 5-minute ATR(14) ≥ `min_atr_5m` (18 points)

**`scan_bar()`** is called per closed 1-minute bar between 9:30 and 15:15. It returns at most one CALL and one PUT per day:

| Signal | Conditions | Stop (spot) |
|---|---|---|
| **CALL** (momentum) | close > ORB high, price above VWAP, breaks the previous swing high, volume > `volume_multiplier` × volume SMA | ORB low |
| **PUT** (sweep reversal) | price trades above the ORB high, then **closes back below it within `sweep_closeback_bars` (5)**, price below VWAP, breaks the previous swing low | the sweep high |

The output is a `TradeSignal`: direction, entry spot, SL, TP1/TP2, R, **ATM strike** (spot rounded to `strike_step`), VWAP and bar time.

## Entries and exits

**Sweep trade (`_enter_sweep_trade`)**
- Finds the ATM CE/PE on NFO with the nearest weekly expiry (`broker.find_option_token`).
- Quantity is sized from `risk_per_trade` and the premium-based stop, in multiples of `lot_size`.
- Orders are split above `order_freeze_qty`.
- Paper mode records the fill at the option's LTP. Live mode sends a market order.

**Exits (`_manage_sweep_position` / `_manage_premium_exits`)**: premium-based, with separate settings for each direction:

| | CALL | PUT |
|---|---|---|
| Stop | −30% premium | −30% premium |
| Target | +50% | +75% |
| Trailing starts at | +20% | +25% |
| Trail distance | 15% | 20% |

Plus spot-based TP1 (1R, exits 50%) and the hard time exits.

**Expiry straddle (`_enter_straddle` / `_manage_straddle`)**
- Sells the ATM CE + PE at about 9:31.
- Exits when combined premium is +50% (stop) or −40% (target), with a trailing stop, or at 15:10.
- Risk is 3% of capital.

## Safety layer (safety.py)

| Piece | What it does |
|---|---|
| `BrokerAPIGuard` | Wraps every Kite call with retries and backoff. Tracks consecutive failures, sets **outage** mode and alerts. |
| `PositionReconciler` | Compares internal positions with the broker's, flags mismatches (e.g. a manual exit), and corrects the state |
| Kill switch | Daily loss ≥ `daily_dd_limit` or weekly ≥ `weekly_dd_limit` → flatten and stop for the day |
| Guards | VIX halt, spread check (`max_spread_pct`), margin buffer, Zerodha rate limits, no entries after 14:00 |

## Paper vs live
`ZerodhaBroker.place_buy` / `place_sell` / `_get_positions` / `_get_orders` / `_cancel_order` all check `cfg.paper_mode`. When it's on, they simulate the call and return fake order IDs. **Market data is always real.**

## Costs
`costs/zerodha.py` models Zerodha F&O charges: brokerage, STT, exchange fees, GST, SEBI fees and stamp duty. Backtest P&L is reported **after** these.

## Research code (not used by the live trader)

| Path | Contents |
|---|---|
| `strategies/` | ORB variants, CPR pivot, NR7 positional, PO3, straddle, VRDOB |
| `backtest/` | `engine.py`, `intraday_engine.py`, `straddle_engine.py`, `option_pricer.py` (Black-Scholes with India VIX as IV) |
| `run_*.py` | One script per experiment. These need CSVs in `data/` and write to `results/` |
| `live/paper_trader.py`, `live/po3_paper.py`, `live/nr7_tracker.py` | Earlier paper traders for other strategies |
