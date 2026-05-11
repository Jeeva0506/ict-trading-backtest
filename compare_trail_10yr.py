"""
10-Year Trail SL Config Comparison
====================================
Compares CURRENT live config vs OPTIMAL config on 10-year data.

CURRENT:  BE=1.0R, TRAIL=0.5R  (tp1_r=1.0, trail_buffer_r=0.5)
OPTIMAL:  BE=0.75R, TRAIL=1.0R (tp1_r=0.75, trail_buffer_r=1.0)

Uses pre-built 10YR CSVs directly — no data assembly needed.
"""

import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, ADAPTIVE_RISK,
    ACCOUNT_RULES, MAX_OPEN_POSITIONS, MAX_DAILY_LOSSES, MAX_DAILY_TRADES,
    load_data, compute_htf_ema, get_htf_trend_for_day,
    get_adaptive_risk, StrategyICTClean,
    get_vol_scaled_slippage, is_high_vol_event_day,
)

EMA_FAST = 20
EMA_SLOW = 50

SYMBOLS = ["SPX500", "NDX100", "GER30", "US30", "FRA40"]

DATA_FILES = {
    "SPX500": "SPX500_10YR_M1_CLEAN.csv",
    "NDX100": "NDX100_10YR_M1_CLEAN.csv",
    "GER30":  "GER30_10YR_M1_CLEAN.csv",
    "US30":   "US30_10YR_M1_CLEAN.csv",
    "FRA40":  "FRA40_10YR_M1_CLEAN.csv",
}

CONFIGS = [
    {"label": "CURRENT  (BE=1.0R TRAIL=0.5R)", "tp1_r": 1.0,  "trail_buffer_r": 0.5},
    {"label": "OPTIMAL  (BE=0.75R TRAIL=1.0R)", "tp1_r": 0.75, "trail_buffer_r": 1.0},
]


def generate_signals(symbols, tp1_r, trail_buffer_r):
    all_signals = []
    for sym in symbols:
        cfg = LIVE_INDEX_CONFIG.get(sym)
        if cfg is None:
            continue
        data_file = DATA_FILES[sym]
        try:
            df = load_data(data_file)
        except FileNotFoundError:
            print(f"  {sym}: {data_file} NOT FOUND, skipping")
            continue

        htf_ema = compute_htf_ema(df, fast=EMA_FAST, slow=EMA_SLOW)
        trading_days = sorted(set(df.index.date))
        point_value = BROKER_POINT_VALUES[sym]
        count = 0

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
                orb_minutes=cfg.get("orb_minutes_opt", 10),
                entry_hours=cfg.get("entry_hours", 2),
                breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
                lookback_ranges=cfg.get("lookback_ranges_opt", 6),
                disp_mult=cfg.get("disp_mult_opt", 1.2),
                body_min=cfg.get("body_min_opt", 0.55),
                close_strength=cfg.get("close_strength_opt", 0.20),
                breakout_margin=cfg.get("breakout_margin_opt", 0.03),
                orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
                tp3_r=2.5,
                tp1_r=tp1_r,
                trail_buffer_r=trail_buffer_r,
            )
            signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
            for s in signals:
                all_signals.append({
                    "symbol": sym,
                    "signal": s,
                    "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                    "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                })
                count += 1

    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals


def run_portfolio(all_signals):
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    MAX_DD_PCT = ACCOUNT_RULES.get("max_drawdown_pct", 10.0)
    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL
    day_peak_balance = STARTING_CAPITAL
    day_start_balance = STARTING_CAPITAL
    current_day = None

    open_positions = []
    daily_losses = {}
    daily_trade_counts = {}
    all_trades = []
    account_resets = 0
    account_terminated = False

    _r_by_sym = defaultdict(list)
    for sig in all_signals:
        _r_by_sym[sig["symbol"]].append(float(sig["signal"].risk_points))
    median_R = {sym: float(np.median(rs)) for sym, rs in _r_by_sym.items()}

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
            continue
        if daily_trade_counts.get(trade_day, 0) >= MAX_DAILY_TRADES:
            continue

        open_positions = [p for p in open_positions if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            continue

        MIN_R_BY_SYMBOL = {"SPX500": 3.0, "US2000": 3.0}
        MIN_R_POINTS = MIN_R_BY_SYMBOL.get(symbol, 5.0)
        if risk_points < MIN_R_POINTS:
            continue

        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        if risk_points > 0 and (spread_pts / risk_points) > 0.30:
            continue

        breakout_time = getattr(s, 'breakout_time', None)
        if breakout_time is not None:
            bars_delay = int((pd.Timestamp(s.entry_time) - pd.Timestamp(breakout_time)).total_seconds() / 60)
            if bars_delay > 15:
                continue

        signal_price = getattr(s, 'signal_price', None)
        if signal_price is not None and risk_points > 0:
            drift = abs(float(s.entry_price) - float(signal_price))
            if drift > 0.5 * risk_points:
                continue

        risk_pct, should_stop = get_adaptive_risk(current_balance, day_peak_balance,
                                                   starting_balance=STARTING_CAPITAL)
        if should_stop:
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
        peak_balance = max(peak_balance, current_balance)
        if pnl > 0:
            day_peak_balance = max(day_peak_balance, current_balance)

        overall_dd_pct = ((STARTING_CAPITAL - current_balance) / STARTING_CAPITAL) * 100
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True

        daily_ref_post = max(day_start_balance, day_peak_balance)
        daily_dd_pct = ((daily_ref_post - current_balance) / daily_ref_post * 100) if daily_ref_post > 0 else 0
        trailing_dd_pct = ((peak_balance - current_balance) / peak_balance * 100) if peak_balance > 0 else 0

        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1
        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1

        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})

        all_trades.append({
            "symbol": symbol, "date": entry_time, "result": result,
            "risk_percent": risk_pct, "pnl_points": pnl_points,
            "pnl": pnl, "balance": current_balance, "peak_balance": peak_balance,
            "day_peak_balance": day_peak_balance,
            "daily_dd_pct": round(daily_dd_pct, 2),
            "trailing_dd_pct": round(trailing_dd_pct, 2),
            "lots": lots, "exit_reason": exit_reason, "costs": total_costs,
        })

    return all_trades, account_resets


