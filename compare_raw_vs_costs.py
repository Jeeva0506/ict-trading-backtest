"""
Backtest Comparison: Raw (No Costs) vs With Costs
===================================================
Runs the same 3-year backtest twice:
1. RAW: No spread, no slippage, no commissions
2. WITH COSTS: Current realistic cost model (vol-scaled slippage + spread + commission)
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, ADAPTIVE_RISK,
    ACCOUNT_RULES, MAX_OPEN_POSITIONS, MAX_DAILY_LOSSES,
    MAX_DAILY_TRADES, load_data, compute_htf_ema, get_htf_trend_for_day,
    get_adaptive_risk, StrategyICTClean,
    get_vol_scaled_slippage, is_high_vol_event_day,
)

ALL_SYMBOLS = ["SPX500", "NDX100", "GER30", "US30", "US2000"]
THREE_YEAR_FILES = {
    "SPX500": "SPX500_3YEAR_M1.csv",
    "NDX100": "NDX100_3YEAR_M1.csv",
    "GER30":  "GER30_3YEAR_M1.csv",
    "US30":   "US30_3YEAR_M1.csv",
    "US2000": "US2000_M1_CLEAN.csv",
}


def generate_all_signals():
    all_signals = []
    for sym in ALL_SYMBOLS:
        cfg = LIVE_INDEX_CONFIG[sym]
        data_file = THREE_YEAR_FILES[sym]
        print(f"Loading {sym}: {data_file} ...", end=" ")
        df = load_data(data_file)
        print(f"{len(df):,} M1 bars")
        htf_ema = compute_htf_ema(df, fast=20, slow=50)
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
            signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
            for s in signals:
                all_signals.append({
                    "symbol": sym, "signal": s, "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                    "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                })
                count += 1
        print(f"  {sym}: {count} signals")
    all_signals.sort(key=lambda x: x["entry_time"])
    print(f"\nTotal signals: {len(all_signals)}")
    return all_signals


def run_portfolio(all_signals, zero_costs=False):
    """Run backtest. If zero_costs=True, no spread/slippage/commission."""
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    MAX_DD_PCT = ACCOUNT_RULES.get("max_drawdown_pct", 10.0)
    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL
    open_positions = []
    daily_losses = {}
    daily_trade_counts = {}
    all_trades = []
    account_terminated = False

    _r_by_sym = defaultdict(list)
    for sig in all_signals:
        _r_by_sym[sig["symbol"]].append(float(sig["signal"].risk_points))
    median_R = {sym: float(np.median(rs)) for sym, rs in _r_by_sym.items()}

    current_day = None
    day_peak_balance = STARTING_CAPITAL
    day_start_balance = STARTING_CAPITAL

    for sig in all_signals:
        if account_terminated:
            continue
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
        if not zero_costs and risk_points > 0 and (spread_pts / risk_points) > 0.30:
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

        if zero_costs:
            total_cost_pts = 0.0
            commission_cost = 0.0
        else:
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
        if pnl > 0:
            day_peak_balance = max(day_peak_balance, current_balance)
        peak_balance = max(peak_balance, current_balance)

        overall_dd_pct = ((STARTING_CAPITAL - current_balance) / STARTING_CAPITAL) * 100 if STARTING_CAPITAL > 0 else 0
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True

        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1
        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1

        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})

        all_trades.append({
            "symbol": symbol, "date": entry_time, "direction": s.direction,
            "result": result, "risk_points": risk_points,
            "pnl_points": pnl_points, "pnl_raw": pnl_raw,
            "costs": total_costs, "pnl": pnl,
            "balance": current_balance, "peak_balance": peak_balance,
            "day_peak_balance": day_peak_balance,
            "daily_dd_pct": round(((max(day_start_balance, day_peak_balance) - current_balance) /
                                    max(day_start_balance, day_peak_balance) * 100) if max(day_start_balance, day_peak_balance) > 0 else 0, 2),
            "trailing_dd_pct": round(((peak_balance - current_balance) / peak_balance * 100) if peak_balance > 0 else 0, 2),
            "lots": lots, "exit_reason": exit_reason,
        })

    return all_trades, account_terminated


def compute_metrics(trades_df, starting_capital, label=""):
    total = len(trades_df)
    if total == 0:
        return None

    wins = len(trades_df[trades_df["result"].isin(["win", "partial_win"])])
    losses = len(trades_df[trades_df["result"] == "loss"])
    wr = wins / total * 100 if total > 0 else 0

    net_pnl = trades_df["pnl"].sum()
    gp = trades_df[trades_df["pnl"] > 0]["pnl"].sum()
    gl = trades_df[trades_df["pnl"] < 0]["pnl"].sum()
    pf = abs(gp / gl) if gl != 0 else float("inf")

    avg_win = trades_df[trades_df["pnl_points"] > 0]["pnl_points"].mean() if wins > 0 else 0
    avg_loss = abs(trades_df[trades_df["pnl_points"] < 0]["pnl_points"].mean()) if losses > 0 else 0
    actual_rr = avg_win / avg_loss if avg_loss > 0 else 0

    total_costs = trades_df["costs"].sum()

    equity = trades_df["balance"].values
    peak_eq = np.maximum.accumulate(equity)
    dd_pct = (peak_eq - equity) / peak_eq * 100
    max_dd_pct = dd_pct.max()
    max_dd_idx = dd_pct.argmax()
    max_dd_dollar = (peak_eq - equity)[max_dd_idx]

    final_balance = trades_df["balance"].iloc[-1]

    # Sharpe
    trades_df = trades_df.copy()
    trades_df["trade_date"] = trades_df["date"].dt.date
    daily_pnl = trades_df.groupby("trade_date")["pnl"].sum()
    trading_days = len(daily_pnl)
    avg_dp = daily_pnl.mean()
    std_dp = daily_pnl.std()
    down_std = daily_pnl[daily_pnl < 0].std() if len(daily_pnl[daily_pnl < 0]) > 1 else std_dp
    sharpe = (avg_dp / std_dp * np.sqrt(252)) if std_dp > 0 else 0
    sortino = (avg_dp / down_std * np.sqrt(252)) if down_std > 0 else 0

    max_cl = 0
    cl = 0
    for r in trades_df["result"].tolist():
        if r == "loss":
            cl += 1
            max_cl = max(max_cl, cl)
        else:
            cl = 0

    return {
        "label": label, "trades": total, "wins": wins, "losses": losses,
        "wr": round(wr, 1), "pf": round(pf, 2), "actual_rr": round(actual_rr, 2),
        "net_pnl": round(net_pnl, 2), "total_costs": round(total_costs, 2),
        "max_dd_pct": round(max_dd_pct, 1), "max_dd_dollar": round(max_dd_dollar, 2),
        "sharpe": round(sharpe, 2), "sortino": round(sortino, 2),
        "final_balance": round(final_balance, 2),
        "max_consec_loss": max_cl, "trading_days": trading_days,
        "avg_win_pts": round(avg_win, 1), "avg_loss_pts": round(avg_loss, 1),
    }


def print_comparison(raw_m, cost_m, label="OVERALL"):
    """Print side-by-side comparison."""
    print(f"\n  {'Metric':<22} {'Raw (No Cost)':>15} {'With Costs':>15} {'Impact':>12}")
    print(f"  {'─'*64}")

    rows = [
        ("Trades", f"{raw_m['trades']}", f"{cost_m['trades']}", ""),
        ("Win Rate", f"{raw_m['wr']}%", f"{cost_m['wr']}%",
         f"{cost_m['wr'] - raw_m['wr']:+.1f}%"),
        ("Profit Factor", f"{raw_m['pf']}", f"{cost_m['pf']}",
         f"{cost_m['pf'] - raw_m['pf']:+.2f}"),
        ("Avg RR (W/L pts)", f"{raw_m['actual_rr']}", f"{cost_m['actual_rr']}",
         f"{cost_m['actual_rr'] - raw_m['actual_rr']:+.2f}"),
        ("Avg Win (pts)", f"{raw_m['avg_win_pts']}", f"{cost_m['avg_win_pts']}",
         f"{cost_m['avg_win_pts'] - raw_m['avg_win_pts']:+.1f}"),
        ("Avg Loss (pts)", f"{raw_m['avg_loss_pts']}", f"{cost_m['avg_loss_pts']}",
         f"{cost_m['avg_loss_pts'] - raw_m['avg_loss_pts']:+.1f}"),
        ("Net P&L", f"${raw_m['net_pnl']:>,.0f}", f"${cost_m['net_pnl']:>,.0f}",
         f"${cost_m['net_pnl'] - raw_m['net_pnl']:>+,.0f}"),
        ("Total Costs", f"$0", f"${cost_m['total_costs']:>,.0f}", ""),
        ("Final Balance", f"${raw_m['final_balance']:>,.0f}", f"${cost_m['final_balance']:>,.0f}", ""),
        ("Max DD %", f"{raw_m['max_dd_pct']}%", f"{cost_m['max_dd_pct']}%",
         f"{cost_m['max_dd_pct'] - raw_m['max_dd_pct']:+.1f}%"),
        ("Max DD $", f"${raw_m['max_dd_dollar']:>,.0f}", f"${cost_m['max_dd_dollar']:>,.0f}", ""),
        ("Sharpe", f"{raw_m['sharpe']}", f"{cost_m['sharpe']}",
         f"{cost_m['sharpe'] - raw_m['sharpe']:+.2f}"),
        ("Sortino", f"{raw_m['sortino']}", f"{cost_m['sortino']}",
         f"{cost_m['sortino'] - raw_m['sortino']:+.2f}"),
        ("Max Consec Loss", f"{raw_m['max_consec_loss']}", f"{cost_m['max_consec_loss']}", ""),
    ]

    for name, raw_val, cost_val, impact in rows:
        print(f"  {name:<22} {raw_val:>15} {cost_val:>15} {impact:>12}")


if __name__ == "__main__":
    print("=" * 70)
    print("  COST IMPACT ANALYSIS: Raw vs With Costs (3-Year Backtest)")
    print("=" * 70)

    # Generate signals once
    signals = generate_all_signals()

    # Run 1: RAW (no costs)
    print("\n── Running RAW backtest (no costs) ──")
    raw_trades, raw_terminated = run_portfolio(signals, zero_costs=True)
    print(f"  Executed: {len(raw_trades)} trades" + (" (TERMINATED)" if raw_terminated else ""))

    # Run 2: WITH COSTS
    print("\n── Running WITH COSTS backtest ──")
    cost_trades, cost_terminated = run_portfolio(signals, zero_costs=False)
    print(f"  Executed: {len(cost_trades)} trades" + (" (TERMINATED)" if cost_terminated else ""))

    raw_df = pd.DataFrame(raw_trades)
    raw_df["date"] = pd.to_datetime(raw_df["date"])
    cost_df = pd.DataFrame(cost_trades)
    cost_df["date"] = pd.to_datetime(cost_df["date"])

    starting_cap = ACCOUNT_RULES["starting_balance"]

    # OVERALL
    raw_m = compute_metrics(raw_df, starting_cap, "Raw")
    cost_m = compute_metrics(cost_df, starting_cap, "With Costs")

    print("\n" + "=" * 70)
    print("  OVERALL PORTFOLIO")
    print("=" * 70)
    print_comparison(raw_m, cost_m)

    # Cost breakdown
    if len(cost_df) > 0:
        total_costs = cost_df["costs"].sum()
        cost_per_trade = total_costs / len(cost_df)
        cost_pct_of_raw = (total_costs / raw_m["net_pnl"] * 100) if raw_m["net_pnl"] > 0 else 0
        print(f"\n  ── COST BREAKDOWN ──")
        print(f"  Total costs paid:     ${total_costs:>10,.2f}")
        print(f"  Avg cost per trade:   ${cost_per_trade:>10,.2f}")
        print(f"  Cost as % of raw PnL: {cost_pct_of_raw:>10.1f}%")
        print(f"  PnL retained:         {100 - cost_pct_of_raw:>10.1f}%")

    # PER-INDEX
    print("\n" + "=" * 70)
    print("  PER-INDEX COMPARISON")
    print("=" * 70)

    for sym in ALL_SYMBOLS:
        raw_sym = raw_df[raw_df["symbol"] == sym].reset_index(drop=True)
        cost_sym = cost_df[cost_df["symbol"] == sym].reset_index(drop=True)
        if len(raw_sym) == 0:
            continue
        rm = compute_metrics(raw_sym, starting_cap, f"{sym} Raw")
        cm = compute_metrics(cost_sym, starting_cap, f"{sym} Cost")
        if rm is None or cm is None:
            continue

        sym_costs = cost_sym["costs"].sum()
        print(f"\n  {sym} ─────────────────────────────────────")
        print(f"  {'':22} {'Raw':>12} {'With Costs':>12} {'Impact':>10}")
        print(f"  {'Trades':<22} {rm['trades']:>12} {cm['trades']:>12}")
        print(f"  {'Win Rate':<22} {rm['wr']:>11.1f}% {cm['wr']:>11.1f}% {cm['wr']-rm['wr']:>+9.1f}%")
        print(f"  {'Profit Factor':<22} {rm['pf']:>12.2f} {cm['pf']:>12.2f} {cm['pf']-rm['pf']:>+10.2f}")
        print(f"  {'Net P&L':<22} ${rm['net_pnl']:>10,.0f} ${cm['net_pnl']:>10,.0f} ${cm['net_pnl']-rm['net_pnl']:>+9,.0f}")
        print(f"  {'Total Costs':<22} {'$0':>12} ${sym_costs:>10,.0f}")
        print(f"  {'Max DD %':<22} {rm['max_dd_pct']:>11.1f}% {cm['max_dd_pct']:>11.1f}%")

    # YEARLY
    print("\n" + "=" * 70)
    print("  YEARLY COMPARISON")
    print("=" * 70)

    raw_df["year"] = raw_df["date"].dt.year
    cost_df["year"] = cost_df["date"].dt.year

    print(f"\n  {'Year':<6} {'':>3} {'Trades':>7} {'WR':>7} {'PF':>7} {'Net PnL':>12} {'MaxDD%':>8} {'Sharpe':>8}")
    print(f"  {'─'*62}")
    for yr in sorted(raw_df["year"].unique()):
        ryr = raw_df[raw_df["year"] == yr].reset_index(drop=True)
        cyr = cost_df[cost_df["year"] == yr].reset_index(drop=True)
        yr_start_raw = ryr["balance"].iloc[0] - ryr["pnl"].iloc[0]
        yr_start_cost = cyr["balance"].iloc[0] - cyr["pnl"].iloc[0] if len(cyr) > 0 else starting_cap
        rm = compute_metrics(ryr, yr_start_raw, f"{yr}")
        cm = compute_metrics(cyr, yr_start_cost, f"{yr}") if len(cyr) > 0 else None
        if rm:
            print(f"  {yr}   RAW {rm['trades']:>7} {rm['wr']:>6.1f}% {rm['pf']:>6.2f} ${rm['net_pnl']:>10,.0f} {rm['max_dd_pct']:>7.1f}% {rm['sharpe']:>7.2f}")
        if cm:
            print(f"  {yr}  COST {cm['trades']:>7} {cm['wr']:>6.1f}% {cm['pf']:>6.2f} ${cm['net_pnl']:>10,.0f} {cm['max_dd_pct']:>7.1f}% {cm['sharpe']:>7.2f}")
        print()

    print("=" * 70)
