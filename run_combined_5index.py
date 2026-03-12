"""
5-Index Combined Portfolio Backtest + FundedNext $100K Compounding Sim
=====================================================================
Combines ALL available indices into one master portfolio backtest:
  - SPX500: HistData (Nov 2010 - Feb 2026, full)
  - NDX100: HistData (Nov 2010 - Feb 2026, full)
  - GER30:  HistData split (Period A: 2010-2020, Period B: 2024-2026)
  - FRA40:  HistData (Nov 2010 - Feb 2026, full) -- default rules
  - US30:   Dukascopy (Jan 2016 - Jun 2019, sparse)

Each index uses its OWN default config from LIVE_INDEX_CONFIG.

Outputs:
  1. Combined portfolio metrics (PF, Sharpe, DD, etc.)
  2. Per-index breakdown
  3. Monthly payout table per year
  4. Data gaps listing
  5. $100K FundedNext compounding sim (50% reinvestment)

Usage:
  python run_combined_5index.py
"""

import sys
import io
import json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict
from datetime import date, datetime


class TeeWriter:
    """Writes to both stdout and a StringIO buffer simultaneously."""
    def __init__(self, original_stdout):
        self.stdout = original_stdout
        self.buffer = io.StringIO()

    def write(self, text):
        self.stdout.write(text)
        self.buffer.write(text)

    def flush(self):
        self.stdout.flush()

    def getvalue(self):
        return self.buffer.getvalue()

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

# ---- DATA CONFIG ----
DATA_FILES = {
    "SPX500": "SPX500_10YR_M1_CLEAN.csv",
    "NDX100": "NDX100_10YR_M1_CLEAN.csv",
    "GER30":  "GER30_10YR_M1_CLEAN.csv",
    "US30":   "US30_10YR_M1_CLEAN.csv",
    "FRA40":  "FRA40_10YR_M1_CLEAN.csv",
}

GER30_CLEAN_PERIODS = {
    "GER30_A": {
        "label": "GER30 Period A (2010-2020)",
        "start": date(2010, 11, 1),
        "end":   date(2020, 6, 12),
    },
    "GER30_B": {
        "label": "GER30 Period B (2024-2026)",
        "start": date(2023, 12, 4),
        "end":   date(2026, 12, 31),
    },
}

# Known data gaps for the report
DATA_GAPS = {
    "GER30": [
        "2020-06-15 to 2023-12-03: Price scaling break (values ~3.7x too low). "
        "Period SKIPPED. Two clean segments used: Period A (2010-2020) + Period B (2024-2026)."
    ],
    "US30": [
        "Only 2016-01 to 2019-06 available (Dukascopy gap-filled). "
        "No HistData coverage. Missing: 2010-2015 and 2019-07 onwards."
    ],
    "SPX500": ["Full coverage: Nov 2010 - Feb 2026 (no gaps)."],
    "NDX100": ["Full coverage: Nov 2010 - Feb 2026 (no gaps)."],
    "FRA40":  ["Full coverage: Nov 2010 - Feb 2026 (no gaps)."],
}


def load_and_filter(symbol, start_date=None, end_date=None, skip_years=None):
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
    return df if len(df) > 0 else None


def generate_signals_for_data(symbol, df, label=None):
    """Generate signals using each index's OWN default config."""
    if label is None:
        label = symbol
    cfg = LIVE_INDEX_CONFIG.get(symbol)
    if cfg is None:
        print(f"    {label}: No config, skipping")
        return []

    htf_ema = compute_htf_ema(df, fast=EMA_FAST, slow=EMA_SLOW)
    trading_days = sorted(set(df.index.date))
    point_value = BROKER_POINT_VALUES[symbol]

    print(f"    {label}: {len(df):,} M1 bars | {len(htf_ema)} H1 | "
          f"{len(trading_days)} days ({trading_days[0]} -> {trading_days[-1]})")

    signals = []
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
            entry_hours=cfg.get("entry_hours", 3),
            breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
            disp_mult=cfg.get("disp_mult", 1.0),
            body_min=cfg.get("body_min", 0.70),
            close_strength=cfg.get("close_strength", 0.25),
            breakout_margin=cfg.get("breakout_margin", 0.15),
            orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
            tp3_r=2.5,
        )
        day_signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
        for s in day_signals:
            signals.append({
                "symbol": symbol, "segment": label, "signal": s,
                "point_value": point_value,
                "entry_time": pd.Timestamp(s.entry_time),
                "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
            })

    if skipped_days > 0:
        print(f"      Skipped {skipped_days}/{len(trading_days)} days (<80 bars)")
    print(f"      -> {len(signals)} signals")
    return signals


