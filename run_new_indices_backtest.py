"""
New Indices Backtest — FRA40, UK100, HK50, JP225
==================================================
Same filters as live: spread (30%), stale (15 bars), drift (0.5R), min R (5 pts).
Same cost model and portfolio constraints as the main 3-year backtest.
"""
import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, ADAPTIVE_RISK,
    ACCOUNT_RULES, FUNDEDNEXT_RULES, MAX_OPEN_POSITIONS, MAX_DAILY_LOSSES,
    MAX_DAILY_TRADES, LEVERAGE, load_data, compute_htf_ema, get_htf_trend_for_day,
    get_adaptive_risk, StrategyICTClean,
)

ALL_SYMBOLS = ["FRA40", "UK100", "HK50", "JP225"]

DATA_FILES = {
    "FRA40": "FRA40_M1_CLEAN.csv",
    "UK100": "UK100_M1_CLEAN.csv",
    "HK50":  "HK50_M1_CLEAN.csv",
    "JP225": "JP225_M1_CLEAN.csv",
}


def generate_all_signals():
    """Generate signals for all 4 new indices."""
    all_signals = []

    for sym in ALL_SYMBOLS:
        cfg = LIVE_INDEX_CONFIG[sym]
        data_file = DATA_FILES[sym]
        print(f"Loading {sym} data: {data_file} ...", end=" ")
        df = load_data(data_file)
        print(f"{len(df):,} M1 bars")

        htf_ema = compute_htf_ema(df, fast=20, slow=50)
        print(f"  HTF EMA computed: {len(htf_ema)} H1 bars")

        trading_days = sorted(set(df.index.date))
        print(f"  Trading days: {len(trading_days)} ({trading_days[0]} to {trading_days[-1]})")

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
                    "symbol": sym,
                    "signal": s,
                    "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                    "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                })
                count += 1

        print(f"  {sym}: {count} signals generated")

    all_signals.sort(key=lambda x: x["entry_time"])
    print(f"\nTotal raw signals: {len(all_signals)}")
    return all_signals


def run_portfolio(all_signals):
    """Execute portfolio backtest with full constraints including 10% max DD enforcement."""
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    MAX_DD_PCT = ACCOUNT_RULES.get("max_drawdown_pct", 10.0)
    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL

    open_positions = []
    daily_losses = {}
    daily_trade_counts = {}
    all_trades = []
    skipped_reasons = {"max_positions": 0, "daily_loss_limit": 0, "adaptive_stop": 0,
                       "max_daily_trades": 0, "max_dd_breach": 0,
                       "spread_guard": 0, "price_drift": 0, "stale_signal": 0,
                       "min_R": 0}
    account_terminated = False

    current_day = None
    day_peak_balance = STARTING_CAPITAL
    day_start_balance = STARTING_CAPITAL

    for sig in all_signals:
        if account_terminated:
            skipped_reasons["max_dd_breach"] += 1
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

        # Constraint: max daily losses
        if daily_losses.get(trade_day, 0) >= MAX_DAILY_LOSSES:
            skipped_reasons["daily_loss_limit"] += 1
            continue

        # Constraint: max trades per day
        if daily_trade_counts.get(trade_day, 0) >= MAX_DAILY_TRADES:
            skipped_reasons["max_daily_trades"] += 1
            continue

        # Constraint: max open positions
        open_positions = [p for p in open_positions if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            skipped_reasons["max_positions"] += 1
            continue

        # ── LIVE FILTER: Minimum R guard (5 pts) ─────────────────────
        MIN_R_POINTS = 5.0
        if risk_points < MIN_R_POINTS:
            skipped_reasons["min_R"] += 1
            continue

        # ── LIVE FILTER: Spread-to-R guard (30%) ─────────────────────
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        if risk_points > 0 and (spread_pts / risk_points) > 0.30:
            skipped_reasons["spread_guard"] += 1
            continue

        # ── LIVE FILTER: Stale signal guard (>15 bars) ───────────────
        breakout_time = getattr(s, 'breakout_time', None)
        if breakout_time is not None:
            bars_delay = int((pd.Timestamp(s.entry_time) - pd.Timestamp(breakout_time)).total_seconds() / 60)
            if bars_delay > 15:
                skipped_reasons["stale_signal"] += 1
                continue

        # ── LIVE FILTER: Price drift guard (0.5R — matches live) ─────
        signal_price = getattr(s, 'signal_price', None)
        if signal_price is not None and risk_points > 0:
            drift = abs(float(s.entry_price) - float(signal_price))
            if drift > 0.5 * risk_points:
                skipped_reasons["price_drift"] += 1
                continue

        # Adaptive risk
        risk_pct, should_stop = get_adaptive_risk(current_balance, day_peak_balance,
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
        slippage_pts = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        half_spread = spread_pts / 2.0
        entry_cost_pts = slippage_pts + half_spread

        if exit_reason == "TP":
            exit_cost_pts = half_spread
        else:
            exit_cost_pts = slippage_pts + half_spread

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

        # Max DD enforcement
        overall_dd_pct = ((STARTING_CAPITAL - current_balance) / STARTING_CAPITAL) * 100 if STARTING_CAPITAL > 0 else 0
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True
            print(f"  *** ACCOUNT TERMINATED: DD={overall_dd_pct:.1f}% >= {MAX_DD_PCT}% on {entry_time.date()} ***")

        daily_ref_post = max(day_start_balance, day_peak_balance)
        daily_dd_pct = ((daily_ref_post - current_balance) / daily_ref_post * 100) if daily_ref_post > 0 else 0
        trailing_dd_pct = ((peak_balance - current_balance) / peak_balance * 100) if peak_balance > 0 else 0

        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1

        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1

        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})

        all_trades.append({
            "symbol": symbol, "date": entry_time, "direction": s.direction,
            "result": result, "risk_percent": risk_pct, "risk_amount": risk_amount,
            "risk_points": risk_points, "pnl_points": pnl_points,
            "adjusted_pnl_points": adjusted_pnl_points,
            "pnl_raw": pnl_raw, "costs": total_costs, "pnl": pnl,
            "balance": current_balance, "peak_balance": peak_balance,
            "day_peak_balance": day_peak_balance,
            "daily_dd_pct": round(daily_dd_pct, 2),
            "trailing_dd_pct": round(trailing_dd_pct, 2),
            "lots": lots, "exit_reason": exit_reason,
        })

    return all_trades, skipped_reasons, current_balance, peak_balance


