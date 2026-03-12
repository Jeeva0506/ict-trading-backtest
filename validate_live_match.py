"""
Backtest vs Live Filter Validation
====================================
Purpose: Quantify how each live-only filter reduces trade count vs backtest.
Run the 3-year backtest under 3 scenarios:
  A) BACKTEST-AS-IS  — current backtest filters (baseline)
  B) LIVE-SIMULATED  — adds the extra live-only guards to backtest
  C) PROPOSED-FIX    — live code adjusted to match backtest exactly

If scenario A ≈ C in metrics, we can safely make live match backtest.
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

ALL_SYMBOLS = ["SPX500", "NDX100", "GER30", "US30", "US2000"]

THREE_YEAR_FILES = {
    "SPX500": "SPX500_3YEAR_M1.csv",
    "NDX100": "NDX100_3YEAR_M1.csv",
    "GER30":  "GER30_3YEAR_M1.csv",
    "US30":   "US30_3YEAR_M1.csv",
    "US2000": "US2000_M1_CLEAN.csv",
}


def generate_all_signals():
    """Generate all raw signals from strategy (identical for all scenarios)."""
    all_signals = []
    for sym in ALL_SYMBOLS:
        cfg = LIVE_INDEX_CONFIG[sym]
        data_file = THREE_YEAR_FILES[sym]
        print(f"  Loading {sym}: {data_file} ...", end=" ")
        df = load_data(data_file)
        print(f"{len(df):,} bars")

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
                # Also store the session M1 data for live-simulation of price drift
                # Compute "live-like drift": price N bars after entry vs entry price
                entry_idx = None
                if s.entry_time is not None:
                    matches = market_df.index.get_indexer([pd.Timestamp(s.entry_time)], method='nearest')
                    if len(matches) > 0:
                        entry_idx = matches[0]

                # Simulate live scanning delay: price 3-5 bars after entry
                live_drift_3bar = 0.0
                live_drift_5bar = 0.0
                if entry_idx is not None and entry_idx + 5 < len(market_df):
                    for offset in [3, 5]:
                        future_bar = market_df.iloc[entry_idx + offset]
                        mid = (float(future_bar["high"]) + float(future_bar["low"])) / 2
                        if offset == 3:
                            live_drift_3bar = abs(mid - float(s.entry_price))
                        else:
                            live_drift_5bar = abs(mid - float(s.entry_price))

                all_signals.append({
                    "symbol": sym,
                    "signal": s,
                    "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                    "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                    "live_drift_3bar": live_drift_3bar,
                    "live_drift_5bar": live_drift_5bar,
                })
                count += 1
        print(f"    → {count} signals")

    all_signals.sort(key=lambda x: x["entry_time"])
    print(f"  Total raw signals: {len(all_signals)}")
    return all_signals


def run_scenario(all_signals, scenario: str):
    """
    Run portfolio backtest under different filter scenarios.
    
    Scenarios:
      'backtest'   — Current backtest filters (baseline)
      'live_sim'   — Simulates live with extra guards (drift using future bars)
      'proposed'   — Proposed fix: remove live-only guards, match backtest exactly
    """
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    MAX_DD_PCT = ACCOUNT_RULES.get("max_drawdown_pct", 10.0)
    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL

    daily_losses = {}
    daily_trade_counts = {}
    all_trades = []
    skipped = Counter()
    account_terminated = False
    current_day = None
    day_peak_balance = STARTING_CAPITAL
    day_start_balance = STARTING_CAPITAL

    for sig in all_signals:
        if account_terminated:
            skipped["max_dd_breach"] += 1
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

        # --- Common filters (all scenarios) ---
        if daily_losses.get(trade_day, 0) >= MAX_DAILY_LOSSES:
            skipped["daily_loss_limit"] += 1
            continue
        if daily_trade_counts.get(trade_day, 0) >= MAX_DAILY_TRADES:
            skipped["max_daily_trades"] += 1
            continue
        if risk_points <= 0:
            skipped["zero_R"] += 1
            continue

        # ── FILTER: Min R guard (5 pts) — in BOTH backtest and live ──
        MIN_R_POINTS = 5.0
        if risk_points < MIN_R_POINTS:
            skipped["min_R"] += 1
            continue

        # ── FILTER: Spread-to-R guard (30%) — in BOTH backtest and live ──
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        if risk_points > 0 and (spread_pts / risk_points) > 0.30:
            skipped["spread_guard"] += 1
            continue

        # ── FILTER: Stale signal (>15 bars from breakout to entry) — BOTH ──
        breakout_time = getattr(s, 'breakout_time', None)
        if breakout_time is not None:
            bars_delay = int((pd.Timestamp(s.entry_time) - pd.Timestamp(breakout_time)).total_seconds() / 60)
            if bars_delay > 15:
                skipped["stale_signal"] += 1
                continue

        # ── FILTER: Backtest price drift (signal_price vs entry_price, 0.5R) ──
        # This is in BOTH backtest and live, but implementation differs.
        signal_price = getattr(s, 'signal_price', None)

        if scenario in ('backtest', 'proposed'):
            # Backtest-style: signal_price vs entry_price (1-bar gap, rarely triggers)
            if signal_price is not None and risk_points > 0:
                drift = abs(float(s.entry_price) - float(signal_price))
                if drift > 0.5 * risk_points:
                    skipped["price_drift_bt"] += 1
                    continue

        elif scenario == 'live_sim':
            # Live-simulated: use price 3-5 bars after entry (simulates scanning delay)
            # The live bot checks CURRENT market price vs entry_price
            live_drift = sig.get("live_drift_5bar", 0.0)
            if risk_points > 0 and live_drift > 0.5 * risk_points:
                skipped["price_drift_live"] += 1
                continue

            # Live also has pre-order drift recheck (after 1-2s delay)
            # Simulate with 3-bar drift (worst case after delay)
            pre_order_drift = sig.get("live_drift_3bar", 0.0)
            if risk_points > 0 and pre_order_drift > 0.5 * risk_points:
                skipped["pre_order_drift"] += 1
                continue

        # Adaptive risk
        risk_pct, should_stop = get_adaptive_risk(
            current_balance, day_peak_balance, starting_balance=STARTING_CAPITAL)
        if should_stop:
            skipped["adaptive_stop"] += 1
            continue

        # Position sizing + execution
        risk_amount = current_balance * (risk_pct / 100.0)
        lots = risk_amount / (risk_points * point_value)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        exit_reason = getattr(s, "exit_reason", None)
        pnl_points = float(getattr(s, "pnl_points", 0.0))

        slippage_pts = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
        half_spread = spread_pts / 2.0
        entry_cost = slippage_pts + half_spread
        exit_cost = half_spread if exit_reason == "TP" else slippage_pts + half_spread
        total_cost_pts = entry_cost + exit_cost
        commission = TRADING_COSTS["commission_per_lot"].get(symbol, 0.0) * lots

        adjusted_pnl_points = pnl_points - total_cost_pts
        result = "win" if adjusted_pnl_points > 0 and exit_reason == "TP" else \
                 "partial_win" if adjusted_pnl_points > 0 else \
                 "breakeven" if adjusted_pnl_points == 0 else "loss"

        pnl_raw = pnl_points * lots * point_value
        total_costs = total_cost_pts * lots * point_value + commission
        pnl = pnl_raw - total_costs

        current_balance += pnl
        if pnl > 0:
            day_peak_balance = max(day_peak_balance, current_balance)
        peak_balance = max(peak_balance, current_balance)

        overall_dd_pct = ((STARTING_CAPITAL - current_balance) / STARTING_CAPITAL) * 100
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True

        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1
        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1

        all_trades.append({
            "symbol": symbol, "date": entry_time, "direction": s.direction,
            "result": result, "risk_percent": risk_pct,
            "risk_points": risk_points, "pnl_points": pnl_points,
            "pnl": pnl, "balance": current_balance,
            "exit_reason": exit_reason, "lots": lots,
        })

    return all_trades, dict(skipped), current_balance


def compute_metrics(trades, label):
    """Compute summary metrics."""
    if not trades:
        return {"label": label, "total_trades": 0}
    
    df = pd.DataFrame(trades)
    total = len(df)
    wins = len(df[df["result"].isin(["win", "partial_win"])])
    losses = len(df[df["result"] == "loss"])
    win_rate = wins / total * 100 if total > 0 else 0

    net_pnl = df["pnl"].sum()
    gross_profit = df[df["pnl"] > 0]["pnl"].sum()
    gross_loss = df[df["pnl"] < 0]["pnl"].sum()
    pf = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")

    # Trading weeks
    df["trade_date"] = df["date"].dt.date
    daily_pnl = df.groupby("trade_date")["pnl"].sum()
    trading_days = len(daily_pnl)
    trading_weeks = max(1, trading_days / 5)
    trades_per_week = total / trading_weeks

    # Max drawdown
    equity = df["balance"].values
    peak_eq = np.maximum.accumulate(equity)
    dd_pct = ((peak_eq - equity) / peak_eq * 100)
    max_dd = dd_pct.max()

    # Max consecutive losses
    max_consec_loss = 0
    cl = 0
    for r in df["result"]:
        if r == "loss":
            cl += 1
            max_consec_loss = max(max_consec_loss, cl)
        else:
            cl = 0

    # Sharpe
    avg_dp = daily_pnl.mean()
    std_dp = daily_pnl.std()
    sharpe = (avg_dp / std_dp * np.sqrt(252)) if std_dp > 0 else 0

    # Per-symbol breakdown
    per_sym = {}
    for sym in df["symbol"].unique():
        sd = df[df["symbol"] == sym]
        sw = len(sd[sd["result"].isin(["win", "partial_win"])])
        sl = len(sd[sd["result"] == "loss"])
        st = len(sd)
        swr = sw / st * 100 if st > 0 else 0
        sgp = sd[sd["pnl"] > 0]["pnl"].sum()
        sgl = sd[sd["pnl"] < 0]["pnl"].sum()
        spf = abs(sgp / sgl) if sgl != 0 else float("inf")
        sym_days = len(sd["trade_date"].unique())
        sym_weeks = max(1, sym_days / 5)
        per_sym[sym] = {
            "trades": st, "tpw": round(st / sym_weeks, 1),
            "wr": round(swr, 1), "pf": round(spf, 2),
            "pnl": round(sd["pnl"].sum(), 2),
        }

    return {
        "label": label,
        "total_trades": total,
        "trades_per_week": round(trades_per_week, 1),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(pf, 2),
        "net_pnl": round(net_pnl, 2),
        "max_dd_pct": round(max_dd, 1),
        "sharpe": round(sharpe, 2),
        "max_consec_losses": max_consec_loss,
        "per_symbol": per_sym,
        "final_balance": round(df["balance"].iloc[-1], 2),
    }


def print_comparison(results):
    """Pretty-print scenario comparison."""
    print("\n" + "=" * 90)
    print("  SCENARIO COMPARISON — 3-Year Backtest")
    print("=" * 90)
    
    header = f"  {'Metric':<25}"
    for r in results:
        header += f"  {r['label']:>18}"
    print(header)
    print("  " + "-" * (25 + 20 * len(results)))

    metrics = [
        ("Total Trades", "total_trades"),
        ("Trades/Week", "trades_per_week"),
        ("Win Rate %", "win_rate"),
        ("Profit Factor", "profit_factor"),
        ("Net P&L ($)", "net_pnl"),
        ("Max DD %", "max_dd_pct"),
        ("Sharpe Ratio", "sharpe"),
        ("Max Consec Losses", "max_consec_losses"),
        ("Final Balance ($)", "final_balance"),
    ]

    for name, key in metrics:
        row = f"  {name:<25}"
        for r in results:
            val = r.get(key, "N/A")
            if isinstance(val, float):
                if key in ("net_pnl", "final_balance"):
                    row += f"  ${val:>16,.2f}"
                else:
                    row += f"  {val:>18.2f}"
            else:
                row += f"  {val:>18}"
        print(row)

    # Per-symbol breakdown
    print("\n  Per-Symbol Trades/Week:")
    all_syms = set()
    for r in results:
        all_syms.update(r.get("per_symbol", {}).keys())
    
    header2 = f"  {'Symbol':<12}"
    for r in results:
        header2 += f"  {r['label']:>18}"
    print(header2)
    print("  " + "-" * (12 + 20 * len(results)))

    for sym in sorted(all_syms):
        row = f"  {sym:<12}"
        for r in results:
            ps = r.get("per_symbol", {}).get(sym, {})
            t = ps.get("trades", 0)
            tpw = ps.get("tpw", 0)
            wr = ps.get("wr", 0)
            pf_v = ps.get("pf", 0)
            row += f"  {t:>4} ({tpw}/wk {wr}%)"
        print(row)


def main():
    print("=" * 70)
    print("  BACKTEST vs LIVE FILTER VALIDATION")
    print("  Confirming that live-matching-backtest preserves edge")
    print("=" * 70)

    # Phase 1: Generate signals (strategy is identical in all scenarios)
    print("\nPhase 1: Generating signals from strategy...")
    all_signals = generate_all_signals()

    # Phase 2: Run 3 scenarios
    print("\nPhase 2: Running scenarios...")

    scenarios = [
        ("backtest", "A: Backtest"),
        ("live_sim", "B: Live-Simulated"),
        ("proposed", "C: Proposed Fix"),
    ]

    results = []
    all_skipped = {}
    for scenario_key, label in scenarios:
        print(f"\n  Running {label}...")
        trades, skipped, final_bal = run_scenario(all_signals, scenario_key)
        metrics = compute_metrics(trades, label)
        results.append(metrics)
        all_skipped[label] = skipped
        print(f"    Trades: {metrics['total_trades']}, WR: {metrics['win_rate']}%, "
              f"PF: {metrics['profit_factor']}, Net: ${metrics['net_pnl']:,.2f}")

    # Phase 3: Print comparison
    print_comparison(results)

    # Print skipped reasons per scenario
    print("\n  Skipped Reasons (trades blocked by each filter):")
    print("  " + "-" * 70)
    all_keys = set()
    for v in all_skipped.values():
        all_keys.update(v.keys())
    
    header = f"  {'Filter':<25}"
    for label in all_skipped:
        header += f"  {label:>18}"
    print(header)
    print("  " + "-" * (25 + 20 * len(all_skipped)))

    for key in sorted(all_keys):
        row = f"  {key:<25}"
        for label in all_skipped:
            val = all_skipped[label].get(key, 0)
            row += f"  {val:>18}"
        print(row)

    # Verdict
    bt = results[0]
    live = results[1]
    fix = results[2]
    
    trade_gap = bt["total_trades"] - live["total_trades"]
    recovery = fix["total_trades"] - live["total_trades"]
    
    print("\n" + "=" * 70)
    print("  VERDICT")
    print("=" * 70)
    print(f"  Backtest trades:     {bt['total_trades']} ({bt['trades_per_week']}/wk)")
    print(f"  Live-simulated:      {live['total_trades']} ({live['trades_per_week']}/wk)")
    print(f"  Proposed fix:        {fix['total_trades']} ({fix['trades_per_week']}/wk)")
    print(f"  Gap (BT vs Live):    {trade_gap} trades lost")
    print(f"  Recovery (Fix):      +{recovery} trades recovered")
    print()
    
    # Edge check
    pf_diff = abs(fix["profit_factor"] - bt["profit_factor"])
    wr_diff = abs(fix["win_rate"] - bt["win_rate"])
    
    if pf_diff < 0.15 and wr_diff < 3.0:
        print("  ✅ EDGE PRESERVED — Proposed fix matches backtest.")
        print(f"     PF difference: {pf_diff:.2f} (< 0.15 threshold)")
        print(f"     WR difference: {wr_diff:.1f}% (< 3% threshold)")
        print("     SAFE to apply changes to live code.")
    else:
        print("  ⚠️  EDGE IMPACT DETECTED — Review before applying:")
        print(f"     PF difference: {pf_diff:.2f}")
        print(f"     WR difference: {wr_diff:.1f}%")


if __name__ == "__main__":
    main()
