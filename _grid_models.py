#!/usr/bin/env python3
"""Test Model D: 50% TP at 1R, trail rest on M15 swings — across all 3 pairs.
Also test hybrid: Model A(1R) for WR, compare to Model C."""

import numpy as np
from ob_ict_engine import Config, load_m1, strict_backtest

PAIRS = {
    "EURUSD": {"csv": "forexDATA/EURUSD_3YEAR_M1.csv", "spread": 0.00012, "slip": 0.00015, "buf": 0.00005},
    "USDJPY": {"csv": "forexDATA/USDJPY_3YEAR_M1.csv", "spread": 0.015, "slip": 0.015, "buf": 0.005},
    "XAUUSD": {"csv": "forexDATA/XAUUSD_3YEAR_M1.csv", "spread": 0.30, "slip": 0.15, "buf": 0.05},
}

data = {sym: load_m1(p["csv"]) for sym, p in PAIRS.items()}

# Best combos to test based on grid results
combos = [
    # (label, max_ob_atr, exit_model, tp_R, max_ob_desc)
    ("C_ob1.5", 1.5, "C", 2.5, "tight OB"),
    ("C_ob2.0", 2.0, "C", 2.5, "default OB"),
    ("B_ob1.5", 1.5, "B", 2.5, "tiered exit"),
    ("B_ob2.0", 2.0, "B", 2.5, "tiered exit"),
]

print(f"{'Label':>10} {'Sym':>6} {'Trades':>7} {'WR':>7} {'PF':>7} {'SumR':>8} {'MaxDD':>8} {'L_WR':>7} {'S_WR':>7} {'Reach1R':>8}")
print("-" * 95)

for label, max_ob, exit_model, tp, desc in combos:
    total_R = 0
    for sym, p in PAIRS.items():
        cfg = Config(
            symbol=sym, csv_path=p["csv"], out_dir="./tmp_grid",
            first_months=6,
            london_start=2, london_end=10, ny_start=99, ny_end=99,
            disp_atr_mult=1.5, disp_body_pct=0.60, atr_window=20,
            ob_lookback=10, max_ob_atr=max_ob, ob_max_wait=40,
            exit_model=exit_model, tp_R=tp, pivot_len=3,
            spread=p["spread"], entry_slippage=p["slip"], exit_slippage=p["slip"],
            sl_buffer=p["buf"], miss_fill_prob=0.08, seed=7,
            require_mss=False, max_risk_atr=0.0, max_entry_overshoot=0.0,
        )
        trades_df, metrics = strict_backtest(data[sym], cfg)
        r = trades_df["R"].to_numpy(float) if len(trades_df) else np.array([])
        longs = trades_df[trades_df["direction"] == "LONG"]["R"].to_numpy(float) if len(trades_df) else np.array([])
        shorts = trades_df[trades_df["direction"] == "SHORT"]["R"].to_numpy(float) if len(trades_df) else np.array([])
        wr = (r > 0).mean() if len(r) else 0
        lwr = (longs > 0).mean() if len(longs) else 0
        swr = (shorts > 0).mean() if len(shorts) else 0
        pf = metrics["profit_factor"]
        reached_1r = (r >= 0).sum() if len(r) else 0
        reached_pct = reached_1r / len(r) if len(r) else 0
        total_R += r.sum() if len(r) else 0
        print(f"{label:>10} {sym:>6} {len(r):>7} {wr:>6.1%} {pf:>7.2f} {r.sum():>+8.1f} {metrics['max_dd_R']:>8.1f} {lwr:>6.1%} {swr:>6.1%} {reached_pct:>7.1%}")
    print(f"{'':>10} {'TOTAL':>6} {'':>7} {'':>7} {'':>7} {total_R:>+8.1f}")
    print()
