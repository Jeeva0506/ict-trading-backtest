"""
TP/SL ratio scan — find the R:R that pushes PF > 2.0 with best filter combo.
The filter combo improves WR to ~48% but PF is stuck at 1.29.
Root cause: TP:SL ratio of 2:1 (50%/25%) is just below the 2.26 needed at 47% WR.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from data.download_data import load_all
from data.download_intraday import load_nifty1m_historical, resample_from_1m
from strategies.orb_enhanced_strategy import ENHANCED_PARAMS, generate_enhanced_signals
from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics

CAPITAL = 1_000_000
BEST_FILTERS = {"fvg", "sweep", "bar_quality", "ema_slope"}


def run_with_tp_sl(df_intraday, df_daily, df_vix, tp_pct, sl_pct, start=None, end=None):
    p = {**ENHANCED_PARAMS, "tp_pct": tp_pct, "sl_pct": sl_pct}
    sigs = generate_enhanced_signals(df_intraday, df_daily, df_vix, p,
                                     active_filters=BEST_FILTERS)
    if start:
        sigs = sigs[sigs.index >= pd.Timestamp(start)]
    if end:
        sigs = sigs[sigs.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]

    df = df_intraday.loc[sigs.index].copy()
    df["signal"]  = sigs["signal"]
    df["vix_val"] = sigs["vix_val"]
    df["entry_bar_close"] = sigs["entry_price"]

    trades, active_date = [], None
    exit_h, exit_m = 15, 15
    i = 0
    while i < len(df):
        row = df.iloc[i]
        bar_date = df.index[i].date()
        if bar_date == active_date:
            i += 1; continue
        if row["signal"] not in ("call", "put") or pd.isna(row["vix_val"]):
            i += 1; continue

        opt_type   = row["signal"]
        vix        = row["vix_val"]
        entry_spot = row["entry_bar_close"]
        K          = atm_strike(entry_spot)
        dte        = p["entry_dte"]
        entry_prem = bs_price(entry_spot, K, dte/365, p["rfr"], vix/100, opt_type)
        if entry_prem < 1.0:
            i += 1; continue

        max_budget = p["risk_per_trade"] / sl_pct
        lots  = max(1, min(int(max_budget / (entry_prem * p["lot_size"])), p["max_lots"]))
        qty   = lots * p["lot_size"]
        tp_px = entry_prem * (1 + tp_pct)
        sl_px = entry_prem * (1 - sl_pct)

        active_date = bar_date
        exit_prem = exit_str = None
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
            ol = bs_price(nrow["Low"] if opt_type=="call" else nrow["High"],
                          K, dte/365, p["rfr"], vix/100, opt_type)
            if oh >= tp_px:
                exit_prem, exit_str = tp_px, "TP"; i += 1; break
            elif ol <= sl_px:
                exit_prem, exit_str = sl_px, "SL"; i += 1; break
            i += 1

        if exit_str is None:
            last = df[df.index.date == bar_date].iloc[-1]
            exit_prem = bs_price(last["Close"], K, dte/365, p["rfr"], vix/100, opt_type)
            exit_str  = "3:15"

        sell = max(exit_prem, 0.05)
        gross = (sell - entry_prem) * qty
        cost  = round_trip_cost(entry_prem, sell, qty)
        trades.append({"entry_date": str(bar_date), "exit_date": str(bar_date),
                       "direction": opt_type, "pnl_net": round(gross - cost, 2),
                       "exit_type": exit_str})

    m = compute_metrics(trades, CAPITAL)
    return m


if __name__ == "__main__":
    print("Loading data ...")
    daily = load_all()
    df_5m = resample_from_1m(load_nifty1m_historical(), "5m")

    print("\nTP/SL Ratio scan — best filter combo (fvg+sweep+bar_quality+ema_slope) on 5m")
    print(f"\n  {'TP%':>5} {'SL%':>5} {'R:R':>5}  {'n':>5}  {'WR%':>6}  {'PF':>6}  {'Net P&L':>12}  {'TP hits':>8}  {'SL hits':>8}  Gate")
    print("  " + "-" * 90)

    combos = [
        (0.50, 0.25),   # current baseline
        (0.60, 0.25),
        (0.75, 0.25),
        (1.00, 0.25),
        (1.50, 0.25),
        (0.75, 0.20),
        (1.00, 0.20),
        (1.00, 0.15),
        (1.50, 0.20),
        (2.00, 0.25),
    ]

    for tp, sl in combos:
        m = run_with_tp_sl(df_5m, daily["nifty"], daily["indiavix"], tp, sl)
        if "error" in m:
            print(f"  TP={tp:.0%} SL={sl:.0%}: no trades"); continue
        eb = m.get("exit_breakdown", {})
        tp_h = eb.get("TP", 0); sl_h = eb.get("SL", 0)
        rr   = tp / sl
        gate = "[PASS]" if m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55 else ""
        print(f"  {tp:>4.0%} {sl:>4.0%} {rr:>5.1f}  {m['n_trades']:>5}  "
              f"{m['win_rate_pct']:>5.1f}%  {m['profit_factor']:>6.3f}  "
              f"Rs{m['net_pnl']:>10,.0f}  {tp_h:>8}  {sl_h:>8}  {gate}")

    # IS/OOS for the best TP/SL that passes
    print("\nIS/OOS validation for TP=100% SL=25% (best R:R candidate) ...")
    for period, (s, e) in [("IS  2015-2018", ("2015-01-01","2018-12-31")),
                            ("OOS 2019-2024", ("2019-01-01","2024-03-27"))]:
        m = run_with_tp_sl(df_5m, daily["nifty"], daily["indiavix"],
                           tp_pct=1.00, sl_pct=0.25, start=s, end=e)
        if "error" in m: print(f"  {period}: no trades"); continue
        gate = "[PASS]" if m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55 else ""
        print(f"  {period}:  n={m['n_trades']}  WR={m['win_rate_pct']:.1f}%  "
              f"PF={m['profit_factor']:.3f}  Net=Rs{m['net_pnl']:,.0f}  {gate}")