def compute_metrics(trades_df, starting_capital):
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

    equity = trades_df["balance"].values
    peak_eq = np.maximum.accumulate(equity)
    dd_pct = (peak_eq - equity) / peak_eq * 100
    max_dd_pct = dd_pct.max()
    max_dd_dollar = (peak_eq - equity).max()

    trades_df = trades_df.copy()
    trades_df["trade_date"] = trades_df["date"].dt.date
    daily_pnl = trades_df.groupby("trade_date")["pnl"].sum()
    avg_dp = daily_pnl.mean()
    std_dp = daily_pnl.std()
    sharpe = (avg_dp / std_dp * np.sqrt(252)) if std_dp > 0 else 0

    final_balance = trades_df["balance"].iloc[-1]
    total_return_pct = ((final_balance - starting_capital) / starting_capital * 100)

    # Exit reason breakdown
    tp_count = len(trades_df[trades_df["exit_reason"] == "TP"])
    tp_pct = tp_count / total * 100 if total > 0 else 0

    return {
        "total_trades": total,
        "wins": wins,
        "partial_wins": partial_wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "net_pnl": round(net_pnl, 2),
        "final_balance": round(final_balance, 2),
        "total_return_pct": round(total_return_pct, 1),
        "max_dd_pct": round(max_dd_pct, 1),
        "max_dd_dollar": round(max_dd_dollar, 2),
        "sharpe": round(sharpe, 2),
        "tp_count": tp_count,
        "tp_pct": round(tp_pct, 1),
        "starting_capital": starting_capital,
    }


def print_comparison(results_list, account_resets_list):
    starting_cap = ACCOUNT_RULES["starting_balance"]

    print("\n" + "=" * 72)
    print("  10-YEAR TRAIL SL COMPARISON  (pre-built CSVs, adaptive risk)")
    print("=" * 72)
    print(f"\n  {'Metric':<30} {'CURRENT':>16} {'OPTIMAL':>16}")
    print(f"  {'':30} {'BE=1.0R TRAIL=0.5R':>16} {'BE=0.75R TRAIL=1.0R':>16}")
    print(f"  {'-'*62}")

    labels = [
        ("Total Trades",      "total_trades",   "{:>16}",    "{:>16}"),
        ("Win Rate",          "win_rate",        "{:>15.1f}%", "{:>15.1f}%"),
        ("Profit Factor",     "profit_factor",   "{:>16.2f}",  "{:>16.2f}"),
        ("Net P&L",           "net_pnl",         "${:>15,.2f}", "${:>15,.2f}"),
        ("Total Return",      "total_return_pct","{:>15.1f}%", "{:>15.1f}%"),
        ("Max Drawdown",      "max_dd_pct",      "{:>15.1f}%", "{:>15.1f}%"),
        ("Sharpe Ratio",      "sharpe",          "{:>16.2f}",  "{:>16.2f}"),
        ("TP Hits",           "tp_count",        "{:>16}",    "{:>16}"),
        ("TP Hit Rate",       "tp_pct",          "{:>15.1f}%", "{:>15.1f}%"),
        ("Account Resets",    None,              None,         None),
    ]

    for display_name, key, fmt1, fmt2 in labels:
        if key is None:
            v1 = account_resets_list[0]
            v2 = account_resets_list[1]
            c1 = f"{v1:>16}"
            c2 = f"{v2:>16}"
        else:
            v1 = results_list[0][key]
            v2 = results_list[1][key]
            c1 = fmt1.format(v1)
            c2 = fmt2.format(v2)

        # Mark better value
        if key in ("profit_factor", "net_pnl", "total_return_pct", "sharpe", "tp_count", "tp_pct", "win_rate"):
            marker = " <--" if (isinstance(v2, (int, float)) and isinstance(v1, (int, float)) and v2 > v1) else ""
        elif key in ("max_dd_pct", "max_dd_dollar") or (key is None):
            marker = " <--" if (isinstance(v2, (int, float)) and isinstance(v1, (int, float)) and v2 < v1) else ""
        else:
            marker = ""

        print(f"  {display_name:<30} {c1} {c2}{marker}")

    print(f"\n  '<--' marks which config wins on that metric")

    # Yearly breakdown for both
    print(f"\n  ---- YEARLY BREAKDOWN ----")
    print(f"  {'':6} {'':20} {'CURRENT':>14} {'':4} {'OPTIMAL':>14}")
    print(f"  {'Year':<6} {'':20} {'PF':>6} {'Net$':>8} {'':4} {'PF':>6} {'Net$':>8}")
    print(f"  {'-'*62}")


