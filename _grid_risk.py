#!/usr/bin/env python3
"""Grid search: risk cap + OB size filter across all 3 pairs (no MSS)."""

import numpy as np
from ob_ict_engine import Config, load_m1, strict_backtest

PAIRS = {
    "EURUSD": {"csv": "forexDATA/EURUSD_3YEAR_M1.csv", "spread": 0.00012, "slip": 0.00015, "buf": 0.00005},
    "USDJPY": {"csv": "forexDATA/USDJPY_3YEAR_M1.csv", "spread": 0.015, "slip": 0.015, "buf": 0.005},
    "XAUUSD": {"csv": "forexDATA/XAUUSD_3YEAR_M1.csv", "spread": 0.30, "slip": 0.15, "buf": 0.05},
}

# Pre-load data
data = {sym: load_m1(p["csv"]) for sym, p in PAIRS.items()}

print(f"{'Sym':>6} {'max_ob':>7} {'riskCap':>8} {'Trades':>7} {'WR':>7} {'PF':>7} {'SumR':>8} {'MaxDD':>8} {'L_WR':>7} {'S_WR':>7}")
print("-" * 85)

for sym, p in PAIRS.items():
    for max_ob in [1.0, 1.5, 2.0]:
        for max_risk in [0.0, 1.5, 2.0]:
            cfg = Config(
                symbol=sym, csv_path=p["csv"], out_dir="./tmp_grid",
                first_months=6,
                london_start=2, london_end=10, ny_start=99, ny_end=99,
                disp_atr_mult=1.5, disp_body_pct=0.60, atr_window=20,
                ob_lookback=10, max_ob_atr=max_ob, ob_max_wait=40,
                exit_model="C", tp_R=2.5, pivot_len=3,
                spread=p["spread"], entry_slippage=p["slip"], exit_slippage=p["slip"],
                sl_buffer=p["buf"], miss_fill_prob=0.08, seed=7,
                require_mss=False, max_risk_atr=max_risk, max_entry_overshoot=0.0,
            )
            trades_df, metrics = strict_backtest(data[sym], cfg)
            r = trades_df["R"].to_numpy(float) if len(trades_df) else np.array([])
            longs = trades_df[trades_df["direction"] == "LONG"]["R"].to_numpy(float) if len(trades_df) else np.array([])
            shorts = trades_df[trades_df["direction"] == "SHORT"]["R"].to_numpy(float) if len(trades_df) else np.array([])
            wr = (r > 0).mean() if len(r) else 0
            lwr = (longs > 0).mean() if len(longs) else 0
            swr = (shorts > 0).mean() if len(shorts) else 0
            pf = metrics["profit_factor"]
            print(f"{sym:>6} {max_ob:>7.1f} {max_risk:>8.1f} {len(r):>7} {wr:>6.1%} {pf:>7.2f} {r.sum():>+8.1f} {metrics['max_dd_R']:>8.1f} {lwr:>6.1%} {swr:>6.1%}")
    print()
