"""
PO3 Full Test: Download -> Optimize (1yr) -> Forward Test (3yr)
==============================================================

IS  period : 2023-06-04 -> 2024-06-04   (1 year, optimization)
OOS period : 2024-06-04 -> 2026-06-04   (2 years, forward test)
Pre-context: 2022-06-04 -> 2023-06-04   (1 year, 15-min OB context for IS start)

Data fetched from Zerodha in 60-day chunks, saved to data/.
Optimization: grid search on 81 param combos, scored by Profit Factor × sqrt(N).
Forward test: best IS params applied unchanged to OOS data.

Usage:
    python run_po3_full_test.py [--skip-download]
"""
import sys, os, time, json, itertools, argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backtest.po3_backtest  import PO3Backtest
from backtest.metrics       import compute_metrics
from strategies.po3_strategy import PO3_PARAMS

DATA_DIR   = ROOT / "data"
REPORT_DIR = ROOT / "reports" / "po3"
DATA_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ── Date splits ────────────────────────────────────────────────────────────────
CONTEXT_START = "2022-06-04"   # extra 15-min context (OB lookback)
IS_START      = "2023-06-04"   # in-sample optimization start
IS_END        = "2024-06-04"   # in-sample end / OOS start
OOS_END       = "2026-06-04"   # out-of-sample end (today)

NIFTY_TOKEN   = 256265
VIX_TOKEN     = 264969

# ── Parameter grid (optimised on IS) ──────────────────────────────────────────
PARAM_GRID = {
    "entry_mode"           : ["ifvg",  "breakout"],   # iFVG retracement vs. breakout bar
    "tp_pct"               : [0.40,   0.60,  0.80],  # TP at 40/60/80% premium gain
    "sl_pct"               : [0.20,   0.30,  0.40],  # hard SL at 20/30/40% premium loss
    "accum_atr_mult"       : [0.8,    1.2,   1.8 ],  # consolidation tightness
}
# 2 × 3 × 3 × 3 = 54 combinations


# ── Data download ──────────────────────────────────────────────────────────────

def _fetch_range(kite, token, interval, from_dt, to_dt, chunk_days=60):
    """Fetch candles in chunks, return DataFrame."""
    all_rows = []
    cur = from_dt
    while cur <= to_dt:
        end = min(cur + timedelta(days=chunk_days - 1), to_dt)
        for attempt in range(3):
            try:
                rows = kite.historical_data(token, from_date=cur,
                                            to_date=end, interval=interval)
                all_rows.extend(rows)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"    WARNING: {cur}->{end} failed: {e}")
                time.sleep(2)
        time.sleep(0.35)
        cur = end + timedelta(days=1)

    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows).set_index("date")
    df.index = pd.to_datetime(df.index)
    df.columns = [c.capitalize() for c in df.columns]
    return df.sort_index()


def download_data():
    from live.paper_trader import _get_kite
    kite  = _get_kite()
    today = date.today()

    ctx_start  = date.fromisoformat(CONTEXT_START)
    oos_end_dt = date.fromisoformat(OOS_END)

    # ── 3-min Nifty ────────────────────────────────────────────────────────────
    f3 = DATA_DIR / "nifty_3min_full.csv"
    if not f3.exists():
        print(f"\nDownloading Nifty 3-min  {ctx_start} -> {oos_end_dt}  (60-day chunks) ...")
        df = _fetch_range(kite, NIFTY_TOKEN, "3minute", ctx_start, oos_end_dt, 60)
        if not df.empty:
            df = df.between_time("09:00", "15:35")
            df.to_csv(f3)
            print(f"  Saved {len(df):,} bars -> {f3.name}")
        else:
            print("  ERROR: no 3-min data")
    else:
        print(f"  3-min data exists ({f3.name}) — skipping download")

    # ── 15-min Nifty ───────────────────────────────────────────────────────────
    f15 = DATA_DIR / "nifty_15min_full.csv"
    if not f15.exists():
        print(f"\nDownloading Nifty 15-min {ctx_start} -> {oos_end_dt}  (60-day chunks) ...")
        df = _fetch_range(kite, NIFTY_TOKEN, "15minute", ctx_start, oos_end_dt, 60)
        if not df.empty:
            df = df.between_time("09:00", "15:35")
            df.to_csv(f15)
            print(f"  Saved {len(df):,} bars -> {f15.name}")
        else:
            print("  ERROR: no 15-min data")
    else:
        print(f"  15-min data exists ({f15.name}) — skipping download")

    # ── VIX daily ──────────────────────────────────────────────────────────────
    fv = DATA_DIR / "india_vix_daily_full.csv"
    if not fv.exists():
        print(f"\nDownloading India VIX daily {ctx_start} -> {oos_end_dt} ...")
        df = _fetch_range(kite, VIX_TOKEN, "day", ctx_start, oos_end_dt, 365)
        if not df.empty:
            df.to_csv(fv)
            print(f"  Saved {len(df):,} bars -> {fv.name}")
        else:
            print("  ERROR: no VIX data")
    else:
        print(f"  VIX data exists ({fv.name}) — skipping download")


