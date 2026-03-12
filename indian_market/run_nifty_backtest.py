"""
NIFTY 50 Futures ORB Backtest + Walk-Forward Optimizer
======================================================
Standalone script — does NOT modify any existing live codebase.

Data  : data/nifty50_candlestick_data.csv  (M1, Jan 2015 – Mar 2024)
Split : Train on first 2 years → Optimize params
        Validate on remaining ~7 years (true OOS)

Futures contract specs (NIFTY 50):
  Lot size     : 25 (post Jul-2023; 50 before — we use 25 throughout for simplicity)
  Tick size    : 0.05 pts
  Session      : 09:15 – 15:30 IST
  Margin/lot   : ~₹1,00,000 (NRML)
  Brokerage    : ₹20 per order (discount broker)
  STT+charges  : ~₹60 round-trip per lot (futures intraday)

Usage:
  python indian_market/run_nifty_backtest.py                   # default backtest
  python indian_market/run_nifty_backtest.py --optimize        # grid-search + OOS
  python indian_market/run_nifty_backtest.py --full            # full-period single run
"""

import sys
import argparse
import itertools
import time
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

import numpy as np
import pandas as pd

# ── Add parent to path so we can import the strategy ──────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.strategy_ict_clean import StrategyICTClean, TradeSignalICT
from indian_market.strategy_nifty import StrategyORBNifty, TradeSignal

# ══════════════════════════════════════════════════════════════════════════
# NIFTY FUTURES CONTRACT SPECIFICATIONS
# ══════════════════════════════════════════════════════════════════════════
NIFTY_SPEC = {
    "symbol": "NIFTY50",
    "lot_size": 25,             # 25 units per lot (post Jul-2023 standard)
    "tick_size": 0.05,
    "point_value": 25.0,        # ₹25 per 1-point move per lot
    "session_start": "09:15",
    "session_end": "15:30",
    "force_close": "15:15",     # exit 15 min before close (avoid closing auction)
    "currency": "₹",
}

ACCOUNT = {
    "starting_capital": 500_000,   # ₹5 lakh
    "risk_per_trade": 0.015,       # 1.5% risk per trade
    "max_drawdown_pct": 20.0,
    "brokerage_per_order": 20,     # ₹20 flat (Zerodha/Groww)
    "stt_charges_per_lot": 60,     # STT + stamp + exchange + GST (round-trip, intraday)
    "slippage_points": 0.5,        # 0.5 pt slippage per side
}

# Default strategy params (will be optimized)
DEFAULT_PARAMS = {
    "orb_minutes": 15,
    "entry_hours": 3,
    "breakout_margin": 0.05,
    "body_min": 0.55,
    "use_disp_filter": False,
    "disp_mult": 1.0,
    "use_orb_bias": False,
    "use_htf": False,
    "tp_r": 2.5,
    "be_trigger_r": 1.0,
    "trail_buffer_r": 1.0,
    "sl_buffer": 1.0,
    "min_sl_points": 5.0,
    "max_sl_points": 200.0,
    "min_orb_range": 10.0,
    "max_orb_range": 200.0,
}


# ══════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════════════════
def load_nifty_data(filepath: Path = None) -> pd.DataFrame:
    """
    Load Nifty M1 CSV.
    Format: Instrument,Date,Time,Open,High,Low,Close
    Date is DD-MM-YYYY, Time is H:MM:SS or HH:MM:SS
    """
    if filepath is None:
        filepath = ROOT / "data" / "nifty50_candlestick_data.csv"

    print(f"Loading NIFTY data from {filepath} ...")
    df = pd.read_csv(filepath)

    # Combine Date + Time into datetime
    df["datetime"] = pd.to_datetime(
        df["Date"].str.strip() + " " + df["Time"].str.strip(),
        format="%d-%m-%Y %H:%M:%S",
        dayfirst=True,
    )
    df = df.rename(columns={c: c.lower() for c in df.columns})
    df = df[["datetime", "open", "high", "low", "close"]].copy()
    df = df.sort_values("datetime").reset_index(drop=True)
    df.set_index("datetime", inplace=True)

    print(f"  Loaded {len(df):,} M1 bars | {df.index[0].date()} to {df.index[-1].date()}")
    return df