def run_config(cfg, symbols):
    label = cfg["label"]
    tp1_r = cfg["tp1_r"]
    trail_r = cfg["trail_buffer_r"]
    print(f"\n{'='*60}")
    print(f"  Running: {label}")
    print(f"  tp1_r={tp1_r}, trail_buffer_r={trail_r}")
    print(f"{'='*60}")

    print(f"\n  Generating signals ({len(symbols)} symbols)...")
    all_signals = generate_signals(symbols, tp1_r, trail_r)
    print(f"  Total raw signals: {len(all_signals)}")

    print(f"\n  Running portfolio backtest...")
    all_trades, account_resets = run_portfolio(all_signals)
    print(f"  Executed trades: {len(all_trades)} | Account resets: {account_resets}")

    if not all_trades:
        print("  ERROR: No trades executed!")
        return None, 0, None

    trades_df = pd.DataFrame(all_trades)
    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)

    starting_cap = ACCOUNT_RULES["starting_balance"]
    metrics = compute_metrics(trades_df, starting_cap)

    # Yearly breakdown
    trades_df["year"] = trades_df["date"].dt.year
    yearly = {}
    for yr in sorted(trades_df["year"].unique()):
        ydf = trades_df[trades_df["year"] == yr].reset_index(drop=True)
        if len(ydf) > 0:
            yr_start_cap = ydf["balance"].iloc[0] - ydf["pnl"].iloc[0]
            m = compute_metrics(ydf, yr_start_cap)
            yearly[int(yr)] = m

    return metrics, account_resets, yearly


def main():
    print(f"\n  Checking available data files...")
    available_symbols = []
    for sym in SYMBOLS:
        fname = DATA_FILES[sym]
        fpath = Path(__file__).parent / "data" / "raw" / fname
        if fpath.exists():
            size_mb = fpath.stat().st_size / 1024 / 1024
            print(f"    {sym}: {fname} ({size_mb:.1f} MB) OK")
            available_symbols.append(sym)
        else:
            print(f"    {sym}: {fname} NOT FOUND — skipping")

    if not available_symbols:
        print("  ERROR: No data files found. Exiting.")
        return

    results_list = []
    account_resets_list = []
    yearly_list = []

    for cfg in CONFIGS:
        metrics, resets, yearly = run_config(cfg, available_symbols)
        results_list.append(metrics)
        account_resets_list.append(resets)
        yearly_list.append(yearly)

    print_comparison(results_list, account_resets_list)

    # Detailed yearly table
    all_years = sorted(set(yr for y in yearly_list for yr in y.keys()))
    print(f"\n  {'Year':<6} {'Trades':>7} {'WR%':>6} {'PF':>6} {'Net$':>9}  |  {'Trades':>7} {'WR%':>6} {'PF':>6} {'Net$':>9}")
    print(f"  {'':6} {'--- CURRENT ---':^31}  {'--- OPTIMAL ---':^31}")
    print(f"  {'-'*68}")
    for yr in all_years:
        c = yearly_list[0].get(yr, {})
        o = yearly_list[1].get(yr, {})
        c_str = f"{c.get('total_trades',''):>7} {c.get('win_rate',''):>5.1f}% {c.get('profit_factor',''):>5.2f} ${c.get('net_pnl',0):>8,.0f}" if c else " " * 31
        o_str = f"{o.get('total_trades',''):>7} {o.get('win_rate',''):>5.1f}% {o.get('profit_factor',''):>5.2f} ${o.get('net_pnl',0):>8,.0f}" if o else " " * 31
        marker = " *" if (o and c and o.get("profit_factor", 0) > c.get("profit_factor", 0)) else ""
        print(f"  {yr:<6} {c_str}  |  {o_str}{marker}")

    print(f"\n  * = OPTIMAL wins that year on PF")

    # Save results
    output_dir = Path(__file__).parent / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "trail_10yr_comparison.json"
    with open(out_path, "w") as f:
        json.dump({
            "configs": [c["label"] for c in CONFIGS],
            "metrics": results_list,
            "account_resets": account_resets_list,
            "yearly": [{str(k): v for k, v in y.items()} for y in yearly_list],
        }, f, indent=2, default=str)
    print(f"\n  Results saved: {out_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
