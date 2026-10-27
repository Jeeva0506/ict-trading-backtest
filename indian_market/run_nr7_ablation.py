"""
NR7 Filter Ablation Study
==========================
Tests every useful combination of the 6 filters to measure
each filter's individual contribution and find the optimal subset.

Requires data already cached by run_nr7_backtest.py.
Run:
    python run_nr7_backtest.py          # first run (downloads data)
    python run_nr7_ablation.py          # ablation on cached data
"""
import sys, json
from pathlib import Path
from collections import defaultdict
from itertools import combinations

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from strategies.nr7_positional import (
    compute_indicators, generate_signals, simulate_trades,
    CAPITAL, RISK_RS, FILTER_NAMES, FILTERS_ALL, FILTERS_NONE,
)
from run_nr7_backtest import load_from_cache, _universe, _get_kite, _fetch_daily, NIFTY50_TOKEN

CACHE_DIR        = ROOT / "data" / "nr7_cache"
LOOKBACK_DAYS    = 3 * 365 + 60
MIN_TRADES       = 10    # skip configs with too few trades to be meaningful


# ── Run one filter config ─────────────────────────────────────────────────────
def _run_config(stock_data, nifty_df, filters):
    all_trades = []
    for sym, df in stock_data.items():
        df_ind = compute_indicators(df)
        sigs   = generate_signals(df_ind, nifty_df=nifty_df, filters=filters)
        for s in sigs: s['symbol'] = sym
        trades = [t for t in simulate_trades(df_ind, sigs)
                  if t.get('exit_reason') != 'DATA_END']
        all_trades.extend(trades)

    if len(all_trades) < MIN_TRADES:
        return None

    wins = [t for t in all_trades if t['win']]
    loss = [t for t in all_trades if not t['win']]
    gw   = sum(t['net_rs'] for t in wins)
    gl   = sum(abs(t['net_rs']) for t in loss)
    return {
        "n":     len(all_trades),
        "wr":    len(wins) / len(all_trades) * 100,
        "pf":    gw / gl if gl > 0 else 9999.0,
        "net":   gw - gl,
        "avg_r": sum(t['r_multiple'] for t in all_trades) / len(all_trades),
    }


