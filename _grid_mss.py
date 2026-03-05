#!/usr/bin/env python3
"""Quick grid: test MSS lookback values + risk cap combinations on XAUUSD only (fastest pair)."""

import time
import numpy as np
from ob_ict_engine import Config, load_m1, strict_backtest

CSV = "forexDATA/XAUUSD_3YEAR_M1.csv"
df = load_m1(CSV)

print(f"{'MSS_LB':>7} {'MaxRiskATR':>11} {'Trades':>7} {'WR':>7} {'PF':>7} {'SumR':>8} {'MaxDD':>8} {'L_WR':>7} {'S_WR':>7}")
print("-" * 80)

for mss_lb in [0, 5, 10, 15, 20]:
    for max_risk in [0.0, 1.5, 2.0]:
        cfg = Config(
            symbol="XAUUSD", csv_path=CSV, out_dir="./tmp_grid",
            first_months=6,
            london_start=2, london_end=10, ny_start=99, ny_end=99,
            disp_atr_mult=1.5, disp_body_pct=0.60, atr_window=20,
            ob_lookback=10, max_ob_atr=2.0, ob_max_wait=40,
            exit_model="C", tp_R=2.5, pivot_len=3,
            spread=0.30, entry_slippage=0.15, exit_slippage=0.15,
            sl_buffer=0.05, miss_fill_prob=0.08, seed=7,
            require_mss=(mss_lb > 0), mss_lookback=mss_lb,
            max_risk_atr=max_risk, max_entry_overshoot=0.0,
        )
        trades_df, metrics = strict_backtest(df, cfg)
        r = trades_df["R"].to_numpy(float) if len(trades_df) else np.array([])
        longs = trades_df[trades_df["direction"] == "LONG"]["R"].to_numpy(float) if len(trades_df) else np.array([])
        shorts = trades_df[trades_df["direction"] == "SHORT"]["R"].to_numpy(float) if len(trades_df) else np.array([])
        wr = (r > 0).mean() if len(r) else 0
        lwr = (longs > 0).mean() if len(longs) else 0
        swr = (shorts > 0).mean() if len(shorts) else 0
        pf = metrics["profit_factor"]
        print(f"{mss_lb:>7} {max_risk:>11.1f} {len(r):>7} {wr:>6.1%} {pf:>7.2f} {r.sum():>+8.1f} {metrics['max_dd_R']:>8.1f} {lwr:>6.1%} {swr:>6.1%}")
