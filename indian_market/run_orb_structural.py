"""
Structural ORB Backtest — Structure + Math Filters + Trailing SL
=================================================================
Baseline  : fvg + sweep + bar_quality + ema_slope  (Enhanced combo, fixed SL)
Step 1    : Same baseline with TRAILING SL (shows what trailing alone adds)
Step 2    : Add new filters one-by-one ON TOP of baseline + trailing SL
Step 3    : Combination search of new filters
Step 4    : IS / OOS validation of best combo
"""
import sys, os, itertools
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np

from data.download_data import load_all
from data.download_intraday import load_nifty1m_historical, resample_from_1m
from strategies.orb_structural_strategy import STRUCTURAL_PARAMS, generate_structural_signals
from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics

CAPITAL   = 1_000_000
RESULTS   = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS, exist_ok=True)

IS_END    = "2018-12-31"
OOS_START = "2019-01-01"
OOS_END   = "2024-03-27"

# Proven baseline filters from Enhanced ORB scan
BASELINE_FILTERS = {"fvg", "sweep", "bar_quality", "ema_slope"}

# New structural / math filters to test on top of baseline
NEW_FILTERS = ["body_strength", "vix_percentile", "momentum_candle", "atr"]


# ── Backtest engine with trailing SL ──────────────────────────────────────────

