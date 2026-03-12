"""
FRA40: Custom Params vs Universal Params A/B Test
=================================================
Runs FRA40 with two param sets:
  A = Custom (disp_mult=1.5, body_min=0.60, close_strength=0.30, breakout_margin=0.10, entry_hours=2)
  B = Universal (disp_mult=1.0, body_min=0.70, close_strength=0.25, breakout_margin=0.15, entry_hours=3)
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

EMA_FAST, EMA_SLOW = 20, 50

DATA_FILE = "FRA40_10YR_M1_CLEAN.csv"

# Two param sets to compare
CUSTOM_FRA40 = {
    "disp_mult": 1.5, "body_min": 0.60, "close_strength": 0.30,
    "breakout_margin": 0.10, "entry_hours": 2,
}
UNIVERSAL = {
    "disp_mult": 1.0, "body_min": 0.70, "close_strength": 0.25,
    "breakout_margin": 0.15, "entry_hours": 3,
}


def load_fra40():
    try:
        df = load_data(DATA_FILE)
    except FileNotFoundError:
        print(f"  File not found: {DATA_FILE}")
        return None
    return df if len(df) > 0 else None


def generate_signals(df, param_override=None, label="FRA40"):
    """Generate signals with optional param override."""
    cfg = LIVE_INDEX_CONFIG.get("FRA40")
    if cfg is None:
        print("  FRA40 not in LIVE_INDEX_CONFIG!")
        return []

    htf_ema = compute_htf_ema(df, fast=EMA_FAST, slow=EMA_SLOW)
    trading_days = sorted(set(df.index.date))
    point_value = BROKER_POINT_VALUES["FRA40"]

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

        kw = {
            "sl_buffer": cfg["sl_buffer"],
            "min_sl_points": cfg.get("min_sl_points", 2.0),
            "max_sl_points": cfg.get("max_sl_points", 30.0),
            "orb_minutes": cfg.get("orb_minutes", 15),
            "entry_hours": cfg.get("entry_hours", 2),
            "breakout_window_minutes": cfg.get("breakout_window_minutes", 60),
            "disp_mult": cfg.get("disp_mult", 1.5),
            "body_min": cfg.get("body_min", 0.60),
            "close_strength": cfg.get("close_strength", 0.30),
            "breakout_margin": cfg.get("breakout_margin", 0.10),
            "orb_range_min_mult": cfg.get("orb_range_min_mult", 0.5),
            "tp3_r": 2.5,
        }
        if param_override:
            kw.update(param_override)

        strategy = StrategyICTClean(**kw)
        day_signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
        for s in day_signals:
            signals.append({
                "symbol": "FRA40", "segment": label, "signal": s,
                "point_value": point_value,
                "entry_time": pd.Timestamp(s.entry_time),
                "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
            })

    if skipped_days > 0:
        print(f"      Skipped {skipped_days}/{len(trading_days)} days (<80 bars)")
    return signals


def run_portfolio(all_signals):
    """Run portfolio sim with same constraints as main backtest."""
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    MAX_DD_PCT = ACCOUNT_RULES.get("max_drawdown_pct", 10.0)
    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL
    open_positions = []
    daily_losses = {}
    daily_trade_counts = {}
    all_trades = []
    skipped = {"max_positions": 0, "daily_loss_limit": 0, "adaptive_stop": 0,
               "max_daily_trades": 0, "spread_guard": 0, "price_drift": 0,
               "stale_signal": 0, "min_R": 0}
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
            skipped["daily_loss_limit"] += 1
            continue
        if daily_trade_counts.get(trade_day, 0) >= MAX_DAILY_TRADES:
            skipped["max_daily_trades"] += 1
            continue
        open_positions = [p for p in open_positions if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            skipped["max_positions"] += 1
            continue

        MIN_R = 5.0
        if risk_points < MIN_R:
            skipped["min_R"] += 1
            continue

        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        if risk_points > 0 and (spread_pts / risk_points) > 0.30:
            skipped["spread_guard"] += 1
            continue

        breakout_time = getattr(s, 'breakout_time', None)
        if breakout_time is not None:
            bars_delay = int((pd.Timestamp(s.entry_time) -
                              pd.Timestamp(breakout_time)).total_seconds() / 60)
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
        entry_cost = slippage_pts + half_spread
        exit_cost = half_spread if exit_reason == "TP" else slippage_pts + half_spread
        total_cost_pts = entry_cost + exit_cost
        commission_cost = TRADING_COSTS["commission_per_lot"].get(symbol, 0.0) * lots

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

        result = "win" if pnl > 0 else ("breakeven" if pnl == 0 else "loss")
        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1
        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1
        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})

        all_trades.append({
            "symbol": symbol, "date": entry_time, "result": result,
            "risk_points": risk_points, "pnl_points": pnl_points,
            "pnl_raw": pnl_raw, "costs": total_costs, "pnl": pnl,
            "balance": current_balance, "lots": lots, "exit_reason": exit_reason,
        })

    return all_trades, skipped, current_balance, peak_balance, account_resets


def calc_metrics(trades):
    """Quick metrics from trade list."""
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    total = len(df)
    wins = len(df[df["pnl"] > 0])
    wr = wins / total * 100 if total > 0 else 0
    gross_win = df[df["pnl"] > 0]["pnl"].sum()
    gross_loss = abs(df[df["pnl"] < 0]["pnl"].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    net_pnl = df["pnl"].sum()

    # Max DD
    balances = df["balance"].values
    peak = balances[0]
    max_dd = 0
    for b in balances:
        peak = max(peak, b)
        dd = peak - b
        if dd > max_dd:
            max_dd = dd

    # Max consec losses
    streaks = []
    streak = 0
    for _, r in df.iterrows():
        if r["pnl"] < 0:
            streak += 1
        else:
            if streak > 0:
                streaks.append(streak)
            streak = 0
    if streak > 0:
        streaks.append(streak)
    mcl = max(streaks) if streaks else 0

    # Sharpe (daily)
    df["day"] = pd.to_datetime(df["date"]).dt.date
    daily_pnl = df.groupby("day")["pnl"].sum()
    sharpe = (daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)) if daily_pnl.std() > 0 else 0

    avg_win = df[df["pnl"] > 0]["pnl"].mean() if wins > 0 else 0
    avg_loss = df[df["pnl"] < 0]["pnl"].mean() if (total - wins) > 0 else 0

    return {
        "trades": total, "wins": wins, "wr": round(wr, 1),
        "pf": round(pf, 2), "net_pnl": round(net_pnl, 2),
        "max_dd": round(max_dd, 2), "mcl": mcl,
        "sharpe": round(sharpe, 2),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
    }


# ============================================================================
#  MAIN
# ============================================================================
if __name__ == "__main__":
    print("=" * 75)
    print("  FRA40: CUSTOM vs UNIVERSAL PARAMS")
    print("=" * 75)

    df = load_fra40()
    if df is None:
        print("No FRA40 data found. Exiting.")
        sys.exit(1)
    print(f"  Loaded FRA40: {len(df):,} M1 bars")

    # ---- VERSION A: Custom (current backtest params) ----
    print(f"\n  [A] Custom params: disp=1.5, body=0.60, close=0.30, bkout=0.10, hrs=2")
    sigs_a = generate_signals(df, param_override=CUSTOM_FRA40, label="FRA40-Custom")
    sigs_a.sort(key=lambda x: x["entry_time"])
    print(f"      -> {len(sigs_a)} signals")

    trades_a, skip_a, bal_a, peak_a, resets_a = run_portfolio(sigs_a)
    m_a = calc_metrics(trades_a)

    # ---- VERSION B: Universal ----
    print(f"\n  [B] Universal params: disp=1.0, body=0.70, close=0.25, bkout=0.15, hrs=3")
    sigs_b = generate_signals(df, param_override=UNIVERSAL, label="FRA40-Universal")
    sigs_b.sort(key=lambda x: x["entry_time"])
    print(f"      -> {len(sigs_b)} signals")

    trades_b, skip_b, bal_b, peak_b, resets_b = run_portfolio(sigs_b)
    m_b = calc_metrics(trades_b)

    # ---- COMPARISON ----
    print("\n" + "=" * 75)
    print("  HEAD-TO-HEAD: FRA40 ONLY")
    print("=" * 75)
    print(f"\n  {'Metric':<25} {'A: Custom':>16} {'B: Universal':>16} {'Delta':>12}")
    print(f"  {'-'*25} {'-'*16} {'-'*16} {'-'*12}")

    rows = [
        ("Total Trades", m_a.get("trades",0), m_b.get("trades",0)),
        ("Win Rate %", m_a.get("wr",0), m_b.get("wr",0)),
        ("Profit Factor", m_a.get("pf",0), m_b.get("pf",0)),
        ("Sharpe Ratio", m_a.get("sharpe",0), m_b.get("sharpe",0)),
        ("Net PnL ($)", f"${m_a.get('net_pnl',0):,.0f}", f"${m_b.get('net_pnl',0):,.0f}"),
        ("Max DD ($)", f"${m_a.get('max_dd',0):,.0f}", f"${m_b.get('max_dd',0):,.0f}"),
        ("Max Consec Losses", m_a.get("mcl",0), m_b.get("mcl",0)),
        ("Avg Win ($)", f"${m_a.get('avg_win',0):,.2f}", f"${m_b.get('avg_win',0):,.2f}"),
        ("Avg Loss ($)", f"${m_a.get('avg_loss',0):,.2f}", f"${m_b.get('avg_loss',0):,.2f}"),
        ("Account Resets", resets_a, resets_b),
    ]
    for label, va, vb in rows:
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            delta = vb - va
            ds = f"{delta:+}" if isinstance(delta, int) else f"{delta:+.2f}"
        else:
            ds = ""
        print(f"  {label:<25} {str(va):>16} {str(vb):>16} {ds:>12}")

    # Skipped
    print(f"\n  Skipped signals:")
    print(f"    {'Reason':<25} {'A':>8} {'B':>8}")
    for k in skip_a:
        print(f"    {k:<25} {skip_a[k]:>8} {skip_b[k]:>8}")

    # Verdict
    print("\n" + "=" * 75)
    print("  VERDICT")
    print("=" * 75)
    pnl_diff = m_b.get("net_pnl",0) - m_a.get("net_pnl",0)
    pf_diff = m_b.get("pf",0) - m_a.get("pf",0)
    dd_diff = m_b.get("max_dd",0) - m_a.get("max_dd",0)

    pf_a = m_a.get("pf", 0)
    pf_b = m_b.get("pf", 0)
    pnl_a = m_a.get("net_pnl", 0)
    pnl_b = m_b.get("net_pnl", 0)

    if pf_b > pf_a and pnl_b > pnl_a:
        verdict = "UNIVERSAL WINS"
    elif pf_a > pf_b and pnl_a > pnl_b:
        verdict = "CUSTOM WINS"
    else:
        verdict = "MIXED - Review manually"

    print(f"\n  {verdict}")
    print(f"  PF:     {pf_a:.2f} (Custom) vs {pf_b:.2f} (Universal)  [{pf_diff:+.2f}]")
    print(f"  PnL:    ${pnl_a:,.0f} vs ${pnl_b:,.0f}  [${pnl_diff:+,.0f}]")
    print(f"  DD:     ${m_a.get('max_dd',0):,.0f} vs ${m_b.get('max_dd',0):,.0f}  [${dd_diff:+,.0f}]")
    print(f"  Trades: {m_a.get('trades',0)} vs {m_b.get('trades',0)}  [{m_b.get('trades',0)-m_a.get('trades',0):+}]")
    print("=" * 75)
