"""
A+ Setup Analysis v2: Failed Breakout + PDH/PDL Sweep
=======================================================
Two A+ filters checked independently and combined:

1. FAILED BREAKOUT (opposite side trap):
   - Bearish A+: Price trades ABOVE ORB high (close/body, not just wick)
     but fails to confirm bullish → then gives bearish breakout below ORB low.
   - Bullish A+: Price trades BELOW ORB low but fails to confirm bearish
     → then gives bullish breakout above ORB high.

2. PDH/PDL SWEEP:
   - Bearish A+: Price swept Previous Day High before bearish breakout
   - Bullish A+: Price swept Previous Day Low before bullish breakout
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, ADAPTIVE_RISK,
    ACCOUNT_RULES, FUNDEDNEXT_RULES, MAX_OPEN_POSITIONS, MAX_DAILY_LOSSES,
    MAX_DAILY_TRADES, LEVERAGE, load_data, compute_htf_ema, get_htf_trend_for_day,
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


def check_failed_breakout(session_df, cfg, direction, breakout_time):
    """
    Check if price went beyond the OPPOSITE ORB level (close, not just wick)
    before the actual breakout. This is a failed breakout / trap.

    For BEARISH breakout: did any candle CLOSE above ORB high before breaking low?
    For BULLISH breakout: did any candle CLOSE below ORB low before breaking high?

    Returns: (failed_bo_found: bool, how_far_pct: float)
    """
    session_start = session_df.index[0]
    orb_minutes = cfg.get("orb_minutes", 15)
    orb_end = session_start + pd.Timedelta(minutes=orb_minutes)

    orb = session_df[(session_df.index >= session_start) & (session_df.index < orb_end)]
    if len(orb) < 3:
        return False, 0.0

    orbH = float(orb["high"].max())
    orbL = float(orb["low"].min())
    orb_range = orbH - orbL
    if orb_range <= 0:
        return False, 0.0

    # Candles between ORB end and breakout candle
    between = session_df[(session_df.index >= orb_end) & (session_df.index < pd.Timestamp(breakout_time))]
    if len(between) == 0:
        return False, 0.0

    if direction == "bearish":
        # Bearish breakout: check if any candle CLOSED above ORB high
        # (price traded above high = false bullish breakout)
        closes_above = between[between["close"] > orbH]
        if len(closes_above) > 0:
            max_close = float(closes_above["close"].max())
            depth = (max_close - orbH) / orb_range
            return True, depth
        # Also check if any candle's HIGH went above ORB high significantly
        # (body went above, even if close pulled back)
        highs_above = between[between["high"] > orbH]
        if len(highs_above) > 0:
            # Check if any of these had body (open or close) above ORB high
            for _, bar in highs_above.iterrows():
                bar_body_top = max(float(bar["open"]), float(bar["close"]))
                if bar_body_top > orbH:
                    depth = (bar_body_top - orbH) / orb_range
                    return True, depth

    elif direction == "bullish":
        # Bullish breakout: check if any candle CLOSED below ORB low
        closes_below = between[between["close"] < orbL]
        if len(closes_below) > 0:
            min_close = float(closes_below["close"].min())
            depth = (orbL - min_close) / orb_range
            return True, depth
        # Check body below
        lows_below = between[between["low"] < orbL]
        if len(lows_below) > 0:
            for _, bar in lows_below.iterrows():
                bar_body_bot = min(float(bar["open"]), float(bar["close"]))
                if bar_body_bot < orbL:
                    depth = (orbL - bar_body_bot) / orb_range
                    return True, depth

    return False, 0.0


def check_pdh_pdl_sweep(full_df, session_df, cfg, direction, breakout_time, trade_day):
    """
    Check if price swept PDH (Previous Day High) or PDL (Previous Day Low)
    before the breakout.

    For BEARISH breakout: did price go above PDH (swept buy-side) before breaking down?
    For BULLISH breakout: did price go below PDL (swept sell-side) before breaking up?

    Returns: (pdhl_swept: bool, pd_level: float, sweep_amount: float)
    """
    session_start = session_df.index[0]
    orb_minutes = cfg.get("orb_minutes", 15)
    orb_end = session_start + pd.Timedelta(minutes=orb_minutes)

    # Get previous trading day's data
    # Use session times to find previous day's session
    from datetime import timedelta
    prev_day = trade_day - timedelta(days=1)

    # Look for previous session data (handle weekends)
    for lookback in range(1, 5):  # up to 4 days back for weekends
        check_day = trade_day - timedelta(days=lookback)
        prev_start = pd.Timestamp(check_day).replace(
            hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
        prev_end = pd.Timestamp(check_day).replace(
            hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])

        prev_session = full_df[(full_df.index >= prev_start) & (full_df.index <= prev_end)]
        if len(prev_session) >= 30:
            break
    else:
        return False, 0.0, 0.0

    pdh = float(prev_session["high"].max())
    pdl = float(prev_session["low"].min())

    # Check candles from session start up to breakout time
    today_before_bo = session_df[(session_df.index >= session_start) & 
                                  (session_df.index <= pd.Timestamp(breakout_time))]
    if len(today_before_bo) == 0:
        return False, 0.0, 0.0

    if direction == "bearish":
        # Swept PDH: price went above previous day's high
        max_high = float(today_before_bo["high"].max())
        if max_high > pdh:
            return True, pdh, max_high - pdh
    elif direction == "bullish":
        # Swept PDL: price went below previous day's low
        min_low = float(today_before_bo["low"].min())
        if min_low < pdl:
            return True, pdl, pdl - min_low

    return False, 0.0, 0.0


def generate_signals_with_tags():
    """Generate signals and tag each with failed-breakout and PDH/PDL sweep."""
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

        sym_stats = {"total": 0, "failed_bo": 0, "pdhl": 0, "both": 0}

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
                breakout_time = getattr(s, 'breakout_time', None)
                failed_bo = False
                failed_bo_depth = 0.0
                pdhl_swept = False
                pdhl_level = 0.0
                pdhl_amount = 0.0

                if breakout_time is not None:
                    failed_bo, failed_bo_depth = check_failed_breakout(
                        market_df, cfg, s.direction, breakout_time)

                    pdhl_swept, pdhl_level, pdhl_amount = check_pdh_pdl_sweep(
                        df, market_df, cfg, s.direction, breakout_time, day)

                sym_stats["total"] += 1
                if failed_bo:
                    sym_stats["failed_bo"] += 1
                if pdhl_swept:
                    sym_stats["pdhl"] += 1
                if failed_bo and pdhl_swept:
                    sym_stats["both"] += 1

                all_signals.append({
                    "symbol": sym,
                    "signal": s,
                    "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                    "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                    "failed_bo": failed_bo,
                    "failed_bo_depth": failed_bo_depth,
                    "pdhl_swept": pdhl_swept,
                    "pdhl_amount": pdhl_amount,
                })

        t = sym_stats["total"]
        if t > 0:
            print(f"  {sym}: {t} signals | Failed BO: {sym_stats['failed_bo']} ({sym_stats['failed_bo']/t*100:.0f}%) | PDH/PDL: {sym_stats['pdhl']} ({sym_stats['pdhl']/t*100:.0f}%) | Both: {sym_stats['both']} ({sym_stats['both']/t*100:.0f}%)")

    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals


def run_portfolio(all_signals):
    """Execute portfolio backtest with tags preserved."""
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

        rr = pnl_points / risk_points if risk_points > 0 else 0

        all_trades.append({
            "symbol": symbol,
            "date": entry_time,
            "direction": s.direction,
            "result": result,
            "risk_points": risk_points,
            "pnl_points": pnl_points,
            "adjusted_pnl_points": adjusted_pnl_points,
            "pnl": pnl,
            "balance": current_balance,
            "lots": lots,
            "exit_reason": exit_reason,
            "rr": rr,
            "failed_bo": sig["failed_bo"],
            "failed_bo_depth": sig["failed_bo_depth"],
            "pdhl_swept": sig["pdhl_swept"],
            "pdhl_amount": sig["pdhl_amount"],
        })

    return all_trades


def stats_line(subset, label, indent="  "):
    """Print one-line stats for a subset."""
    n = len(subset)
    if n == 0:
        print(f"{indent}{label}: 0 trades")
        return

    wins = len(subset[subset["result"].isin(["win", "partial_win"])])
    losses = len(subset[subset["result"] == "loss"])
    wr = wins / n * 100

    net_pnl = subset["pnl"].sum()
    gross_profit = subset[subset["pnl"] > 0]["pnl"].sum()
    gross_loss = subset[subset["pnl"] < 0]["pnl"].sum()
    pf = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")

    avg_rr = subset["rr"].mean()

    avg_win_pts = subset[subset["pnl_points"] > 0]["pnl_points"].mean() if wins > 0 else 0
    avg_loss_pts = abs(subset[subset["pnl_points"] < 0]["pnl_points"].mean()) if losses > 0 else 0
    actual_rr = avg_win_pts / avg_loss_pts if avg_loss_pts > 0 else 0

    tp_pct = len(subset[subset["exit_reason"] == "TP"]) / n * 100
    sl_pct = len(subset[subset["exit_reason"] == "SL_HIT"]) / n * 100
    be_pct = len(subset[subset["exit_reason"] == "SL_BE"]) / n * 100

    print(f"{indent}{label}")
    print(f"{indent}  Trades: {n} | WR: {wr:.1f}% ({wins}W/{losses}L) | PF: {pf:.2f} | Avg RR: {avg_rr:+.2f}R | Actual W/L RR: {actual_rr:.2f} | P&L: ${net_pnl:>+,.0f}")
    print(f"{indent}  Exits: TP={tp_pct:.0f}% SL={sl_pct:.0f}% BE={be_pct:.0f}%")


def analyze_results(trades):
    """Full analysis."""
    df = pd.DataFrame(trades)
    if len(df) == 0:
        print("No trades!")
        return

    print("\n" + "=" * 70)
    print("  A+ SETUP v2: Failed Breakout + PDH/PDL Sweep Analysis")
    print("=" * 70)

    # ── BASELINE ──
    stats_line(df, "ALL TRADES (baseline)")

    # ── FAILED BREAKOUT ──
    print("\n" + "─" * 70)
    print("  FILTER 1: FAILED BREAKOUT (opposite side traded before real BO)")
    print("─" * 70)
    fbo_yes = df[df["failed_bo"] == True]
    fbo_no = df[df["failed_bo"] == False]
    stats_line(fbo_yes, "FAILED BO (A+ trap)")
    stats_line(fbo_no, "CLEAN breakout (no trap)")

    # ── PDH/PDL SWEEP ──
    print("\n" + "─" * 70)
    print("  FILTER 2: PDH/PDL SWEEP (swept prev day level before BO)")
    print("─" * 70)
    pdhl_yes = df[df["pdhl_swept"] == True]
    pdhl_no = df[df["pdhl_swept"] == False]
    stats_line(pdhl_yes, "PDH/PDL SWEPT (A+)")
    stats_line(pdhl_no, "NO PDH/PDL sweep")

    # ── COMBINED: Both filters ──
    print("\n" + "─" * 70)
    print("  COMBINED: Failed BO + PDH/PDL Sweep")
    print("─" * 70)
    both = df[(df["failed_bo"] == True) & (df["pdhl_swept"] == True)]
    either = df[(df["failed_bo"] == True) | (df["pdhl_swept"] == True)]
    neither = df[(df["failed_bo"] == False) & (df["pdhl_swept"] == False)]
    stats_line(both, "BOTH (A++ ultra)")
    stats_line(either, "EITHER (A+)")
    stats_line(neither, "NEITHER (standard)")

    # ── PER-INDEX BREAKDOWN ──
    print("\n" + "=" * 70)
    print("  PER-INDEX: Failed BO + PDH/PDL")
    print("=" * 70)

    for sym in ALL_SYMBOLS:
        sym_df = df[df["symbol"] == sym]
        if len(sym_df) == 0:
            continue
        n = len(sym_df)

        sym_fbo = sym_df[sym_df["failed_bo"] == True]
        sym_nofbo = sym_df[sym_df["failed_bo"] == False]
        sym_pdhl = sym_df[sym_df["pdhl_swept"] == True]
        sym_nopdhl = sym_df[sym_df["pdhl_swept"] == False]
        sym_both = sym_df[(sym_df["failed_bo"] == True) & (sym_df["pdhl_swept"] == True)]

        fbo_n = len(sym_fbo)
        pdhl_n = len(sym_pdhl)
        both_n = len(sym_both)

        fbo_wr = (len(sym_fbo[sym_fbo["result"].isin(["win", "partial_win"])]) / fbo_n * 100) if fbo_n > 0 else 0
        nofbo_wr = (len(sym_nofbo[sym_nofbo["result"].isin(["win", "partial_win"])]) / len(sym_nofbo) * 100) if len(sym_nofbo) > 0 else 0

        pdhl_wr = (len(sym_pdhl[sym_pdhl["result"].isin(["win", "partial_win"])]) / pdhl_n * 100) if pdhl_n > 0 else 0
        nopdhl_wr = (len(sym_nopdhl[sym_nopdhl["result"].isin(["win", "partial_win"])]) / len(sym_nopdhl) * 100) if len(sym_nopdhl) > 0 else 0

        fbo_rr = sym_fbo["rr"].mean() if fbo_n > 0 else 0
        nofbo_rr = sym_nofbo["rr"].mean() if len(sym_nofbo) > 0 else 0
        pdhl_rr = sym_pdhl["rr"].mean() if pdhl_n > 0 else 0
        nopdhl_rr = sym_nopdhl["rr"].mean() if len(sym_nopdhl) > 0 else 0

        fbo_pnl = sym_fbo["pnl"].sum() if fbo_n > 0 else 0
        nofbo_pnl = sym_nofbo["pnl"].sum() if len(sym_nofbo) > 0 else 0
        pdhl_pnl = sym_pdhl["pnl"].sum() if pdhl_n > 0 else 0
        nopdhl_pnl = sym_nopdhl["pnl"].sum() if len(sym_nopdhl) > 0 else 0

        print(f"\n  {sym} ({n} trades):")
        print(f"    Failed BO:  {fbo_n:3d} trades | WR {fbo_wr:5.1f}% | RR {fbo_rr:+.2f}R | P&L ${fbo_pnl:>+9,.0f}")
        print(f"    Clean BO:   {len(sym_nofbo):3d} trades | WR {nofbo_wr:5.1f}% | RR {nofbo_rr:+.2f}R | P&L ${nofbo_pnl:>+9,.0f}")
        print(f"    PDH/PDL:    {pdhl_n:3d} trades | WR {pdhl_wr:5.1f}% | RR {pdhl_rr:+.2f}R | P&L ${pdhl_pnl:>+9,.0f}")
        print(f"    No PDH/PDL: {len(sym_nopdhl):3d} trades | WR {nopdhl_wr:5.1f}% | RR {nopdhl_rr:+.2f}R | P&L ${nopdhl_pnl:>+9,.0f}")
        if both_n > 0:
            both_wr = len(sym_both[sym_both["result"].isin(["win", "partial_win"])]) / both_n * 100
            both_rr = sym_both["rr"].mean()
            both_pnl = sym_both["pnl"].sum()
            print(f"    BOTH:       {both_n:3d} trades | WR {both_wr:5.1f}% | RR {both_rr:+.2f}R | P&L ${both_pnl:>+9,.0f}")

    # ── DIRECTION BREAKDOWN ──
    print("\n" + "=" * 70)
    print("  DIRECTION: Failed BO + PDH/PDL")
    print("=" * 70)

    for direction in ["bullish", "bearish"]:
        dir_df = df[df["direction"] == direction]
        if len(dir_df) == 0:
            continue

        fbo = dir_df[dir_df["failed_bo"] == True]
        nofbo = dir_df[dir_df["failed_bo"] == False]
        pdhl = dir_df[dir_df["pdhl_swept"] == True]
        nopdhl = dir_df[dir_df["pdhl_swept"] == False]

        fn = len(fbo)
        pn = len(pdhl)

        fwr = (len(fbo[fbo["result"].isin(["win", "partial_win"])]) / fn * 100) if fn > 0 else 0
        nwr = (len(nofbo[nofbo["result"].isin(["win", "partial_win"])]) / len(nofbo) * 100) if len(nofbo) > 0 else 0
        pwr = (len(pdhl[pdhl["result"].isin(["win", "partial_win"])]) / pn * 100) if pn > 0 else 0

        frr = fbo["rr"].mean() if fn > 0 else 0
        nrr = nofbo["rr"].mean() if len(nofbo) > 0 else 0
        prr = pdhl["rr"].mean() if pn > 0 else 0

        print(f"\n  {direction.upper()} ({len(dir_df)} total):")
        print(f"    Failed BO:  {fn:3d} trades | WR {fwr:5.1f}% | RR {frr:+.2f}R")
        print(f"    Clean BO:   {len(nofbo):3d} trades | WR {nwr:5.1f}% | RR {nrr:+.2f}R")
        print(f"    PDH/PDL:    {pn:3d} trades | WR {pwr:5.1f}% | RR {prr:+.2f}R")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    print("Generating signals with Failed BO + PDH/PDL detection...\n")
    signals = generate_signals_with_tags()
    total = len(signals)
    fbo_total = sum(1 for s in signals if s["failed_bo"])
    pdhl_total = sum(1 for s in signals if s["pdhl_swept"])
    both_total = sum(1 for s in signals if s["failed_bo"] and s["pdhl_swept"])
    print(f"\nTotal signals: {total}")
    print(f"Failed BO: {fbo_total} ({fbo_total/total*100:.1f}%)")
    print(f"PDH/PDL swept: {pdhl_total} ({pdhl_total/total*100:.1f}%)")
    print(f"Both: {both_total} ({both_total/total*100:.1f}%)")

    print("\nRunning portfolio backtest...")
    trades = run_portfolio(signals)
    print(f"Executed trades: {len(trades)}")

    analyze_results(trades)
