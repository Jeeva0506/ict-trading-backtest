"""
Trail Buffer A/B Test: 1.0R (current) vs 0.5R (tighter trail)
==============================================================
Runs the full 16-year HistData backtest with both trail buffer values
and compares results side by side.
"""

import sys
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

DATA_FILES = {
    "SPX500": "SPX500_10YR_M1_CLEAN.csv",
    "NDX100": "NDX100_10YR_M1_CLEAN.csv",
    "GER30":  "GER30_10YR_M1_CLEAN.csv",
}

GER30_CLEAN_PERIODS = {
    "GER30_A": {"label": "GER30 Period A", "start": date(2010, 11, 1), "end": date(2020, 6, 12)},
    "GER30_B": {"label": "GER30 Period B", "start": date(2023, 12, 4), "end": date(2026, 12, 31)},
}


def load_and_filter(symbol, start_date=None, end_date=None):
    data_file = DATA_FILES.get(symbol)
    if data_file is None:
        return None
    try:
        df = load_data(data_file)
    except FileNotFoundError:
        return None
    if start_date:
        df = df[df.index.date >= start_date]
    if end_date:
        df = df[df.index.date <= end_date]
    return df if len(df) > 0 else None


def generate_signals(symbol, df, label, trail_buffer_r=1.0):
    """Generate signals with a specific trail_buffer_r."""
    cfg = LIVE_INDEX_CONFIG.get(symbol)
    if cfg is None:
        return []
    htf_ema = compute_htf_ema(df, fast=EMA_FAST, slow=EMA_SLOW)
    trading_days = sorted(set(df.index.date))
    point_value = BROKER_POINT_VALUES[symbol]
    signals = []

    for day in trading_days:
        session_start = pd.Timestamp(day).replace(
            hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
        session_end = pd.Timestamp(day).replace(
            hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
        market_df = df[(df.index >= session_start) & (df.index <= session_end)].copy()
        if len(market_df) < 80:
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
            trail_buffer_r=trail_buffer_r,
        )
        day_signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
        for s in day_signals:
            signals.append({
                "symbol": symbol, "segment": label, "signal": s,
                "point_value": point_value,
                "entry_time": pd.Timestamp(s.entry_time),
                "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
            })
    return signals


def generate_all_signals(trail_buffer_r=1.0):
    """Generate signals for all 3 HistData indices."""
    all_signals = []

    # SPX500
    df = load_and_filter("SPX500")
    if df is not None:
        all_signals.extend(generate_signals("SPX500", df, "SPX500", trail_buffer_r))

    # NDX100
    df = load_and_filter("NDX100")
    if df is not None:
        all_signals.extend(generate_signals("NDX100", df, "NDX100", trail_buffer_r))

    # GER30 (split)
    for pkey, pcfg in GER30_CLEAN_PERIODS.items():
        df = load_and_filter("GER30", start_date=pcfg["start"], end_date=pcfg["end"])
        if df is not None:
            all_signals.extend(generate_signals("GER30", df, pcfg["label"], trail_buffer_r))

    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals


def run_portfolio(all_signals):
    """Portfolio sim — returns metrics dict."""
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    MAX_DD_PCT = ACCOUNT_RULES.get("max_drawdown_pct", 10.0)
    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL

    daily_losses = {}
    daily_trade_counts = {}
    open_positions = []
    account_terminated = False
    account_resets = 0

    _r_by_sym = defaultdict(list)
    for sig in all_signals:
        _r_by_sym[sig["symbol"]].append(float(sig["signal"].risk_points))
    median_R = {sym: float(np.median(rs)) for sym, rs in _r_by_sym.items()}

    current_day = None
    day_peak_balance = STARTING_CAPITAL
    day_start_balance = STARTING_CAPITAL

    results = []
    exit_counts = defaultdict(int)
    exit_pnl_sums = defaultdict(float)
    daily_returns = []
    prev_balance = STARTING_CAPITAL
    skipped = defaultdict(int)
    wins = 0
    losses = 0
    gross_win = 0.0
    gross_loss = 0.0
    max_dd_pct = 0.0
    max_consec_loss = 0
    cur_consec_loss = 0

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
            prev_balance = STARTING_CAPITAL

        s = sig["signal"]
        symbol = sig["symbol"]
        point_value = sig["point_value"]
        entry_time = sig["entry_time"]
        exit_time = sig["exit_time"]
        trade_day = entry_time.date()
        risk_points = float(s.risk_points)

        if trade_day != current_day:
            if current_day is not None:
                daily_returns.append(current_balance / prev_balance - 1)
                prev_balance = current_balance
            current_day = trade_day
            day_peak_balance = current_balance
            day_start_balance = current_balance

        day_peak_balance = max(day_peak_balance, current_balance)

        if daily_losses.get(trade_day, 0) >= MAX_DAILY_LOSSES:
            skipped["daily_loss_limit"] += 1
            continue
        if daily_trade_counts.get(trade_day, 0) >= MAX_DAILY_TRADES:
            skipped["max_daily_trades"] += 1
            continue
        open_positions = [p for p in open_positions if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            skipped["max_positions"] += 1
            continue

        MIN_R_BY_SYMBOL = {"SPX500": 3.0, "US2000": 3.0}
        MIN_R_POINTS = MIN_R_BY_SYMBOL.get(symbol, 5.0)
        if risk_points < MIN_R_POINTS:
            skipped["min_R"] += 1
            continue

        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        if risk_points > 0 and (spread_pts / risk_points) > 0.30:
            skipped["spread_guard"] += 1
            continue

        breakout_time = getattr(s, 'breakout_time', None)
        if breakout_time is not None:
            bars_delay = int((pd.Timestamp(s.entry_time) - pd.Timestamp(breakout_time)).total_seconds() / 60)
            if bars_delay > 15:
                skipped["stale_signal"] += 1
                continue

        signal_price = getattr(s, 'signal_price', None)
        if signal_price is not None and risk_points > 0:
            drift = abs(float(s.entry_price) - float(signal_price))
            if drift > 0.5 * risk_points:
                skipped["price_drift"] += 1
                continue

        risk_pct, should_stop = get_adaptive_risk(
            current_balance, day_peak_balance, starting_balance=STARTING_CAPITAL)
        if should_stop:
            skipped["adaptive_stop"] += 1
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
            base_slippage, risk_points, median_R.get(symbol, risk_points), high_vol)
        half_spread = spread_pts / 2.0
        entry_cost_pts = slippage_pts + half_spread

        if exit_reason == "TP":
            exit_cost_pts = half_spread
        else:
            exit_cost_pts = slippage_pts + half_spread

        total_cost_pts = entry_cost_pts + exit_cost_pts
        commission_cost = TRADING_COSTS["commission_per_lot"].get(symbol, 0.0) * lots
        pnl_raw = pnl_points * lots * point_value
        total_costs = total_cost_pts * lots * point_value + commission_cost
        pnl = pnl_raw - total_costs

        current_balance += pnl
        peak_balance = max(peak_balance, current_balance)

        overall_dd_pct = ((STARTING_CAPITAL - current_balance) / STARTING_CAPITAL) * 100
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True

        trailing_dd = (peak_balance - current_balance) / peak_balance * 100 if peak_balance > 0 else 0
        max_dd_pct = max(max_dd_pct, trailing_dd)

        if pnl > 0:
            daily_losses[trade_day] = daily_losses.get(trade_day, 0)
            wins += 1
            gross_win += pnl
            cur_consec_loss = 0
        else:
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1
            losses += 1
            gross_loss += abs(pnl)
            cur_consec_loss += 1
            max_consec_loss = max(max_consec_loss, cur_consec_loss)

        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1
        if exit_time is not None:
            open_positions.append({"exit_time": exit_time})

        exit_counts[exit_reason] += 1
        exit_pnl_sums[exit_reason] += pnl

    total = wins + losses
    wr = wins / total * 100 if total > 0 else 0
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
    net = current_balance - STARTING_CAPITAL

    # Sharpe
    if daily_returns:
        dr = np.array(daily_returns)
        sharpe = (dr.mean() / dr.std() * np.sqrt(252)) if dr.std() > 0 else 0
    else:
        sharpe = 0

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "profit_factor": pf,
        "net_pnl": net,
        "final_balance": current_balance,
        "max_dd_pct": max_dd_pct,
        "sharpe": sharpe,
        "max_consec_loss": max_consec_loss,
        "resets": account_resets,
        "exit_counts": dict(exit_counts),
        "exit_avg_pnl": {k: exit_pnl_sums[k] / exit_counts[k] for k in exit_counts},
        "skipped": dict(skipped),
    }


