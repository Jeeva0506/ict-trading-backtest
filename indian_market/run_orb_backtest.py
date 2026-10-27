"""
Opening Range Breakout (ORB) Options Backtest
==============================================
Entry  : First bar to close above ORB_H (call) or below ORB_L (put)
         within the entry window (9:30-11:30)
Exit   : TP / SL / mandatory 3:15 PM close
Capital: Rs10,00,000 | Risk 1% = Rs10,000/trade
Data   : Historical 1m Nifty (Jan 2015 - Mar 2024) via nifty50_candlestick_data.csv

Run: python run_orb_backtest.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np

from data.download_data import load_all
from data.download_intraday import load_intraday, load_nifty1m_historical, resample_from_1m
from strategies.orb_strategy import generate_orb_signals, ORB_PARAMS
from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics, print_report

CAPITAL = 1_000_000
RESULTS = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS, exist_ok=True)


def run_orb(df_intraday, df_daily, df_vix, params, start=None, end=None):
    p = {**ORB_PARAMS, **(params or {})}

    sigs = generate_orb_signals(df_intraday, df_daily, df_vix, p)

    if start:
        ts = pd.Timestamp(start)
        sigs = sigs[sigs.index >= ts]
    if end:
        ts = pd.Timestamp(end) + pd.Timedelta(days=1)
        sigs = sigs[sigs.index <= ts]

    df = df_intraday.loc[sigs.index].copy()
    df["signal"]          = sigs["signal"]
    df["vix_val"]         = sigs["vix_val"]
    df["entry_bar_close"] = sigs["entry_price"]

    trades = []
    active_date = None

    exit_time_h, exit_time_m = map(int, p["mandatory_exit"].split(":"))

    i = 0
    while i < len(df):
        row      = df.iloc[i]
        date     = df.index[i]
        bar_date = date.date()

        if bar_date == active_date:
            i += 1
            continue

        if row["signal"] not in ("call", "put") or pd.isna(row["vix_val"]):
            i += 1
            continue

        option_type = row["signal"]
        vix         = row["vix_val"]
        entry_spot  = row["entry_bar_close"]
        K           = atm_strike(entry_spot, step=p["strike_step"])
        dte         = p["entry_dte"]
        entry_prem  = bs_price(entry_spot, K, dte/365, p["rfr"], vix/100, option_type)

        if entry_prem < 1.0:
            i += 1
            continue

        max_budget = p["risk_per_trade"] / p["sl_pct"]
        lots       = max(1, min(int(max_budget / (entry_prem * p["lot_size"])), p["max_lots"]))
        qty        = lots * p["lot_size"]
        tp_price   = entry_prem * (1 + p["tp_pct"])
        sl_price   = entry_prem * (1 - p["sl_pct"])

        active_date    = bar_date
        exit_prem      = None
        exit_type_str  = None

        i += 1
        while i < len(df):
            nrow  = df.iloc[i]
            ndate = df.index[i]

            if ndate.date() != bar_date:
                break

            t = ndate.time()
            if t.hour > exit_time_h or (t.hour == exit_time_h and t.minute >= exit_time_m):
                cp = bs_price(nrow["Close"], K, dte/365, p["rfr"], vix/100, option_type)
                exit_prem, exit_type_str = cp, "3:15"
                i += 1
                break

            if option_type == "call":
                opt_high = bs_price(nrow["High"], K, dte/365, p["rfr"], vix/100, "call")
                opt_low  = bs_price(nrow["Low"],  K, dte/365, p["rfr"], vix/100, "call")
            else:
                opt_high = bs_price(nrow["Low"],  K, dte/365, p["rfr"], vix/100, "put")
                opt_low  = bs_price(nrow["High"], K, dte/365, p["rfr"], vix/100, "put")

            if opt_high >= tp_price:
                exit_prem, exit_type_str = tp_price, "TP"
                i += 1
                break
            elif opt_low <= sl_price:
                exit_prem, exit_type_str = sl_price, "SL"
                i += 1
                break

            i += 1

        if exit_type_str is None:
            last = df[df.index.date == bar_date].iloc[-1]
            exit_prem     = bs_price(last["Close"], K, dte/365, p["rfr"], vix/100, option_type)
            exit_type_str = "3:15"

        sell      = max(exit_prem, 0.05)
        gross_pnl = (sell - entry_prem) * qty
        cost      = round_trip_cost(entry_prem, sell, qty)
        net_pnl   = gross_pnl - cost

        trades.append({
            "entry_date": str(bar_date),
            "exit_date" : str(bar_date),
            "direction" : option_type,
            "strike"    : K,
            "entry_spot": round(entry_spot, 2),
            "entry_prem": round(entry_prem, 2),
            "exit_prem" : round(sell, 2),
            "lots"      : lots,
            "qty"       : qty,
            "days_held" : 0,
            "exit_type" : exit_type_str,
            "gross_pnl" : round(gross_pnl, 2),
            "cost"      : round(cost, 2),
            "pnl_net"   : round(net_pnl, 2),
            "vix"       : round(vix, 2),
        })

    return trades


if __name__ == "__main__":
    print("=" * 65)
    print("  ORB Options Backtest — Nifty Intraday")
    print(f"  Capital Rs{CAPITAL:,} | Risk 1% = Rs{CAPITAL*0.01:,.0f}/trade")
    print("=" * 65)

    daily = load_all()
    df_daily = daily["nifty"]
    df_vix   = daily["indiavix"]

    # Load historical 1m data
    print("\nLoading historical 1m data ...")
    df_1m = load_nifty1m_historical()
    if df_1m.empty:
        print("ERROR: Historical 1m data not found")
        sys.exit(1)

    results_summary = {}

    for tf, df_intra in [("1m", df_1m),
                          ("5m", resample_from_1m(df_1m, "5m")),
                          ("15m", resample_from_1m(df_1m, "15m"))]:
        print(f"\n{'='*55}")
        print(f"  Timeframe: {tf} | {len(df_intra):,} bars "
              f"({df_intra.index[0].date()} to {df_intra.index[-1].date()})")
        print(f"{'='*55}")

        start_dt = str(df_intra.index[0].date())
        end_dt   = str(df_intra.index[-1].date())

        trades = run_orb(df_intra, df_daily, df_vix, ORB_PARAMS, start_dt, end_dt)
        m      = compute_metrics(trades, CAPITAL)

        if "error" in m:
            print(f"  No trades generated")
            continue

        print_report(m, label=f"ORB {tf} — After Zerodha Costs")

        n_days = (df_intra.index[-1].date() - df_intra.index[0].date()).days
        trades_per_year = m["n_trades"] / max(n_days / 365, 0.01)
        print(f"  Trade frequency: {trades_per_year:.0f} trades/year (target 100)")

        if trades:
            out = os.path.join(RESULTS, f"orb_{tf}_trades.csv")
            pd.DataFrame(trades).to_csv(out, index=False)
            print(f"  Saved {out}")

        results_summary[tf] = {
            "n_trades"     : m["n_trades"],
            "win_rate_pct" : m["win_rate_pct"],
            "profit_factor": m["profit_factor"],
            "net_pnl"      : m["net_pnl"],
            "trades_per_yr": round(trades_per_year),
            "pass"         : m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55,
        }

    print(f"\n{'='*65}")
    print("  SUMMARY — ORB Options (after Zerodha costs)")
    print(f"{'='*65}")
    print(f"  {'TF':<6}  {'N':>4}  {'WR%':>6}  {'PF':>6}  {'Net P&L':>12}  {'Freq/yr':>8}  Gate")
    print(f"{'-'*65}")
    for tf, r in results_summary.items():
        gate = "PASS" if r["pass"] else "FAIL"
        print(f"  {tf:<6}  {r['n_trades']:>4}  {r['win_rate_pct']:>5.1f}%  "
              f"{r['profit_factor']:>6.3f}  Rs{r['net_pnl']:>9,.0f}  "
              f"{r['trades_per_yr']:>8}  [{gate}]")
    print(f"{'='*65}")
