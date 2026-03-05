#!/usr/bin/env python3
"""
OB Walk-Forward: 6-month IS → 2.5-year OOS
Grid: tp_R × breakeven_at_R × tap_depth × pivot_len = 24 combos
All direction-neutral. No per-symbol tuning.
"""

import itertools, json, os, time
import numpy as np, pandas as pd
from ob_strict_engine import Config, load_m1, strict_backtest, profit_factor, max_drawdown_R

SYMBOL_COSTS = {
    "EURUSD": dict(spread=0.00012, entry_slippage=0.00015, exit_slippage=0.00015, sl_buffer=0.00005),
    "USDJPY": dict(spread=0.015,   entry_slippage=0.015,   exit_slippage=0.015,   sl_buffer=0.005),
    "XAUUSD": dict(spread=0.30,    entry_slippage=0.15,    exit_slippage=0.15,    sl_buffer=0.05),
}

SYMBOLS = {
    "EURUSD": "forexDATA/EURUSD_3YEAR_M1.csv",
    "USDJPY": "forexDATA/USDJPY_3YEAR_M1.csv",
    "XAUUSD": "forexDATA/XAUUSD_3YEAR_M1.csv",
}

# Direction-neutral grid — structural params only
PARAM_GRID = {
    "tp_R":            [1.5, 2.0, 2.5],
    "breakeven_at_R":  [0.0, 1.0],       # off or at 1R
    "tap_depth":       [0.50, 0.618],
    "pivot_len":       [3, 5],
}

# Fixed session: 02:00-10:00 NY continuous
SESSION = dict(use_killzones=True, london_start=2, london_end=10, ny_start=99, ny_end=99)

# Fixed structural params not in grid
FIXED = dict(ob_lookback_m15=10, impulse_lookback_m15=20, ema_fast=21, ema_slow=50,
             use_h1_bias=True, seed=7, miss_fill_prob=0.08, spread_on_exit=False, swap_per_day_R=0.0)


def split_data(df, is_months=6):
    cutoff = df["datetime"].min() + pd.DateOffset(months=is_months)
    return df[df["datetime"] < cutoff].reset_index(drop=True), df[df["datetime"] >= cutoff].reset_index(drop=True)


def run_one(df_m1, symbol, params):
    cfg = Config(symbol=symbol, csv_path="", months=0, **params, **SYMBOL_COSTS[symbol], **SESSION, **FIXED)
    _, m = strict_backtest(df_m1, cfg)
    return m


