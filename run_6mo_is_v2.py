#!/usr/bin/env python3
"""Run 6-month IN-SAMPLE backtest with v3 entry (M3 pivot confirmation) + Exit Model C across all 3 pairs."""

import os, json, time
import pandas as pd
import numpy as np
from ob_strict_engine import Config, load_m1, strict_backtest, profit_factor

PAIRS = {
    "EURUSD": {
        "csv": "forexDATA/EURUSD_3YEAR_M1.csv",
        "spread": 0.00012, "entry_slip": 0.00015, "exit_slip": 0.00015,
        "sl_buffer": 0.00005,
    },
    "USDJPY": {
        "csv": "forexDATA/USDJPY_3YEAR_M1.csv",
        "spread": 0.015, "entry_slip": 0.015, "exit_slip": 0.015,
        "sl_buffer": 0.005,
    },
    "XAUUSD": {
        "csv": "forexDATA/XAUUSD_3YEAR_M1.csv",
        "spread": 0.30, "entry_slip": 0.15, "exit_slip": 0.15,
        "sl_buffer": 0.05,
    },
}

OUT_DIR = "reports/ob_v3_6mo_is_m3pivot"
os.makedirs(OUT_DIR, exist_ok=True)

results = []

for sym, p in PAIRS.items():
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"  {sym} — 6mo IS, Exit Model C, v3 entry (M3 pivot confirmation)")
    print(f"{'='*60}")

    cfg = Config(
        symbol=sym,
        csv_path=p["csv"],
        out_dir=OUT_DIR,
        first_months=6,       # FIRST 6 months only (IS)

        # Session: 02:00-10:00 NY continuous
        london_start=2, london_end=10,
        ny_start=99, ny_end=99,

        # v3 entry: M3 pivot + deeper surface rejection (30-60%)
        min_ob_pen=0.30,
        max_ob_pen=0.60,
        m3_pivot_confirms=2,

        # Exit model C (BE@1R, 50%@2R, trail M15 swings)
        exit_model="C",
        tp_R=2.5,

        # Costs
        spread=p["spread"],
        entry_slippage=p["entry_slip"],
        exit_slippage=p["exit_slip"],
        sl_buffer=p["sl_buffer"],
        miss_fill_prob=0.08,

        pivot_len=3,
        seed=7,
    )

    df = load_m1(cfg.csv_path)
    trades_df, metrics = strict_backtest(df, cfg)
    elapsed = time.time() - t0

    # Save
    trades_df.to_csv(os.path.join(OUT_DIR, f"{sym}_v3_6mo_is_trades.csv"), index=False)
    with open(os.path.join(OUT_DIR, f"{sym}_v3_6mo_is_metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)

    r = trades_df["R"].to_numpy(float) if len(trades_df) else np.array([])
    longs = trades_df[trades_df["direction"] == "LONG"]["R"].to_numpy(float) if len(trades_df) else np.array([])
    shorts = trades_df[trades_df["direction"] == "SHORT"]["R"].to_numpy(float) if len(trades_df) else np.array([])

    print(f"  Trades : {len(r)}")
    print(f"  WR     : {(r > 0).mean():.1%}" if len(r) else "  WR     : N/A")
    print(f"  PF     : {metrics['profit_factor']:.2f}")
    print(f"  SumR   : {r.sum():.1f}")
    print(f"  MaxDD  : {metrics['max_dd_R']:.1f}R")
    print(f"  AvgR   : {r.mean():.3f}" if len(r) else "  AvgR   : N/A")
    print(f"  Longs  : {len(longs)} (WR {(longs>0).mean():.1%}, Sum {longs.sum():.1f}R)" if len(longs) else "  Longs  : 0")
    print(f"  Shorts : {len(shorts)} (WR {(shorts>0).mean():.1%}, Sum {shorts.sum():.1f}R)" if len(shorts) else "  Shorts : 0")
    print(f"  Time   : {elapsed:.1f}s")

    results.append({
        "symbol": sym,
        "trades": len(r),
        "winrate": float((r > 0).mean()) if len(r) else 0,
        "profit_factor": metrics["profit_factor"],
        "sum_R": float(r.sum()),
        "max_dd_R": metrics["max_dd_R"],
        "avg_R": float(r.mean()) if len(r) else 0,
        "longs": len(longs),
        "long_wr": float((longs > 0).mean()) if len(longs) else 0,
        "shorts": len(shorts),
        "short_wr": float((shorts > 0).mean()) if len(shorts) else 0,
    })

# Summary
print(f"\n{'='*60}")
print("  SUMMARY — v2 Surface Rejection | 6mo IS | Exit C")
print(f"{'='*60}")
print(f"{'Symbol':<10} {'Trades':>7} {'WR':>7} {'PF':>7} {'SumR':>8} {'MaxDD':>8}")
print("-" * 50)
total_R = 0
for r in results:
    print(f"{r['symbol']:<10} {r['trades']:>7} {r['winrate']:>6.1%} {r['profit_factor']:>7.2f} {r['sum_R']:>+8.1f} {r['max_dd_R']:>8.1f}")
    total_R += r["sum_R"]
print("-" * 50)
print(f"{'TOTAL':<10} {'':>7} {'':>7} {'':>7} {total_R:>+8.1f}")

with open(os.path.join(OUT_DIR, "summary.json"), "w") as f:
    json.dump(results, f, indent=2)
print(f"\nResults saved to {OUT_DIR}/")
