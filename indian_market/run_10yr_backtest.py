"""
10-Year Out-of-Sample Validation for VRDOB Strategy
=====================================================
Period  : 2014-01-01 → 2023-12-31 (10 years)
Params  : Loaded from results/best_params_3yr.json (optimized on 3yr)
Goal    : Confirm PF > 2.0 AND WR > 55% on full decade (after costs)

Run ONLY after run_3yr_backtest.py passes the gate.
    python run_10yr_backtest.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import json

from data.download_data import load_all
from strategies.vrdob_strategy import generate_signals, DEFAULT_PARAMS
from backtest.engine import OptionsBacktest
from backtest.metrics import compute_metrics, print_report

CAPITAL   = 1_000_000
START_10  = "2014-01-01"
END_10    = "2023-12-31"
RESULTS   = os.path.join(os.path.dirname(__file__), "results")

# Sub-period breakdown (for consistency check)
PERIODS = {
    "2014-2016 (low vol)": ("2014-01-01", "2016-12-31"),
    "2017-2019 (pre-covid)": ("2017-01-01", "2019-12-31"),
    "2020-2021 (covid+recovery)": ("2020-01-01", "2021-12-31"),
    "2022-2023 (rate hike cycle)": ("2022-01-01", "2023-12-31"),
}


def load_best_params():
    path = os.path.join(RESULTS, "best_params_3yr.json")
    if not os.path.exists(path):
        print("ERROR: best_params_3yr.json not found.")
        print("       Run run_3yr_backtest.py first and ensure it passes the gate.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def run_backtest(nifty, vix, params, start, end, label=""):
    signals = generate_signals(nifty, vix, params)
    engine  = OptionsBacktest(capital=CAPITAL, params=params)
    trades  = engine.run(nifty, vix, signals, start_date=start, end_date=end)
    m       = compute_metrics(trades, capital=CAPITAL)
    m["trades"] = trades
    return m


if __name__ == "__main__":
    print("="*60)
    print("  VRDOB Strategy — 10-Year Out-of-Sample Validation")
    print(f"  Capital: ₹{CAPITAL:,} | Risk/trade: 1% = ₹{CAPITAL*0.01:,.0f}")
    print(f"  Period : {START_10} → {END_10}")
    print("="*60)

    # Load data
    data  = load_all()
    nifty = data["nifty"]
    vix   = data["indiavix"]

    # Load optimized params from 3yr run
    params = load_best_params()
    # Merge any missing keys with defaults
    params = {**DEFAULT_PARAMS, **params}

    print("\nLoaded params from 3yr optimization:")
    for k, v in params.items():
        d = DEFAULT_PARAMS.get(k)
        tag = " ← optimized" if v != d else ""
        print(f"  {k:20s} = {v}{tag}")

    # ── Full 10-year backtest ───────────────────────────────────────────────
    print("\n[Running] 10-year full backtest …")
    m10 = run_backtest(nifty, vix, params, START_10, END_10)
    print_report(m10, label="VRDOB — 10-Year Full Period (after costs)")

    # ── Sub-period breakdown ────────────────────────────────────────────────
    print("\n[Sub-period breakdown]")
    print(f"{'─'*72}")
    print(f"  {'Period':<30} {'PF':>6}  {'WR%':>6}  {'N':>4}  {'Net P&L':>12}  {'Status'}")
    print(f"{'─'*72}")

    period_results = []
    for label, (s, e) in PERIODS.items():
        m = run_backtest(nifty, vix, params, s, e, label)
        if "error" in m:
            print(f"  {label:<30} {'N/A':>6}  {'N/A':>6}  {'0':>4}  {'₹0':>12}  [no data]")
            continue
        pf  = m["profit_factor"]
        wr  = m["win_rate_pct"]
        n   = m["n_trades"]
        pnl = m["net_pnl"]
        ok  = "PASS" if pf > 1.5 and wr > 50 else "FAIL"
        print(f"  {label:<30} {pf:>6.3f}  {wr:>5.1f}%  {n:>4}  ₹{pnl:>10,.0f}  [{ok}]")
        period_results.append({"period": label, **m})

    # ── Year-by-year breakdown ──────────────────────────────────────────────
    print(f"\n[Year-by-year breakdown]")
    print(f"{'─'*60}")
    print(f"  {'Year':>6}  {'PF':>6}  {'WR%':>6}  {'N':>4}  {'Net P&L':>12}")
    print(f"{'─'*60}")

    all_trades = pd.DataFrame(m10["trades"])
    if not all_trades.empty:
        all_trades["year"] = pd.to_datetime(all_trades["entry_date"]).dt.year
        for yr in sorted(all_trades["year"].unique()):
            yr_trades = all_trades[all_trades["year"] == yr].to_dict("records")
            ym = compute_metrics(yr_trades, capital=CAPITAL)
            if "error" in ym:
                continue
            print(f"  {yr:>6}  {ym['profit_factor']:>6.3f}  "
                  f"{ym['win_rate_pct']:>5.1f}%  {ym['n_trades']:>4}  "
                  f"₹{ym['net_pnl']:>10,.0f}")

    # ── Equity curve ───────────────────────────────────────────────────────
    if not all_trades.empty:
        all_trades = all_trades.sort_values("entry_date")
        all_trades["cumulative_pnl"] = all_trades["pnl_net"].cumsum()
        all_trades["equity"]         = CAPITAL + all_trades["cumulative_pnl"]
        out = os.path.join(RESULTS, "equity_10yr.csv")
        all_trades[["entry_date", "exit_date", "direction", "exit_type",
                    "pnl_net", "equity"]].to_csv(out, index=False)
        print(f"\n  Equity curve saved → results/equity_10yr.csv")

    # Save all trades
    if not all_trades.empty:
        all_trades.to_csv(os.path.join(RESULTS, "trades_10yr.csv"), index=False)
        print(f"  All trades saved   → results/trades_10yr.csv")

    # ── Final verdict ───────────────────────────────────────────────────────
    pf_10 = m10.get("profit_factor", 0)
    wr_10 = m10.get("win_rate_pct", 0)
    n_10  = m10.get("n_trades", 0)

    print(f"\n{'='*60}")
    print(f"  10-YEAR VALIDATION VERDICT")
    print(f"{'='*60}")
    print(f"  Profit Factor : {pf_10:.3f}  (need > 2.0)")
    print(f"  Win Rate      : {wr_10:.1f}%  (need > 55%)")
    print(f"  Total Trades  : {n_10}")
    print(f"  Net P&L       : ₹{m10.get('net_pnl', 0):,.0f}")
    print(f"  CAGR          : {m10.get('cagr_pct', 0):.1f}%")
    print(f"  Max Drawdown  : {m10.get('max_drawdown_pct', 0):.1f}%")
    print(f"  Sharpe        : {m10.get('sharpe', 0):.3f}")
    print(f"{'─'*60}")
    if pf_10 > 2.0 and wr_10 > 55:
        print("  ✓ CONFIRMED: Strategy is robust across 10 years")
        print("  → Ready for paper trading / live deployment")
    elif pf_10 > 1.5 and wr_10 > 50:
        print("  ~ MARGINAL: Strategy shows edge but below threshold")
        print("  → Further refinement recommended before live trading")
    else:
        print("  ✗ NOT CONFIRMED: 10yr results do not meet thresholds")
        print("  → Strategy is likely overfit to 3yr period — do not trade live")
    print(f"{'='*60}\n")
