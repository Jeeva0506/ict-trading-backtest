# 7. Indian Market Bot: Build Your Own Strategy

[← Back to README](../README.md)

The trader (`ORBSweepTrader`) already handles the following, all of which are strategy-agnostic:
- the Zerodha connection
- option strike and expiry lookup
- sizing
- premium-based exits
- the kill switch and safety checks
- the journals and Telegram

The strategy only needs to say **"at this closed 1-minute bar, buy a CALL or PUT, with this spot SL"**.

## The contract

A strategy class needs these methods, the same ones `ORBSweepStrategy` has:

```python
class MyStrategy:
    def __init__(self, config: TradingConfig): ...
    def reset_day(self): ...                                    # clear per-day state
    def compute_orb(self, df_1m, trade_date) -> ORBLevels|None  # called once at 9:30
    def check_regime(self, df_1m, trade_date) -> bool           # True = allowed to trade today
    def atm_strike(self, spot: float) -> int
    def scan_bar(self, df_1m, bar_idx, local_pos=-1) -> TradeSignal|None
```

`df_1m` has columns `Open, High, Low, Close, Volume` (capitalised) with an IST timestamp index. `scan_bar` **may only look at rows `<= bar_idx`**.

`TradeSignal` (in `orb_sweep/strategy.py`):
```python
TradeSignal(direction="CALL"|"PUT", entry_spot, stop_loss, tp1, tp2,
            r_value, strike, vwap_at_entry, sweep_price, bar_time, entry_mode="")
```

## Step 1: Write the strategy

The easiest way is to subclass `ORBSweepStrategy` and replace only `scan_bar`. You keep the ORB and regime logic for free.

```python
# indian_market/orb_sweep/strategy_mine.py
from typing import Optional
import pandas as pd
from orb_sweep.strategy import ORBSweepStrategy, TradeSignal
from orb_sweep.indicators import vwap

class EMAPullbackStrategy(ORBSweepStrategy):
    """Example: after an ORB-high breakout, buy a CALL on the first pullback to the 9-EMA."""

    def scan_bar(self, df_1m: pd.DataFrame, bar_idx: int, local_pos: int = -1) -> Optional[TradeSignal]:
        if self.orb is None or not self.regime_pass:
            return None
        if self._signal_fired_today["CALL"]:
            return None

        t = df_1m.index[bar_idx]
        if not (pd.Timestamp("09:30").time() <= t.time() < pd.Timestamp("14:00").time()):
            return None

        today = df_1m.iloc[: bar_idx + 1]                 # ← only the past and the current bar
        today = today[today.index.date == t.date()]
        ema9 = today["Close"].ewm(span=9, adjust=False).mean()
        bar = today.iloc[-1]

        broke_out = (today["Close"] > self.orb.orb_high).any()
        touched_ema = bar["Low"] <= ema9.iloc[-1] < bar["Close"]
        if not (broke_out and touched_ema):
            return None

        entry = float(bar["Close"])
        sl = float(min(today["Low"].iloc[-5:]))           # recent swing low
        r = entry - sl
        if r <= 0:
            return None

        self._signal_fired_today["CALL"] = True
        return TradeSignal(
            direction="CALL", entry_spot=entry, stop_loss=sl,
            tp1=entry + self.cfg.tp1_r_multiple * r,
            tp2=entry + self.cfg.tp2_r_multiple * r,
            r_value=r, strike=self.atm_strike(entry),
            vwap_at_entry=float(vwap(today).iloc[-1]),
            sweep_price=0.0, bar_time=t, entry_mode="ema_pullback",
        )
```

> The base class stores the config as `self.cfg`, today's ORB as `self.orb`, the regime result as `self.regime_pass` and the per-direction flags as `self._signal_fired_today`.

## Step 2: Plug it into the trader

`ORBSweepTrader.__init__` creates `self.strategy = ORBSweepStrategy(cfg)`. Swap it in a small launcher instead of editing the trader:

```python
# indian_market/start_my_strategy.py
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from orb_sweep.trader import ORBSweepTrader
from orb_sweep.config import TradingConfig
from orb_sweep.strategy_mine import EMAPullbackStrategy

cfg = TradingConfig()            # paper_mode=True by default
trader = ORBSweepTrader(cfg)
trader.strategy = EMAPullbackStrategy(cfg)
trader.run()
```

## Step 3: Tune exits and filters without code

Edit `TradingConfig` (`orb_sweep/config.py`) or override fields when you create it:
```python
cfg = TradingConfig(min_atr_5m=15, call_tp_pct=0.6, call_sl_pct=0.25, max_trades_per_day=2)
```

To turn off the expiry straddle, set the straddle filters so they can never pass, e.g. `trader.straddle_cfg.max_vix = 0`.

## Step 4: Backtest it

`orb_sweep/backtest.py` (`ORBSweepBacktester`) replays the strategy bar by bar over a 1-minute Nifty CSV. It prices options synthetically (Black-Scholes with India VIX) and applies Zerodha costs.

1. Download data (needs a valid access token):
   ```bat
   cd indian_market
   python data/zerodha_data.py         :: writes Nifty 1m + VIX CSVs into data/
   ```
   The backtester expects `data/zerodha_nifty_1m.csv` and `data/zerodha_vix_day.csv`. Override them with `--data` / `--vix`.
2. Point the backtester at your class: set `bt.strategy = EMAPullbackStrategy(cfg)` after you construct `ORBSweepBacktester(cfg)`, or copy `cmd_backtest` in `run.py`.
3. Run:
   ```bat
   python -m orb_sweep.run backtest --start 2023-01-01
   python -m orb_sweep.run walkforward
   ```

**The project's own validation rule:** optimise on 3 years, then validate unchanged on 10 years (out of sample). Continue only if PF > 2 and WR > 55% hold on both. Read [doc 8](08-backtesting-and-pitfalls.md).

## Using the research strategies
`strategies/*.py` (CPR, NR7, PO3, VRDOB…) are standalone research classes driven by the `run_*.py` scripts and `backtest/` engines. They do **not** follow the `scan_bar` contract. To trade one live, port its entry rule into a `scan_bar` subclass as shown above.
