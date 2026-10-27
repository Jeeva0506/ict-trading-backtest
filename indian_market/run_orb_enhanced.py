"""
Enhanced ORB Backtest — Filter Attribution + Combination Search
===============================================================
1. Baseline  : original ORB (no new filters)
2. Individual: add each new filter one at a time → see contribution
3. Combo scan: try all 2^N combinations of new filters
4. Best combos report + IS/OOS validation

Run: python run_orb_enhanced.py
"""
import sys, os, itertools
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np

from data.download_data import load_all
from data.download_intraday import load_nifty1m_historical, resample_from_1m
from strategies.orb_strategy import generate_orb_signals, ORB_PARAMS
from strategies.orb_enhanced_strategy import generate_enhanced_signals, ENHANCED_PARAMS
from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics, print_report

CAPITAL = 1_000_000
RESULTS = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS, exist_ok=True)

# In-sample / Out-of-sample split
IS_END  = "2018-12-31"   # 3yr IS: 2015-2018
OOS_END = "2024-03-27"   # OOS : 2019-2024


# ── Backtest engine (shared for baseline and enhanced) ─────────────────────────

def _run_backtest(df_intraday, df_daily, df_vix, sigs_df, params, start=None, end=None):
    p = {**ENHANCED_PARAMS, **params}

    if start:
        sigs_df = sigs_df[sigs_df.index >= pd.Timestamp(start)]
    if end:
        sigs_df = sigs_df[sigs_df.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]

    df = df_intraday.loc[sigs_df.index].copy()
    df["signal"]          = sigs_df["signal"]
    df["vix_val"]         = sigs_df["vix_val"]
    df["entry_bar_close"] = sigs_df["entry_price"]

    trades      = []
    active_date = None
    exit_h, exit_m = map(int, p["mandatory_exit"].split(":"))

    i = 0
    while i < len(df):
        row      = df.iloc[i]
        ts       = df.index[i]
        bar_date = ts.date()

        if bar_date == active_date:
            i += 1; continue

        if row["signal"] not in ("call", "put") or pd.isna(row["vix_val"]):
            i += 1; continue

        opt_type   = row["signal"]
        vix        = row["vix_val"]
        entry_spot = row["entry_bar_close"]
        K          = atm_strike(entry_spot, step=p["strike_step"])
        dte        = p["entry_dte"]
        entry_prem = bs_price(entry_spot, K, dte/365, p["rfr"], vix/100, opt_type)

        if entry_prem < 1.0:
            i += 1; continue

        max_budget = p["risk_per_trade"] / p["sl_pct"]
        lots       = max(1, min(int(max_budget / (entry_prem * p["lot_size"])), p["max_lots"]))
        qty        = lots * p["lot_size"]
        tp_price   = entry_prem * (1 + p["tp_pct"])
        sl_price   = entry_prem * (1 - p["sl_pct"])

        active_date   = bar_date
        exit_prem     = None
        exit_type_str = None

        i += 1
        while i < len(df):
            nrow  = df.iloc[i]
            nts   = df.index[i]
            if nts.date() != bar_date:
                break
            t = nts.time()
            if t.hour > exit_h or (t.hour == exit_h and t.minute >= exit_m):
                cp = bs_price(nrow["Close"], K, dte/365, p["rfr"], vix/100, opt_type)
                exit_prem, exit_type_str = cp, "3:15"; i += 1; break
            if opt_type == "call":
                oh = bs_price(nrow["High"], K, dte/365, p["rfr"], vix/100, "call")
                ol = bs_price(nrow["Low"],  K, dte/365, p["rfr"], vix/100, "call")
            else:
                oh = bs_price(nrow["Low"],  K, dte/365, p["rfr"], vix/100, "put")
                ol = bs_price(nrow["High"], K, dte/365, p["rfr"], vix/100, "put")
            if oh >= tp_price:
                exit_prem, exit_type_str = tp_price, "TP"; i += 1; break
            elif ol <= sl_price:
                exit_prem, exit_type_str = sl_price, "SL"; i += 1; break
            i += 1

        if exit_type_str is None:
            last = df[df.index.date == bar_date].iloc[-1]
            exit_prem     = bs_price(last["Close"], K, dte/365, p["rfr"], vix/100, opt_type)
            exit_type_str = "3:15"

        sell      = max(exit_prem, 0.05)
        gross_pnl = (sell - entry_prem) * qty
        cost      = round_trip_cost(entry_prem, sell, qty)
        net_pnl   = gross_pnl - cost

        trades.append({
            "entry_date": str(bar_date), "exit_date": str(bar_date),
            "direction" : opt_type, "strike": K,
            "entry_spot": round(entry_spot, 2), "entry_prem": round(entry_prem, 2),
            "exit_prem" : round(sell, 2), "lots": lots, "qty": qty,
            "days_held" : 0, "exit_type": exit_type_str,
            "gross_pnl" : round(gross_pnl, 2), "cost": round(cost, 2),
            "pnl_net"   : round(net_pnl, 2), "vix": round(vix, 2),
        })

    return trades