def load_data():
    f3  = DATA_DIR / "nifty_3min_full.csv"
    f15 = DATA_DIR / "nifty_15min_full.csv"
    fv  = DATA_DIR / "india_vix_daily_full.csv"
    missing = [str(f) for f in [f3, f15, fv] if not f.exists()]
    if missing:
        raise FileNotFoundError(f"Missing: {missing}")
    df_3m  = pd.read_csv(f3,  index_col=0, parse_dates=True)
    df_15m = pd.read_csv(f15, index_col=0, parse_dates=True)
    vix_df = pd.read_csv(fv,  index_col=0, parse_dates=True)
    # VIX indexed by date
    vix_series = vix_df["Close"].copy()
    vix_series.index = pd.to_datetime(vix_series.index).date
    return df_3m, df_15m, vix_series


# ── Day cache ─────────────────────────────────────────────────────────────────

def build_day_cache(df_3m, df_15m, start_date, end_date, params=None):
    """
    Precompute HTF bias and OB zones once per trading day.
    Since these don't depend on grid-search params, we compute once and reuse
    across all 81 combos.  Returns {date: {"bias": str, "ob_zones": list}}.
    """
    from strategies.po3_strategy import find_ob_zones, _sma, PO3_PARAMS
    p = {**PO3_PARAMS, **(params or {})}

    def _to_date(idx):
        if hasattr(idx, "date"):
            return idx.date
        return pd.to_datetime(idx).date

    sd = pd.Timestamp(start_date).date()
    ed = pd.Timestamp(end_date).date()
    df_3m_filt  = df_3m[(_to_date(df_3m.index) >= sd) & (_to_date(df_3m.index) <= ed)]
    df_15m_filt = df_15m[(_to_date(df_15m.index) >= sd - pd.Timedelta(days=120)) &
                         (_to_date(df_15m.index) <= ed)]
    trading_days = sorted(set(df_3m_filt.index.date))

    cache = {}
    for day in trading_days:
        hist15 = df_15m_filt[_to_date(df_15m_filt.index) <= day]
        if len(hist15) < p["htf_sma_period"] + 5:
            cache[day] = {"bias": None, "ob_zones": []}
            continue
        sma50     = _sma(hist15["Close"], p["htf_sma_period"])
        htf_close = hist15["Close"].iloc[-1]
        bias = None if pd.isna(sma50.iloc[-1]) else (
            "bull" if htf_close > sma50.iloc[-1] else "bear"
        )
        ob_zones = find_ob_zones(hist15, p["ob_impulse_bars"], p["ob_max_retests"])
        cache[day] = {"bias": bias, "ob_zones": ob_zones}

    print(f"  Day cache built: {len(cache)} days")
    return cache


# ── Optimization ───────────────────────────────────────────────────────────────

def _score(metrics: dict) -> float:
    """Objective: PF × sqrt(N) — rewards both edge quality and frequency."""
    n  = metrics.get("n_trades", 0)
    pf = metrics.get("profit_factor", 0)
    if n < 10 or pf == 0:
        return 0.0
    return float(pf) * np.sqrt(n)


