"""
Trail SL parameter scan + fixed TP comparison
Best filters: baseline (fvg+sweep+bar_quality+ema_slope) + body_strength + vix_percentile + atr
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from strategies.orb_structural_strategy import STRUCTURAL_PARAMS, generate_structural_signals
from data.download_data import load_all
from data.download_intraday import load_nifty1m_historical, resample_from_1m
from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics

CAPITAL = 1_000_000
BEST_FILTERS = {"fvg", "sweep", "bar_quality", "ema_slope",
                "body_strength", "vix_percentile", "atr"}
IS_END    = "2018-12-31"
OOS_START = "2019-01-01"
OOS_END   = "2024-03-27"


def _backtest(df5m, df_daily, df_vix, sigs, params,
              tp_pct=5.0, sl_pct=0.25, trail_pct=0.20, trail_act=0.10,
              use_trail=True, start=None, end=None):
    p = {**STRUCTURAL_PARAMS, **params}
    if start:
        sigs = sigs[sigs.index >= pd.Timestamp(start)]
    if end:
        sigs = sigs[sigs.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    if sigs.empty:
        return []

    df = df5m.loc[sigs.index].copy()
    df["signal"]   = sigs["signal"]
    df["vix_val"]  = sigs["vix_val"]
    df["ep_close"] = sigs["entry_price"]

    trades, active_date = [], None
    exit_h, exit_m = 15, 15

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
        entry_spot = row["ep_close"]
        K          = atm_strike(entry_spot, step=p["strike_step"])
        dte        = p["entry_dte"]
        ep         = bs_price(entry_spot, K, dte/365, p["rfr"], vix/100, opt_type)
        if ep < 1.0:
            i += 1; continue

        max_b  = p["risk_per_trade"] / sl_pct
        lots   = max(1, min(int(max_b / (ep * p["lot_size"])), p["max_lots"]))
        qty    = lots * p["lot_size"]

        isl    = ep * (1 - sl_pct)
        tp_px  = ep * (1 + tp_pct)
        act_t  = ep * (1 + trail_act)
        peak   = ep
        tact   = False

        active_date   = bar_date
        exit_prem     = None
        exit_str      = None

        i += 1
        while i < len(df):
            nrow = df.iloc[i]; nts = df.index[i]
            if nts.date() != bar_date: break
            t = nts.time()
            if t.hour > exit_h or (t.hour == exit_h and t.minute >= exit_m):
                exit_prem = bs_price(nrow["Close"], K, dte/365, p["rfr"], vix/100, opt_type)
                exit_str = "3:15"; i += 1; break

            oh = bs_price(nrow["High"] if opt_type=="call" else nrow["Low"],
                          K, dte/365, p["rfr"], vix/100, opt_type)
            ol = bs_price(nrow["Low"]  if opt_type=="call" else nrow["High"],
                          K, dte/365, p["rfr"], vix/100, opt_type)

            if oh > peak: peak = oh
            if use_trail and peak >= act_t: tact = True

            eff_sl = max(isl, peak * (1 - trail_pct)) if tact else isl

            if oh >= tp_px:
                exit_prem, exit_str = tp_px, "TP"; i += 1; break
            if ol <= eff_sl:
                exit_prem = eff_sl
                exit_str  = "Trail SL" if tact and eff_sl > isl else "SL"
                i += 1; break
            i += 1

        if exit_str is None:
            last = df[df.index.date == bar_date].iloc[-1]
            exit_prem = bs_price(last["Close"], K, dte/365, p["rfr"], vix/100, opt_type)
            exit_str  = "3:15"

        sell  = max(exit_prem, 0.05)
        gross = (sell - ep) * qty
        cost  = round_trip_cost(ep, sell, qty)
        trades.append({"entry_date": str(bar_date), "exit_date": str(bar_date),
                       "direction": opt_type, "pnl_net": round(gross - cost, 2),
                       "exit_type": exit_str, "peak_prem": round(peak, 2),
                       "entry_prem": round(ep, 2), "exit_prem": round(sell, 2)})
    return trades


def _m(trades, lbl):
    if not trades: return f"  {lbl}: no trades"
    m  = compute_metrics(trades, CAPITAL)
    if "error" in m: return f"  {lbl}: error"
    eb = m.get("exit_breakdown", {})
    return (f"  {lbl:<52}  n={m['n_trades']:>4}  WR={m['win_rate_pct']:>5.1f}%  "
            f"PF={m['profit_factor']:>5.3f}  Rs{m['net_pnl']:>9,.0f}  "
            f"TSL={eb.get('Trail SL',0):>3} SL={eb.get('SL',0):>3} EOD={eb.get('3:15',0):>3}")


if __name__ == "__main__":
    print("Loading data ...")
    daily  = load_all()
    df_1m  = load_nifty1m_historical()
    df_5m  = resample_from_1m(df_1m, "5m")
    df_d   = daily["nifty"]
    df_vix = daily["indiavix"]

    print("\nGenerating signals for best filter set ...")
    sigs = generate_structural_signals(df_5m, df_d, df_vix, active_filters=BEST_FILTERS)

    # ── Section 1: Trail SL parameter scan ────────────────────────────────────
    print("\n" + "="*90)
    print("  TRAIL SL PARAMETER SCAN  (best filters: baseline+body_str+vix_pct+atr)")
    print("="*90)
    print(f"  {'Mode':<52}  {'n':>4}  {'WR':>6}  {'PF':>6}  {'Net P&L':>10}  TSL  SL  EOD")
    print("  " + "-"*88)

    # No trail (fixed SL, wide TP)
    t = _backtest(df_5m, df_d, df_vix, sigs, {}, tp_pct=5.0, sl_pct=0.25, use_trail=False)
    print(_m(t, "Fixed SL=25%  TP=500% (safety net, no trail)"))

    # Fixed TP scans (no trail)
    for tp in [0.50, 0.75, 1.00, 1.50]:
        t = _backtest(df_5m, df_d, df_vix, sigs, {}, tp_pct=tp, sl_pct=0.25, use_trail=False)
        print(_m(t, f"Fixed SL=25%  TP={tp:.0%}"))

    print()

    # Trail scans: vary activation threshold
    for act in [0.10, 0.20, 0.30, 0.40, 0.50]:
        t = _backtest(df_5m, df_d, df_vix, sigs, {}, tp_pct=5.0, sl_pct=0.25,
                      trail_pct=0.20, trail_act=act, use_trail=True)
        print(_m(t, f"Trail 20% below peak, activates at +{act:.0%}"))

    print()

    # Trail scans: vary trail %
    for tr in [0.10, 0.15, 0.20, 0.25, 0.30]:
        t = _backtest(df_5m, df_d, df_vix, sigs, {}, tp_pct=5.0, sl_pct=0.25,
                      trail_pct=tr, trail_act=0.30, use_trail=True)
        print(_m(t, f"Trail {tr:.0%} below peak, activates at +30%"))

    print()

    # Hybrid: fixed TP + trail
    for tp, tr, act in [(1.00, 0.20, 0.30), (1.50, 0.20, 0.30), (1.00, 0.15, 0.25)]:
        t = _backtest(df_5m, df_d, df_vix, sigs, {}, tp_pct=tp, sl_pct=0.25,
                      trail_pct=tr, trail_act=act, use_trail=True)
        print(_m(t, f"TP={tp:.0%}  Trail {tr:.0%} below peak, activates at +{act:.0%}"))

    # ── Section 2: IS / OOS for best config found ─────────────────────────────
    print("\n" + "="*90)
    print("  IS / OOS — best config candidates")
    print("="*90)

    candidates = [
        ("Fixed SL=25% TP=75%",      dict(tp_pct=0.75, sl_pct=0.25, use_trail=False)),
        ("Fixed SL=25% TP=100%",     dict(tp_pct=1.00, sl_pct=0.25, use_trail=False)),
        ("Trail 20% act+30% TP=500%",dict(tp_pct=5.0,  sl_pct=0.25, trail_pct=0.20, trail_act=0.30, use_trail=True)),
        ("Trail 15% act+30% TP=150%",dict(tp_pct=1.50, sl_pct=0.25, trail_pct=0.15, trail_act=0.25, use_trail=True)),
    ]

    for lbl, kw in candidates:
        print(f"\n  {lbl}")
        for period, (s, e) in [("IS  2015-2018", ("2015-01-01", IS_END)),
                                ("OOS 2019-2024", (OOS_START, OOS_END))]:
            t  = _backtest(df_5m, df_d, df_vix, sigs, {}, start=s, end=e, **kw)
            gate = "[PASS]" if t and compute_metrics(t, CAPITAL).get("profit_factor",0) > 2.0 \
                              and compute_metrics(t, CAPITAL).get("win_rate_pct",0) > 55 else ""
            print(_m(t, f"    {period}") + f"  {gate}")