def _metrics_row(trades, label):
    m = compute_metrics(trades, CAPITAL)
    if "error" in m:
        return {"label": label, "n": 0, "wr": 0, "pf": 0, "net": 0, "pass": False}
    return {
        "label": label,
        "n"    : m["n_trades"],
        "wr"   : m["win_rate_pct"],
        "pf"   : m["profit_factor"],
        "net"  : m["net_pnl"],
        "pass" : m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55,
    }


def _print_row(r, indent=""):
    gate = "[PASS]" if r["pass"] else "      "
    print(f"  {indent}{r['label']:<38}  n={r['n']:>4}  "
          f"WR={r['wr']:>5.1f}%  PF={r['pf']:>5.3f}  "
          f"Net=Rs{r['net']:>10,.0f}  {gate}")


# ── All new filter names ───────────────────────────────────────────────────────
ALL_FILTERS = ["fvg", "sweep", "htf_ema", "atr", "bar_quality", "ema_slope"]


# ── Signal generators ──────────────────────────────────────────────────────────

def _baseline_sigs(df_1m, df_daily, df_vix):
    """Original ORB signals (no new filters)."""
    p = dict(ORB_PARAMS)
    sigs = generate_orb_signals(df_1m, df_daily, df_vix, p)
    # Align to enhanced format
    out          = sigs.rename(columns={"entry_price": "entry_price"})
    out["vix_val"] = sigs["vix_val"]
    return out