def run_optimization(df_3m, df_15m, vix_series):
    print("\n" + "=" * 65)
    print("  OPTIMIZATION  (in-sample: {} -> {})".format(IS_START, IS_END))
    print("=" * 65)

    keys   = list(PARAM_GRID.keys())
    combos = list(itertools.product(*[PARAM_GRID[k] for k in keys]))
    print(f"  Grid: {len(combos)} param combinations  (entry_mode × tp × sl × accum_atr)")

    # Precompute HTF bias + OB zones per day ONCE (not per combo)
    print("  Building day cache (bias + OB zones)...")
    day_cache = build_day_cache(df_3m, df_15m, IS_START, IS_END)
    print()

    results = []
    for idx, vals in enumerate(combos):
        params = {**PO3_PARAMS, **dict(zip(keys, vals))}
        bt = PO3Backtest(capital=1_000_000, params=params)
        trades = bt.run(df_3m, df_15m, _vix_df_from_series(vix_series),
                        start_date=IS_START, end_date=IS_END, day_cache=day_cache)
        m = compute_metrics(trades, 1_000_000) if trades else {"error": "no trades"}
        sc = _score(m)
        results.append({
            "params" : dict(zip(keys, vals)),
            "score"  : sc,
            "n"      : m.get("n_trades", 0),
            "pf"     : m.get("profit_factor", 0),
            "wr"     : m.get("win_rate_pct", 0),
            "net_pnl": m.get("net_pnl", 0),
            "max_dd" : m.get("max_drawdown_pct", 0),
            "metrics": m,
        })
        if (idx + 1) % 10 == 0 or idx == len(combos) - 1:
            print(f"  Progress: {idx+1}/{len(combos)}  best score so far: "
                  f"{max(r['score'] for r in results):.2f}")

    results.sort(key=lambda r: r["score"], reverse=True)

    # Save full grid results
    grid_rows = []
    for r in results:
        row = {**r["params"], "score": r["score"], "n": r["n"],
               "pf": r["pf"], "wr": r["wr"], "net_pnl": r["net_pnl"],
               "max_dd": r["max_dd"]}
        grid_rows.append(row)
    pd.DataFrame(grid_rows).to_csv(REPORT_DIR / "po3_grid_search.csv", index=False)

    best = results[0]
    print(f"\n  Best params (score={best['score']:.2f}):")
    for k, v in best["params"].items():
        print(f"    {k:<30}: {v}")
    print(f"\n  IS metrics:")
    _print_metrics(best["metrics"])

    return best["params"], best["metrics"]


def _vix_df_from_series(s: pd.Series) -> pd.DataFrame:
    df = s.reset_index()
    df.columns = ["date", "Close"]
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


# ── Forward test ───────────────────────────────────────────────────────────────

def run_forward_test(df_3m, df_15m, vix_series, best_params):
    print("\n" + "=" * 65)
    print("  FORWARD TEST  (out-of-sample: {} -> {})".format(IS_END, OOS_END))
    print("=" * 65)

    print("  Building day cache...")
    day_cache = build_day_cache(df_3m, df_15m, IS_END, OOS_END)

    params = {**PO3_PARAMS, **best_params}
    bt = PO3Backtest(capital=1_000_000, params=params)
    trades = bt.run(df_3m, df_15m, _vix_df_from_series(vix_series),
                    start_date=IS_END, end_date=OOS_END, day_cache=day_cache)
    m = compute_metrics(trades, 1_000_000) if trades else {"error": "no trades"}

    print(f"\n  OOS metrics:")
    _print_metrics(m)

    if trades:
        df_trades = pd.DataFrame(trades)
        df_trades.to_csv(REPORT_DIR / "po3_oos_trades.csv", index=False)
        print(f"\n  Trade log -> {REPORT_DIR / 'po3_oos_trades.csv'}")

        # Monthly breakdown
        df_trades["entry_date"] = pd.to_datetime(df_trades["entry_date"], errors="coerce")
        df_trades["month"] = df_trades["entry_date"].dt.to_period("M")
        monthly = df_trades.groupby("month").agg(
            trades=("pnl_net", "count"),
            net_pnl=("pnl_net", "sum"),
            wins=("pnl_net", lambda x: (x > 0).sum()),
        )
        monthly["wr"] = monthly["wins"] / monthly["trades"]
        print("\n  Monthly breakdown (OOS):")
        print(monthly.to_string())

    return m, trades


def _print_metrics(m):
    if "error" in m:
        print(f"    {m['error']}")
        return
    print(f"    Trades        : {m.get('n_trades', 0)}")
    print(f"    Win Rate      : {m.get('win_rate_pct', 0):.1f}%")
    print(f"    Profit Factor : {m.get('profit_factor', 0):.2f}")
    print(f"    Expectancy    : Rs {m.get('expectancy', 0):,.0f}")
    print(f"    Net P&L       : Rs {m.get('net_pnl', 0):,.0f}")
    print(f"    CAGR          : {m.get('cagr_pct', 0):.1f}%")
    print(f"    Max DD        : {m.get('max_drawdown_pct', 0):.2f}%")
    print(f"    Calmar        : {m.get('calmar', 0):.2f}")
    exits = m.get("exit_breakdown", {})
    if exits:
        print(f"    Exits         : {exits}")


# ── Walk-forward validation ────────────────────────────────────────────────────