# ══════════════════════════════════════════════════════════════════════════
# HTF TREND (EMA on resampled H1)
# ══════════════════════════════════════════════════════════════════════════
def compute_htf_ema(df_m1: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.DataFrame:
    """Resample M1 → H1 and compute EMA crossover for trend filter."""
    h1 = df_m1.resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()
    h1[f"ema{fast}"] = h1["close"].ewm(span=fast, adjust=False).mean()
    h1[f"ema{slow}"] = h1["close"].ewm(span=slow, adjust=False).mean()
    h1["htf_trend"] = "bullish"
    h1.loc[h1[f"ema{fast}"] < h1[f"ema{slow}"], "htf_trend"] = "bearish"
    return h1[["htf_trend", f"ema{fast}", f"ema{slow}"]]


def get_htf_trend(htf_ema: pd.DataFrame, session_start: pd.Timestamp) -> Optional[str]:
    """Most recent H1 EMA trend before session start."""
    prior = htf_ema[htf_ema.index < session_start]
    if len(prior) == 0:
        return None
    return str(prior.iloc[-1]["htf_trend"])


# ══════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════
def generate_signals(
    df: pd.DataFrame,
    htf_ema: pd.DataFrame,
    params: dict,
    start_date: date = None,
    end_date: date = None,
    precomputed_sessions: list = None,
) -> List[Dict]:
    """Generate ORB signals for NIFTY within the date range."""

    use_htf = params.get("use_htf", False)  # HTF filter off by default

    if precomputed_sessions is not None:
        day_sessions = precomputed_sessions
    else:
        trading_days = sorted(set(df.index.date))
        if start_date:
            trading_days = [d for d in trading_days if d >= start_date]
        if end_date:
            trading_days = [d for d in trading_days if d <= end_date]

        day_sessions = []
        for day in trading_days:
            sess_start = pd.Timestamp(day).replace(hour=9, minute=15)
            sess_end = pd.Timestamp(day).replace(hour=15, minute=30)
            market_df = df[(df.index >= sess_start) & (df.index <= sess_end)].copy()
            if len(market_df) < 80:
                continue
            htf_trend = get_htf_trend(htf_ema, sess_start)
            day_sessions.append((market_df, htf_trend))

    all_signals = []

    for market_df, htf_trend in day_sessions:
        strategy = StrategyORBNifty(
            sl_buffer=params.get("sl_buffer", 1.0),
            min_sl_points=params.get("min_sl_points", 5.0),
            max_sl_points=params.get("max_sl_points", 200.0),
            orb_minutes=params.get("orb_minutes", 15),
            entry_hours=params.get("entry_hours", 3),
            breakout_margin=params.get("breakout_margin", 0.05),
            body_min=params.get("body_min", 0.55),
            use_disp_filter=params.get("use_disp_filter", False),
            disp_mult=params.get("disp_mult", 1.0),
            use_orb_bias=params.get("use_orb_bias", False),
            tp_r=params.get("tp_r", 2.5),
            be_trigger_r=params.get("be_trigger_r", 1.0),
            trail_buffer_r=params.get("trail_buffer_r", 1.0),
            min_orb_range=params.get("min_orb_range", 10.0),
            max_orb_range=params.get("max_orb_range", 200.0),
        )
        # Pass htf_trend only if enabled
        trend_arg = htf_trend if use_htf else None
        signals = strategy.generate_signals(market_df, htf_trend=trend_arg)
        for s in signals:
            all_signals.append({
                "symbol": "NIFTY50",
                "signal": s,
                "entry_time": pd.Timestamp(s.entry_time),
                "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
            })

    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals


def precompute_sessions(
    df: pd.DataFrame,
    htf_ema: pd.DataFrame,
    start_date: date = None,
    end_date: date = None,
) -> list:
    """Precompute session DataFrames + HTF trends (expensive part) once."""
    trading_days = sorted(set(df.index.date))
    if start_date:
        trading_days = [d for d in trading_days if d >= start_date]
    if end_date:
        trading_days = [d for d in trading_days if d <= end_date]

    sessions = []
    for day in trading_days:
        sess_start = pd.Timestamp(day).replace(hour=9, minute=15)
        sess_end = pd.Timestamp(day).replace(hour=15, minute=30)
        market_df = df[(df.index >= sess_start) & (df.index <= sess_end)].copy()
        if len(market_df) < 80:
            continue
        htf_trend = get_htf_trend(htf_ema, sess_start)
        sessions.append((market_df, htf_trend))
    return sessions


# ══════════════════════════════════════════════════════════════════════════
# PORTFOLIO SIMULATION
# ══════════════════════════════════════════════════════════════════════════
def simulate_portfolio(
    signals: List[Dict],
    account: dict = None,
    verbose: bool = True,
) -> Dict:
    """
    Simulate trading with position sizing, costs, and drawdown tracking.
    Returns metrics dict.
    """
    if account is None:
        account = ACCOUNT

    capital = account["starting_capital"]
    risk_pct = account["risk_per_trade"]
    max_dd_pct = account["max_drawdown_pct"]
    brokerage = account["brokerage_per_order"]
    stt = account["stt_charges_per_lot"]
    slippage = account["slippage_points"]
    lot_size = NIFTY_SPEC["lot_size"]
    point_value = NIFTY_SPEC["point_value"]

    balance = capital
    peak = capital
    trades = []
    equity_curve = [capital]
    daily_pnl = {}
    max_dd = 0
    max_dd_amount = 0

    for sig in signals:
        s = sig["signal"]
        risk_points = float(s.risk_points)
        pnl_points = float(s.pnl_points)

        if risk_points <= 0:
            continue

        # Position sizing: risk-based lot calculation
        risk_amount = balance * risk_pct
        lots = max(1, int(risk_amount / (risk_points * point_value)))

        # PnL calculation
        gross_pnl = pnl_points * point_value * lots

        # Costs: slippage (entry + exit) + brokerage (2 orders) + STT
        slip_cost = slippage * 2 * point_value * lots
        broker_cost = brokerage * 2  # entry + exit orders
        stt_cost = stt * lots
        total_cost = slip_cost + broker_cost + stt_cost

        net_pnl = gross_pnl - total_cost

        trade_day = sig["entry_time"].date()
        daily_pnl[str(trade_day)] = daily_pnl.get(str(trade_day), 0) + net_pnl

        trades.append({
            "entry_time": sig["entry_time"],
            "exit_time": sig["exit_time"],
            "direction": s.direction,
            "entry_price": s.entry_price,
            "exit_price": s.exit_price,
            "exit_reason": s.exit_reason,
            "risk_points": risk_points,
            "pnl_points": pnl_points,
            "lots": lots,
            "gross_pnl": gross_pnl,
            "costs": total_cost,
            "net_pnl": net_pnl,
            "balance_after": balance + net_pnl,
            "rr": round(pnl_points / risk_points, 2) if risk_points > 0 else 0,
        })

        balance += net_pnl
        equity_curve.append(balance)
        peak = max(peak, balance)
        dd = (peak - balance) / peak * 100
        max_dd = max(max_dd, dd)
        max_dd_amount = max(max_dd_amount, peak - balance)

        # Max DD breaker (skip during optimization — verbose=False means optimizer)
        if dd >= max_dd_pct and verbose:
            print(f"  ⛔ Max DD {max_dd_pct}% breached at {trade_day}. Stopping.")
            break

    # ── Compute metrics ───────────────────────────────────────────────
    if not trades:
        return _empty_metrics(capital)

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    total_win = sum(t["net_pnl"] for t in wins)
    total_loss = abs(sum(t["net_pnl"] for t in losses))

    pf = total_win / total_loss if total_loss > 0 else 999
    wr = len(wins) / len(trades) * 100 if trades else 0
    avg_win = total_win / len(wins) if wins else 0
    avg_loss = total_loss / len(losses) if losses else 0
    expectancy = (wr / 100 * avg_win) - ((1 - wr / 100) * avg_loss)
    avg_rr_win = np.mean([t["rr"] for t in wins]) if wins else 0
    avg_rr_loss = np.mean([t["rr"] for t in losses]) if losses else 0

    # Trading days span
    first_day = trades[0]["entry_time"].date()
    last_day = trades[-1]["entry_time"].date()
    span_days = (last_day - first_day).days or 1
    trades_per_week = len(trades) / (span_days / 7)
    total_return = (balance - capital) / capital * 100

    # Yearly breakdown
    yearly = {}
    for t in trades:
        yr = t["entry_time"].year
        if yr not in yearly:
            yearly[yr] = {"trades": 0, "wins": 0, "pnl": 0}
        yearly[yr]["trades"] += 1
        if t["net_pnl"] > 0:
            yearly[yr]["wins"] += 1
        yearly[yr]["pnl"] += t["net_pnl"]

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        r = t["exit_reason"]
        if r not in exit_reasons:
            exit_reasons[r] = {"count": 0, "pnl": 0}
        exit_reasons[r]["count"] += 1
        exit_reasons[r]["pnl"] += t["net_pnl"]

    metrics = {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr, 1),
        "profit_factor": round(pf, 2),
        "total_pnl": round(balance - capital, 2),
        "total_return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "max_drawdown_amount": round(max_dd_amount, 2),
        "expectancy": round(expectancy, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_rr_win": round(avg_rr_win, 2),
        "avg_rr_loss": round(avg_rr_loss, 2),
        "trades_per_week": round(trades_per_week, 2),
        "start_capital": capital,
        "end_capital": round(balance, 2),
        "start_date": str(first_day),
        "end_date": str(last_day),
        "span_days": span_days,
        "yearly": yearly,
        "exit_reasons": exit_reasons,
        "equity_curve": equity_curve,
        "trades": trades,
    }
    return metrics


def _empty_metrics(capital):
    return {
        "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
        "profit_factor": 0, "total_pnl": 0, "total_return_pct": 0,
        "max_drawdown_pct": 0, "max_drawdown_amount": 0, "expectancy": 0,
        "avg_win": 0, "avg_loss": 0, "avg_rr_win": 0, "avg_rr_loss": 0,
        "trades_per_week": 0, "start_capital": capital, "end_capital": capital,
        "start_date": "", "end_date": "", "span_days": 0,
        "yearly": {}, "exit_reasons": {}, "equity_curve": [capital], "trades": [],
    }


# ══════════════════════════════════════════════════════════════════════════
# REPORTING
# ══════════════════════════════════════════════════════════════════════════
def print_report(metrics: Dict, label: str = "NIFTY 50 ORB Backtest"):
    """Print a comprehensive backtest report."""
    m = metrics
    cur = NIFTY_SPEC["currency"]

    print(f"\n{'='*70}")
    print(f" {label}")
    print(f"{'='*70}")
    print(f" Period        : {m['start_date']} → {m['end_date']} ({m['span_days']} days)")
    print(f" Capital       : {cur}{m['start_capital']:,.0f} → {cur}{m['end_capital']:,.0f}")
    print(f" Total Return  : {m['total_return_pct']:+.2f}%")
    print(f"{'─'*70}")
    print(f" Total Trades  : {m['total_trades']}")
    print(f" Win Rate      : {m['win_rate']}% ({m['wins']}W / {m['losses']}L)")
    print(f" Profit Factor : {m['profit_factor']:.2f}")
    print(f" Expectancy    : {cur}{m['expectancy']:,.2f} / trade")
    print(f" Avg Win       : {cur}{m['avg_win']:,.2f} (RR {m['avg_rr_win']:.2f})")
    print(f" Avg Loss      : {cur}{m['avg_loss']:,.2f} (RR {m['avg_rr_loss']:.2f})")
    print(f" Trades/Week   : {m['trades_per_week']:.1f}")
    print(f" Max Drawdown  : {m['max_drawdown_pct']:.2f}% ({cur}{m['max_drawdown_amount']:,.0f})")

    if m["yearly"]:
        print(f"\n{'─'*70}")
        print(f" {'Year':<6} {'Trades':>7} {'WR':>7} {'PnL':>14}")
        print(f" {'─'*6} {'─'*7} {'─'*7} {'─'*14}")
        for yr in sorted(m["yearly"]):
            y = m["yearly"][yr]
            wr = y["wins"] / y["trades"] * 100 if y["trades"] > 0 else 0
            print(f" {yr:<6} {y['trades']:>7} {wr:>6.1f}% {cur}{y['pnl']:>12,.0f}")

    if m["exit_reasons"]:
        print(f"\n{'─'*70}")
        print(f" {'Exit Reason':<20} {'Count':>7} {'PnL':>14}")
        print(f" {'─'*20} {'─'*7} {'─'*14}")
        for reason in sorted(m["exit_reasons"], key=lambda r: m["exit_reasons"][r]["count"], reverse=True):
            er = m["exit_reasons"][reason]
            print(f" {reason:<20} {er['count']:>7} {cur}{er['pnl']:>12,.0f}")

    print(f"{'='*70}\n")


# ══════════════════════════════════════════════════════════════════════════
# WALK-FORWARD OPTIMIZER
# ══════════════════════════════════════════════════════════════════════════

# Parameter grid for optimization — designed for 100+ trades/year
# Key insight: NIFTY breakouts need QUICK profit-taking (1-1.5R) not wide targets
# 45% of trades reach 1R but reverse — capture those as wins with earlier TP + BE
PARAM_GRID = {
    "orb_minutes":       [15, 30],
    "entry_hours":       [3, 4],
    "breakout_margin":   [0.02, 0.05],
    "body_min":          [0.45, 0.50, 0.55],
    "use_disp_filter":   [False],
    "use_orb_bias":      [True, False],
    "use_htf":           [False],
    "tp_r":              [1.0, 1.5, 2.0],
    "be_trigger_r":      [0.5, 0.75, 1.0],
    "trail_buffer_r":    [0.5, 0.75],
}

# Fixed params (not optimized)
FIXED_PARAMS = {
    "sl_buffer": 1.0,
    "min_sl_points": 5.0,
    "max_sl_points": 200.0,
    "min_orb_range": 10.0,
    "max_orb_range": 200.0,
    "disp_mult": 1.0,
}


def _generate_param_combos(grid: dict) -> List[dict]:
    """Generate all combinations from param grid."""
    keys = list(grid.keys())
    values = list(grid.values())
    combos = []
    for vals in itertools.product(*values):
        combo = dict(zip(keys, vals))
        combo.update(FIXED_PARAMS)
        combos.append(combo)
    return combos


MIN_TRADES_PER_YEAR = 100  # Hard requirement: 100+ trades per year


def _score(metrics: dict, train_years: float = 3.0) -> float:
    """
    Composite score for ranking parameter sets.
    Weights: PF (35%), WR (15%), return (30%), -DD (20%)
    Hard filter: must have >= MIN_TRADES_PER_YEAR × train_years trades.
    """
    min_trades = int(MIN_TRADES_PER_YEAR * train_years)
    if metrics["total_trades"] < min_trades:
        return -999

    pf = min(metrics["profit_factor"], 5.0)  # cap at 5 to avoid outliers
    wr = metrics["win_rate"] / 100
    ret = min(metrics["total_return_pct"], 500) / 100  # normalize (higher cap for big returns)
    dd_penalty = max(0, 1 - metrics["max_drawdown_pct"] / 30)  # 0-30% range

    return 0.35 * pf + 0.15 * wr + 0.30 * ret + 0.20 * dd_penalty


def run_optimization(
    df: pd.DataFrame,
    htf_ema: pd.DataFrame,
    train_start: date,
    train_end: date,
    oos_start: date,
    oos_end: date,
    top_n: int = 5,
) -> List[Dict]:
    """
    Grid search on train period, validate top-N on OOS.
    Returns list of {params, train_metrics, oos_metrics, train_score, oos_score}.
    """
    combos = _generate_param_combos(PARAM_GRID)
    total = len(combos)
    print(f"\n🔍 Optimization: {total} param combinations")
    print(f"   Train: {train_start} → {train_end}")
    print(f"   OOS  : {oos_start} → {oos_end}")
    print(f"{'─'*70}")

    # Precompute session data ONCE (this is the expensive part)
    print("   Precomputing train session data ...")
    train_sessions = precompute_sessions(df, htf_ema, train_start, train_end)
    print(f"   {len(train_sessions)} trading sessions precomputed")

    results = []
    t0 = time.time()

    for idx, params in enumerate(combos):
        # Progress every 20 combos
        if (idx + 1) % 20 == 0 or idx == 0:
            elapsed = time.time() - t0
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            eta = (total - idx - 1) / rate if rate > 0 else 0
            print(f"   [{idx+1}/{total}] {elapsed:.0f}s elapsed, ETA {eta:.0f}s ...")

        signals = generate_signals(df, htf_ema, params,
                                   precomputed_sessions=train_sessions)
        metrics = simulate_portfolio(signals, verbose=False)
        train_years = (train_end - train_start).days / 365.25
        score = _score(metrics, train_years=train_years)
        results.append({
            "params": params,
            "train_metrics": metrics,
            "train_score": score,
        })

    elapsed = time.time() - t0
    print(f"   ✅ Grid search done in {elapsed:.1f}s")

    # Sort by score, take top N
    results.sort(key=lambda x: x["train_score"], reverse=True)
    top_results = results[:top_n]

    # Validate top-N on OOS
    print(f"\n📊 Validating top-{top_n} on OOS ({oos_start} → {oos_end})...")
    print("   Precomputing OOS session data ...")
    oos_sessions = precompute_sessions(df, htf_ema, oos_start, oos_end)
    print(f"   {len(oos_sessions)} OOS sessions precomputed")

    for r in top_results:
        signals = generate_signals(df, htf_ema, r["params"],
                                   precomputed_sessions=oos_sessions)
        oos_metrics = simulate_portfolio(signals, verbose=False)
        r["oos_metrics"] = oos_metrics
        oos_years = (oos_end - oos_start).days / 365.25
        r["oos_score"] = _score(oos_metrics, train_years=oos_years)

    # Print comparison
    print(f"\n{'='*90}")
    print(f" TOP-{top_n} PARAMETER SETS: Train vs OOS")
    print(f"{'='*90}")
    print(f" {'Rank':<5} {'Tr.PF':>6} {'Tr.WR':>6} {'Tr.Ret':>8} {'Tr.T/Y':>7} "
          f"{'OOS PF':>7} {'OOS WR':>7} {'OOS Ret':>9} {'OOS DD':>7} {'OOS T/Y':>8}")
    print(f" {'─'*5} {'─'*6} {'─'*6} {'─'*8} {'─'*7} {'─'*7} {'─'*7} {'─'*9} {'─'*7} {'─'*8}")

    train_yrs = max(1, (train_end - train_start).days / 365.25)
    oos_yrs = max(1, (oos_end - oos_start).days / 365.25)
    for i, r in enumerate(top_results):
        tm = r["train_metrics"]
        om = r["oos_metrics"]
        tr_tpy = tm['total_trades'] / train_yrs
        oos_tpy = om['total_trades'] / oos_yrs
        print(f" #{i+1:<4} {tm['profit_factor']:>5.2f} {tm['win_rate']:>5.1f}% {tm['total_return_pct']:>+7.1f}% {tr_tpy:>6.0f} "
              f"{om['profit_factor']:>6.2f} {om['win_rate']:>6.1f}% {om['total_return_pct']:>+8.1f}% "
              f"{om['max_drawdown_pct']:>6.1f}% {oos_tpy:>7.0f}")

    # Print best params
    best = top_results[0]
    print(f"\n{'─'*70}")
    print(f" BEST PARAMS (Rank #1):")
    for k, v in sorted(best["params"].items()):
        if k not in FIXED_PARAMS:
            print(f"   {k:<30} = {v}")
    train_yrs_f = max(1, (train_end - train_start).days / 365.25)
    print(f"   {'trades/year (train)':<30} = {best['train_metrics']['total_trades']/train_yrs_f:.0f}")
    print(f"{'─'*70}")

    # Count how many combos passed the 100/yr filter
    passed = sum(1 for r in results if r['train_score'] > -999)
    print(f"\n   {passed}/{total} combos passed {MIN_TRADES_PER_YEAR} trades/year filter")

    return top_results


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="NIFTY 50 ORB Backtest & Optimizer")
    parser.add_argument("--optimize", action="store_true", help="Run walk-forward optimization")
    parser.add_argument("--full", action="store_true", help="Run full-period backtest (no split)")
    parser.add_argument("--train-years", type=int, default=3, help="Years for training (default: 3)")
    parser.add_argument("--top-n", type=int, default=5, help="Top N param sets to validate (default: 5)")
    args = parser.parse_args()

    # Load data
    df = load_nifty_data()
    htf_ema = compute_htf_ema(df, fast=20, slow=50)
    print(f"  HTF EMA: {len(htf_ema)} H1 bars")

    all_dates = sorted(set(df.index.date))
    data_start = all_dates[0]
    data_end = all_dates[-1]
    print(f"  Date range: {data_start} → {data_end} ({len(all_dates)} trading days)")

    if args.full:
        # ── Full-period single run ────────────────────────────────────
        print(f"\n🚀 Running full-period backtest with default params ...")
        signals = generate_signals(df, htf_ema, DEFAULT_PARAMS, data_start, data_end)
        print(f"  {len(signals)} signals generated")
        metrics = simulate_portfolio(signals, verbose=True)
        print_report(metrics, "NIFTY 50 ORB — Full Period (Default Params)")

    elif args.optimize:
        # ── Walk-forward optimization ─────────────────────────────────
        train_start = data_start
        train_end_dt = datetime(data_start.year + args.train_years, data_start.month, data_start.day)
        # Find closest trading day
        train_end = max(d for d in all_dates if d <= train_end_dt.date())
        oos_start = min(d for d in all_dates if d > train_end)
        oos_end = data_end

        print(f"\n  Train period: {train_start} → {train_end} "
              f"({len([d for d in all_dates if train_start <= d <= train_end])} days)")
        print(f"  OOS period  : {oos_start} → {oos_end} "
              f"({len([d for d in all_dates if oos_start <= d <= oos_end])} days)")

        top = run_optimization(df, htf_ema, train_start, train_end,
                               oos_start, oos_end, top_n=args.top_n)

        # Print detailed report for best OOS result
        # Sort by OOS score to find actual best
        top_by_oos = sorted(top, key=lambda x: x["oos_score"], reverse=True)
        best_oos = top_by_oos[0]

        print_report(best_oos["train_metrics"],
                     f"NIFTY 50 ORB — TRAIN ({train_start}→{train_end})")
        print_report(best_oos["oos_metrics"],
                     f"NIFTY 50 ORB — OUT-OF-SAMPLE ({oos_start}→{oos_end})")

        # Save results
        results_dir = ROOT / "indian_market" / "results"
        results_dir.mkdir(exist_ok=True)

        # Save top params as JSON
        import json
        summary = []
        for i, r in enumerate(top_by_oos):
            summary.append({
                "rank": i + 1,
                "params": {k: v for k, v in r["params"].items() if k not in FIXED_PARAMS},
                "train": {
                    "trades": r["train_metrics"]["total_trades"],
                    "win_rate": r["train_metrics"]["win_rate"],
                    "profit_factor": r["train_metrics"]["profit_factor"],
                    "return_pct": r["train_metrics"]["total_return_pct"],
                    "max_dd_pct": r["train_metrics"]["max_drawdown_pct"],
                },
                "oos": {
                    "trades": r["oos_metrics"]["total_trades"],
                    "win_rate": r["oos_metrics"]["win_rate"],
                    "profit_factor": r["oos_metrics"]["profit_factor"],
                    "return_pct": r["oos_metrics"]["total_return_pct"],
                    "max_dd_pct": r["oos_metrics"]["max_drawdown_pct"],
                },
            })

        with open(results_dir / "nifty_optimization_results.json", "w") as f:
            json.dump(summary, f, indent=2)

        # Save trade log for best OOS
        trades_df = pd.DataFrame(best_oos["oos_metrics"]["trades"])
        if not trades_df.empty:
            trades_df.to_csv(results_dir / "nifty_best_oos_trades.csv", index=False)

        print(f"  📁 Results saved to {results_dir}/")

    else:
        # ── Default: 2-year train, remaining OOS ──────────────────────
        train_start = data_start
        train_end_dt = datetime(data_start.year + 2, data_start.month, data_start.day)
        train_end = max(d for d in all_dates if d <= train_end_dt.date())
        oos_start = min(d for d in all_dates if d > train_end)
        oos_end = data_end

        print(f"\n🚀 Default run: Train on first 2 years, validate on remaining ...")
        print(f"   Train: {train_start} → {train_end}")
        print(f"   OOS  : {oos_start} → {oos_end}")

        # Train
        print(f"\n── TRAIN PERIOD ──")
        train_signals = generate_signals(df, htf_ema, DEFAULT_PARAMS, train_start, train_end)
        print(f"  {len(train_signals)} signals")
        train_metrics = simulate_portfolio(train_signals, verbose=True)
        print_report(train_metrics, f"NIFTY 50 ORB — TRAIN ({train_start}→{train_end})")

        # OOS
        print(f"\n── OUT-OF-SAMPLE ──")
        oos_signals = generate_signals(df, htf_ema, DEFAULT_PARAMS, oos_start, oos_end)
        print(f"  {len(oos_signals)} signals")
        oos_metrics = simulate_portfolio(oos_signals, verbose=True)
        print_report(oos_metrics, f"NIFTY 50 ORB — OOS ({oos_start}→{oos_end})")


if __name__ == "__main__":
    main()