def _run_backtest(df_intraday, df_daily, df_vix, sigs_df, params,
                  start=None, end=None, use_trail=True):
    p = {**STRUCTURAL_PARAMS, **params}

    if start:
        sigs_df = sigs_df[sigs_df.index >= pd.Timestamp(start)]
    if end:
        sigs_df = sigs_df[sigs_df.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    if sigs_df.empty:
        return []

    df = df_intraday.loc[sigs_df.index].copy()
    df["signal"]          = sigs_df["signal"]
    df["vix_val"]         = sigs_df["vix_val"]
    df["entry_bar_close"] = sigs_df["entry_price"]

    trades      = []
    active_date = None
    exit_h, exit_m = map(int, p["mandatory_exit"].split(":"))

    trail_pct      = p.get("trail_pct", 0.20)
    trail_activate = p.get("trail_activate_pct", 0.10)

    i = 0
    while i < len(df):
        row      = df.iloc[i]
        bar_date = df.index[i].date()

        if bar_date == active_date:
            i += 1; continue

        if row["signal"] not in ("call", "put") or pd.isna(row["vix_val"]):
            i += 1; continue

        opt_type   = row["signal"]
        vix        = row["vix_val"]
        entry_spot = row["entry_bar_close"]
        K          = atm_strike(entry_spot, step=p["strike_step"])
        dte        = p["entry_dte"]
        entry_prem = bs_price(entry_spot, K, dte / 365, p["rfr"], vix / 100, opt_type)

        if entry_prem < 1.0:
            i += 1; continue

        max_budget = p["risk_per_trade"] / p["sl_pct"]
        lots       = max(1, min(int(max_budget / (entry_prem * p["lot_size"])), p["max_lots"]))
        qty        = lots * p["lot_size"]

        initial_sl  = entry_prem * (1 - p["sl_pct"])
        tp_price    = entry_prem * (1 + p["tp_pct"])
        act_thresh  = entry_prem * (1 + trail_activate)

        peak_prem     = entry_prem
        trail_active  = False
        active_date   = bar_date
        exit_prem     = None
        exit_type_str = None

        i += 1
        while i < len(df):
            nrow = df.iloc[i]
            nts  = df.index[i]

            if nts.date() != bar_date:
                break

            t = nts.time()
            if t.hour > exit_h or (t.hour == exit_h and t.minute >= exit_m):
                cp = bs_price(nrow["Close"], K, dte / 365, p["rfr"], vix / 100, opt_type)
                exit_prem, exit_type_str = cp, "3:15"
                i += 1; break

            # Option price range for this bar (H/L proxy)
            if opt_type == "call":
                opt_h = bs_price(nrow["High"], K, dte / 365, p["rfr"], vix / 100, "call")
                opt_l = bs_price(nrow["Low"],  K, dte / 365, p["rfr"], vix / 100, "call")
            else:
                opt_h = bs_price(nrow["Low"],  K, dte / 365, p["rfr"], vix / 100, "put")
                opt_l = bs_price(nrow["High"], K, dte / 365, p["rfr"], vix / 100, "put")

            # Update peak
            if opt_h > peak_prem:
                peak_prem = opt_h

            # Trailing SL activation
            if use_trail and peak_prem >= act_thresh:
                trail_active = True

            # Effective stop
            if trail_active:
                effective_sl = max(initial_sl, peak_prem * (1 - trail_pct))
            else:
                effective_sl = initial_sl

            # Safety-net TP
            if opt_h >= tp_price:
                exit_prem, exit_type_str = tp_price, "TP"
                i += 1; break

            # Stop hit
            if opt_l <= effective_sl:
                exit_prem = effective_sl
                exit_type_str = "Trail SL" if trail_active and effective_sl > initial_sl else "SL"
                i += 1; break

            i += 1

        if exit_type_str is None:
            last = df[df.index.date == bar_date].iloc[-1]
            exit_prem     = bs_price(last["Close"], K, dte / 365, p["rfr"], vix / 100, opt_type)
            exit_type_str = "3:15"

        sell      = max(exit_prem, 0.05)
        gross     = (sell - entry_prem) * qty
        cost      = round_trip_cost(entry_prem, sell, qty)

        trades.append({
            "entry_date" : str(bar_date),
            "exit_date"  : str(bar_date),
            "direction"  : opt_type,
            "entry_prem" : round(entry_prem, 2),
            "exit_prem"  : round(sell, 2),
            "peak_prem"  : round(peak_prem, 2),
            "lots"       : lots,
            "qty"        : qty,
            "exit_type"  : exit_type_str,
            "pnl_net"    : round(gross - cost, 2),
            "vix"        : round(vix, 2),
        })

    return trades


def _mrow(trades, label):
    m = compute_metrics(trades, CAPITAL)
    if "error" in m:
        return {"label": label, "n": 0, "wr": 0.0, "pf": 0.0, "net": 0, "pass": False, "eb": {}}
    return {
        "label": label,
        "n"    : m["n_trades"],
        "wr"   : m["win_rate_pct"],
        "pf"   : m["profit_factor"],
        "net"  : m["net_pnl"],
        "pass" : m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55,
        "eb"   : m.get("exit_breakdown", {}),
    }


def _prow(r, indent=""):
    gate = "[PASS]" if r["pass"] else ""
    eb   = r.get("eb", {})
    tsl  = eb.get("Trail SL", 0)
    sl   = eb.get("SL", 0)
    eod  = eb.get("3:15", 0)
    print(f"  {indent}{r['label']:<44} n={r['n']:>4}  WR={r['wr']:>5.1f}%  "
          f"PF={r['pf']:>5.3f}  Rs{r['net']:>10,.0f}  "
          f"TSL={tsl:>3} SL={sl:>3} EOD={eod:>3}  {gate}")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    print("=" * 80)
    print("  STRUCTURAL ORB — Structure + Math Filters + Trailing SL")
    print(f"  Capital Rs{CAPITAL:,} | Risk 1% = Rs{CAPITAL*0.01:,.0f}/trade")
    print(f"  Trailing SL: trail={STRUCTURAL_PARAMS['trail_pct']:.0%} below peak, "
          f"activates at +{STRUCTURAL_PARAMS['trail_activate_pct']:.0%}")
    print(f"  IS: 2015-{IS_END[:4]}  |  OOS: {OOS_START[:4]}-2024")
    print("=" * 80)

    # ── Load data ──────────────────────────────────────────────────────────────
    print("\n[1/5] Loading data ...")
    daily   = load_all()
    df_daily = daily["nifty"]
    df_vix   = daily["indiavix"]
    df_1m    = load_nifty1m_historical()
    if df_1m.empty:
        print("ERROR: no 1m data"); sys.exit(1)
    df_5m = resample_from_1m(df_1m, "5m")
    print(f"  5m: {len(df_5m):,} bars  {df_5m.index[0].date()} to {df_5m.index[-1].date()}")

    # ── Step 1: Baseline (best Enhanced combo) ────────────────────────────────
    print("\n[2/5] Baseline comparisons (fvg+sweep+bar_quality+ema_slope, 5m) ...")
    print(f"\n  {'Label':<44} {'n':>5}  {'WR':>6}  {'PF':>6}  {'Net P&L':>12}  "
          f"{'TSL':>4} {'SL':>4} {'EOD':>4}")
    print("  " + "-" * 95)

    base_sigs = generate_structural_signals(df_5m, df_daily, df_vix,
                                             active_filters=BASELINE_FILTERS)

    # Fixed SL (reference from Enhanced)
    base_fixed = _run_backtest(df_5m, df_daily, df_vix, base_sigs,
                                STRUCTURAL_PARAMS, use_trail=False)
    r_fixed = _mrow(base_fixed, "Baseline — fixed SL (reference)")
    _prow(r_fixed)

    # Trailing SL (same filters, new exit)
    base_trail = _run_backtest(df_5m, df_daily, df_vix, base_sigs,
                                STRUCTURAL_PARAMS, use_trail=True)
    r_trail = _mrow(base_trail, "Baseline — trailing SL")
    _prow(r_trail)

    # ── Step 2: Individual new filter attribution (+ trailing SL) ────────────
    print("\n[3/5] New filter attribution on top of baseline + trailing SL ...")
    print(f"\n  {'Filter':<44} {'n':>5}  {'WR':>6}  {'PF':>6}  {'Net P&L':>12}  "
          f"{'TSL':>4} {'SL':>4} {'EOD':>4}")
    print("  " + "-" * 95)
    _prow(r_trail)

    individual = []
    for f in NEW_FILTERS:
        filters = BASELINE_FILTERS | {f}
        sigs    = generate_structural_signals(df_5m, df_daily, df_vix,
                                               active_filters=filters)
        trades  = _run_backtest(df_5m, df_daily, df_vix, sigs, STRUCTURAL_PARAMS)
        r       = _mrow(trades, f"  + {f}")
        _prow(r, indent="  ")
        individual.append((f, r))

    # ── Step 3: Combination search of new filters ─────────────────────────────
    print("\n[4/5] Combination search of new filters (+ trailing SL, 5m) ...")
    print(f"\n  {'Filters (on top of baseline)':<44} {'n':>5}  {'WR':>6}  {'PF':>6}  "
          f"{'Net P&L':>12}  {'TSL':>4} {'SL':>4} {'EOD':>4}")
    print("  " + "-" * 95)
    _prow(r_trail)

    combo_results = []
    for r in range(2, len(NEW_FILTERS) + 1):
        for combo in itertools.combinations(NEW_FILTERS, r):
            filters = BASELINE_FILTERS | set(combo)
            sigs    = generate_structural_signals(df_5m, df_daily, df_vix,
                                                   active_filters=filters)
            trades  = _run_backtest(df_5m, df_daily, df_vix, sigs, STRUCTURAL_PARAMS)
            label   = "+".join(combo)
            rc      = _mrow(trades, label)
            combo_results.append(rc)

    combo_results.sort(key=lambda x: x["pf"], reverse=True)
    for rc in combo_results[:15]:
        _prow(rc, indent="  ")

    # ── Step 4: IS / OOS validation ───────────────────────────────────────────
    print("\n[5/5] IS / OOS validation — top combos ...")

    # Include baseline + trail and all combos
    all_results = [r_trail] + combo_results
    top = [r for r in all_results if r["n"] >= 50][:6]

    print(f"\n  {'Combo':<44} {'Period':<6} {'n':>4}  {'WR':>6}  {'PF':>6}  {'Net':>12}  Gate")
    print("  " + "-" * 88)

    for r in top:
        label = r["label"].strip()
        # Reconstruct active filter set
        new_parts = [p for p in label.replace("Baseline — trailing SL", "").split("+") if p.strip()]
        if "Baseline" in label:
            filters = BASELINE_FILTERS
        else:
            extra   = set(label.split("+"))
            filters = BASELINE_FILTERS | extra

        for period, (s, e) in [("IS", ("2015-01-01", IS_END)),
                                ("OOS", (OOS_START, OOS_END))]:
            sigs   = generate_structural_signals(df_5m, df_daily, df_vix,
                                                  active_filters=filters)
            trades = _run_backtest(df_5m, df_daily, df_vix, sigs,
                                    STRUCTURAL_PARAMS, start=s, end=e)
            rp     = _mrow(trades, label)
            gate   = "[PASS]" if rp["pass"] else ""
            print(f"  {label:<44} {period:<6} {rp['n']:>4}  "
                  f"{rp['wr']:>5.1f}%  {rp['pf']:>5.3f}  "
                  f"Rs{rp['net']:>10,.0f}  {gate}")
        print()

    # ── Save best combo trades ─────────────────────────────────────────────────
    if combo_results:
        best_label   = combo_results[0]["label"]
        best_filters = BASELINE_FILTERS | set(best_label.split("+"))
        best_sigs    = generate_structural_signals(df_5m, df_daily, df_vix,
                                                    active_filters=best_filters)
        best_trades  = _run_backtest(df_5m, df_daily, df_vix, best_sigs, STRUCTURAL_PARAMS)
        if best_trades:
            out_path = os.path.join(RESULTS, "orb_structural_best_trades.csv")
            pd.DataFrame(best_trades).to_csv(out_path, index=False)
            print(f"  Saved: {out_path}")

    print("\n" + "=" * 80)
    print("  DONE — Gate: WR > 55% AND PF > 2.0")
    print("=" * 80)