def _enhanced_sigs(df_1m, df_daily, df_vix, active_filters, extra_params=None):
    """Enhanced signals with specified subset of new filters active."""
    p = {**ENHANCED_PARAMS, **(extra_params or {})}
    # Override individual flags
    for f in ALL_FILTERS:
        p[f"use_{f}_filter"] = (f in active_filters)
        p[f"use_{f}"]        = (f in active_filters)
    return generate_enhanced_signals(df_1m, df_daily, df_vix, p, active_filters=set(active_filters))


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    print("=" * 70)
    print("  ENHANCED ORB — Filter Attribution & Combination Search")
    print(f"  Capital Rs{CAPITAL:,} | Risk 1% = Rs{CAPITAL*0.01:,.0f}/trade")
    print(f"  IS: 2015–{IS_END[:4]}  |  OOS: 2019–2024")
    print("=" * 70)

    # ── Load data ──────────────────────────────────────────────────────────────
    print("\n[1/5] Loading data ...")
    daily    = load_all()
    df_daily = daily["nifty"]
    df_vix   = daily["indiavix"]
    df_1m    = load_nifty1m_historical()
    if df_1m.empty:
        print("ERROR: No 1m data found"); sys.exit(1)
    df_5m    = resample_from_1m(df_1m, "5m")
    print(f"  1m : {len(df_1m):,} bars  {df_1m.index[0].date()} to {df_1m.index[-1].date()}")
    print(f"  5m : {len(df_5m):,} bars")

    # ── Run on 5m (best baseline TF) ──────────────────────────────────────────
    print("\n[2/5] Baseline (original ORB, 5m) — full period ...")
    base_sigs_5m = _baseline_sigs(df_5m, df_daily, df_vix)
    base_trades  = _run_backtest(df_5m, df_daily, df_vix, base_sigs_5m, ORB_PARAMS)
    base_row     = _metrics_row(base_trades, "BASELINE (original ORB 5m)")
    print(f"  {base_row['n']} trades | WR {base_row['wr']:.1f}% | PF {base_row['pf']:.3f} | Net Rs{base_row['net']:,.0f}")

    # ── Individual filter contribution (full period) ───────────────────────────
    print("\n[3/5] Individual filter contribution (5m, full period) ...")
    print(f"\n  {'Filter':<38}  {'n':>5}  {'WR':>6}  {'PF':>6}  {'Net P&L':>12}  Gate")
    print("  " + "-" * 80)
    _print_row(base_row)

    individual_rows = []
    for f in ALL_FILTERS:
        sigs   = _enhanced_sigs(df_5m, df_daily, df_vix, {f})
        trades = _run_backtest(df_5m, df_daily, df_vix, sigs, ENHANCED_PARAMS)
        r      = _metrics_row(trades, f"+ {f}")
        _print_row(r, indent="  ")
        individual_rows.append(r)

    # ── Combination search (full period) ──────────────────────────────────────
    print("\n[4/5] Combination search (5m, full period) ...")
    print(f"\n  {'Filters Active':<42}  {'n':>5}  {'WR':>6}  {'PF':>6}  {'Net P&L':>12}  Gate")
    print("  " + "-" * 84)
    _print_row(base_row)

    combo_results = []
    for r in range(2, len(ALL_FILTERS) + 1):
        for combo in itertools.combinations(ALL_FILTERS, r):
            sigs   = _enhanced_sigs(df_5m, df_daily, df_vix, set(combo))
            trades = _run_backtest(df_5m, df_daily, df_vix, sigs, ENHANCED_PARAMS)
            r_dict = _metrics_row(trades, "+".join(combo))
            combo_results.append(r_dict)

    # Sort by PF descending
    combo_results.sort(key=lambda x: x["pf"], reverse=True)
    for r in combo_results[:20]:
        _print_row(r, indent="  ")

    # ── IS / OOS validation of best combos ────────────────────────────────────
    print("\n[5/5] IS / OOS validation — top combos that pass or come closest ...")
    top_combos = [r for r in combo_results if r["n"] >= 50][:8]

    print(f"\n  {'Combo':<30} {'Period':<6}  {'n':>4}  {'WR':>6}  {'PF':>6}  {'Net P&L':>12}  Gate")
    print("  " + "-" * 74)

    for r in top_combos:
        filters = set(r["label"].split("+"))
        for period, (s, e) in [("IS", ("2015-01-01", IS_END)),
                                ("OOS", (str(int(IS_END[:4])+1)+"-01-01", OOS_END))]:
            sigs   = _enhanced_sigs(df_5m, df_daily, df_vix, filters)
            trades = _run_backtest(df_5m, df_daily, df_vix, sigs, ENHANCED_PARAMS, start=s, end=e)
            row    = _metrics_row(trades, r["label"])
            gate   = "[PASS]" if row["pass"] else "      "
            print(f"  {r['label']:<30} {period:<6}  {row['n']:>4}  "
                  f"{row['wr']:>5.1f}%  {row['pf']:>5.3f}  "
                  f"Net=Rs{row['net']:>9,.0f}  {gate}")
        print()

    # ── Also test 1m for the best combo ───────────────────────────────────────
    if top_combos:
        best = top_combos[0]
        best_filters = set(best["label"].split("+"))
        print(f"\n  Best combo on 1m timeframe ({best['label']}):")
        sigs_1m = _enhanced_sigs(df_1m, df_daily, df_vix, best_filters)
        trades_1m = _run_backtest(df_1m, df_daily, df_vix, sigs_1m, ENHANCED_PARAMS)
        r1m = _metrics_row(trades_1m, f"1m — {best['label']}")
        _print_row(r1m, indent="  ")
        sigs_5m = _enhanced_sigs(df_5m, df_daily, df_vix, best_filters)
        trades_5m = _run_backtest(df_5m, df_daily, df_vix, sigs_5m, ENHANCED_PARAMS)
        r5m = _metrics_row(trades_5m, f"5m — {best['label']}")
        _print_row(r5m, indent="  ")

        # Save best combo trades
        if trades_5m:
            out_path = os.path.join(RESULTS, "orb_enhanced_best_trades.csv")
            pd.DataFrame(trades_5m).to_csv(out_path, index=False)
            print(f"\n  Saved: {out_path}")

    print("\n" + "=" * 70)
    print("  DONE — Gate: WR > 55% AND PF > 2.0")
    print("=" * 70)
