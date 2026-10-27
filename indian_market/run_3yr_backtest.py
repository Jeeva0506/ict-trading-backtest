"""
3-Year Backtest + Parameter Optimization for VRDOB Strategy
============================================================
Period  : 2021-01-01 → 2023-12-31 (3 years)
Goal    : Find param set where PF > 2.0 AND WR > 55% (after Zerodha costs)
Method  : Grid search over key parameters, ranked by profit factor

Run:
    python run_3yr_backtest.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
import json
from itertools import product

from data.download_data import download_all, load_all
from strategies.vrdob_strategy import generate_signals, DEFAULT_PARAMS
from backtest.engine import OptionsBacktest
from backtest.metrics import compute_metrics, print_report

CAPITAL      = 1_000_000   # ₹10 lakh
START_3YR    = "2021-01-01"
END_3YR      = "2023-12-31"
RESULTS_DIR  = os.path.join(os.path.dirname(__file__), "results")

os.makedirs(RESULTS_DIR, exist_ok=True)


# ── Load / download data ───────────────────────────────────────────────────
def load_data():
    try:
        return load_all()
    except FileNotFoundError:
        print("Data not found — downloading …")
        return download_all()


# ── Single backtest run ────────────────────────────────────────────────────
def run_one(nifty, vix, params, start, end) -> dict:
    signals = generate_signals(nifty, vix, params)
    engine  = OptionsBacktest(capital=CAPITAL, params=params)
    trades  = engine.run(nifty, vix, signals, start_date=start, end_date=end)
    metrics = compute_metrics(trades, capital=CAPITAL)
    metrics["params"] = params
    metrics["trades"] = trades
    return metrics


# ── Parameter grid ─────────────────────────────────────────────────────────
GRID = {
    "ivr_threshold"  : [25, 35, 45],
    "vix_max"        : [18, 20, 22],
    "vix_min"        : [11, 13],
    "adx_threshold"  : [20, 25],
    "ema_fast"       : [10, 20],
    "ema_slow"       : [50],
    "rsi_call_lo"    : [52, 57],
    "rsi_call_hi"    : [70, 75],
    "rsi_put_lo"     : [25, 30],
    "rsi_put_hi"     : [43, 48],
    "sl_pct"         : [0.35, 0.45],
    "tp_pct"         : [0.70, 0.90, 1.10],
    "max_hold_days"  : [4, 6],
    "entry_dte"      : [7, 10, 14],
}


def grid_search(nifty, vix, start, end):
    keys   = list(GRID.keys())
    values = list(GRID.values())
    combos = list(product(*values))
    total  = len(combos)
    print(f"\n[Grid Search] {total} combinations on {start} → {end}")

    best_pf   = 0
    best_wr   = 0
    results   = []

    for i, combo in enumerate(combos):
        params = {**DEFAULT_PARAMS, **dict(zip(keys, combo))}
        # Quick validity check
        if params["ema_fast"] >= params["ema_slow"]:
            continue
        if params["rsi_call_lo"] >= params["rsi_call_hi"]:
            continue
        if params["rsi_put_lo"] >= params["rsi_put_hi"]:
            continue

        m = run_one(nifty, vix, params, start, end)
        if "error" in m:
            continue

        pf = m["profit_factor"]
        wr = m["win_rate_pct"]
        n  = m["n_trades"]

        # Minimum trade filter (need statistical significance)
        if n < 15:
            continue

        results.append({
            "pf"  : pf,
            "wr"  : wr,
            "n"   : n,
            "sharpe": m["sharpe"],
            "net_pnl": m["net_pnl"],
            "max_dd" : m["max_drawdown_pct"],
            "params": params,
            "metrics": m,
        })

        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{total}] best so far PF={best_pf:.2f} WR={best_wr:.1f}%")

        best_pf = max(best_pf, pf)
        best_wr = max(best_wr, wr) if pf > 1.5 else best_wr

    return sorted(results, key=lambda x: x["pf"], reverse=True)


# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*60)
    print("  VRDOB Strategy — 3-Year Backtest + Optimization")
    print(f"  Capital: ₹{CAPITAL:,} | Risk/trade: 1% = ₹{CAPITAL*0.01:,.0f}")
    print(f"  Period : {START_3YR} → {END_3YR}")
    print("="*60)

    data  = load_data()
    nifty = data["nifty"]
    vix   = data["indiavix"]

    # ── Step 1: Default params baseline ────────────────────────────────────
    print("\n[Step 1] Default parameter baseline …")
    base = run_one(nifty, vix, DEFAULT_PARAMS.copy(), START_3YR, END_3YR)
    print_report(base, label="Baseline (default params) — 3yr")

    # ── Step 2: Grid search ─────────────────────────────────────────────────
    print("\n[Step 2] Grid search for best parameters …")
    ranked = grid_search(nifty, vix, START_3YR, END_3YR)

    if not ranked:
        print("No valid parameter combinations found.")
        sys.exit(1)

    # Show top 10
    print(f"\n{'─'*70}")
    print(f"  TOP 10 PARAMETER SETS (by Profit Factor, after Zerodha costs)")
    print(f"{'─'*70}")
    header = f"{'#':>3}  {'PF':>6}  {'WR%':>6}  {'N':>4}  {'Sharpe':>6}  {'Net P&L':>12}  {'MaxDD%':>7}"
    print(header)
    print("─"*70)
    for rank, r in enumerate(ranked[:10], 1):
        flag = " ✓" if r["pf"] > 2.0 and r["wr"] > 55 else ""
        print(f"  {rank:>2}.  {r['pf']:>6.3f}  {r['wr']:>5.1f}%  "
              f"{r['n']:>4}  {r['sharpe']:>6.3f}  "
              f"₹{r['net_pnl']:>10,.0f}  {r['max_dd']:>6.1f}%{flag}")

    # ── Step 3: Best param detailed report ─────────────────────────────────
    best = ranked[0]
    print(f"\n[Step 3] Best parameter set detailed report …")
    print_report(best["metrics"], label="BEST PARAMS — 3yr Backtest (after costs)")

    best_params = best["params"]
    print("\nBest parameters:")
    for k, v in best_params.items():
        default = DEFAULT_PARAMS.get(k)
        tag = " ← changed" if v != default else ""
        print(f"  {k:20s} = {v}{tag}")

    # ── Check gate ──────────────────────────────────────────────────────────
    passes = best["pf"] > 2.0 and best["wr"] > 55
    if passes:
        print("\n  ✓ GATE PASSED: PF > 2.0 AND WR > 55% (after costs)")
        print("  → Proceed to run_10yr_backtest.py with these parameters")
        # Save best params for 10yr run
        out_path = os.path.join(RESULTS_DIR, "best_params_3yr.json")
        save_params = {k: v for k, v in best_params.items()
                       if k not in ("trades",)}
        with open(out_path, "w") as f:
            json.dump(save_params, f, indent=2)
        print(f"  Saved to {out_path}")

        # Save all trades
        trades_df = pd.DataFrame(best["metrics"]["trades"])
        trades_df.to_csv(os.path.join(RESULTS_DIR, "trades_3yr.csv"), index=False)
        print(f"  Trades saved to results/trades_3yr.csv")
    else:
        print("\n  ✗ GATE NOT PASSED:")
        print(f"    PF = {best['pf']:.3f} (need > 2.0)")
        print(f"    WR = {best['wr']:.1f}% (need > 55%)")
        print("  → Strategy needs further refinement before 10yr validation")

    # ── Save full grid results ───────────────────────────────────────────────
    grid_df = pd.DataFrame([{
        "pf": r["pf"], "wr": r["wr"], "n": r["n"],
        "sharpe": r["sharpe"], "net_pnl": r["net_pnl"],
        "max_dd": r["max_dd"],
        **{f"p_{k}": v for k, v in r["params"].items()
           if k not in ("risk_per_trade", "lot_size", "strike_step", "rfr",
                        "max_lots", "ivr_lookback")}
    } for r in ranked])
    grid_df.to_csv(os.path.join(RESULTS_DIR, "grid_search_3yr.csv"), index=False)
    print(f"\n  Full grid results saved to results/grid_search_3yr.csv")
