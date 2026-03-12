"""
Second-Chance Rule A/B Test
============================
Compares the combined 5-index portfolio performance:
  A) Original:       1 trade per instrument per day (allow_second_chance=False)
  B) Second Chance:  If trade #1 wins -> done. If loses -> allow 1 more trade
                     in the OPPOSITE direction only (allow_second_chance=True)

Usage:
  python run_second_chance_ab.py
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


def load_and_filter(symbol, start_date=None, end_date=None):
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
    return df if len(df) > 0 else None


def generate_signals_for_data(symbol, df, label=None, allow_second_chance=False):
    """Generate signals using each index's OWN default config."""
    if label is None:
        label = symbol
    cfg = LIVE_INDEX_CONFIG.get(symbol)
    if cfg is None:
        return []

    htf_ema = compute_htf_ema(df, fast=EMA_FAST, slow=EMA_SLOW)
    trading_days = sorted(set(df.index.date))
    point_value = BROKER_POINT_VALUES[symbol]

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
            entry_hours=cfg.get("entry_hours", 2),
            breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
            disp_mult=cfg.get("disp_mult", 1.3),
            body_min=cfg.get("body_min", 0.60),
            close_strength=cfg.get("close_strength", 0.25),
            breakout_margin=cfg.get("breakout_margin", 0.05),
            orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
            tp3_r=2.5,
            allow_second_chance=allow_second_chance,
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


def generate_all_signals(allow_second_chance=False):
    all_signals = []

    # SPX500
    df = load_and_filter("SPX500")
    if df is not None:
        all_signals.extend(generate_signals_for_data("SPX500", df,
                           allow_second_chance=allow_second_chance))

    # NDX100
    df = load_and_filter("NDX100")
    if df is not None:
        all_signals.extend(generate_signals_for_data("NDX100", df,
                           allow_second_chance=allow_second_chance))

    # GER30 (split)
    for pkey, pcfg in GER30_CLEAN_PERIODS.items():
        df = load_and_filter("GER30", start_date=pcfg["start"], end_date=pcfg["end"])
        if df is not None:
            all_signals.extend(generate_signals_for_data("GER30", df, pcfg["label"],
                               allow_second_chance=allow_second_chance))

    # FRA40
    df = load_and_filter("FRA40")
    if df is not None:
        all_signals.extend(generate_signals_for_data("FRA40", df,
                           allow_second_chance=allow_second_chance))

    # US30
    df = load_and_filter("US30")
    if df is not None:
        all_signals.extend(generate_signals_for_data("US30", df, "US30 (Dukascopy)",
                           allow_second_chance=allow_second_chance))

    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals


def run_portfolio(all_signals):
    """Run the full portfolio engine. Returns (trades_list, results_dict)."""
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
        intraday_dd_pct = ((day_start_balance - current_balance) / day_start_balance) * 100 \
            if day_start_balance > 0 else 0

        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True

        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1
        if pnl < 0:
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1

        open_positions.append({"symbol": symbol, "exit_time": exit_time or entry_time})

        all_trades.append({
            "symbol": symbol,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "direction": s.direction,
            "entry_price": float(s.entry_price),
            "exit_price": float(s.exit_price) if s.exit_price else None,
            "exit_reason": exit_reason,
            "risk_points": risk_points,
            "pnl_points": pnl_points,
            "pnl_usd": round(pnl, 2),
            "lots": lots,
            "balance": round(current_balance, 2),
            "result": result,
            "costs_pts": round(total_cost_pts, 2),
        })

    trades_df = pd.DataFrame(all_trades)
    return trades_df, account_resets, skipped_reasons