def compute_metrics(trades_df, starting_capital, label="Overall"):
    """Compute comprehensive metrics from a trades DataFrame."""
    total = len(trades_df)
    if total == 0:
        return None

    wins = len(trades_df[trades_df["result"] == "win"])
    partial_wins = len(trades_df[trades_df["result"] == "partial_win"])
    breakevens = len(trades_df[trades_df["result"] == "breakeven"])
    losses = len(trades_df[trades_df["result"] == "loss"])
    profitable = wins + partial_wins
    win_rate = (profitable / total * 100) if total > 0 else 0

    net_pnl = trades_df["pnl"].sum()
    gross_profit = trades_df[trades_df["pnl"] > 0]["pnl"].sum()
    gross_loss = trades_df[trades_df["pnl"] < 0]["pnl"].sum()
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")

    avg_win_pnl = trades_df[trades_df["pnl"] > 0]["pnl"].mean() if profitable > 0 else 0
    avg_loss_pnl = abs(trades_df[trades_df["pnl"] < 0]["pnl"].mean()) if losses > 0 else 0
    avg_win_pts = trades_df[trades_df["pnl_points"] > 0]["pnl_points"].mean() if profitable > 0 else 0
    avg_loss_pts = abs(trades_df[trades_df["pnl_points"] < 0]["pnl_points"].mean()) if losses > 0 else 0
    actual_rr = (avg_win_pts / avg_loss_pts) if avg_loss_pts > 0 else float("inf")

    expectancy = net_pnl / total
    total_costs = trades_df["costs"].sum()

    largest_win = trades_df["pnl"].max()
    largest_loss = trades_df["pnl"].min()

    equity = trades_df["balance"].values
    peak_equity = np.maximum.accumulate(equity)
    drawdowns_pct = (peak_equity - equity) / peak_equity * 100
    drawdowns_dollar = peak_equity - equity
    max_dd_pct = drawdowns_pct.max()
    max_dd_idx = drawdowns_pct.argmax()
    max_dd_dollar = drawdowns_dollar[max_dd_idx]

    peak_daily_dd = trades_df["daily_dd_pct"].max()
    peak_trailing_dd = trades_df["trailing_dd_pct"].max()

    results_list = trades_df["result"].tolist()
    max_consec_loss = max_consec_win = 0
    cl = cw = 0
    for r in results_list:
        if r == "loss":
            cl += 1; cw = 0
            max_consec_loss = max(max_consec_loss, cl)
        elif r in ("win", "partial_win"):
            cw += 1; cl = 0
            max_consec_win = max(max_consec_win, cw)
        else:
            cl = cw = 0

    trades_df = trades_df.copy()
    trades_df["trade_date"] = trades_df["date"].dt.date
    daily_pnl = trades_df.groupby("trade_date")["pnl"].sum()
    trading_days = len(daily_pnl)
    trading_weeks = max(1, trading_days / 5)
    trades_per_week = total / trading_weeks

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
        "label": label,
        "total_trades": total,
        "trades_per_week": round(trades_per_week, 1),
        "wins": wins,
        "partial_wins": partial_wins,
        "breakevens": breakevens,
        "losses": losses,
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


