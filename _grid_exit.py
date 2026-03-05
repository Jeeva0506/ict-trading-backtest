#!/usr/bin/env python3
"""Grid: exit model comparison (Model A at various TP levels vs Model C) on XAUUSD with max_ob_atr=1.5."""

import numpy as np
from ob_ict_engine import Config, load_m1, strict_backtest

CSV = "forexDATA/XAUUSD_3YEAR_M1.csv"
df = load_m1(CSV)

print(f"{'Model':>7} {'TP_R':>6} {'Trades':>7} {'WR':>7} {'PF':>7} {'SumR':>8} {'MaxDD':>8} {'AvgR':>7} {'L_WR':>7} {'S_WR':>7}")
print("-" * 85)

# Model A with various TP levels
for tp in [1.0, 1.5, 2.0, 2.5, 3.0]:
    cfg = Config(
        symbol="XAUUSD", csv_path=CSV, out_dir="./tmp_grid",
        first_months=6,
        london_start=2, london_end=10, ny_start=99, ny_end=99,
        disp_atr_mult=1.5, disp_body_pct=0.60, atr_window=20,
        ob_lookback=10, max_ob_atr=1.5, ob_max_wait=40,
        exit_model="A", tp_R=tp, breakeven_at_R=0.0,
        pivot_len=3,
        spread=0.30, entry_slippage=0.15, exit_slippage=0.15,
        sl_buffer=0.05, miss_fill_prob=0.08, seed=7,
        require_mss=False, max_risk_atr=0.0, max_entry_overshoot=0.0,
    )
    trades_df, metrics = strict_backtest(df, cfg)
    r = trades_df["R"].to_numpy(float) if len(trades_df) else np.array([])
    longs = trades_df[trades_df["direction"] == "LONG"]["R"].to_numpy(float) if len(trades_df) else np.array([])
    shorts = trades_df[trades_df["direction"] == "SHORT"]["R"].to_numpy(float) if len(trades_df) else np.array([])
    wr = (r > 0).mean() if len(r) else 0
    lwr = (longs > 0).mean() if len(longs) else 0
    swr = (shorts > 0).mean() if len(shorts) else 0
    avgr = r.mean() if len(r) else 0
    pf = metrics["profit_factor"]
    print(f"{'A':>7} {tp:>6.1f} {len(r):>7} {wr:>6.1%} {pf:>7.2f} {r.sum():>+8.1f} {metrics['max_dd_R']:>8.1f} {avgr:>+7.3f} {lwr:>6.1%} {swr:>6.1%}")

# Model A with BE
for tp in [2.0, 3.0]:
    cfg = Config(
        symbol="XAUUSD", csv_path=CSV, out_dir="./tmp_grid",
        first_months=6,
        london_start=2, london_end=10, ny_start=99, ny_end=99,
        disp_atr_mult=1.5, disp_body_pct=0.60, atr_window=20,
        ob_lookback=10, max_ob_atr=1.5, ob_max_wait=40,
        exit_model="A", tp_R=tp, breakeven_at_R=1.0,
        pivot_len=3,
        spread=0.30, entry_slippage=0.15, exit_slippage=0.15,
        sl_buffer=0.05, miss_fill_prob=0.08, seed=7,
        require_mss=False, max_risk_atr=0.0, max_entry_overshoot=0.0,
    )
    trades_df, metrics = strict_backtest(df, cfg)
    r = trades_df["R"].to_numpy(float) if len(trades_df) else np.array([])
    longs = trades_df[trades_df["direction"] == "LONG"]["R"].to_numpy(float) if len(trades_df) else np.array([])
    shorts = trades_df[trades_df["direction"] == "SHORT"]["R"].to_numpy(float) if len(trades_df) else np.array([])
    wr = (r > 0).mean() if len(r) else 0
    lwr = (longs > 0).mean() if len(longs) else 0
    swr = (shorts > 0).mean() if len(shorts) else 0
    avgr = r.mean() if len(r) else 0
    pf = metrics["profit_factor"]
    print(f"{'A+BE':>7} {tp:>6.1f} {len(r):>7} {wr:>6.1%} {pf:>7.2f} {r.sum():>+8.1f} {metrics['max_dd_R']:>8.1f} {avgr:>+7.3f} {lwr:>6.1%} {swr:>6.1%}")

# Model C (trailing)
cfg = Config(
    symbol="XAUUSD", csv_path=CSV, out_dir="./tmp_grid",
    first_months=6,
    london_start=2, london_end=10, ny_start=99, ny_end=99,
    disp_atr_mult=1.5, disp_body_pct=0.60, atr_window=20,
    ob_lookback=10, max_ob_atr=1.5, ob_max_wait=40,
    exit_model="C", tp_R=2.5, pivot_len=3,
    spread=0.30, entry_slippage=0.15, exit_slippage=0.15,
    sl_buffer=0.05, miss_fill_prob=0.08, seed=7,
    require_mss=False, max_risk_atr=0.0, max_entry_overshoot=0.0,
)
trades_df, metrics = strict_backtest(df, cfg)
r = trades_df["R"].to_numpy(float) if len(trades_df) else np.array([])
longs = trades_df[trades_df["direction"] == "LONG"]["R"].to_numpy(float) if len(trades_df) else np.array([])
shorts = trades_df[trades_df["direction"] == "SHORT"]["R"].to_numpy(float) if len(trades_df) else np.array([])
wr = (r > 0).mean() if len(r) else 0
lwr = (longs > 0).mean() if len(longs) else 0
swr = (shorts > 0).mean() if len(shorts) else 0
avgr = r.mean() if len(r) else 0
pf = metrics["profit_factor"]
print(f"{'C':>7} {'trail':>6} {len(r):>7} {wr:>6.1%} {pf:>7.2f} {r.sum():>+8.1f} {metrics['max_dd_R']:>8.1f} {avgr:>+7.3f} {lwr:>6.1%} {swr:>6.1%}")