def main():
    print("=" * 70)
    print("  TRAIL BUFFER A/B TEST: 0.5R (current) vs 0.75R")
    print("=" * 70)

    # --- Variant A: trail_buffer = 0.5R (current) ---
    print("\n[A] Generating signals with trail_buffer = 0.5R ...")
    sigs_a = generate_all_signals(trail_buffer_r=0.5)
    print(f"    {len(sigs_a)} raw signals")
    print("    Running portfolio ...")
    res_a = run_portfolio(sigs_a)

    # --- Variant B: trail_buffer = 0.75R ---
    print("\n[B] Generating signals with trail_buffer = 0.75R ...")
    sigs_b = generate_all_signals(trail_buffer_r=0.75)
    print(f"    {len(sigs_b)} raw signals")
    print("    Running portfolio ...")
    res_b = run_portfolio(sigs_b)

    # --- Print comparison ---
    print("\n")
    print("=" * 70)
    print("  RESULTS COMPARISON")
    print("=" * 70)

    header = f"  {'Metric':<25} {'0.5R buffer':>15} {'0.75R buffer':>15} {'Diff':>12}"
    print(header)
    print("  " + "-" * 67)

    rows = [
        ("Trades",       f"{res_a['trades']}",       f"{res_b['trades']}"),
        ("Win Rate",     f"{res_a['win_rate']:.1f}%", f"{res_b['win_rate']:.1f}%"),
        ("Profit Factor", f"{res_a['profit_factor']:.2f}", f"{res_b['profit_factor']:.2f}"),
        ("Net P&L",      f"${res_a['net_pnl']:,.0f}", f"${res_b['net_pnl']:,.0f}"),
        ("Final Balance", f"${res_a['final_balance']:,.0f}", f"${res_b['final_balance']:,.0f}"),
        ("Max DD %",     f"{res_a['max_dd_pct']:.1f}%", f"{res_b['max_dd_pct']:.1f}%"),
        ("Sharpe",       f"{res_a['sharpe']:.2f}",   f"{res_b['sharpe']:.2f}"),
        ("Max Consec L", f"{res_a['max_consec_loss']}", f"{res_b['max_consec_loss']}"),
        ("Resets",       f"{res_a['resets']}",        f"{res_b['resets']}"),
    ]

    for label, va, vb in rows:
        # Compute numeric diff where possible
        try:
            fa = float(va.replace('$','').replace(',','').replace('%',''))
            fb = float(vb.replace('$','').replace(',','').replace('%',''))
            diff = fb - fa
            sign = "+" if diff >= 0 else ""
            if '%' in va:
                diff_str = f"{sign}{diff:.1f}%"
            elif '$' in va:
                diff_str = f"{sign}${diff:,.0f}"
            else:
                diff_str = f"{sign}{diff:.2f}"
        except Exception:
            diff_str = ""
        print(f"  {label:<25} {va:>15} {vb:>15} {diff_str:>12}")

    # Exit reason breakdown
    print("\n  EXIT REASON BREAKDOWN:")
    print(f"  {'Exit':<18} {'0.5R count':>10} {'0.5R avg$':>12} {'0.75R count':>10} {'0.75R avg$':>12}")
    print("  " + "-" * 62)
    all_exits = set(list(res_a["exit_counts"].keys()) + list(res_b["exit_counts"].keys()))
    for ex in sorted(all_exits):
        ca = res_a["exit_counts"].get(ex, 0)
        cb = res_b["exit_counts"].get(ex, 0)
        aa = res_a["exit_avg_pnl"].get(ex, 0)
        ab = res_b["exit_avg_pnl"].get(ex, 0)
        print(f"  {ex:<18} {ca:>10} {aa:>12,.0f} {cb:>10} {ab:>12,.0f}")

    print("\n" + "=" * 70)
    print("  INTERPRETATION:")
    print("=" * 70)

    pf_diff = res_b["profit_factor"] - res_a["profit_factor"]
    pnl_diff = res_b["net_pnl"] - res_a["net_pnl"]
    wr_diff = res_b["win_rate"] - res_a["win_rate"]

    if pnl_diff > 0:
        print(f"  0.75R buffer earned ${pnl_diff:,.0f} MORE ({pnl_diff/max(res_a['net_pnl'],1)*100:.1f}%)")
    else:
        print(f"  0.75R buffer earned ${abs(pnl_diff):,.0f} LESS ({abs(pnl_diff)/max(res_a['net_pnl'],1)*100:.1f}%)")

    print(f"  Win rate change: {wr_diff:+.1f}%")
    print(f"  PF change: {pf_diff:+.2f}")

    if res_b["profit_factor"] > res_a["profit_factor"] and res_b["net_pnl"] > res_a["net_pnl"]:
        print("  -> 0.75R buffer is BETTER on both PF and PnL")
    elif res_b["profit_factor"] < res_a["profit_factor"] and res_b["net_pnl"] < res_a["net_pnl"]:
        print("  -> 0.5R buffer is BETTER on both PF and PnL (keep current)")
    else:
        print("  -> Mixed result — check tradeoffs above")

    print()


if __name__ == "__main__":
    main()
