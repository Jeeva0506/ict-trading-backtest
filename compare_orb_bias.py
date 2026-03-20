"""
A/B Comparison: ORB Bias Filter ON vs OFF
==========================================
Runs the full backtest for all 5 live indices (GER30, FRA40, SPX500, US30, NDX100)
with and without the ORB bias alignment filter.

Variant A (current): HTF trend MUST match ORB bias (double confirmation)
Variant B (proposed): HTF trend only — ORB bias ignored

Usage:
  python compare_orb_bias.py           # ~2.8yr clean data
  python compare_orb_bias.py --10yr    # full 10yr+ data (Nov 2010 → Feb 2026)
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from backtest_live_setup import run_live_simulation, LIVE_INDEX_CONFIG, BROKER_POINT_VALUES
from pathlib import Path
import pandas as pd
import numpy as np

USE_10YR = "--10yr" in sys.argv

# Indices to compare (the ones used in live trading)
SYMBOLS = ["GER30", "FRA40", "SPX500", "US30", "NDX100"]

# Check which files actually exist
data_dir = Path(__file__).parent / "data" / "raw"

if USE_10YR:
    CLEAN_M1 = {s: f"{s}_10YR_M1_CLEAN.csv" for s in SYMBOLS}
    H1_EMA   = {s: f"{s}_10YR_H1_EMA.csv" for s in SYMBOLS}
else:
    CLEAN_M1 = {s: f"{s}_M1_CLEAN.csv" for s in SYMBOLS}
    H1_EMA   = {s: f"{s}_H1_EMA.csv" for s in SYMBOLS}

for sym in SYMBOLS:
    if not (data_dir / CLEAN_M1[sym]).exists():
        CLEAN_M1[sym] = None
    if not (data_dir / H1_EMA[sym]).exists():
        H1_EMA[sym] = None


def compute_metrics(signals, label):
    """Compute standard backtest metrics from raw signals."""
    if not signals:
        return {"label": label, "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "avg_r": 0, "total_r": 0, "profit_factor": 0, "max_dd_r": 0}

    wins = 0
    losses = 0
    breakevens = 0
    gross_profit_r = 0.0
    gross_loss_r = 0.0
    r_values = []

    for sig_dict in signals:
        s = sig_dict["signal"]
        risk_pts = float(s.risk_points)
        pnl_pts = float(getattr(s, "pnl_points", 0.0))
        if risk_pts <= 0:
            continue
        r_multiple = pnl_pts / risk_pts
        r_values.append(r_multiple)

        if r_multiple > 0.1:
            wins += 1
            gross_profit_r += r_multiple
        elif r_multiple < -0.1:
            losses += 1
            gross_loss_r += abs(r_multiple)
        else:
            breakevens += 1

    total = wins + losses + breakevens
    if total == 0:
        return {"label": label, "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "avg_r": 0, "total_r": 0, "profit_factor": 0, "max_dd_r": 0}

    # Max drawdown in R
    cumulative = np.cumsum(r_values)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd_r = float(np.max(drawdown)) if len(drawdown) > 0 else 0

    win_rate = (wins / total * 100) if total > 0 else 0
    avg_r = float(np.mean(r_values)) if r_values else 0
    total_r = float(np.sum(r_values))
    pf = (gross_profit_r / gross_loss_r) if gross_loss_r > 0 else float("inf")

    return {
        "label": label,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "breakevens": breakevens,
        "win_rate": round(win_rate, 1),
        "avg_r": round(avg_r, 3),
        "total_r": round(total_r, 2),
        "profit_factor": round(pf, 2),
        "max_dd_r": round(max_dd_r, 2),
    }


def run_comparison():
    data_label = "10YR+ (Nov 2010 - Feb 2026)" if USE_10YR else "~2.8yr clean"
    print("=" * 75)
    print("  ORB BIAS FILTER: A/B COMPARISON")
    print(f"  A = With ORB bias (current)  |  B = HTF-only (no ORB bias)")
    print(f"  Data: {data_label}")
    print("=" * 75)

    all_a = []
    all_b = []
    per_index_results = []

    for sym in SYMBOLS:
        m1 = CLEAN_M1.get(sym)
        h1 = H1_EMA.get(sym)
        print(f"\n{'─'*60}")
        print(f"  {sym} — running A (with ORB bias)...")
        sigs_a, days_a = run_live_simulation(
            run_days=None, symbol=sym, m1_file=m1, h1_file=h1,
            orb_bias_filter=True
        )
        print(f"  {sym} — running B (HTF-only, no ORB bias)...")
        sigs_b, days_b = run_live_simulation(
            run_days=None, symbol=sym, m1_file=m1, h1_file=h1,
            orb_bias_filter=False
        )

        ma = compute_metrics(sigs_a, f"{sym} A (bias)")
        mb = compute_metrics(sigs_b, f"{sym} B (HTF-only)")

        per_index_results.append((sym, ma, mb))
        all_a.extend(sigs_a)
        all_b.extend(sigs_b)

    # Combined metrics
    ma_all = compute_metrics(all_a, "PORTFOLIO A (bias)")
    mb_all = compute_metrics(all_b, "PORTFOLIO B (HTF-only)")

    # Print results
    print("\n\n" + "=" * 75)
    print("  RESULTS SUMMARY")
    print("=" * 75)

    header = f"{'Index':<12} {'Variant':<18} {'Trades':>7} {'WR%':>6} {'AvgR':>7} {'TotalR':>8} {'PF':>6} {'MaxDD_R':>8}"
    print(f"\n{header}")
    print("─" * len(header))

    for sym, ma, mb in per_index_results:
        for m in [ma, mb]:
            tag = "A (bias)" if "A" in m["label"] else "B (HTF)"
            print(f"{sym:<12} {tag:<18} {m['trades']:>7} {m['win_rate']:>5.1f}% {m['avg_r']:>7.3f} {m['total_r']:>8.2f} {m['profit_factor']:>6.2f} {m['max_dd_r']:>8.2f}")
        # Delta
        dt = mb["trades"] - ma["trades"]
        dr = mb["total_r"] - ma["total_r"]
        dwr = mb["win_rate"] - ma["win_rate"]
        print(f"{'':>12} {'Δ (B-A)':<18} {dt:>+7} {dwr:>+5.1f}% {'':>7} {dr:>+8.2f}")
        print()

    print("─" * len(header))
    for m in [ma_all, mb_all]:
        tag = "A (bias)" if "A" in m["label"] else "B (HTF)"
        print(f"{'PORTFOLIO':<12} {tag:<18} {m['trades']:>7} {m['win_rate']:>5.1f}% {m['avg_r']:>7.3f} {m['total_r']:>8.2f} {m['profit_factor']:>6.2f} {m['max_dd_r']:>8.2f}")

    dt = mb_all["trades"] - ma_all["trades"]
    dr = mb_all["total_r"] - ma_all["total_r"]
    dwr = mb_all["win_rate"] - ma_all["win_rate"]
    print(f"{'':>12} {'Δ (B-A)':<18} {dt:>+7} {dwr:>+5.1f}% {'':>7} {dr:>+8.2f}")

    print("\n" + "=" * 75)
    if mb_all["total_r"] > ma_all["total_r"] and mb_all["win_rate"] >= ma_all["win_rate"] - 3:
        print("  >> Removing ORB bias filter IMPROVES portfolio total R.")
        print("     Consider switching to HTF-only mode for live trading.")
    elif mb_all["total_r"] > ma_all["total_r"]:
        print("  >> HTF-only has higher total R but lower win rate.")
        print("     More trades, potentially more variance — review per-index.")
    else:
        print("  >> ORB bias filter HELPS overall. Keep the current double-confirmation.")
    print("=" * 75)


if __name__ == "__main__":
    run_comparison()
