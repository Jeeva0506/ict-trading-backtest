"""
FRA40 & UK100 HistData Backtest (Default Rules)
================================================
Runs ORB Clean Breakout strategy on FRA40 (CAC 40) and UK100 (FTSE 100)
using each index's own default config from LIVE_INDEX_CONFIG.

Data sources:
  - FRA40: HistData frxeur (Nov 2010 - Feb 2026, 3.2M bars, continuous)
  - UK100: HistData ukxgbp (Nov 2010 - Feb 2026, 4.0M bars, continuous)

Strategy params (per-index defaults from backtest_live_setup.py):
  FRA40: sl_buffer=1.0, entry_hours=2, disp_mult=1.5, body_min=0.60,
         close_strength=0.30, breakout_margin=0.10, max_sl=30.0
  UK100: sl_buffer=1.0, entry_hours=2, disp_mult=1.7, body_min=0.65,
         close_strength=0.30, breakout_margin=0.10, max_sl=25.0

Usage:
  python run_fra40_uk100_backtest.py
"""

import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict
from datetime import date

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, ADAPTIVE_RISK,
    ACCOUNT_RULES, FUNDEDNEXT_RULES, MAX_OPEN_POSITIONS, MAX_DAILY_LOSSES,
    MAX_DAILY_TRADES, LEVERAGE, load_data, compute_htf_ema, get_htf_trend_for_day,
    get_adaptive_risk, StrategyICTClean,
    get_vol_scaled_slippage, is_high_vol_event_day,
)

DATA_DIR = Path(__file__).parent / "data" / "raw"
EMA_FAST = 20
EMA_SLOW = 50

# ---- DATA ----
DATA_FILES = {
    "FRA40": "FRA40_10YR_M1_CLEAN.csv",
    "UK100": "UK100_10YR_M1_CLEAN.csv",
}




def load_and_filter(symbol, start_date=None, end_date=None, skip_years=None):
    """Load a 10YR M1 CSV and optionally filter by date range."""
    data_file = DATA_FILES.get(symbol)
    if data_file is None:
        return None
    try:
        df = load_data(data_file)
    except FileNotFoundError:
        print(f"    File not found: {data_file}")
        return None

    if start_date is not None:
        df = df[df.index.date >= start_date]
    if end_date is not None:
        df = df[df.index.date <= end_date]
    if skip_years:
        df = df[~df.index.year.isin(skip_years)]
    if len(df) == 0:
        return None
    return df