def main():
    out_dir = "reports/ob_walkforward"
    os.makedirs(out_dir, exist_ok=True)

    print("Loading & splitting data (IS=6mo, OOS=rest)...")
    data = {}
    for sym, csv in SYMBOLS.items():
        df = load_m1(csv)
        is_df, oos_df = split_data(df, 6)
        data[sym] = {"is": is_df, "oos": oos_df}
        print(f"  {sym}: IS {len(is_df):,} rows ({is_df.datetime.iloc[0]} -> {is_df.datetime.iloc[-1]}), "
              f"OOS {len(oos_df):,} rows ({oos_df.datetime.iloc[0]} -> {oos_df.datetime.iloc[-1]})")

    keys = list(PARAM_GRID.keys())
    combos = list(itertools.product(*[PARAM_GRID[k] for k in keys]))
    print(f"\nGrid: {len(combos)} combos x {len(SYMBOLS)} pairs = {len(combos)*len(SYMBOLS)} IS runs")

    # -- IS GRID --
    print("\n=== IN-SAMPLE (first 6 months) ===")
    results = []
    t0 = time.time()
    for idx, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        row = {"params": params.copy()}
        total_R, pf_sum, worst_dd, all_pos = 0, 0, 0, True

        for sym in SYMBOLS:
            m = run_one(data[sym]["is"], sym, params)
            row[f"{sym}_trades"] = m["trades"]
            row[f"{sym}_wr"]     = m["winrate"]
            row[f"{sym}_pf"]     = m["profit_factor"]
            row[f"{sym}_sumR"]   = m["sum_R"]
            row[f"{sym}_dd"]     = m["max_dd_R"]
            total_R += m["sum_R"]
            pf_sum  += m["profit_factor"] if np.isfinite(m["profit_factor"]) else 0
            worst_dd = min(worst_dd, m["max_dd_R"])
            if m["sum_R"] <= 0: all_pos = False

        row["total_R"]  = total_R
        row["avg_pf"]   = pf_sum / len(SYMBOLS)
        row["worst_dd"] = worst_dd
        row["all_pos"]  = all_pos
        results.append(row)

        if (idx+1) % 8 == 0:
            e = time.time()-t0; eta = e/(idx+1)*(len(combos)-idx-1)
            print(f"  [{idx+1}/{len(combos)}] {e:.0f}s elapsed, ~{eta:.0f}s remaining")

    print(f"IS done in {time.time()-t0:.0f}s")

    # Rank: prefer all-positive, then by total_R
    results.sort(key=lambda r: (r["all_pos"], r["total_R"]), reverse=True)

    # Show top 10
    print(f"\n{'Rk':<3} {'tp':>4} {'BE':>4} {'tap':>5} {'piv':>4} | {'EU_R':>6} {'UJ_R':>6} {'XAU_R':>6} | {'Tot':>7} {'DD':>7} {'PF':>5} {'all+':>4}")
    for i, r in enumerate(results[:10], 1):
        p = r["params"]
        print(f"{i:<3} {p['tp_R']:>4.1f} {p['breakeven_at_R']:>4.1f} {p['tap_depth']:>5.3f} {p['pivot_len']:>4} | "
              f"{r['EURUSD_sumR']:>6.1f} {r['USDJPY_sumR']:>6.1f} {r['XAUUSD_sumR']:>6.1f} | "
              f"{r['total_R']:>7.1f} {r['worst_dd']:>7.1f} {r['avg_pf']:>5.2f} {'Y' if r['all_pos'] else 'N':>4}")

    best_params = results[0]["params"]
    print(f"\n>>> BEST IS PARAMS: {best_params}")

    # -- OOS --
    print("\n=== OUT-OF-SAMPLE (~2.5 years) ===")
    oos = {}
    for sym in SYMBOLS:
        m = run_one(data[sym]["oos"], sym, best_params)
        oos[sym] = m
        print(f"  {sym}: Trades={m['trades']}, WR={m['winrate']:.1%}, PF={m['profit_factor']:.2f}, "
              f"SumR={m['sum_R']:.1f}, MaxDD={m['max_dd_R']:.1f}")

    total = sum(m["sum_R"] for m in oos.values())
    worst = min(m["max_dd_R"] for m in oos.values())
    print(f"\n  TOTAL OOS SumR: {total:.1f}R")
    print(f"  Worst OOS DD:   {worst:.1f}R")

    # Also run top-3 IS combos on OOS for robustness check
    print("\n=== TOP-3 IS -> OOS COMPARISON ===")
    for rank, r in enumerate(results[:3], 1):
        p = r["params"]
        oos_total = 0
        oos_detail = []
        for sym in SYMBOLS:
            m = run_one(data[sym]["oos"], sym, p)
            oos_total += m["sum_R"]
            oos_detail.append(f"{sym}={m['sum_R']:.1f}R")
        print(f"  #{rank} {p} -> IS={r['total_R']:.1f}R -> OOS={oos_total:.1f}R ({', '.join(oos_detail)})")

    # Save
    summary = {
        "is_months": 6,
        "grid_size": len(combos),
        "best_params": best_params,
        "is_top10": [{"params": r["params"], "total_R": r["total_R"], "worst_dd": r["worst_dd"],
                       "EURUSD": r["EURUSD_sumR"], "USDJPY": r["USDJPY_sumR"], "XAUUSD": r["XAUUSD_sumR"]}
                      for r in results[:10]],
        "oos_results": {s: {k:v for k,v in m.items() if k!="params"} for s,m in oos.items()},
        "oos_total": total, "oos_worst_dd": worst,
    }
    path = os.path.join(out_dir, "walkforward_summary.json")
    with open(path, "w") as f: json.dump(summary, f, indent=2)
    print(f"\nSaved: {path}")


if __name__ == "__main__":
    main()