def generate_all_signals():
    all_signals = []
    print("\n  -- SIGNAL GENERATION (each index's default rules) --")

    # SPX500
    df = load_and_filter("SPX500")
    if df is not None:
        all_signals.extend(generate_signals_for_data("SPX500", df))

    # NDX100
    df = load_and_filter("NDX100")
    if df is not None:
        all_signals.extend(generate_signals_for_data("NDX100", df))

    # GER30 (split)
    for pkey, pcfg in GER30_CLEAN_PERIODS.items():
        df = load_and_filter("GER30", start_date=pcfg["start"], end_date=pcfg["end"])
        if df is not None:
            all_signals.extend(generate_signals_for_data("GER30", df, pcfg["label"]))

    # FRA40
    df = load_and_filter("FRA40")
    if df is not None:
        all_signals.extend(generate_signals_for_data("FRA40", df))

    # US30
    df = load_and_filter("US30")
    if df is not None:
        all_signals.extend(generate_signals_for_data("US30", df, "US30 (Dukascopy)"))

    all_signals.sort(key=lambda x: x["entry_time"])
    print(f"\n  Total signals: {len(all_signals)}")
    return all_signals


# ---- PORTFOLIO ENGINE (same as run_histdata_backtest.py) ----

def run_portfolio(all_signals):
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
        "max_daily_trades": 0, "spread_guard": 0, "price_drift": 0,
        "stale_signal": 0, "min_R": 0,
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

        open_positions = [p for p in open_positions if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            skipped_reasons["max_positions"] += 1
            continue

        MIN_R_BY_SYMBOL = {"SPX500": 3.0, "US2000": 3.0}
        MIN_R_POINTS = MIN_R_BY_SYMBOL.get(symbol, 5.0)
        if risk_points < MIN_R_POINTS:
            skipped_reasons["min_R"] += 1
            continue

        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        if risk_points > 0 and (spread_pts / risk_points) > 0.30:
            skipped_reasons["spread_guard"] += 1
            continue

        breakout_time = getattr(s, 'breakout_time', None)
        if breakout_time is not None:
            bars_delay = int((pd.Timestamp(s.entry_time) -
                              pd.Timestamp(breakout_time)).total_seconds() / 60)
            if bars_delay > 15:
                skipped_reasons["stale_signal"] += 1
                continue

        signal_price = getattr(s, 'signal_price', None)
        if signal_price is not None and risk_points > 0:
            drift = abs(float(s.entry_price) - float(signal_price))
            if drift > 0.5 * risk_points:
                skipped_reasons["price_drift"] += 1
                continue

        risk_pct, should_stop = get_adaptive_risk(
            current_balance, day_peak_balance, starting_balance=STARTING_CAPITAL)
        if should_stop:
            skipped_reasons["adaptive_stop"] += 1
            continue

        risk_amount = current_balance * (risk_pct / 100.0)
        if risk_points <= 0:
            continue
        lots = risk_amount / (risk_points * point_value)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        exit_reason = getattr(s, "exit_reason", None)
        pnl_points = float(getattr(s, "pnl_points", 0.0))

        base_slippage = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
        high_vol = is_high_vol_event_day(entry_time, symbol)
        slippage_pts = get_vol_scaled_slippage(
            base_slippage, risk_points,
            median_R.get(symbol, risk_points), high_vol)
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        half_spread = spread_pts / 2.0
        entry_cost_pts = slippage_pts + half_spread
        exit_cost_pts = half_spread if exit_reason == "TP" else slippage_pts + half_spread
        total_cost_pts = entry_cost_pts + exit_cost_pts
        commission_cost = TRADING_COSTS["commission_per_lot"].get(symbol, 0.0) * lots

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

        overall_dd_pct = ((STARTING_CAPITAL - current_balance) / STARTING_CAPITAL) * 100
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True
            print(f"    *** ACCOUNT RESET #{account_resets+1}: "
                  f"DD={overall_dd_pct:.1f}% on {entry_time.date()} ***")

        daily_ref_post = max(day_start_balance, day_peak_balance)
        daily_dd_pct = ((daily_ref_post - current_balance) /
                        daily_ref_post * 100) if daily_ref_post > 0 else 0
        trailing_dd_pct = ((peak_balance - current_balance) /
                           peak_balance * 100) if peak_balance > 0 else 0

        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1
        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1
        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})

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
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")

    avg_win_pts = trades_df[trades_df["pnl_points"] > 0]["pnl_points"].mean() if profitable > 0 else 0
    avg_loss_pts = abs(trades_df[trades_df["pnl_points"] < 0]["pnl_points"].mean()) if losses > 0 else 0
    actual_rr = (avg_win_pts / avg_loss_pts) if avg_loss_pts > 0 else float("inf")

    expectancy = net_pnl / total
    total_costs = trades_df["costs"].sum()

    equity = trades_df["balance"].values
    peak_equity = np.maximum.accumulate(equity)
    drawdowns_pct = (peak_equity - equity) / peak_equity * 100
    drawdowns_dollar = peak_equity - equity
    max_dd_pct = drawdowns_pct.max()
    max_dd_idx = drawdowns_pct.argmax()
    max_dd_dollar = drawdowns_dollar[max_dd_idx]

    peak_daily_dd = trades_df["daily_dd_pct"].max()
    peak_trailing_dd = trades_df["trailing_dd_pct"].max()

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
    down_std = daily_pnl[daily_pnl < 0].std() if len(daily_pnl[daily_pnl < 0]) > 1 else std_dp
    sharpe = (avg_dp / std_dp * np.sqrt(252)) if std_dp > 0 else 0
    sortino = (avg_dp / down_std * np.sqrt(252)) if down_std > 0 else 0

    final_balance = trades_df["balance"].iloc[-1]
    total_return_pct = ((final_balance - starting_capital) / starting_capital * 100)
    annual_return = net_pnl / max(1, trading_days / 252)
    calmar = (annual_return / max_dd_dollar) if max_dd_dollar > 0 else 0
    recovery_factor = (net_pnl / max_dd_dollar) if max_dd_dollar > 0 else 0

    return {
        "label": label, "total_trades": total,
        "trades_per_week": round(trades_per_week, 1),
        "wins": wins, "partial_wins": partial_wins, "losses": losses,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "actual_rr": round(actual_rr, 2),
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
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


# ---- MONTHLY PAYOUT TABLE ----

def monthly_payout_table(trades_df):
    """Build month-by-month PnL table by year with annual totals."""
    tdf = trades_df.copy()
    tdf["year"] = tdf["date"].dt.year
    tdf["month"] = tdf["date"].dt.month

    years = sorted(tdf["year"].unique())
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    # Build grid: year x month
    grid = {}
    for yr in years:
        grid[yr] = {}
        for m in range(1, 13):
            mdf = tdf[(tdf["year"] == yr) & (tdf["month"] == m)]
            if len(mdf) > 0:
                grid[yr][m] = {
                    "pnl": mdf["pnl"].sum(),
                    "trades": len(mdf),
                    "wr": len(mdf[mdf["pnl"] > 0]) / len(mdf) * 100,
                }
            else:
                grid[yr][m] = None

    return years, grid, month_names


def print_monthly_table(years, grid, month_names):
    """Print the monthly PnL breakdown."""
    # Header
    print(f"\n  {'Year':>6}", end="")
    for mn in month_names:
        print(f" {mn:>8}", end="")
    print(f" {'TOTAL':>10} {'Months+':>8}")
    print(f"  {'-' * 6}", end="")
    for _ in month_names:
        print(f" {'-' * 8}", end="")
    print(f" {'-' * 10} {'-' * 8}")

    for yr in years:
        print(f"  {yr:>6}", end="")
        yr_total = 0
        pos_months = 0
        for m in range(1, 13):
            cell = grid[yr].get(m)
            if cell is None:
                print(f" {'--':>8}", end="")
            else:
                pnl = cell["pnl"]
                yr_total += pnl
                if pnl > 0:
                    pos_months += 1
                if abs(pnl) >= 1000:
                    print(f" {pnl / 1000:>7.1f}k", end="")
                else:
                    print(f" {pnl:>8.0f}", end="")
        print(f" ${yr_total:>8,.0f} {pos_months:>5}/12")
    print()


# ---- $100K FUNDEDNEXT COMPOUNDING SIM ----

def fundednext_compounding_sim(trades_df, funded_balance=100_000,
                               reinvest_pct=0.50, payout_split=0.90):
    """
    Simulate FundedNext $100K funded account with compounding.

    Model:
    - Compute monthly RETURN % from backtest (PnL / start-of-month balance)
    - Each active $100K FundedNext account independently earns that return %
    - FundedNext Stellar-2: 90/10 profit split (trader gets 90%)
    - Monthly payout if NET across all accounts is profitable
    - 50% of payout reinvested toward new $100K accounts
    - New account opens when reinvestment pool reaches $100K

    This correctly separates the backtest compounding from the
    funded account simulation.
    """
    BACKTEST_CAPITAL = ACCOUNT_RULES["starting_balance"]  # $6,000

    tdf = trades_df.copy()
    tdf["year"] = tdf["date"].dt.year
    tdf["month"] = tdf["date"].dt.month

    # Compute monthly return percentage from backtest
    # (uses start-of-month balance to compute return %)
    monthly_groups = tdf.groupby(["year", "month"])
    monthly_returns = []
    for (yr, mo), grp in monthly_groups:
        month_pnl = grp["pnl"].sum()
        # Balance at start of month = balance before first trade of month
        start_bal = grp["balance"].iloc[0] - grp["pnl"].iloc[0]
        if start_bal > 0:
            ret_pct = month_pnl / start_bal  # decimal return
        else:
            ret_pct = 0
        monthly_returns.append((yr, mo, ret_pct, month_pnl))

    results = []
    total_payouts = 0
    total_reinvested = 0
    total_kept = 0
    reinvest_pool = 0  # accumulates toward next account
    num_accounts = 1
    accounts = [{"size": funded_balance, "opened": "Initial"}]

    for yr, mo, ret_pct, _ in monthly_returns:
        # Each active account independently earns this month's return
        gross_per_account = funded_balance * ret_pct
        gross_total = gross_per_account * num_accounts

        # 90% profit split (on positive total only)
        trader_pnl = gross_total * payout_split if gross_total > 0 else gross_total
        payout = max(0, trader_pnl)
        reinvest = payout * reinvest_pct
        keep = payout - reinvest

        total_payouts += payout
        reinvest_pool += reinvest
        total_reinvested += reinvest
        total_kept += keep

        # Open new accounts when pool has enough
        new_acct = False
        while reinvest_pool >= funded_balance:
            reinvest_pool -= funded_balance
            num_accounts += 1
            accounts.append({
                "size": funded_balance,
                "opened": f"{yr}-{mo:02d}",
            })
            new_acct = True

        results.append({
            "year": yr, "month": mo,
            "return_pct": round(ret_pct * 100, 2),
            "gross_pnl": round(gross_total, 2),
            "trader_pnl": round(trader_pnl, 2),
            "payout": round(payout, 2),
            "reinvest": round(reinvest, 2),
            "keep": round(keep, 2),
            "reinvest_pool": round(reinvest_pool, 2),
            "cumul_payouts": round(total_payouts, 2),
            "cumul_reinvested": round(total_reinvested, 2),
            "cumul_kept": round(total_kept, 2),
            "accounts": num_accounts,
            "new_account": new_acct,
        })

    return results, accounts


def print_compounding_report(sim_results, accounts, funded_balance):
    """Print the FundedNext compounding simulation."""
    print(f"\n  Starting: 1x ${funded_balance:,.0f} FundedNext Stellar-2 account")
    print(f"  Profit split: 90/10 | Reinvest: 50% of payout")
    print(f"  New account opened when reinvestment pool reaches ${funded_balance:,.0f}")
    print(f"  Monthly return % derived from backtest (not raw PnL scaling)")

    # Annual summary
    print(f"\n  {'Year':>6} {'Accts':>6} {'Gross PnL':>12} {'Payouts':>12} {'Kept':>12} "
          f"{'Reinvested':>12} {'End Accts':>9}")
    print(f"  {'-' * 71}")

    by_year = defaultdict(lambda: {"gross": 0, "payout": 0, "keep": 0, "reinvest": 0,
                                   "start_accts": 0, "end_accts": 0})
    for r in sim_results:
        yr = r["year"]
        by_year[yr]["gross"] += r["gross_pnl"]
        by_year[yr]["payout"] += r["payout"]
        by_year[yr]["keep"] += r["keep"]
        by_year[yr]["reinvest"] += r["reinvest"]
        by_year[yr]["end_accts"] = r["accounts"]
        if by_year[yr]["start_accts"] == 0:
            prev_accts = r["accounts"] - (1 if r["new_account"] else 0)
            by_year[yr]["start_accts"] = prev_accts

    for yr in sorted(by_year.keys()):
        d = by_year[yr]
        print(f"  {yr:>6} {d['start_accts']:>5}x ${d['gross']:>11,.0f} ${d['payout']:>11,.0f} "
              f"${d['keep']:>11,.0f} ${d['reinvest']:>11,.0f} {d['end_accts']:>8}x")

    # Totals
    total_gross = sum(r["gross_pnl"] for r in sim_results)
    total_payout = sum(r["payout"] for r in sim_results)
    total_keep = sum(r["keep"] for r in sim_results)
    total_reinvest = sum(r["reinvest"] for r in sim_results)
    final_accts = sim_results[-1]["accounts"] if sim_results else 1

    print(f"  {'-' * 71}")
    print(f"  {'TOTAL':>6} {'':>6} ${total_gross:>11,.0f} ${total_payout:>11,.0f} "
          f"${total_keep:>11,.0f} ${total_reinvest:>11,.0f} {final_accts:>8}x")

    # Monthly detail
    print(f"\n  -- MONTHLY PAYOUT DETAIL --")
    print(f"  {'Month':>8} {'Ret%':>7} {'Gross':>11} {'Payout':>11} {'Keep':>11} "
          f"{'Reinvest':>11} {'Pool':>9} {'Accts':>6}")
    print(f"  {'-' * 78}")

    for r in sim_results:
        marker = " <NEW" if r["new_account"] else ""
        print(f"  {r['year']}-{r['month']:02d}  "
              f"{r['return_pct']:>6.1f}% "
              f"${r['gross_pnl']:>10,.0f} "
              f"${r['payout']:>10,.0f} "
              f"${r['keep']:>10,.0f} "
              f"${r['reinvest']:>10,.0f} "
              f"${r['reinvest_pool']:>8,.0f} "
              f"{r['accounts']:>5}{marker}")

    print(f"\n  -- ACCOUNTS OPENED --")
    for i, a in enumerate(accounts):
        print(f"    Account #{i+1}: ${a['size']:,.0f} -- opened {a['opened']}")

    # Monthly income projection (using last 3 years average)
    recent = [r for r in sim_results if r["year"] >= sim_results[-1]["year"] - 2]
    if recent:
        avg_monthly_keep = sum(r["keep"] for r in recent) / len(recent)
        avg_monthly_payout = sum(r["payout"] for r in recent) / len(recent)
        print(f"\n  Avg monthly payout (last 3yr): ${avg_monthly_payout:,.0f}")
        print(f"  Avg monthly income kept (last 3yr): ${avg_monthly_keep:,.0f}")
        print(f"  Projected annual income kept: ${avg_monthly_keep * 12:,.0f}")

    # Realistic caveat
    pos_months = sum(1 for r in sim_results if r["return_pct"] > 0)
    neg_months = sum(1 for r in sim_results if r["return_pct"] <= 0)
    avg_pos_ret = np.mean([r["return_pct"] for r in sim_results if r["return_pct"] > 0]) if pos_months > 0 else 0
    avg_neg_ret = np.mean([r["return_pct"] for r in sim_results if r["return_pct"] <= 0]) if neg_months > 0 else 0
    print(f"\n  Backtest monthly stats: {pos_months} pos months ({avg_pos_ret:.1f}% avg) | "
          f"{neg_months} neg months ({avg_neg_ret:.1f}% avg)")


# ---- MAIN ----

def main():
    print("=" * 75)
    print("  5-INDEX COMBINED PORTFOLIO BACKTEST")
    print("  SPX500 + NDX100 + GER30 + FRA40 + US30")
    print(f"  Capital: ${ACCOUNT_RULES['starting_balance']:,.0f} | "
          "Risk: 1.5% (adaptive) | Each index uses own default rules")
    print("=" * 75)

    # Phase 1: Generate signals
    all_signals = generate_all_signals()
    if not all_signals:
        print("  No signals. Check data files.")
        return

    # Phase 2: Portfolio backtest
    print("\n  Running portfolio backtest with FundedNext constraints...")
    all_trades, skipped, final_bal, peak_bal, account_resets = run_portfolio(all_signals)
    print(f"  Executed trades: {len(all_trades)}")

    if not all_trades:
        print("  No trades executed.")
        return

    trades_df = pd.DataFrame(all_trades)
    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)

    output_dir = Path(__file__).parent / "reports" / "histdata_backtest"
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(output_dir / "combined_5index_trades.csv", index=False)

    # Phase 3: Compute metrics
    starting_cap = ACCOUNT_RULES["starting_balance"]
    results = {}
    results["overall"] = compute_metrics(trades_df, starting_cap, "Combined 5-Index")

    # Per-index
    results["per_index"] = {}
    for sym in sorted(trades_df["symbol"].unique()):
        sdf = trades_df[trades_df["symbol"] == sym].reset_index(drop=True)
        if len(sdf) > 0:
            results["per_index"][sym] = compute_metrics(sdf, starting_cap, sym)

    # Yearly
    trades_df["year"] = trades_df["date"].dt.year
    results["yearly"] = {}
    for yr in sorted(trades_df["year"].unique()):
        ydf = trades_df[trades_df["year"] == yr].reset_index(drop=True)
        if len(ydf) > 0:
            yr_cap = ydf["balance"].iloc[0] - ydf["pnl"].iloc[0]
            results["yearly"][str(yr)] = compute_metrics(ydf, yr_cap, str(yr))

    # Per-index yearly
    results["per_index_yearly"] = {}
    for sym in sorted(trades_df["symbol"].unique()):
        sdf = trades_df[trades_df["symbol"] == sym]
        results["per_index_yearly"][sym] = {}
        for yr in sorted(sdf["year"].unique()):
            sydf = sdf[sdf["year"] == yr].reset_index(drop=True)
            if len(sydf) > 0:
                yr_cap = sydf["balance"].iloc[0] - sydf["pnl"].iloc[0]
                results["per_index_yearly"][sym][str(yr)] = compute_metrics(
                    sydf, yr_cap, f"{sym} {yr}")

    # Exit reasons
    exit_counts = trades_df["exit_reason"].value_counts()
    results["exit_reasons"] = {}
    for reason, count in exit_counts.items():
        pct = count / len(trades_df) * 100
        avg_pnl = trades_df[trades_df["exit_reason"] == reason]["pnl"].mean()
        results["exit_reasons"][reason] = {
            "count": int(count), "pct": round(pct, 1), "avg_pnl": round(avg_pnl, 2),
        }

    # ════════════════════════════════════════════════════════════
    # PRINT REPORT
    # ════════════════════════════════════════════════════════════

    o = results["overall"]
    print(f"""
+=====================================================================+
|         5-INDEX COMBINED PORTFOLIO BACKTEST                          |
|         ORB Clean Breakout -- 16-Year HistData                      |
+=====================================================================+
|  Indices         : SPX500, NDX100, GER30, FRA40, US30               |
|  Strategy        : Each index uses own default LIVE_INDEX_CONFIG     |
|  Data            : HistData 16yr + US30 Dukascopy (2016-2019)       |
+=====================================================================+
|  Total Trades    : {o['total_trades']:<8} ({o['trades_per_week']:.1f}/wk)                              |
|  Win Rate        : {o['win_rate']}%                                               |
|  Profit Factor   : {o['profit_factor']:<8}                                          |
|  Avg RR          : 1:{o['actual_rr']:<8}                                        |
|  Sharpe Ratio    : {o['sharpe']:<8}                                          |
|  Sortino Ratio   : {o['sortino']:<8}                                          |
+=====================================================================+
|  Starting Capital: ${o['starting_capital']:>10,.2f}                                    |
|  Final Balance   : ${o['final_balance']:>10,.2f}                                    |
|  Net P&L         : ${o['net_pnl']:>10,.2f}                                    |
|  Total Return    : {o['total_return_pct']:>8.1f}%                                       |
|  Account Resets  : {account_resets:<8} (10% DD terminations)                     |
+=====================================================================+
|  Max Drawdown    : {o['max_dd_pct']}% (${o['max_dd_dollar']:,.2f})                              |
|  Peak Daily DD   : {o['peak_daily_dd']}%                                              |
|  Max Consec Loss : {o['max_consec_losses']:<8}                                          |
|  Recovery Factor : {o['recovery_factor']:<8}                                          |
+=====================================================================+""")

    # ── Per-Index Summary ──
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

    # ── Yearly Breakdown ──
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

    # ── Per-Index Yearly ──
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

    # ── Exit Reasons ──
    print(f"\n  -- EXIT REASONS --")
    for reason, info in sorted(results["exit_reasons"].items(),
                                key=lambda x: -x[1]['count']):
        print(f"  {reason:<15} : {info['count']:>5} ({info['pct']:>5.1f}%)  "
              f"avg PnL: ${info['avg_pnl']:>8,.2f}")

    # ── Skipped ──
    print(f"\n  -- SKIPPED SIGNALS --")
    for reason, count in sorted(skipped.items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"  {reason:<20} : {count:>5}")

    # ── Monthly PnL Table ──
    print("\n" + "=" * 75)
    print("  MONTHLY P&L BREAKDOWN (Backtest $6K account)")
    print("=" * 75)
    years, grid, month_names = monthly_payout_table(trades_df)
    print_monthly_table(years, grid, month_names)

    # Monthly payout stats
    tdf2 = trades_df.copy()
    tdf2["month_key"] = tdf2["date"].dt.to_period("M")
    monthly_grp = tdf2.groupby("month_key").agg(
        trades=("pnl", "count"), pnl=("pnl", "sum"))
    payout_months = len(monthly_grp[monthly_grp["pnl"] > 0])
    total_months = len(monthly_grp)
    avg_mo_pnl = monthly_grp["pnl"].mean()
    avg_trades_mo = monthly_grp["trades"].mean()
    print(f"  Payout months: {payout_months}/{total_months} "
          f"({payout_months / total_months * 100:.0f}%) | "
          f"Avg trades/month: {avg_trades_mo:.1f} | "
          f"Avg month PnL: ${avg_mo_pnl:,.0f}")

    # ── Data Gaps ──
    print("\n" + "=" * 75)
    print("  DATA GAPS & COVERAGE")
    print("=" * 75)
    for sym in ["SPX500", "NDX100", "GER30", "FRA40", "US30"]:
        gaps = DATA_GAPS.get(sym, ["Unknown"])
        print(f"\n  {sym}:")
        for g in gaps:
            print(f"    {g}")

    # ── $100K FundedNext Compounding Sim ──
    print("\n" + "=" * 75)
    print("  FUNDEDNEXT $100K COMPOUNDING SIMULATION")
    print("  50% of monthly payout reinvested to new accounts")
    print("=" * 75)
    funded_balance = 100_000
    sim_results, accounts = fundednext_compounding_sim(
        trades_df, funded_balance=funded_balance,
        reinvest_pct=0.50, payout_split=0.90)
    print_compounding_report(sim_results, accounts, funded_balance)

    # ── Save ──
    save_results = {k: v for k, v in results.items()
                    if not k.startswith("_")}
    json_path = output_dir / "metrics_combined_5index.json"
    with open(json_path, "w") as f:
        json.dump(save_results, f, indent=2, default=str)

    # Save full report to text file
    report_path = output_dir / "Combined_5Index_FundedNext_Report.txt"
    if hasattr(sys.stdout, 'getvalue'):
        report_text = sys.stdout.getvalue()
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)
        print(f"\n  Report saved : {report_path}")

    print(f"  Trades saved : {output_dir / 'combined_5index_trades.csv'}")
    print(f"  Metrics saved: {json_path}")
    print("=" * 75)


if __name__ == "__main__":
    tee = TeeWriter(sys.stdout)
    sys.stdout = tee
    try:
        main()
    finally:
        sys.stdout = tee.stdout
        # Write final output (including save messages) to report file
        report_dir = Path(__file__).parent / "reports" / "histdata_backtest"
        report_path = report_dir / "Combined_5Index_FundedNext_Report.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(tee.getvalue())