# ── Ablation ──────────────────────────────────────────────────────────────────
def run_ablation():
    print("=" * 72)
    print("  NR7 FILTER ABLATION STUDY")
    print("=" * 72)

    kite       = _get_kite()
    universe   = _universe(kite)
    nifty_raw  = _fetch_daily(kite, NIFTY50_TOKEN, days=LOOKBACK_DAYS)
    nifty_df   = compute_indicators(nifty_raw) if not nifty_raw.empty else pd.DataFrame()
    stock_data = load_from_cache(universe)
    print(f"Loaded {len(stock_data)} stocks | Nifty {len(nifty_df)} days\n")

    results = []

    # ── 1. Baseline: no optional filters ─────────────────────────────────────
    r = _run_config(stock_data, nifty_df, FILTERS_NONE)
    if r:
        r['label'] = "BASELINE (no filters)"
        r['filters_on'] = []
        results.append(r)

    # ── 2. Each filter individually ───────────────────────────────────────────
    for fname in FILTER_NAMES:
        f = {k: (k == fname) for k in FILTER_NAMES}
        r = _run_config(stock_data, nifty_df, f)
        if r:
            r['label']      = f"Only: {fname}"
            r['filters_on'] = [fname]
            results.append(r)

    # ── 3. User-spec filters only (ema_cross + nifty_trend) ──────────────────
    f = {k: k in ('ema_cross', 'nifty_trend') for k in FILTER_NAMES}
    r = _run_config(stock_data, nifty_df, f)
    if r:
        r['label']      = "User filters (ema_cross + nifty_trend)"
        r['filters_on'] = ['ema_cross', 'nifty_trend']
        results.append(r)

    # ── 4. My additions only (vol_surge + tight_nr7 + nr_streak + near_52w) ──
    mine = ['vol_surge', 'tight_nr7', 'nr_streak', 'near_52w']
    f = {k: k in mine for k in FILTER_NAMES}
    r = _run_config(stock_data, nifty_df, f)
    if r:
        r['label']      = "My filters only (4 extras)"
        r['filters_on'] = mine
        results.append(r)

    # ── 5. All 2-filter combos ────────────────────────────────────────────────
    for combo in combinations(FILTER_NAMES, 2):
        f = {k: k in combo for k in FILTER_NAMES}
        r = _run_config(stock_data, nifty_df, f)
        if r:
            r['label']      = f"Pair: {'+'.join(combo)}"
            r['filters_on'] = list(combo)
            results.append(r)

    # ── 6. All 3-filter combos ────────────────────────────────────────────────
    for combo in combinations(FILTER_NAMES, 3):
        f = {k: k in combo for k in FILTER_NAMES}
        r = _run_config(stock_data, nifty_df, f)
        if r:
            r['label']      = f"Triple: {'+'.join(combo)}"
            r['filters_on'] = list(combo)
            results.append(r)

    # ── 7. All filters ON ─────────────────────────────────────────────────────
    r = _run_config(stock_data, nifty_df, FILTERS_ALL)
    if r:
        r['label']      = "ALL FILTERS ON"
        r['filters_on'] = FILTER_NAMES
        results.append(r)

    # ── Print sorted by PF ────────────────────────────────────────────────────
    results.sort(key=lambda x: x['pf'], reverse=True)

    print(f"{'Config':<44} {'N':>5} {'WR':>6} {'PF':>6} {'AvgR':>6} {'Net':>11}")
    print("-" * 80)
    for r in results:
        pf_s = f"{r['pf']:.2f}" if r['pf'] < 100 else ">100"
        print(f"{r['label']:<44} {r['n']:>5} {r['wr']:>5.0f}%  {pf_s:>6}  "
              f"{r['avg_r']:>+5.2f}R  Rs{r['net']:>+9,.0f}")

    # ── Individual filter contribution ────────────────────────────────────────
    print("\n\nINDIVIDUAL FILTER IMPACT (vs baseline):")
    print("-" * 60)
    base = next((r for r in results if r['label'].startswith("BASELINE")), None)
    if base:
        base_pf  = base['pf']
        base_net = base['net']
        for fname in FILTER_NAMES:
            solo = next((r for r in results if r['filters_on'] == [fname]), None)
            if solo:
                dpf  = solo['pf']  - base_pf
                dnet = solo['net'] - base_net
                arrow = "+" if dpf >= 0 else ""
                print(f"  {fname:<15}  PF: {base_pf:.2f} -> {solo['pf']:.2f} "
                      f"({arrow}{dpf:+.2f})   Net: Rs{dnet:+,.0f}")

    # ── Best config ───────────────────────────────────────────────────────────
    best = results[0]
    print(f"\n\nBEST CONFIG: {best['label']}")
    print(f"  Filters : {best['filters_on']}")
    print(f"  Trades  : {best['n']}")
    print(f"  Win rate: {best['wr']:.1f}%")
    print(f"  PF      : {best['pf']:.2f}")
    print(f"  Avg R   : {best['avg_r']:+.2f}R")
    print(f"  Net P&L : Rs{best['net']:+,.0f}")

    # ── Save ──────────────────────────────────────────────────────────────────
    out_dir = ROOT / "reports" / "nr7_positional"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = [{k: v for k, v in r.items()} for r in results]
    (out_dir / "nr7_ablation.json").write_text(json.dumps(out, indent=2))
    pd.DataFrame(results).drop(columns=['filters_on'], errors='ignore').to_csv(
        out_dir / "nr7_ablation.csv", index=False
    )
    print(f"\nResults saved to {out_dir}/nr7_ablation.json")


if __name__ == "__main__":
    run_ablation()