def main():
    print("=" * 70)
    print("  NEW INDICES BACKTEST — FRA40, UK100, HK50, JP225")
    print(f"  Capital: ${ACCOUNT_RULES['starting_balance']:,.0f} | Risk: 1.5% (adaptive)")
    print("  Filters: spread(30%), stale(15bar), drift(0.5R), minR(5pts)")
    print("=" * 70)

    all_signals = generate_all_signals()
    if not all_signals:
        print("No signals generated. Exiting.")
        return

    print("\nRunning portfolio backtest with constraints...")
    all_trades, skipped, final_balance, peak_balance = run_portfolio(all_signals)
    print(f"Executed trades: {len(all_trades)}")
    print(f"Skipped: {skipped}")

    if not all_trades:
        print("No trades executed. Exiting.")
        return

    trades_df = pd.DataFrame(all_trades)
    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)

    output_dir = Path(__file__).parent / "reports" / "new_indices_backtest"
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(output_dir / "all_trades.csv", index=False)

    # Compute metrics
    results = {}
    starting_cap = ACCOUNT_RULES["starting_balance"]
    overall = compute_metrics(trades_df, starting_cap, "Overall")
    results["overall"] = overall

    results["per_index"] = {}
    for sym in ALL_SYMBOLS:
        sdf = trades_df[trades_df["symbol"] == sym].reset_index(drop=True)
        if len(sdf) > 0:
            m = compute_metrics(sdf, starting_cap, f"{sym}")
            results["per_index"][sym] = m

    trades_df["year"] = trades_df["date"].dt.year
    results["yearly"] = {}
    for yr in sorted(trades_df["year"].unique()):
        ydf = trades_df[trades_df["year"] == yr].reset_index(drop=True)
        if len(ydf) > 0:
            yr_start_cap = ydf["balance"].iloc[0] - ydf["pnl"].iloc[0]
            m = compute_metrics(ydf, yr_start_cap, str(yr))
            results["yearly"][str(yr)] = m

    trades_df["month"] = trades_df["date"].dt.to_period("M")
    results["monthly"] = {}
    for month in sorted(trades_df["month"].unique()):
        mdf = trades_df[trades_df["month"] == month].reset_index(drop=True)
        if len(mdf) > 0:
            m_start_cap = mdf["balance"].iloc[0] - mdf["pnl"].iloc[0]
            m = compute_metrics(mdf, m_start_cap, str(month))
            results["monthly"][str(month)] = m

    results["per_index_yearly"] = {}
    for sym in ALL_SYMBOLS:
        sdf = trades_df[trades_df["symbol"] == sym]
        results["per_index_yearly"][sym] = {}
        for yr in sorted(sdf["year"].unique()):
            sydf = sdf[sdf["year"] == yr].reset_index(drop=True)
            if len(sydf) > 0:
                yr_start = sydf["balance"].iloc[0] - sydf["pnl"].iloc[0]
                m = compute_metrics(sydf, yr_start, f"{sym} {yr}")
                results["per_index_yearly"][sym][str(yr)] = m

    exit_counts = trades_df["exit_reason"].value_counts()
    results["exit_reasons"] = {}
    for reason, count in exit_counts.items():
        pct = count / len(trades_df) * 100
        avg_pnl = trades_df[trades_df["exit_reason"] == reason]["pnl"].mean()
        results["exit_reasons"][reason] = {
            "count": int(count), "pct": round(pct, 1), "avg_pnl": round(avg_pnl, 2)
        }

    json_path = output_dir / "metrics_new_indices.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nMetrics saved to: {json_path}")

    # ─── PRINT REPORT ───
    print("\n" + "=" * 75)
    print("  NEW INDICES BACKTEST RESULTS")
    print("  Indices: FRA40, UK100, HK50, JP225")
    print("=" * 75)

    o = results["overall"]
    print(f"""
  ┌─────────────────────────────────────────────────────────────┐
  │                    OVERALL PORTFOLIO                        │
  ├─────────────────────────────────────────────────────────────┤
  │  Total Trades      : {o['total_trades']:<8} ({o['trades_per_week']}/wk)                  │
  │  Win Rate          : {o['win_rate']}%                                   │
  │  Profit Factor     : {o['profit_factor']:<8}                              │
  │  Avg RR            : 1:{o['actual_rr']:<8}                            │
  │  Sharpe Ratio      : {o['sharpe']:<8}                              │
  │  Sortino Ratio     : {o['sortino']:<8}                              │
  ├─────────────────────────────────────────────────────────────┤
  │  Starting Capital  : ${o['starting_capital']:>10,.2f}                        │
  │  Final Balance     : ${o['final_balance']:>10,.2f}                        │
  │  Net P&L           : ${o['net_pnl']:>10,.2f}                        │
  │  Total Return      : {o['total_return_pct']:>8.1f}%                           │
  ├─────────────────────────────────────────────────────────────┤
  │  Max Drawdown      : {o['max_dd_pct']}% (${o['max_dd_dollar']:,.2f})                  │
  │  Peak Daily DD     : {o['peak_daily_dd']}%                                  │
  │  Max Consec Losses : {o['max_consec_losses']:<8}                              │
  │  Recovery Factor   : {o['recovery_factor']:<8}                              │
  └─────────────────────────────────────────────────────────────┘""")

    print(f"\n  {'Index':<10} {'Trades':>6} {'Tr/Wk':>6} {'WR':>6} {'PF':>6} {'Net PnL':>10} {'MaxDD%':>7} {'Sharpe':>7}")
    print(f"  {'-'*62}")
    for sym in ALL_SYMBOLS:
        if sym in results["per_index"]:
            m = results["per_index"][sym]
            print(f"  {sym:<10} {m['total_trades']:>6} {m['trades_per_week']:>5.1f} {m['win_rate']:>5.1f}% {m['profit_factor']:>5.2f} ${m['net_pnl']:>9,.2f} {m['max_dd_pct']:>6.1f}% {m['sharpe']:>6.2f}")

    print(f"\n  ── YEARLY BREAKDOWN ──")
    print(f"  {'Year':<6} {'Trades':>6} {'Tr/Wk':>6} {'W':>4} {'L':>4} {'WR':>6} {'PF':>6} {'Net PnL':>10} {'MaxDD%':>7} {'Sharpe':>7} {'MaxCL':>5}")
    print(f"  {'-'*75}")
    for yr, m in results["yearly"].items():
        print(f"  {yr:<6} {m['total_trades']:>6} {m['trades_per_week']:>5.1f} {m['wins']+m['partial_wins']:>4} {m['losses']:>4} {m['win_rate']:>5.1f}% {m['profit_factor']:>5.2f} ${m['net_pnl']:>9,.2f} {m['max_dd_pct']:>6.1f}% {m['sharpe']:>6.2f} {m['max_consec_losses']:>5}")

    print(f"\n  ── MONTHLY BREAKDOWN ──")
    print(f"  {'Month':<10} {'Trades':>6} {'W':>4} {'L':>4} {'WR':>6} {'PF':>6} {'Net PnL':>10} {'Cum PnL':>10} {'MaxCL':>5}")
    print(f"  {'-'*68}")
    cum_pnl = 0.0
    for month, m in results["monthly"].items():
        cum_pnl += m["net_pnl"]
        w_count = m['wins'] + m['partial_wins']
        print(f"  {month:<10} {m['total_trades']:>6} {w_count:>4} {m['losses']:>4} {m['win_rate']:>5.1f}% {m['profit_factor']:>5.2f} ${m['net_pnl']:>9,.2f} ${cum_pnl:>9,.2f} {m['max_consec_losses']:>5}")

    for sym in ALL_SYMBOLS:
        if sym in results["per_index_yearly"]:
            print(f"\n  ── {sym} YEARLY ──")
            print(f"  {'Year':<6} {'Trades':>6} {'W':>4} {'L':>4} {'WR':>6} {'PF':>6} {'Net PnL':>10}")
            print(f"  {'-'*50}")
            for yr, m in results["per_index_yearly"][sym].items():
                w_count = m['wins'] + m['partial_wins']
                print(f"  {yr:<6} {m['total_trades']:>6} {w_count:>4} {m['losses']:>4} {m['win_rate']:>5.1f}% {m['profit_factor']:>5.2f} ${m['net_pnl']:>9,.2f}")

    print(f"\n  ── EXIT REASONS ──")
    for reason, info in results["exit_reasons"].items():
        print(f"  {reason:<15} : {info['count']:>4} ({info['pct']:>5.1f}%)  avg PnL: ${info['avg_pnl']:>8,.2f}")

    print(f"\n  Skipped: {skipped}")
    print("=" * 75)
    print("  Costs: Spread + Slippage (realistic model)")
    print("  Entry: Next-bar open (no look-ahead bias)")
    print("=" * 75)


if __name__ == "__main__":
    main()
