# 4. Forex / Indices Bot: Build Your Own Strategy

[← Back to README](../README.md)

The live engine (`LiveTrader`) already does the hard parts:
- MT5 connection
- sizing
- the drawdown, news, spread, SL and edge guards
- break-even, trailing and force-close
- the journal and Telegram

A new strategy only has to answer one question: **"is there a trade on this symbol right now, and where are entry, SL and TP?"**

You do that by **subclassing `LiveTrader` and overriding `_generate_signal()`**. Don't edit `mt5_connector.py`. This is the pattern the repo's own `orb15_trader.py` and `smc_trader.py` use.

---

## Step 1: Write the signal logic as a pure function

Keep the logic free of MT5 calls, so the **same function** runs in the backtest and live:

```python
# strategy/my_strategy.py
from dataclasses import dataclass
from typing import Optional
import pandas as pd

@dataclass
class MySignal:
    direction: str        # "bullish" | "bearish"
    entry_time: pd.Timestamp
    entry_price: float
    stop_loss: float
    tp: float
    risk_points: float

def generate_signal(session: pd.DataFrame, params: dict) -> Optional[MySignal]:
    """
    session: today's M1 bars for one symbol, columns open/high/low/close,
             index = broker-time timestamps, ONLY bars that have already CLOSED.
    Must only use data up to and including the last row (no lookahead).
    """
    n = params.get("range_minutes", 30)
    if len(session) < n + 1:
        return None
    rng = session.iloc[:n]
    hi, lo = rng["high"].max(), rng["low"].min()

    last = session.iloc[-1]                         # the bar that just closed
    if last["close"] > hi:                          # breakout up
        sl = lo
        risk = last["close"] - sl
        if risk <= 0:
            return None
        return MySignal("bullish", session.index[-1], float(last["close"]),
                        float(sl), float(last["close"] + 2 * risk), float(risk))
    if last["close"] < lo:                          # breakout down
        sl = hi
        risk = sl - last["close"]
        if risk <= 0:
            return None
        return MySignal("bearish", session.index[-1], float(last["close"]),
                        float(sl), float(last["close"] - 2 * risk), float(risk))
    return None
```

**Rules for the signal function:**
- Decide only on **closed** bars. The bar that is still forming must not change the decision.
- Never look at rows after the decision bar. (See doc 8 for how a hidden "look ahead 20 bars" destroyed a strategy in this repo.)
- Return absolute prices for SL and TP, plus `risk_points = |entry − SL|`.

## Step 2: Wrap it in a LiveTrader subclass

```python
# live/my_trader.py
from __future__ import annotations
from datetime import datetime
from typing import Optional
import pandas as pd
import MetaTrader5 as mt5

from mt5_connector import LiveTrader, logger, get_index_config
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from strategy.my_strategy import generate_signal

PARAMS = {"range_minutes": 30}

class MyTrader(LiveTrader):
    """Only the signal changes. Every guard, the sizing and the exits are inherited."""

    def _generate_signal(self, symbol: str) -> Optional[dict]:
        cfg = get_index_config(symbol)
        if not cfg:
            return None

        m1 = self.mt5.get_ohlc_data(symbol, mt5.TIMEFRAME_M1, count=600)
        if m1.empty:
            return None
        m1 = m1.iloc[:-1]            # drop the still-forming bar

        # Today's session in broker time (copy the DST-proof logic from
        # LiveTrader._generate_signal, or use the static fallback):
        today = m1.index[-1].date()
        s_start = pd.Timestamp(datetime(today.year, today.month, today.day,
                                        cfg["mt5_open_hour"], cfg["mt5_open_minute"]))
        s_end = pd.Timestamp(datetime(today.year, today.month, today.day,
                                      cfg["mt5_close_hour"], cfg["mt5_close_minute"]))
        session = m1[(m1.index >= s_start) & (m1.index < s_end)]

        sig = generate_signal(session, PARAMS)
        if sig is None:
            return None

        # Only act on a signal from the bar that JUST closed
        if session.index[-1] != sig.entry_time:
            return None

        # Respect the symbol's SL bounds (the inherited sizing assumes sane R)
        if not (cfg["min_sl_points"] <= sig.risk_points <= cfg["max_sl_points"]):
            logger.info(f"{symbol}: R={sig.risk_points:.1f} outside SL bounds, skipped")
            return None

        return {
            "direction": sig.direction,
            "entry_price": sig.entry_price,
            "stop_loss": sig.stop_loss,
            "tp": sig.tp,
            "risk_points": sig.risk_points,
            "entry_time": sig.entry_time,
            "signal_time": str(sig.entry_time),
            "signal_price": sig.entry_price,
            "breakout_time": str(sig.entry_time),
            "htf_trend": self._get_htf_trend(symbol) or "",
            "orb_range": 0.0,
            "session_end": str(s_end),
        }
```

## Step 3: Register it in `run_live.py`

Add your strategy name to **both** `choices` lists (the pre-parser near the top and the parser in `main()`) and to the dispatch in `run_live()`:

```python
_pre_parser.add_argument('--strategy', type=str, default='orb',
                         choices=['orb', 'orb15', 'smc', 'mine'])
...
    if _strat == 'smc':
        ...
    elif _strat == 'mine':
        from live.my_trader import MyTrader
        trader = MyTrader()
    else:
        trader = LiveTrader()
```

Then run it:
```bat
python run_live.py --account demo --strategy mine --test
python watchdog.py --account demo --strategy mine
```

## Step 4: What you can still customise

| Want to change… | Where |
|---|---|
| Which symbols / sessions | `PROFITABLE_INDICES` in **both** `config_fundednext.py` copies, or `EXCLUDE_INDICES` |
| Per-symbol SL bounds | `min_sl_points` / `max_sl_points` in the symbol's config |
| Risk % | `.env` `RISK_PER_TRADE`, or `ADAPTIVE_RISK` in config |
| Trades per day / max open | `FUNDEDNEXT_RULES` in config |
| BE / trailing / force-close behaviour | Override `_manage_positions()` in your subclass. Copy the parent's code and adjust it. |
| Extra pre-trade filter | Override `_can_trade()`: call `super()._can_trade(symbol)` first, then add your check |

## Step 5: Backtest before going live

The quickest way is a small loop that feeds your pure function one bar at a time (so lookahead can't happen), then simulates SL/TP on the bars **after** entry:

```python
import pandas as pd
from strategy.my_strategy import generate_signal

df = pd.read_csv("forexDATA/EURUSD_3YEAR_M1.csv", parse_dates=["time"], index_col="time")
trades = []
for day, bars in df.groupby(df.index.date):
    session = bars.between_time("08:00", "16:00")
    for i in range(1, len(session)):
        sig = generate_signal(session.iloc[:i + 1], {"range_minutes": 30})  # only past bars
        if sig:
            future = session.iloc[i + 1:]           # fill on the NEXT bar
            if future.empty:
                break
            entry = future.iloc[0]["open"]
            # walk forward bar by bar; SL first if both touched in one bar (pessimistic)
            ...
            trades.append(...)
            break                                   # 1 trade/day
```

For index CFDs, `backtest_live_setup.py` has a full portfolio simulator with spread, slippage, FundedNext DD rules and per-year stats. Its entry point is `run_portfolio_backtest()`. You can swap `StrategyICTClean` for your class, as long as it exposes `generate_signals(df, htf_trend=None)` and returns objects with the `TradeSignalICT` fields.

**Before trusting any result, go through the checklist in [doc 8](08-backtesting-and-pitfalls.md).**
