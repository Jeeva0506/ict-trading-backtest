"""
Intraday CPR Breakout Options Backtest
=======================================
Timeframes: 5m, 15m, 1h
Entry      : CPR breakout bar close (first qualifying bar within 2hr window)
Exit       : SL / TP / mandatory 3:15 PM close (intraday only)
Capital    : Rs10,00,000 | Risk: 1% = Rs10,000/trade
Costs      : Zerodha (brokerage + STT + exchange + GST + stamp)

Data availability:
  5m  : last ~60 days (from yfinance)
  15m : last ~60 days (from yfinance)
  1h  : last ~730 days (from yfinance)
  Daily: 12+ years (for CPR calculation + long backtests)

For 3yr / 10yr intraday backtest, use:
  Zerodha Kite API / AngelOne SmartAPI / Upstox API historical endpoint
  Place files as data/nifty_5m.csv with columns: Open,High,Low,Close,Volume

Run: python run_intraday_backtest.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
import json

from data.download_data import load_all
from data.download_intraday import load_intraday, download_intraday
from strategies.intraday_cpr_strategy import (
    generate_intraday_signals, INTRADAY_CPR_PARAMS, compute_daily_pivots
)
from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics, print_report

CAPITAL = 1_000_000
RESULTS = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS, exist_ok=True)


# ── Intraday engine (self-contained) ──────────────────────────────────────────
def run_intraday(df_intraday, df_daily, df_vix, params, start=None, end=None):
    p = {**INTRADAY_CPR_PARAMS, **(params or {})}

    sigs = generate_intraday_signals(df_intraday, df_daily, df_vix, p)

    if start:
        sigs = sigs[sigs.index >= pd.Timestamp(start, tz=sigs.index.tz)]
    if end:
        sigs = sigs[sigs.index <= pd.Timestamp(end, tz=sigs.index.tz) + pd.Timedelta(days=1)]

    # Merge signals with intraday OHLCV
    df = df_intraday.loc[sigs.index].copy()
    df["signal"]    = sigs["signal"]
    df["tc"]        = sigs["tc"]
    df["bc"]        = sigs["bc"]
    df["vix_val"]   = sigs["vix_val"]
    df["entry_bar_close"] = sigs["entry_price"]

    trades = []
    active_date = None  # allow one entry per day

    exit_time_h, exit_time_m = map(int, p["mandatory_exit"].split(":"))

    i = 0
    while i < len(df):
        row  = df.iloc[i]
        date = df.index[i]
        bar_date = date.date()

        if bar_date == active_date:
            i += 1
            continue  # already traded today

        if row["signal"] not in ("call", "put") or pd.isna(row["vix_val"]):
            i += 1
            continue

        option_type = row["signal"]
        vix         = row["vix_val"]
        entry_spot  = row["entry_bar_close"]   # fill at breakout bar close
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

        active_date = bar_date
        exit_prem, exit_type = None, None

        # ── Scan remaining bars of same day ──────────────────────────────
        i += 1
        while i < len(df):
            nrow = df.iloc[i]
            ndate = df.index[i]
            if ndate.date() != bar_date:
                break  # new day → time-exit at prior day close

            # Check if mandatory exit time reached
            t = ndate.time()
            if t.hour > exit_time_h or (t.hour == exit_time_h and t.minute >= exit_time_m):
                close_prem = bs_price(nrow["Close"], K, dte/365, p["rfr"], vix/100, option_type)
                exit_prem, exit_type = close_prem, "3:15"
                i += 1
                break

            # Option intraday high/low
            if option_type == "call":
                opt_high = bs_price(nrow["High"], K, dte/365, p["rfr"], vix/100, "call")
                opt_low  = bs_price(nrow["Low"],  K, dte/365, p["rfr"], vix/100, "call")
            else:
                opt_high = bs_price(nrow["Low"],  K, dte/365, p["rfr"], vix/100, "put")
                opt_low  = bs_price(nrow["High"], K, dte/365, p["rfr"], vix/100, "put")

            if opt_high >= tp_price:
                exit_prem, exit_type = tp_price, "TP"
                i += 1
                break
            elif opt_low <= sl_price:
                exit_prem, exit_type = sl_price, "SL"
                i += 1
                break

            i += 1

        # If no exit found within day bars (e.g., last bar of day): exit at last close
        if exit_type is None:
            last_intraday = df[df.index.date == bar_date].iloc[-1]
            exit_prem = bs_price(last_intraday["Close"], K, dte/365, p["rfr"], vix/100, option_type)
            exit_type = "3:15"

        sell      = max(exit_prem, 0.05)
        gross_pnl = (sell - entry_prem) * qty
        cost      = round_trip_cost(entry_prem, sell, qty)
        net_pnl   = gross_pnl - cost

        trades.append({
            "entry_date" : str(bar_date),
            "exit_date"  : str(bar_date),
            "direction"  : option_type,
            "strike"     : K,
            "entry_spot" : round(entry_spot, 2),
            "entry_prem" : round(entry_prem, 2),
            "exit_prem"  : round(sell, 2),
            "lots"       : lots,
            "qty"        : qty,
            "days_held"  : 0,
            "exit_type"  : exit_type,
            "gross_pnl"  : round(gross_pnl, 2),
            "cost"       : round(cost, 2),
            "pnl_net"    : round(net_pnl, 2),
            "vix"        : round(vix, 2),
        })

    return trades


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*65)
    print("  Intraday CPR Breakout — Multi-Timeframe Options Backtest")
    print(f"  Capital Rs{CAPITAL:,} | Risk 1% = Rs{CAPITAL*0.01:,.0f}/trade")
    print("="*65)

    # Load daily data (for CPR + VIX)
    daily = load_all()
    df_daily = daily["nifty"]
    df_vix   = daily["indiavix"]

    results_summary = {}

    for tf in ["1m", "5m", "15m", "1h"]:
        print(f"\n{'='*50}")
        print(f"  Timeframe: {tf}")
        print(f"{'='*50}")

        try:
            df_intra = load_intraday(tf)
        except Exception:
            df_intra = download_intraday(tf)

        if df_intra.empty:
            print(f"  No {tf} data available — skipping")
            continue

        # Trades on all available intraday data
        start_dt = str(df_intra.index[0].date())
        end_dt   = str(df_intra.index[-1].date())
        print(f"  Data range: {start_dt} to {end_dt}")

        trades = run_intraday(df_intra, df_daily, df_vix,
                              INTRADAY_CPR_PARAMS, start_dt, end_dt)
        m = compute_metrics(trades, CAPITAL)

        if "error" in m:
            print(f"  No trades generated")
            continue

        print_report(m, label=f"CPR {tf} — After Zerodha Costs")

        # Signal frequency analysis
        n_days = (df_intra.index[-1].date() - df_intra.index[0].date()).days
        n_years = max(n_days / 365, 0.01)
        trades_per_year = m["n_trades"] / n_years
        print(f"  Trade frequency: {trades_per_year:.0f} trades/year (target 100)")

        # Save trades
        if trades:
            pd.DataFrame(trades).to_csv(
                os.path.join(RESULTS, f"intraday_{tf}_trades.csv"), index=False)
            print(f"  Saved results/intraday_{tf}_trades.csv")

        results_summary[tf] = {
            "n_trades"     : m["n_trades"],
            "win_rate_pct" : m["win_rate_pct"],
            "profit_factor": m["profit_factor"],
            "net_pnl"      : m["net_pnl"],
            "trades_per_yr": round(trades_per_year),
            "pass"         : m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55,
        }

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("  SUMMARY — Intraday CPR Options (after Zerodha costs)")
    print(f"{'='*65}")
    print(f"  {'TF':<6}  {'N':>4}  {'WR%':>6}  {'PF':>6}  {'Net P&L':>12}  {'Freq/yr':>8}  Gate")
    print(f"{'-'*65}")
    for tf, r in results_summary.items():
        gate = "PASS" if r["pass"] else "FAIL"
        print(f"  {tf:<6}  {r['n_trades']:>4}  {r['win_rate_pct']:>5.1f}%  "
              f"{r['profit_factor']:>6.3f}  Rs{r['net_pnl']:>9,.0f}  "
              f"{r['trades_per_yr']:>8}  [{gate}]")

    print(f"\n  Note: Backtest above uses {end_dt[:7]} intraday data (yfinance 60-day limit)")
    print("  For 3yr/10yr intraday backtest:")
    print("    - Use Zerodha Kite Connect /instruments/historical API")
    print("    - Or AngelOne SmartAPI getCandleData (free with account)")
    print("    - Place as data/nifty_5m_YYYY.csv and rerun")
    print(f"{'='*65}")
