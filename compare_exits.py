#!/usr/bin/env python3
"""Compare exit models A, B, C across all 3 forex pairs."""

from ob_strict_engine import Config, load_m1, strict_backtest

COSTS = {
    "EURUSD": dict(spread=0.00012, entry_slippage=0.00015, exit_slippage=0.00015, sl_buffer=0.00005),
    "USDJPY": dict(spread=0.015,   entry_slippage=0.015,   exit_slippage=0.015,   sl_buffer=0.005),
    "XAUUSD": dict(spread=0.30,    entry_slippage=0.15,    exit_slippage=0.15,    sl_buffer=0.05),
}
DATA = {
    "EURUSD": "forexDATA/EURUSD_3YEAR_M1.csv",
    "USDJPY": "forexDATA/USDJPY_3YEAR_M1.csv",
    "XAUUSD": "forexDATA/XAUUSD_3YEAR_M1.csv",
}

BASE = dict(tap_depth=0.618, pivot_len=3, ob_lookback_m15=10, impulse_lookback_m15=20,
            ema_fast=21, ema_slow=50, use_h1_bias=True, seed=7, miss_fill_prob=0.08,
            spread_on_exit=False, swap_per_day_R=0.0,
            use_killzones=True, london_start=2, london_end=10, ny_start=99, ny_end=99)

models = {
    "A (TP=2.5R)":       dict(exit_model="A", tp_R=2.5, breakeven_at_R=0.0),
    "A (TP=2.5+BE@1R)":  dict(exit_model="A", tp_R=2.5, breakeven_at_R=1.0),
    "B (tiered 1/2/3R)": dict(exit_model="B", tp_R=2.5, breakeven_at_R=0.0),
    "C (trail M15)":     dict(exit_model="C", tp_R=2.5, breakeven_at_R=0.0),
}

loaded = {}
for sym, path in DATA.items():
    loaded[sym] = load_m1(path)
    print(f"Loaded {sym}: {len(loaded[sym]):,} rows")

print()
hdr = f"{'Model':<22} | {'Pair':<7} | {'Trades':>6} | {'WR':>6} | {'PF':>6} | {'SumR':>7} | {'MaxDD':>7}"
print(hdr)
print("-" * len(hdr))

for mname, mparams in models.items():
    totals = {"trades": 0, "sumR": 0.0, "worst_dd": 0.0}
    for sym in DATA:
        cfg = Config(symbol=sym, csv_path="", months=0, **mparams, **COSTS[sym], **BASE)
        _, m = strict_backtest(loaded[sym], cfg)
        wr = f"{m['winrate']:.1%}" if m["trades"] > 0 else "N/A"
        pf = f"{m['profit_factor']:.2f}" if m["trades"] > 0 else "N/A"
        print(f"{mname:<22} | {sym:<7} | {m['trades']:>6} | {wr:>6} | {pf:>6} | {m['sum_R']:>7.1f} | {m['max_dd_R']:>7.1f}")
        totals["trades"] += m["trades"]
        totals["sumR"] += m["sum_R"]
        totals["worst_dd"] = min(totals["worst_dd"], m["max_dd_R"])
    print(f"{mname:<22} | {'TOTAL':<7} | {totals['trades']:>6} |        |        | {totals['sumR']:>7.1f} | {totals['worst_dd']:>7.1f}")
    print("-" * len(hdr))
