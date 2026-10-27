"""
NR7 Trailing SL Comparison — 8 methods on cached data
======================================================
Compares trailing stop approaches without touching the live paper trader.

Methods tested:
  ema50    : 50 EMA trail (current baseline)
  sma20    : 20 SMA trail (faster)
  sma50    : 50 SMA trail
  atr1.5   : Chandelier exit — highest_high_since_entry − 1.5×ATR
  atr2.0   : Chandelier exit — highest_high_since_entry − 2.0×ATR
  atr3.0   : Chandelier exit — highest_high_since_entry − 3.0×ATR (Wilder classic)
  be_ema50 : Lock breakeven at +1R profit, then trail with 50 EMA
  psar     : Parabolic SAR — Welles Wilder (AF 0.02 → 0.20, step 0.02)

Run:
    python run_nr7_trail_comparison.py --cached        # use existing cache (fast)
    python run_nr7_trail_comparison.py                 # re-download (slow)
    python run_nr7_trail_comparison.py --cached --vol  # vol_surge filter only
"""

import sys
import time
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from strategies.nr7_positional import (
    compute_indicators, generate_signals,
    CAPITAL, RISK_RS, MAX_PCT_PER_TRADE,
    FILTERS_ALL,
)
from costs.zerodha_equity import equity_round_trip_cost

# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK_YEARS   = 3
MIN_TRADING_DAYS = 400
NIFTY50_TOKEN    = 256265
CACHE_DIR        = ROOT / "data" / "nr7_cache"

TRAIL_METHODS = {
    'ema50':    '50 EMA trail        (current baseline)',
    'sma20':    '20 SMA trail        (faster)',
    'sma50':    '50 SMA trail',
    'atr1.5':   'Chandelier 1.5xATR  (tight)',
    'atr2.0':   'Chandelier 2.0xATR  (balanced)',
    'atr3.0':   'Chandelier 3.0xATR  (Wilder classic)',
    'be_ema50': 'Breakeven+EMA50     (lock at 1R then trail)',
    'psar':     'Parabolic SAR       (AF 0.02->0.20)',
}