def compute_metrics(trades_df, label=""):
    """Compute standard metrics from trades DataFrame."""
    if trades_df.empty:
        return {}

    wins = trades_df[trades_df["pnl_usd"] > 0]
    losses = trades_df[trades_df["pnl_usd"] < 0]

    gross_profit = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    win_rate = len(wins) / len(trades_df) * 100

    avg_win = wins["pnl_usd"].mean() if len(wins) > 0 else 0
    avg_loss = losses["pnl_usd"].mean() if len(losses) > 0 else 0

    monthly = trades_df.set_index("entry_time").resample("ME")["pnl_usd"].sum()
    sharpe = (monthly.mean() / monthly.std() * np.sqrt(12)) if monthly.std() > 0 else 0
    neg = monthly[monthly < 0]
    sortino = (monthly.mean() / neg.std() * np.sqrt(12)) if len(neg) > 0 and neg.std() > 0 else 0

    cum = trades_df["pnl_usd"].cumsum()
    peak = cum.cummax()
    dd = cum - peak
    max_dd = abs(dd.min()) if len(dd) > 0 else 0

    net_pnl = trades_df["pnl_usd"].sum()

    # Consecutive losses
    is_loss = (trades_df["pnl_usd"] < 0).astype(int)
    streaks = is_loss * (is_loss.groupby((is_loss != is_loss.shift()).cumsum()).cumcount() + 1)
    max_consec_losses = int(streaks.max()) if len(streaks) > 0 else 0

    # Second-chance specific metrics
    second_chance_trades = 0
    second_chance_wins = 0
    days_with_trades = trades_df.groupby(trades_df["entry_time"].dt.date)
    double_trade_days = 0
    for day_date, day_trades in days_with_trades:
        for sym in day_trades["symbol"].unique():
            sym_trades = day_trades[day_trades["symbol"] == sym]
            if len(sym_trades) >= 2:
                double_trade_days += 1
                # The 2nd trade is the second-chance trade
                t2 = sym_trades.iloc[1]
                second_chance_trades += 1
                if t2["pnl_usd"] > 0:
                    second_chance_wins += 1

    return {
        "label": label,
        "total_trades": len(trades_df),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "pf": round(pf, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "net_pnl": round(net_pnl, 0),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_dd_usd": round(max_dd, 0),
        "max_consec_losses": max_consec_losses,
        "gross_profit": round(gross_profit, 0),
        "gross_loss": round(gross_loss, 0),
        "second_chance_trades": second_chance_trades,
        "second_chance_wins": second_chance_wins,
        "double_trade_days": double_trade_days,
    }


def per_index_metrics(trades_df):
    """Compute metrics per index."""
    results = {}
    for sym in sorted(trades_df["symbol"].unique()):
        sym_df = trades_df[trades_df["symbol"] == sym].copy()
        results[sym] = compute_metrics(sym_df, label=sym)
    return results


def main():
    print("=" * 75)
    print("  SECOND-CHANCE RULE A/B TEST")
    print("  5-Index Combined Portfolio (SPX500, NDX100, GER30, FRA40, US30)")
    print("=" * 75)

    # ── Run A: Original (1 trade/day) ──
    print("\n  [A] Generating signals: Original 1-trade/day ...")
    sigs_a = generate_all_signals(allow_second_chance=False)
    print(f"      Raw signals: {len(sigs_a)}")
    trades_a, resets_a, skipped_a = run_portfolio(sigs_a)
    m_a = compute_metrics(trades_a, "A: Original")
    pi_a = per_index_metrics(trades_a)

    # ── Run B: Second Chance ──
    print(f"\n  [B] Generating signals: Second-Chance rule ...")
    sigs_b = generate_all_signals(allow_second_chance=True)
    print(f"      Raw signals: {len(sigs_b)}")
    trades_b, resets_b, skipped_b = run_portfolio(sigs_b)
    m_b = compute_metrics(trades_b, "B: Second Chance")
    pi_b = per_index_metrics(trades_b)

    # ── COMPARISON TABLE ──
    print("\n" + "=" * 75)
    print("  HEAD-TO-HEAD COMPARISON")
    print("=" * 75)

    def fmt(val, fmt_str=".2f"):
        if isinstance(val, (int, np.integer)):
            return f"{val:,}"
        return f"{val:{fmt_str}}"

    headers = ["Metric", "A: Original", "B: Second Chance", "Delta"]
    rows = [
        ("Total Trades", m_a["total_trades"], m_b["total_trades"],
         m_b["total_trades"] - m_a["total_trades"]),
        ("Win Rate %", m_a["win_rate"], m_b["win_rate"],
         round(m_b["win_rate"] - m_a["win_rate"], 1)),
        ("Profit Factor", m_a["pf"], m_b["pf"],
         round(m_b["pf"] - m_a["pf"], 2)),
        ("Sharpe Ratio", m_a["sharpe"], m_b["sharpe"],
         round(m_b["sharpe"] - m_a["sharpe"], 2)),
        ("Sortino Ratio", m_a["sortino"], m_b["sortino"],
         round(m_b["sortino"] - m_a["sortino"], 2)),
        ("Net PnL ($)", f"${m_a['net_pnl']:,.0f}", f"${m_b['net_pnl']:,.0f}",
         f"${m_b['net_pnl'] - m_a['net_pnl']:+,.0f}"),
        ("Max DD ($)", f"${m_a['max_dd_usd']:,.0f}", f"${m_b['max_dd_usd']:,.0f}",
         f"${m_b['max_dd_usd'] - m_a['max_dd_usd']:+,.0f}"),
        ("Max Consec Losses", m_a["max_consec_losses"], m_b["max_consec_losses"],
         m_b["max_consec_losses"] - m_a["max_consec_losses"]),
        ("Avg Win ($)", f"${m_a['avg_win']:,.2f}", f"${m_b['avg_win']:,.2f}", ""),
        ("Avg Loss ($)", f"${m_a['avg_loss']:,.2f}", f"${m_b['avg_loss']:,.2f}", ""),
        ("Account Resets", resets_a, resets_b, resets_b - resets_a),
    ]

    print(f"\n  {'Metric':25s}  {'A: Original':>16s}  {'B: Second Chance':>16s}  {'Delta':>12s}")
    print(f"  {'-'*25}  {'-'*16}  {'-'*16}  {'-'*12}")
    for row in rows:
        name, va, vb, delta = row
        print(f"  {name:25s}  {str(va):>16s}  {str(vb):>16s}  {str(delta):>12s}")

    # ── SECOND-CHANCE SPECIFIC STATS ──
    print(f"\n  -- Second-Chance Rule Stats (B only) --")
    print(f"  2nd-chance trades taken:  {m_b['second_chance_trades']}")
    print(f"  2nd-chance wins:          {m_b['second_chance_wins']}")
    sc_wr = (m_b['second_chance_wins'] / m_b['second_chance_trades'] * 100) \
        if m_b['second_chance_trades'] > 0 else 0
    print(f"  2nd-chance win rate:      {sc_wr:.1f}%")
    print(f"  Days with double trades:  {m_b['double_trade_days']}")

    # ── PER-INDEX BREAKDOWN ──
    print("\n" + "=" * 75)
    print("  PER-INDEX COMPARISON")
    print("=" * 75)

    all_syms = sorted(set(list(pi_a.keys()) + list(pi_b.keys())))
    print(f"\n  {'Index':10s}  {'Trades A':>10s}  {'Trades B':>10s}  "
          f"{'PF A':>8s}  {'PF B':>8s}  "
          f"{'WR A':>7s}  {'WR B':>7s}  "
          f"{'PnL A':>12s}  {'PnL B':>12s}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*8}  {'-'*8}  "
          f"{'-'*7}  {'-'*7}  {'-'*12}  {'-'*12}")

    for sym in all_syms:
        a = pi_a.get(sym, {})
        b = pi_b.get(sym, {})
        print(f"  {sym:10s}  "
              f"{a.get('total_trades', 0):10,d}  {b.get('total_trades', 0):10,d}  "
              f"{a.get('pf', 0):8.2f}  {b.get('pf', 0):8.2f}  "
              f"{a.get('win_rate', 0):6.1f}%  {b.get('win_rate', 0):6.1f}%  "
              f"${a.get('net_pnl', 0):>10,.0f}  ${b.get('net_pnl', 0):>10,.0f}")

    # ── RECOMMENDATION ──
    print("\n" + "=" * 75)
    print("  RECOMMENDATION")
    print("=" * 75)

    # Criteria: PF >= 1.40, Sharpe >= 1.5, more trades without quality dilution
    b_better = (
        m_b["pf"] >= 1.40 and
        m_b["sharpe"] >= 1.50 and
        m_b["pf"] >= m_a["pf"] * 0.90 and  # PF didn't drop > 10%
        m_b["net_pnl"] > m_a["net_pnl"]
    )
    b_worse = (
        m_b["pf"] < 1.30 or
        m_b["pf"] < m_a["pf"] * 0.85 or
        m_b["max_dd_usd"] > m_a["max_dd_usd"] * 1.20
    )

    if b_better:
        print("\n  VERDICT: ENABLE Second-Chance rule")
        print(f"  Reason: More trades ({m_b['total_trades']} vs {m_a['total_trades']}), "
              f"quality maintained (PF {m_b['pf']} vs {m_a['pf']})")
        print(f"  Extra PnL: ${m_b['net_pnl'] - m_a['net_pnl']:+,.0f}")
    elif b_worse:
        print("\n  VERDICT: KEEP Original (1 trade/day)")
        print(f"  Reason: Second-chance rule degrades quality "
              f"(PF {m_b['pf']} vs {m_a['pf']}, DD ${m_b['max_dd_usd']:,.0f} vs ${m_a['max_dd_usd']:,.0f})")
    else:
        print("\n  VERDICT: MARGINAL - Review manually")
        print(f"  PF: {m_a['pf']} -> {m_b['pf']}")
        print(f"  PnL: ${m_a['net_pnl']:,.0f} -> ${m_b['net_pnl']:,.0f}")
        print(f"  DD: ${m_a['max_dd_usd']:,.0f} -> ${m_b['max_dd_usd']:,.0f}")

    print("\n" + "=" * 75)

    # Save results
    output_dir = Path(__file__).parent / "reports" / "histdata_backtest"
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_b.to_csv(output_dir / "second_chance_trades.csv", index=False)
    print(f"\n  Second-chance trades saved: {output_dir / 'second_chance_trades.csv'}")


if __name__ == "__main__":
    main()