def generate_signals_for_data(symbol, df, label=None):
    """
    Generate ORB Clean Breakout signals using each index's default config.
    """
    if label is None:
        label = symbol

    cfg = LIVE_INDEX_CONFIG.get(symbol)
    if cfg is None:
        print(f"    {label}: No config in LIVE_INDEX_CONFIG, skipping")
        return []

    htf_ema = compute_htf_ema(df, fast=EMA_FAST, slow=EMA_SLOW)
    trading_days = sorted(set(df.index.date))
    point_value = BROKER_POINT_VALUES[symbol]

    print(f"    {label}: {len(df):,} M1 bars | {len(htf_ema)} H1 bars | "
          f"{len(trading_days)} days ({trading_days[0]} -> {trading_days[-1]})")

    signals = []
    count = 0
    skipped_days = 0

    for day in trading_days:
        session_start = pd.Timestamp(day).replace(
            hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
        session_end = pd.Timestamp(day).replace(
            hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
        market_df = df[(df.index >= session_start) & (df.index <= session_end)].copy()

        if len(market_df) < 80:
            skipped_days += 1
            continue

        htf_trend = get_htf_trend_for_day(htf_ema, session_start)

        strategy = StrategyICTClean(
            sl_buffer=cfg["sl_buffer"],
            min_sl_points=cfg.get("min_sl_points", 2.0),
            max_sl_points=cfg.get("max_sl_points", 25.0),
            orb_minutes=cfg.get("orb_minutes", 15),
            entry_hours=cfg.get("entry_hours", 2),
            breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
            disp_mult=cfg.get("disp_mult", 1.3),
            body_min=cfg.get("body_min", 0.60),
            close_strength=cfg.get("close_strength", 0.25),
            breakout_margin=cfg.get("breakout_margin", 0.05),
            orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
            tp3_r=2.5,
        )

        day_signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
        for s in day_signals:
            signals.append({
                "symbol": symbol,
                "segment": label,
                "signal": s,
                "point_value": point_value,
                "entry_time": pd.Timestamp(s.entry_time),
                "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
            })
            count += 1

    if skipped_days > 0:
        pct_skip = skipped_days / len(trading_days) * 100
        print(f"      Skipped {skipped_days}/{len(trading_days)} days "
              f"({pct_skip:.0f}%) -- <80 session bars")

    print(f"      -> {count} signals")
    return signals


def generate_all_signals():
    """Generate signals for FRA40 and UK100."""
    all_signals = []
    print("\n  -- SIGNAL GENERATION (default rules) --")

    for symbol in ["FRA40", "UK100"]:
        df = load_and_filter(symbol)
        if df is not None:
            sigs = generate_signals_for_data(symbol, df, symbol)
            all_signals.extend(sigs)

    all_signals.sort(key=lambda x: x["entry_time"])
    print(f"\n  Total signals: {len(all_signals)}")
    return all_signals


# ---- PORTFOLIO ENGINE ----
# (Same logic as run_histdata_backtest.py)

def run_portfolio(all_signals):
    """Execute portfolio backtest with full FundedNext constraints."""
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    MAX_DD_PCT = ACCOUNT_RULES.get("max_drawdown_pct", 10.0)
    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL

    open_positions = []
    daily_losses = {}
    daily_trade_counts = {}
    all_trades = []
    skipped_reasons = {
        "max_positions": 0, "daily_loss_limit": 0, "adaptive_stop": 0,
        "max_daily_trades": 0, "max_dd_breach": 0,
        "spread_guard": 0, "price_drift": 0, "stale_signal": 0,
        "min_R": 0, "news_window": 0,
    }
    account_terminated = False
    account_resets = 0

    _r_by_sym = defaultdict(list)
    for sig in all_signals:
        _r_by_sym[sig["symbol"]].append(float(sig["signal"].risk_points))
    median_R = {sym: float(np.median(rs)) for sym, rs in _r_by_sym.items()}

    current_day = None
    day_peak_balance = STARTING_CAPITAL
    day_start_balance = STARTING_CAPITAL

    for sig in all_signals:
        if account_terminated:
            account_resets += 1
            account_terminated = False
            current_balance = STARTING_CAPITAL
            peak_balance = STARTING_CAPITAL
            day_peak_balance = STARTING_CAPITAL
            day_start_balance = STARTING_CAPITAL
            daily_losses.clear()
            daily_trade_counts.clear()
            open_positions.clear()

        s = sig["signal"]
        symbol = sig["symbol"]
        segment = sig.get("segment", symbol)
        point_value = sig["point_value"]
        entry_time = sig["entry_time"]
        exit_time = sig["exit_time"]
        trade_day = entry_time.date()
        risk_points = float(s.risk_points)

        if trade_day != current_day:
            current_day = trade_day
            day_peak_balance = current_balance
            day_start_balance = current_balance
        day_peak_balance = max(day_peak_balance, current_balance)

        if daily_losses.get(trade_day, 0) >= MAX_DAILY_LOSSES:
            skipped_reasons["daily_loss_limit"] += 1
            continue
        if daily_trade_counts.get(trade_day, 0) >= MAX_DAILY_TRADES:
            skipped_reasons["max_daily_trades"] += 1
            continue

        open_positions = [p for p in open_positions
                          if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            skipped_reasons["max_positions"] += 1
            continue

        # Min R guard -- default 5.0 for most indices
        MIN_R_BY_SYMBOL = {"SPX500": 3.0, "US2000": 3.0}
        MIN_R_POINTS = MIN_R_BY_SYMBOL.get(symbol, 5.0)
        if risk_points < MIN_R_POINTS:
            skipped_reasons["min_R"] += 1
            continue

        # Spread-to-R guard
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        if risk_points > 0 and (spread_pts / risk_points) > 0.30:
            skipped_reasons["spread_guard"] += 1
            continue

        # Stale signal guard
        breakout_time = getattr(s, 'breakout_time', None)
        if breakout_time is not None:
            bars_delay = int((pd.Timestamp(s.entry_time) -
                              pd.Timestamp(breakout_time)).total_seconds() / 60)
            if bars_delay > 15:
                skipped_reasons["stale_signal"] += 1
                continue

        # Price drift guard
        signal_price = getattr(s, 'signal_price', None)
        if signal_price is not None and risk_points > 0:
            drift = abs(float(s.entry_price) - float(signal_price))
            if drift > 0.5 * risk_points:
                skipped_reasons["price_drift"] += 1
                continue

        # Adaptive risk
        risk_pct, should_stop = get_adaptive_risk(
            current_balance, day_peak_balance,
            starting_balance=STARTING_CAPITAL)
        if should_stop:
            skipped_reasons["adaptive_stop"] += 1
            continue

        # Position sizing
        risk_amount = current_balance * (risk_pct / 100.0)
        if risk_points <= 0:
            continue
        lots = risk_amount / (risk_points * point_value)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        exit_reason = getattr(s, "exit_reason", None)
        pnl_points = float(getattr(s, "pnl_points", 0.0))

        # Realistic costs
        base_slippage = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
        high_vol = is_high_vol_event_day(entry_time, symbol)
        slippage_pts = get_vol_scaled_slippage(
            base_slippage, risk_points,
            median_R.get(symbol, risk_points), high_vol)
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        half_spread = spread_pts / 2.0
        entry_cost_pts = slippage_pts + half_spread

        if exit_reason == "TP":
            exit_cost_pts = half_spread
        else:
            exit_cost_pts = slippage_pts + half_spread

        total_cost_pts = entry_cost_pts + exit_cost_pts
        commission_cost = TRADING_COSTS["commission_per_lot"].get(
            symbol, 0.0) * lots

        adjusted_pnl_points = pnl_points - total_cost_pts
        if adjusted_pnl_points > 0:
            result = "win" if exit_reason == "TP" else "partial_win"
        elif adjusted_pnl_points == 0:
            result = "breakeven"
        else:
            result = "loss"

        pnl_raw = pnl_points * lots * point_value
        total_costs = total_cost_pts * lots * point_value + commission_cost
        pnl = pnl_raw - total_costs

        current_balance += pnl
        if pnl > 0:
            day_peak_balance = max(day_peak_balance, current_balance)
        peak_balance = max(peak_balance, current_balance)

        # Max DD enforcement
        overall_dd_pct = ((STARTING_CAPITAL - current_balance) /
                          STARTING_CAPITAL) * 100
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True
            print(f"    *** ACCOUNT RESET #{account_resets+1}: "
                  f"DD={overall_dd_pct:.1f}% on {entry_time.date()} ***")

        daily_ref_post = max(day_start_balance, day_peak_balance)
        daily_dd_pct = ((daily_ref_post - current_balance) /
                        daily_ref_post * 100
                        ) if daily_ref_post > 0 else 0
        trailing_dd_pct = ((peak_balance - current_balance) /
                           peak_balance * 100
                           ) if peak_balance > 0 else 0

        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1
        daily_trade_counts[trade_day] = daily_trade_counts.get(
            trade_day, 0) + 1

        if exit_time is not None:
            open_positions.append({
                "symbol": symbol, "exit_time": exit_time
            })

        all_trades.append({
            "symbol": symbol, "segment": segment, "date": entry_time,
            "direction": s.direction,
            "result": result, "risk_percent": risk_pct,
            "risk_amount": risk_amount,
            "risk_points": risk_points, "pnl_points": pnl_points,
            "adjusted_pnl_points": adjusted_pnl_points,
            "pnl_raw": pnl_raw, "costs": total_costs, "pnl": pnl,
            "balance": current_balance, "peak_balance": peak_balance,
            "day_peak_balance": day_peak_balance,
            "daily_dd_pct": round(daily_dd_pct, 2),
            "trailing_dd_pct": round(trailing_dd_pct, 2),
            "lots": lots, "exit_reason": exit_reason,
        })

    return all_trades, skipped_reasons, current_balance, peak_balance, account_resets


# ---- METRICS ----

def compute_metrics(trades_df, starting_capital, label="Overall"):
    """Compute comprehensive metrics from a trades DataFrame."""
    total = len(trades_df)
    if total == 0:
        return None

    wins = len(trades_df[trades_df["result"] == "win"])
    partial_wins = len(trades_df[trades_df["result"] == "partial_win"])
    losses = len(trades_df[trades_df["result"] == "loss"])
    profitable = wins + partial_wins
    win_rate = (profitable / total * 100) if total > 0 else 0

    net_pnl = trades_df["pnl"].sum()
    gross_profit = trades_df[trades_df["pnl"] > 0]["pnl"].sum()
    gross_loss = trades_df[trades_df["pnl"] < 0]["pnl"].sum()
    profit_factor = (abs(gross_profit / gross_loss)
                     if gross_loss != 0 else float("inf"))

    avg_win_pnl = (trades_df[trades_df["pnl"] > 0]["pnl"].mean()
                   if profitable > 0 else 0)
    avg_loss_pnl = (abs(trades_df[trades_df["pnl"] < 0]["pnl"].mean())
                    if losses > 0 else 0)
    avg_win_pts = (trades_df[trades_df["pnl_points"] > 0]["pnl_points"].mean()
                   if profitable > 0 else 0)
    avg_loss_pts = (abs(trades_df[trades_df["pnl_points"] < 0]["pnl_points"].mean())
                    if losses > 0 else 0)
    actual_rr = (avg_win_pts / avg_loss_pts) if avg_loss_pts > 0 else float("inf")

    expectancy = net_pnl / total
    total_costs = trades_df["costs"].sum()
    largest_win = trades_df["pnl"].max()
    largest_loss = trades_df["pnl"].min()

    # Drawdown
    equity = trades_df["balance"].values
    peak_equity = np.maximum.accumulate(equity)
    drawdowns_pct = (peak_equity - equity) / peak_equity * 100
    drawdowns_dollar = peak_equity - equity
    max_dd_pct = drawdowns_pct.max()
    max_dd_idx = drawdowns_pct.argmax()
    max_dd_dollar = drawdowns_dollar[max_dd_idx]

    peak_daily_dd = trades_df["daily_dd_pct"].max()
    peak_trailing_dd = trades_df["trailing_dd_pct"].max()

    # Consecutive streaks
    max_consec_loss = max_consec_win = cl = cw = 0
    for r in trades_df["result"].tolist():
        if r == "loss":
            cl += 1; cw = 0
            max_consec_loss = max(max_consec_loss, cl)
        elif r in ("win", "partial_win"):
            cw += 1; cl = 0
            max_consec_win = max(max_consec_win, cw)
        else:
            cl = cw = 0

    # Sharpe / Sortino
    tdf = trades_df.copy()
    tdf["trade_date"] = tdf["date"].dt.date
    daily_pnl = tdf.groupby("trade_date")["pnl"].sum()
    trading_days = len(daily_pnl)

    first_date = trades_df["date"].min()
    last_date = trades_df["date"].max()
    calendar_days = max(1, (last_date - first_date).days)
    calendar_weeks = max(1, calendar_days / 7)
    trades_per_week = total / calendar_weeks

    avg_dp = daily_pnl.mean()
    std_dp = daily_pnl.std()
    down_std = (daily_pnl[daily_pnl < 0].std()
                if len(daily_pnl[daily_pnl < 0]) > 1 else std_dp)
    sharpe = (avg_dp / std_dp * np.sqrt(252)) if std_dp > 0 else 0
    sortino = (avg_dp / down_std * np.sqrt(252)) if down_std > 0 else 0

    final_balance = trades_df["balance"].iloc[-1]
    total_return_pct = ((final_balance - starting_capital) /
                        starting_capital * 100)

    annual_return = net_pnl / max(1, trading_days / 252)
    calmar = (annual_return / max_dd_dollar) if max_dd_dollar > 0 else 0
    recovery_factor = (net_pnl / max_dd_dollar) if max_dd_dollar > 0 else 0

    return {
        "label": label,
        "total_trades": total,
        "trades_per_week": round(trades_per_week, 1),
        "wins": wins, "partial_wins": partial_wins, "losses": losses,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "actual_rr": round(actual_rr, 2),
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(avg_win_pnl, 2),
        "avg_loss": round(avg_loss_pnl, 2),
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
        "expectancy": round(expectancy, 2),
        "total_costs": round(total_costs, 2),
        "final_balance": round(final_balance, 2),
        "total_return_pct": round(total_return_pct, 1),
        "max_dd_pct": round(max_dd_pct, 1),
        "max_dd_dollar": round(max_dd_dollar, 2),
        "peak_daily_dd": round(peak_daily_dd, 1),
        "peak_trailing_dd": round(peak_trailing_dd, 1),
        "max_consec_wins": max_consec_win,
        "max_consec_losses": max_consec_loss,
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "recovery_factor": round(recovery_factor, 2),
        "starting_capital": starting_capital,
        "trading_days": trading_days,
    }


# ---- REPORT ----

def print_report(results, skipped, account_resets):
    """Print comprehensive backtest report."""
    o = results["overall"]
    symbols_in = list(results["per_index"].keys())

    print(f"""
+===================================================================+
|         FRA40 & UK100 BACKTEST (DEFAULT RULES)                    |
|         ORB Clean Breakout -- 16-Year HistData                    |
+===================================================================+
|  Indices         : {', '.join(symbols_in):<46}|
|  Strategy Params : Per-index defaults from LIVE_INDEX_CONFIG      |
|  Data            : HistData 16yr (Nov 2010 - Feb 2026)            |
+===================================================================+
|  Total Trades    : {o['total_trades']:<8} ({o['trades_per_week']:.1f}/wk)                          |
|  Win Rate        : {o['win_rate']}%                                           |
|  Profit Factor   : {o['profit_factor']:<8}                                      |
|  Avg RR          : 1:{o['actual_rr']:<8}                                    |
|  Sharpe Ratio    : {o['sharpe']:<8}                                      |
|  Sortino Ratio   : {o['sortino']:<8}                                      |
+===================================================================+
|  Starting Capital: ${o['starting_capital']:>10,.2f}                                |
|  Final Balance   : ${o['final_balance']:>10,.2f}                                |
|  Net P&L         : ${o['net_pnl']:>10,.2f}                                |
|  Total Return    : {o['total_return_pct']:>8.1f}%                                   |
|  Account Resets  : {account_resets:<8} (10% DD terminations)                 |
+===================================================================+
|  Max Drawdown    : {o['max_dd_pct']}% (${o['max_dd_dollar']:,.2f})                          |
|  Peak Daily DD   : {o['peak_daily_dd']}%                                          |
|  Max Consec Loss : {o['max_consec_losses']:<8}                                      |
|  Recovery Factor : {o['recovery_factor']:<8}                                      |
+===================================================================+""")

    # Per-Index Summary
    print(f"\n  -- PER-INDEX SUMMARY --")
    print(f"  {'Index':<10} {'Trades':>7} {'Tr/Wk':>6} {'WR':>6} "
          f"{'PF':>6} {'Net PnL':>11} {'MaxDD%':>7} {'Sharpe':>7}")
    print(f"  {'-' * 64}")
    for sym_key in sorted(results["per_index"].keys()):
        m = results["per_index"][sym_key]
        print(f"  {sym_key:<10} {m['total_trades']:>7} {m['trades_per_week']:>5.1f} "
              f"{m['win_rate']:>5.1f}% {m['profit_factor']:>5.2f} "
              f"${m['net_pnl']:>10,.2f} {m['max_dd_pct']:>6.1f}% "
              f"{m['sharpe']:>6.2f}")

    # Yearly Breakdown
    print(f"\n  -- YEARLY BREAKDOWN --")
    print(f"  {'Year':<6} {'Trades':>7} {'Tr/Wk':>6} {'W':>4} {'L':>4} "
          f"{'WR':>6} {'PF':>6} {'Net PnL':>11} {'MaxDD%':>7} "
          f"{'Sharpe':>7} {'MCL':>4}")
    print(f"  {'-' * 80}")
    for yr, m in sorted(results["yearly"].items()):
        w = m['wins'] + m['partial_wins']
        print(f"  {yr:<6} {m['total_trades']:>7} {m['trades_per_week']:>5.1f} "
              f"{w:>4} {m['losses']:>4} {m['win_rate']:>5.1f}% "
              f"{m['profit_factor']:>5.2f} ${m['net_pnl']:>10,.2f} "
              f"{m['max_dd_pct']:>6.1f}% {m['sharpe']:>6.2f} "
              f"{m['max_consec_losses']:>4}")

    # Per-Index Yearly
    for sym_key in sorted(results["per_index_yearly"].keys()):
        yearly = results["per_index_yearly"][sym_key]
        if not yearly:
            continue
        print(f"\n  -- {sym_key} YEARLY --")
        print(f"  {'Year':<6} {'Trades':>7} {'W':>4} {'L':>4} "
              f"{'WR':>6} {'PF':>6} {'Net PnL':>11}")
        print(f"  {'-' * 52}")
        for yr, m in sorted(yearly.items()):
            w = m['wins'] + m['partial_wins']
            print(f"  {yr:<6} {m['total_trades']:>7} {w:>4} {m['losses']:>4} "
                  f"{m['win_rate']:>5.1f}% {m['profit_factor']:>5.2f} "
                  f"${m['net_pnl']:>10,.2f}")

    # Exit Reasons
    print(f"\n  -- EXIT REASONS --")
    for reason, info in sorted(results["exit_reasons"].items(),
                                key=lambda x: -x[1]['count']):
        print(f"  {reason:<15} : {info['count']:>5} ({info['pct']:>5.1f}%)  "
              f"avg PnL: ${info['avg_pnl']:>8,.2f}")

    # Skipped
    print(f"\n  -- SKIPPED SIGNALS --")
    for reason, count in sorted(skipped.items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"  {reason:<20} : {count:>5}")

    # Monthly payout impact
    print(f"\n  -- MONTHLY PAYOUT IMPACT --")
    trades_df = results["_trades_df"]
    tdf_copy = trades_df.copy()
    tdf_copy["month"] = tdf_copy["date"].dt.to_period("M")
    monthly = tdf_copy.groupby("month").agg(
        trades=("pnl", "count"), pnl=("pnl", "sum"))
    payout_months = len(monthly[monthly["pnl"] > 0])
    total_months = len(monthly)
    avg_monthly_pnl = monthly["pnl"].mean()
    avg_trades_mo = monthly["trades"].mean()
    pct_payout = (payout_months / total_months * 100
                  if total_months > 0 else 0)
    print(f"  {avg_trades_mo:.1f} trades/month | "
          f"{payout_months}/{total_months} payout months ({pct_payout:.0f}%) | "
          f"avg month PnL: ${avg_monthly_pnl:,.0f}")


def main():
    print("=" * 70)
    print("  FRA40 & UK100 BACKTEST -- Default Rules")
    print(f"  Capital: ${ACCOUNT_RULES['starting_balance']:,.0f} | "
          f"Risk: 1.5% (adaptive) | MIN_R: 5.0 pts (default)")
    print("  Strategy: Per-index defaults from LIVE_INDEX_CONFIG")
    print("=" * 70)

    # Phase 1: Generate signals
    all_signals = generate_all_signals()
    if not all_signals:
        print("  No signals generated. Check data files.")
        return

    # Phase 2: Run portfolio backtest
    print("\n  Running portfolio backtest with FundedNext constraints...")
    all_trades, skipped, final_balance, peak_balance, account_resets = (
        run_portfolio(all_signals))
    print(f"  Executed trades: {len(all_trades)}")

    if not all_trades:
        print("  No trades executed. Exiting.")
        return

    trades_df = pd.DataFrame(all_trades)
    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)

    # Save trades
    output_dir = Path(__file__).parent / "reports" / "histdata_backtest"
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(output_dir / "fra40_uk100_trades.csv", index=False)

    # Phase 3: Compute metrics
    starting_cap = ACCOUNT_RULES["starting_balance"]
    results = {}

    # Overall
    results["overall"] = compute_metrics(
        trades_df, starting_cap, "Overall (16 Years)")

    # Per-index
    results["per_index"] = {}
    for sym in sorted(trades_df["symbol"].unique()):
        sdf = trades_df[trades_df["symbol"] == sym].reset_index(drop=True)
        if len(sdf) > 0:
            results["per_index"][sym] = compute_metrics(
                sdf, starting_cap, sym)

    # Yearly
    trades_df["year"] = trades_df["date"].dt.year
    results["yearly"] = {}
    for yr in sorted(trades_df["year"].unique()):
        ydf = trades_df[trades_df["year"] == yr].reset_index(drop=True)
        if len(ydf) > 0:
            yr_start_cap = ydf["balance"].iloc[0] - ydf["pnl"].iloc[0]
            results["yearly"][str(yr)] = compute_metrics(
                ydf, yr_start_cap, str(yr))

    # Per-index yearly
    results["per_index_yearly"] = {}
    for sym in sorted(trades_df["symbol"].unique()):
        sdf = trades_df[trades_df["symbol"] == sym]
        results["per_index_yearly"][sym] = {}
        for yr in sorted(sdf["year"].unique()):
            sydf = sdf[sdf["year"] == yr].reset_index(drop=True)
            if len(sydf) > 0:
                yr_start = sydf["balance"].iloc[0] - sydf["pnl"].iloc[0]
                results["per_index_yearly"][sym][str(yr)] = compute_metrics(
                    sydf, yr_start, f"{sym} {yr}")

    # Exit reasons
    exit_counts = trades_df["exit_reason"].value_counts()
    results["exit_reasons"] = {}
    for reason, count in exit_counts.items():
        pct = count / len(trades_df) * 100
        avg_pnl = trades_df[trades_df["exit_reason"] == reason]["pnl"].mean()
        results["exit_reasons"][reason] = {
            "count": int(count), "pct": round(pct, 1),
            "avg_pnl": round(avg_pnl, 2),
        }

    # Attach trades_df for payout calc (not saved to JSON)
    results["_trades_df"] = trades_df

    # Save JSON (without _trades_df)
    save_results = {k: v for k, v in results.items() if k != "_trades_df"}
    json_path = output_dir / "metrics_fra40_uk100.json"
    with open(json_path, "w") as f:
        json.dump(save_results, f, indent=2, default=str)

    # Print report
    print("\n" + "=" * 75)
    print("  FRA40 & UK100 BACKTEST RESULTS -- Default Rules")
    print("=" * 75)
    print_report(results, skipped, account_resets)

    print(f"\n  Trades saved : {output_dir / 'fra40_uk100_trades.csv'}")
    print(f"  Metrics saved: {json_path}")
    print("=" * 75)


if __name__ == "__main__":
    main()