def run_walk_forward(df_3m, df_15m, vix_series, best_params):
    """
    Rolling 6-month OOS windows across the full OOS period.
    Shows consistency of the strategy over time.
    """
    print("\n" + "=" * 65)
    print("  WALK-FORWARD  (6-month rolling windows in OOS period)")
    print("=" * 65)

    params = {**PO3_PARAMS, **best_params}
    wf_start = date.fromisoformat(IS_END)
    wf_end   = date.fromisoformat(OOS_END)
    window   = timedelta(days=180)

    # Precompute OOS day cache once for all walk-forward windows
    print("  Building day cache...")
    wf_day_cache = build_day_cache(df_3m, df_15m, IS_END, OOS_END)

    rows = []
    cur = wf_start
    while cur + window <= wf_end:
        ws = str(cur)
        we = str(cur + window)
        bt = PO3Backtest(capital=1_000_000, params=params)
        trades = bt.run(df_3m, df_15m, _vix_df_from_series(vix_series),
                        start_date=ws, end_date=we, day_cache=wf_day_cache)
        m = compute_metrics(trades, 1_000_000) if trades else {}
        rows.append({
            "window"  : f"{ws} -> {we}",
            "trades"  : m.get("n_trades", 0),
            "win_rate": f"{m.get('win_rate_pct', 0):.1f}%",
            "pf"      : f"{m.get('profit_factor', 0):.2f}",
            "net_pnl" : f"Rs{m.get('net_pnl', 0):,.0f}",
            "max_dd"  : f"{m.get('max_drawdown_pct', 0):.1f}%",
        })
        cur += timedelta(days=90)  # advance 3 months (overlapping windows)

    df_wf = pd.DataFrame(rows)
    print(df_wf.to_string(index=False))
    df_wf.to_csv(REPORT_DIR / "po3_walk_forward.csv", index=False)
    print(f"\n  Walk-forward log -> {REPORT_DIR / 'po3_walk_forward.csv'}")
    return df_wf


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-download", action="store_true",
                        help="Skip data download (use existing CSV files)")
    parser.add_argument("--skip-optimize", action="store_true",
                        help="Skip grid search, use default params")
    args = parser.parse_args()

    # 1. Download
    if not args.skip_download:
        download_data()
    else:
        print("Skipping download (--skip-download)")

    # 2. Load
    print("\nLoading data...")
    df_3m, df_15m, vix_series = load_data()
    print(f"  3-min  : {len(df_3m):,} bars  "
          f"({df_3m.index[0].date()} -> {df_3m.index[-1].date()})")
    print(f"  15-min : {len(df_15m):,} bars")
    print(f"  VIX    : {len(vix_series):,} days")

    # 3. Optimize
    if not args.skip_optimize:
        best_params, is_metrics = run_optimization(df_3m, df_15m, vix_series)
    else:
        print("\nSkipping optimization — using default params")
        best_params = {}
        is_metrics  = {}

    # Save best params
    out_params = REPORT_DIR / "po3_best_params.json"
    with open(out_params, "w") as f:
        json.dump(best_params, f, indent=2)
    print(f"\n  Best params saved -> {out_params}")

    # 4. Forward test
    oos_metrics, oos_trades = run_forward_test(df_3m, df_15m, vix_series, best_params)

    # 5. Walk-forward
    run_walk_forward(df_3m, df_15m, vix_series, best_params)

    # 6. Summary comparison
    print("\n" + "=" * 65)
    print("  SUMMARY COMPARISON")
    print("=" * 65)
    print(f"  {'Metric':<25}  {'In-Sample (1yr)':<20}  {'Out-of-Sample (2yr)'}")
    print(f"  {'-'*25}  {'-'*20}  {'-'*20}")
    for k, label, fmt in [
        ("n_trades",       "Trades",        "d"   ),
        ("win_rate_pct",   "Win Rate %",    ".1f" ),
        ("profit_factor",  "Profit Factor", ".2f" ),
        ("net_pnl",        "Net P&L (Rs)",  ",.0f"),
        ("max_drawdown_pct","Max DD %",     ".2f" ),
        ("cagr_pct",       "CAGR %",        ".1f" ),
    ]:
        iv = is_metrics.get(k, "n/a")
        ov = oos_metrics.get(k, "n/a")
        try:
            iv_s = format(float(iv), fmt) if iv != "n/a" else "n/a"
            ov_s = format(float(ov), fmt) if ov != "n/a" else "n/a"
        except Exception:
            iv_s, ov_s = str(iv), str(ov)
        print(f"  {label:<25}  {iv_s:<20}  {ov_s}")
    print()


if __name__ == "__main__":
    main()
