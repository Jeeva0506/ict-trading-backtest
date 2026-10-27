"""
10-Year Straddle Validation — LVSE Strategy
============================================
Period: 2014-01-01 to 2023-12-31 (10 years)
Params: From 3yr grid-search best (optimized set)

Run after confirming 3yr gate is passed.
    python run_straddle_10yr.py [--params-file results/best_straddle_3yr.json]
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import json

from data.download_data import load_all
from strategies.straddle_strategy import generate_straddle_signals, STRADDLE_PARAMS
from backtest.straddle_engine import StraddleBacktest
from backtest.metrics import compute_metrics, print_report

CAPITAL  = 1_000_000
START_10 = "2014-01-01"
END_10   = "2023-12-31"
RESULTS  = os.path.join(os.path.dirname(__file__), "results")

PERIODS = {
    "2014-2016 (low vol / Modi rally)"  : ("2014-01-01", "2016-12-31"),
    "2017-2019 (pre-covid trend)"       : ("2017-01-01", "2019-12-31"),
    "2020-2021 (covid crash+recovery)"  : ("2020-01-01", "2021-12-31"),
    "2022-2023 (rate hike cycle)"       : ("2022-01-01", "2023-12-31"),
}


def load_best_params():
    path = os.path.join(RESULTS, "best_straddle_3yr.json")
    if not os.path.exists(path):
        print("WARNING: best_straddle_3yr.json not found — using winning params from quick sweep")
        # Fallback: the best param set found during development
        return {
            "ivr_threshold"   : 30,
            "vix_max"         : 18,
            "vix_min"         : 10,
            "vix_decline_days": 3,
            "adx_threshold"   : 18,
            "sl_pct"          : 0.40,
            "tp_pct"          : 0.60,
            "max_hold_days"   : 5,
            "entry_dte"       : 7,
        }
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    print("="*60)
    print("  LVSE Straddle — 10-Year Out-of-Sample Validation")
    print(f"  Capital: Rs{CAPITAL:,} | Risk/trade: 1% = Rs{CAPITAL*0.01:,.0f}")
    print(f"  Period : {START_10} to {END_10}")
    print("="*60)

    data  = load_all()
    nifty = data["nifty"]
    vix   = data["indiavix"]
    params = {**STRADDLE_PARAMS, **load_best_params()}

    print("\nRunning parameters:")
    key_params = ["ivr_threshold", "vix_max", "vix_min", "vix_decline_days",
                  "adx_threshold", "sl_pct", "tp_pct", "max_hold_days", "entry_dte"]
    for k in key_params:
        print(f"  {k:20s} = {params[k]}")

    # ── Full 10yr run ──────────────────────────────────────────────────────
    print("\nRunning 10yr backtest...")
    sigs   = generate_straddle_signals(nifty, vix, params)
    eng    = StraddleBacktest(capital=CAPITAL, params=params)
    trades = eng.run(nifty, vix, sigs, START_10, END_10)
    m10    = compute_metrics(trades, CAPITAL)
    print_report(m10, label="LVSE Straddle — 10-Year Full Period (after Zerodha costs)")

    # ── Sub-period breakdown ───────────────────────────────────────────────
    print("[Sub-period breakdown]")
    print(f"{'─'*72}")
    print(f"  {'Period':<35} {'PF':>6}  {'WR%':>6}  {'N':>4}  {'Net P&L':>12}  Status")
    print(f"{'─'*72}")
    for label, (s, e) in PERIODS.items():
        sigs2 = generate_straddle_signals(nifty, vix, params)
        eng2  = StraddleBacktest(capital=CAPITAL, params=params)
        t2    = eng2.run(nifty, vix, sigs2, s, e)
        m2    = compute_metrics(t2, CAPITAL)
        if "error" in m2:
            print(f"  {label:<35} {'N/A':>6}  {'N/A':>6}  {'0':>4}  {'Rs0':>12}  [no data]")
            continue
        ok = "PASS" if m2["profit_factor"] > 1.5 and m2["win_rate_pct"] > 50 else "FAIL"
        print(f"  {label:<35} {m2['profit_factor']:>6.3f}  "
              f"{m2['win_rate_pct']:>5.1f}%  {m2['n_trades']:>4}  "
              f"Rs{m2['net_pnl']:>9,.0f}  [{ok}]")

    # ── Year-by-year ───────────────────────────────────────────────────────
    print(f"\n[Year-by-year]")
    print(f"{'─'*55}")
    print(f"  {'Year':>6}  {'PF':>6}  {'WR%':>6}  {'N':>4}  {'Net P&L':>12}")
    print(f"{'─'*55}")
    df_t = pd.DataFrame(trades)
    if not df_t.empty:
        df_t["year"] = pd.to_datetime(df_t["entry_date"]).dt.year
        for yr in sorted(df_t["year"].unique()):
            yt = df_t[df_t["year"] == yr].to_dict("records")
            ym = compute_metrics(yt, CAPITAL)
            if "error" in ym:
                continue
            print(f"  {yr:>6}  {ym['profit_factor']:>6.3f}  "
                  f"{ym['win_rate_pct']:>5.1f}%  {ym['n_trades']:>4}  "
                  f"Rs{ym['net_pnl']:>9,.0f}")

    # ── Save trades ────────────────────────────────────────────────────────
    os.makedirs(RESULTS, exist_ok=True)
    if not df_t.empty:
        df_t["cumulative_pnl"] = df_t["pnl_net"].cumsum()
        df_t["equity"]         = CAPITAL + df_t["cumulative_pnl"]
        df_t.to_csv(os.path.join(RESULTS, "straddle_trades_10yr.csv"), index=False)
        print(f"\n  Trades saved to results/straddle_trades_10yr.csv")

    # ── Verdict ────────────────────────────────────────────────────────────
    pf = m10.get("profit_factor", 0)
    wr = m10.get("win_rate_pct", 0)
    n  = m10.get("n_trades", 0)

    print(f"\n{'='*60}")
    print(f"  10-YEAR VALIDATION VERDICT")
    print(f"{'='*60}")
    print(f"  Profit Factor : {pf:.3f}  (need > 2.0)")
    print(f"  Win Rate      : {wr:.1f}%  (need > 55%)")
    print(f"  Total Trades  : {n}")
    print(f"  Net P&L       : Rs{m10.get('net_pnl',0):,.0f}")
    print(f"  CAGR          : {m10.get('cagr_pct',0):.1f}%")
    print(f"  Max Drawdown  : {m10.get('max_drawdown_pct',0):.1f}%")
    print(f"  Sharpe        : {m10.get('sharpe',0):.3f}")
    print(f"{'─'*60}")
    if pf > 2.0 and wr > 55:
        print("  CONFIRMED: Strategy robust across 10 years (after costs)")
        print("  Ready for paper trading on Zerodha")
    elif pf > 1.5 and wr > 50:
        print("  MARGINAL: Edge exists but below threshold targets")
        print("  Further refinement recommended")
    else:
        print("  NOT CONFIRMED: 10yr results do not meet thresholds")
        print("  Strategy may be overfit — do not trade live")
    print(f"{'='*60}")