# ── Extra indicators (additive, never modifies existing columns) ───────────────
def _add_trail_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Append SMA20 and SMA50; EMA50 and ATR14 already exist from compute_indicators."""
    df = df.copy()
    df['sma20'] = df['Close'].rolling(20, min_periods=10).mean()
    df['sma50'] = df['Close'].rolling(50, min_periods=25).mean()
    return df


# ── Parabolic SAR state helper ────────────────────────────────────────────────
class _PSAR:
    """Long-only Parabolic SAR tracker for a single open trade."""

    def __init__(self, initial_sl: float, entry_high: float,
                 af_step: float = 0.02, af_max: float = 0.20):
        self.sar      = initial_sl   # starts at initial stop loss (below entry)
        self.ep       = entry_high   # extreme point = first high
        self.af       = af_step
        self._step    = af_step
        self._max     = af_max
        self._prev_low = initial_sl  # for Wilder's two-prior-low constraint

    def step(self, high: float, low: float) -> float:
        """Advance one bar. Returns new SAR value."""
        new_sar = self.sar + self.af * (self.ep - self.sar)

        # Wilder's rule: SAR cannot exceed the prior two bars' lows
        new_sar = min(new_sar, self._prev_low, low)
        self._prev_low = low

        # Update EP and AF if new high
        if high > self.ep:
            self.ep = high
            self.af = min(self.af + self._step, self._max)

        self.sar = new_sar
        return new_sar


# ── Core simulation with pluggable trail ──────────────────────────────────────
def simulate_trades_trail(df: pd.DataFrame, signals: list,
                          trail_mode: str = 'ema50',
                          starting_capital: float = CAPITAL) -> list:
    """
    Simulate NR7 positional trades with the chosen trailing SL method.
    Entry at next day's open (same as the original simulate_trades).
    Trail SL is raised each bar, never lowered. Exit when Low <= SL.
    """
    trades, open_trade = [], None
    capital = starting_capital
    sig_map = {s['date']: s for s in signals}

    # Parabolic SAR state lives here when active
    psar_state: _PSAR | None = None

    for i in range(len(df)):
        row     = df.iloc[i]
        dt      = df.index[i]
        prev_dt = df.index[i - 1] if i > 0 else None

        # ── Trail SL update + exit check ─────────────────────────────────────
        if open_trade is not None:
            entry        = open_trade['entry_price']
            initial_risk = open_trade['initial_risk']
            high         = float(row['High'])
            low          = float(row['Low'])

            # Track highest high for ATR-based trails
            if high > open_trade.get('high_water', high):
                open_trade['high_water'] = high
            hw = open_trade.get('high_water', high)

            # ── Compute candidate new SL per method ──────────────────────────
            if trail_mode == 'ema50':
                candidate = float(row['ema50'])

            elif trail_mode == 'sma20':
                v = row.get('sma20', np.nan)
                candidate = float(v) if not np.isnan(v) else open_trade['sl']

            elif trail_mode == 'sma50':
                v = row.get('sma50', np.nan)
                candidate = float(v) if not np.isnan(v) else open_trade['sl']

            elif trail_mode == 'atr1.5':
                atr = float(row['atr14'])
                candidate = hw - 1.5 * atr

            elif trail_mode == 'atr2.0':
                atr = float(row['atr14'])
                candidate = hw - 2.0 * atr

            elif trail_mode == 'atr3.0':
                atr = float(row['atr14'])
                candidate = hw - 3.0 * atr

            elif trail_mode == 'be_ema50':
                # Lock breakeven when unrealized gain reaches 1R
                ema_sl = float(row['ema50'])
                if not open_trade.get('be_triggered') and high >= entry + initial_risk:
                    open_trade['be_triggered'] = True
                floor = entry if open_trade.get('be_triggered') else 0.0
                candidate = max(ema_sl, floor)

            elif trail_mode == 'psar':
                if psar_state is None:
                    psar_state = _PSAR(open_trade['sl'], float(row['High']))
                candidate = psar_state.step(high, low)

            else:
                raise ValueError(f"Unknown trail_mode: {trail_mode!r}")

            # Raise SL, never lower
            new_sl = max(open_trade['sl'], candidate)
            open_trade['sl'] = new_sl

            # Exit if low touches SL
            if low <= new_sl:
                exit_p = new_sl
                qty    = open_trade['qty']
                gross  = (exit_p - entry) * qty
                cost   = equity_round_trip_cost(entry, max(exit_p, 0.01), qty)
                net    = gross - cost
                open_trade.update({
                    'exit_date':   dt,
                    'exit_price':  round(exit_p, 2),
                    'exit_reason': 'SL',
                    'gross_rs':    round(gross, 2),
                    'cost_rs':     round(cost, 2),
                    'net_rs':      round(net, 2),
                    'r_multiple':  round(net / RISK_RS, 2),
                    'win':         net > 0,
                    'trail_mode':  trail_mode,
                })
                trades.append(open_trade)
                capital += net
                open_trade = None
                psar_state = None

        # ── New entry ────────────────────────────────────────────────────────
        if open_trade is None and prev_dt is not None and prev_dt in sig_map:
            sig   = sig_map[prev_dt]
            entry = float(row['Open'])
            sl    = sig['stop_loss']
            if entry <= sl:
                continue
            distance = entry - sl
            qty = min(
                int(RISK_RS / distance),
                int(capital * MAX_PCT_PER_TRADE / entry),
            )
            if qty < 1 or capital < 5_000:
                continue
            open_trade = {
                'entry_date':   dt,
                'entry_price':  entry,
                'sl':           sl,
                'initial_risk': distance,
                'qty':          qty,
                'high_water':   float(row['High']),
                'symbol':       sig.get('symbol', ''),
                'score':        sig['score'],
                'atr_at_entry': sig['atr'],
            }
            psar_state = None  # fresh on entry

    # Force-close open trade at data end
    if open_trade is not None:
        exit_p = float(df['Close'].iloc[-1])
        qty    = open_trade['qty']
        entry  = open_trade['entry_price']
        gross  = (exit_p - entry) * qty
        cost   = equity_round_trip_cost(entry, max(exit_p, 0.01), qty)
        net    = gross - cost
        open_trade.update({
            'exit_date':   df.index[-1],
            'exit_price':  round(exit_p, 2),
            'exit_reason': 'DATA_END',
            'gross_rs':    round(gross, 2),
            'cost_rs':     round(cost, 2),
            'net_rs':      round(net, 2),
            'r_multiple':  round(net / RISK_RS, 2),
            'win':         net > 0,
            'trail_mode':  trail_mode,
        })
        trades.append(open_trade)

    return trades


# ── Run one trail mode across all stocks ────────────────────────────────────
def _run_one_trail(stock_data: dict, nifty_df: pd.DataFrame,
                   filters: dict, trail_mode: str) -> dict:
    all_trades = []
    for sym, df in stock_data.items():
        df_ind  = compute_indicators(df)
        df_ind  = _add_trail_indicators(df_ind)
        sigs    = generate_signals(df_ind, nifty_df=nifty_df, filters=filters)
        for s in sigs:
            s['symbol'] = sym
        trades = [
            t for t in simulate_trades_trail(df_ind, sigs, trail_mode=trail_mode)
            if t.get('exit_reason') != 'DATA_END'
        ]
        all_trades.extend(trades)

    if not all_trades:
        return {'n': 0, 'wr': 0, 'pf': 0, 'net': 0, 'avg_r': 0,
                'avg_bars': 0, 'max_dd': 0, 'trades': []}

    wins   = [t for t in all_trades if t['win']]
    losses = [t for t in all_trades if not t['win']]
    gw     = sum(t['net_rs'] for t in wins)
    gl     = sum(abs(t['net_rs']) for t in losses)
    net    = gw - gl
    pf     = gw / gl if gl > 0 else float('inf')
    wr     = len(wins) / len(all_trades) * 100
    avg_r  = sum(t['r_multiple'] for t in all_trades) / len(all_trades)

    # Average holding days
    bars = []
    for t in all_trades:
        try:
            d = (pd.Timestamp(t['exit_date']) - pd.Timestamp(t['entry_date'])).days
            bars.append(d)
        except Exception:
            pass
    avg_bars = sum(bars) / len(bars) if bars else 0

    # Max drawdown on running capital curve
    running = CAPITAL
    peak    = CAPITAL
    max_dd  = 0.0
    for t in sorted(all_trades, key=lambda x: x['entry_date']):
        running += t['net_rs']
        if running > peak:
            peak = running
        dd = (peak - running) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return {
        'n': len(all_trades), 'wr': wr, 'pf': pf,
        'net': net, 'avg_r': avg_r, 'avg_days': avg_bars,
        'max_dd': max_dd, 'trades': all_trades,
    }


# ── Zerodha helpers (same as run_nr7_backtest.py) ────────────────────────────
def _get_kite():
    from kiteconnect import KiteConnect
    env = {}
    for line in (ROOT.parent / "backtest_engine" / ".env.zerodha").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    kite = KiteConnect(api_key=env["ZERODHA_API_KEY"])
    kite.set_access_token(env["ZERODHA_ACCESS_TOKEN"])
    return kite


def _fetch_daily(kite, token: int, days: int) -> pd.DataFrame:
    end, start = datetime.now(), datetime.now() - timedelta(days=days)
    try:
        rows = kite.historical_data(token, start, end, "day", continuous=False, oi=False)
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date").sort_index()[["Open", "High", "Low", "Close", "Volume"]]


def _universe(kite) -> list:
    from run_nr7_backtest import _nifty500_symbols
    stocks = [i for i in kite.instruments("NSE")
              if i["instrument_type"] == "EQ" and i["segment"] == "NSE" and i["lot_size"] == 1]
    syms = _nifty500_symbols()
    return [{"token": s["instrument_token"], "symbol": s["tradingsymbol"]}
            for s in stocks if s["tradingsymbol"] in syms]


def _load_cache() -> dict:
    out = {}
    for fpath in CACHE_DIR.glob("*.csv"):
        sym = fpath.stem
        df  = pd.read_csv(fpath, index_col="Date", parse_dates=True)
        if len(df) >= MIN_TRADING_DAYS:
            out[sym] = df
    return out


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    use_cache = "--cached" in sys.argv
    vol_only  = "--vol"    in sys.argv

    filters = dict(FILTERS_ALL)
    if vol_only:
        filters = {k: False for k in filters}
        filters['vol_surge'] = True
        filters['ema_cross'] = True
        print("Filter mode: vol_surge + ema_cross only\n")
    else:
        print("Filter mode: ALL filters ON\n")

    print("=" * 70)
    print("  NR7 TRAILING SL COMPARISON")
    print(f"  Capital: Rs{CAPITAL:,}  |  Risk/trade: Rs{RISK_RS:,}")
    print("=" * 70)

    # Load data
    if use_cache:
        stock_data = _load_cache()
        nifty_df   = pd.DataFrame()
        print(f"Loaded {len(stock_data)} stocks from cache (--cached)\n")
    else:
        kite       = _get_kite()
        universe   = _universe(kite)
        days       = LOOKBACK_YEARS * 365 + 60
        nifty_raw  = _fetch_daily(kite, NIFTY50_TOKEN, days=days)
        nifty_df   = compute_indicators(nifty_raw) if not nifty_raw.empty else pd.DataFrame()
        # Use cached stock data if available, else skip download prompt
        stock_data = _load_cache()
        print(f"Loaded {len(stock_data)} stocks from cache\n")

    if not stock_data:
        print("ERROR: No cached data found. Run run_nr7_backtest.py first.")
        return

    # Nifty from cache if not loaded
    if nifty_df.empty:
        nifty_path = CACHE_DIR / "NIFTY50.csv"
        if nifty_path.exists():
            nifty_df = compute_indicators(
                pd.read_csv(nifty_path, index_col="Date", parse_dates=True)
            )

    # Run all trail methods
    results = {}
    for mode, label in TRAIL_METHODS.items():
        t0 = time.time()
        r  = _run_one_trail(stock_data, nifty_df, filters, trail_mode=mode)
        elapsed = time.time() - t0
        results[mode] = r
        print(f"  [{mode:<10}] n={r['n']:>4}  WR={r['wr']:>5.1f}%  "
              f"PF={r['pf']:>5.2f}  AvgR={r['avg_r']:>+5.2f}  "
              f"Net=Rs{r['net']:>+10,.0f}  ({elapsed:.1f}s)")

    # ── Summary table ────────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("  SUMMARY TABLE")
    print("=" * 90)
    hdr = (f"  {'Method':<11}  {'Label':<44}  "
           f"{'Trades':>6}  {'WR%':>5}  {'PF':>5}  "
           f"{'AvgR':>5}  {'AvgDays':>7}  {'MaxDD%':>6}  {'Net Rs':>12}")
    print(hdr)
    print("  " + "-" * 88)

    baseline_net = results['ema50']['net']

    for mode, label in TRAIL_METHODS.items():
        r    = results[mode]
        diff = r['net'] - baseline_net
        diff_str = f"({diff:+,.0f})" if mode != 'ema50' else "(baseline)"
        row  = (f"  {mode:<11}  {TRAIL_METHODS[mode]:<44}  "
                f"{r['n']:>6}  {r['wr']:>5.1f}  {r['pf']:>5.2f}  "
                f"{r['avg_r']:>+5.2f}  {r['avg_days']:>7.1f}  "
                f"{r['max_dd']:>6.1f}  {r['net']:>+12,.0f}  {diff_str}")
        print(row)

    print("=" * 90)

    # ── Year-by-year for each method ─────────────────────────────────────────
    print("\n  YEAR-BY-YEAR NET P&L (Rs)\n")
    all_years = sorted({
        pd.Timestamp(t['entry_date']).year
        for r in results.values()
        for t in r['trades']
    })
    yr_hdr = f"  {'Method':<11}  " + "  ".join(f"{y:>8}" for y in all_years)
    print(yr_hdr)
    print("  " + "-" * (13 + 10 * len(all_years)))
    for mode in TRAIL_METHODS:
        yd = defaultdict(float)
        for t in results[mode]['trades']:
            yd[pd.Timestamp(t['entry_date']).year] += t['net_rs']
        row = f"  {mode:<11}  " + "  ".join(f"{yd.get(y, 0):>+8,.0f}" for y in all_years)
        print(row)

    # ── Save full trades for deeper analysis ─────────────────────────────────
    out_dir = ROOT / "reports" / "nr7_positional"
    out_dir.mkdir(parents=True, exist_ok=True)
    all_dfs = []
    for mode, r in results.items():
        for t in r['trades']:
            t['trail_mode'] = mode
        if r['trades']:
            all_dfs.append(pd.DataFrame(r['trades']))
    if all_dfs:
        combined = pd.concat(all_dfs, ignore_index=True)
        out_path = out_dir / "nr7_trail_comparison.csv"
        combined.to_csv(out_path, index=False)
        print(f"\n  Full trade log saved -> {out_path}")


if __name__ == "__main__":
    main()
