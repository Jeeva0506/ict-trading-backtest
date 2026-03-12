"""
A+ Setup Analysis: Liquidity Sweep before ORB Breakout
========================================================
For each trade, check if price swept the OPPOSITE ORB level before breaking out.
  - Bullish breakout: price swept below ORB low (grabbed sell-side liquidity) before breaking high
  - Bearish breakout: price swept above ORB high (grabbed buy-side liquidity) before breaking low

Reports WR and RR for sweep (A+) vs non-sweep trades.
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


def check_sweep(session_df, cfg, direction, breakout_time):
    """
    Check if price swept the opposite ORB level before the breakout.
    
    For bullish breakout: did price wick below ORB low before breaking above ORB high?
    For bearish breakout: did price wick above ORB high before breaking below ORB low?
    
    Returns: (sweep_found: bool, sweep_depth_pct: float)
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
    between = session_df[(session_df.index >= orb_end) & (session_df.index <= pd.Timestamp(breakout_time))]
    
    if len(between) == 0:
        return False, 0.0
    
    if direction == "bullish":
        # For bullish breakout: check if any candle's LOW went below ORB low (swept sell-side)
        min_low = float(between["low"].min())
        if min_low < orbL:
            sweep_depth = (orbL - min_low) / orb_range  # how deep the sweep was
            return True, sweep_depth
    else:
        # For bearish breakout: check if any candle's HIGH went above ORB high (swept buy-side)
        max_high = float(between["high"].max())
        if max_high > orbH:
            sweep_depth = (max_high - orbH) / orb_range  
            return True, sweep_depth
    
    return False, 0.0


def generate_signals_with_sweep():
    """Generate signals and tag each with sweep/no-sweep."""
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
                sweep_found = False
                sweep_depth = 0.0
                
                if breakout_time is not None:
                    sweep_found, sweep_depth = check_sweep(
                        market_df, cfg, s.direction, breakout_time)
                
                all_signals.append({
                    "symbol": sym,
                    "signal": s,
                    "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                    "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                    "sweep": sweep_found,
                    "sweep_depth": sweep_depth,
                })
        
        sweep_count = sum(1 for x in all_signals if x["symbol"] == sym and x["sweep"])
        total = sum(1 for x in all_signals if x["symbol"] == sym)
        print(f"  {sym}: {total} signals, {sweep_count} with sweep ({sweep_count/total*100:.0f}%)" if total > 0 else f"  {sym}: 0 signals")
    
    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals


def run_portfolio_with_sweep(all_signals):
    """Execute portfolio backtest, preserving sweep tag for each trade."""
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
        
        overall_dd_pct = ((STARTING_CAPITAL - current_balance) / STARTING_CAPITAL) * 100 if STARTING_CAPITAL > 0 else 0
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True
        
        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1
        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1
        
        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})
        
        # RR: reward-to-risk ratio for this trade
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
            "sweep": sig["sweep"],
            "sweep_depth": sig["sweep_depth"],
        })
    
    return all_trades


def analyze_results(trades):
    """Analyze and compare sweep vs no-sweep trades."""
    df = pd.DataFrame(trades)
    
    if len(df) == 0:
        print("No trades executed!")
        return
    
    def stats(subset, label):
        n = len(subset)
        if n == 0:
            print(f"\n  {label}: 0 trades")
            return
        
        wins = len(subset[subset["result"].isin(["win", "partial_win"])])
        losses = len(subset[subset["result"] == "loss"])
        wr = wins / n * 100
        
        avg_win_pts = subset[subset["pnl_points"] > 0]["pnl_points"].mean() if wins > 0 else 0
        avg_loss_pts = abs(subset[subset["pnl_points"] < 0]["pnl_points"].mean()) if losses > 0 else 0
        avg_rr = avg_win_pts / avg_loss_pts if avg_loss_pts > 0 else float("inf")
        
        net_pnl = subset["pnl"].sum()
        gross_profit = subset[subset["pnl"] > 0]["pnl"].sum()
        gross_loss = subset[subset["pnl"] < 0]["pnl"].sum()
        pf = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")
        
        avg_rr_actual = subset["rr"].mean()
        
        # Expectancy per trade (in R)
        expectancy_r = subset["rr"].mean()
        
        # Exit reason breakdown
        tp_pct = len(subset[subset["exit_reason"] == "TP"]) / n * 100
        sl_pct = len(subset[subset["exit_reason"] == "SL_HIT"]) / n * 100
        be_pct = len(subset[subset["exit_reason"] == "SL_BE"]) / n * 100
        sc_pct = len(subset[subset["exit_reason"] == "SESSION_CLOSE"]) / n * 100
        
        print(f"\n  {label}")
        print(f"  {'─'*50}")
        print(f"  Trades:        {n}")
        print(f"  Win Rate:      {wr:.1f}%  ({wins}W / {losses}L)")
        print(f"  Profit Factor: {pf:.2f}")
        print(f"  Avg Win (pts): {avg_win_pts:.1f}")
        print(f"  Avg Loss(pts): {avg_loss_pts:.1f}")
        print(f"  Avg RR:        {avg_rr:.2f}")
        print(f"  Expectancy/R:  {expectancy_r:.2f}R")
        print(f"  Net P&L:       ${net_pnl:,.0f}")
        print(f"  Exits: TP={tp_pct:.0f}% SL={sl_pct:.0f}% BE={be_pct:.0f}% Close={sc_pct:.0f}%")
    
    print("\n" + "="*60)
    print("  A+ SETUP ANALYSIS: Liquidity Sweep before Breakout")
    print("="*60)
    
    # Overall
    stats(df, "ALL TRADES (baseline)")
    
    sweep_df = df[df["sweep"] == True]
    no_sweep_df = df[df["sweep"] == False]
    
    stats(sweep_df, "SWEEP TRADES (A+ setup)")
    stats(no_sweep_df, "NO-SWEEP TRADES")
    
    # Per-index breakdown
    print("\n" + "="*60)
    print("  PER-INDEX SWEEP vs NO-SWEEP")
    print("="*60)
    
    for sym in ALL_SYMBOLS:
        sym_df = df[df["symbol"] == sym]
        if len(sym_df) == 0:
            continue
        
        sym_sweep = sym_df[sym_df["sweep"] == True]
        sym_no = sym_df[sym_df["sweep"] == False]
        
        sw_n = len(sym_sweep)
        ns_n = len(sym_no)
        
        sw_wr = (len(sym_sweep[sym_sweep["result"].isin(["win", "partial_win"])]) / sw_n * 100) if sw_n > 0 else 0
        ns_wr = (len(sym_no[sym_no["result"].isin(["win", "partial_win"])]) / ns_n * 100) if ns_n > 0 else 0
        
        sw_rr = sym_sweep["rr"].mean() if sw_n > 0 else 0
        ns_rr = sym_no["rr"].mean() if ns_n > 0 else 0
        
        sw_pnl = sym_sweep["pnl"].sum() if sw_n > 0 else 0
        ns_pnl = sym_no["pnl"].sum() if ns_n > 0 else 0
        
        sw_pf_gross = sym_sweep[sym_sweep["pnl"] > 0]["pnl"].sum() if sw_n > 0 else 0
        sw_pf_loss = sym_sweep[sym_sweep["pnl"] < 0]["pnl"].sum() if sw_n > 0 else 0
        sw_pf = abs(sw_pf_gross / sw_pf_loss) if sw_pf_loss != 0 else float("inf")
        
        ns_pf_gross = sym_no[sym_no["pnl"] > 0]["pnl"].sum() if ns_n > 0 else 0
        ns_pf_loss = sym_no[sym_no["pnl"] < 0]["pnl"].sum() if ns_n > 0 else 0
        ns_pf = abs(ns_pf_gross / ns_pf_loss) if ns_pf_loss != 0 else float("inf")
        
        print(f"\n  {sym}:")
        print(f"    Sweep:    {sw_n:3d} trades | WR {sw_wr:5.1f}% | PF {sw_pf:5.2f} | Avg RR {sw_rr:+.2f}R | P&L ${sw_pnl:>+9,.0f}")
        print(f"    No-sweep: {ns_n:3d} trades | WR {ns_wr:5.1f}% | PF {ns_pf:5.2f} | Avg RR {ns_rr:+.2f}R | P&L ${ns_pnl:>+9,.0f}")
    
    # Sweep depth analysis
    if len(sweep_df) > 0:
        print("\n" + "="*60)
        print("  SWEEP DEPTH ANALYSIS (deeper sweep = more liquidity grabbed)")
        print("="*60)
        
        # Categorize by sweep depth
        shallow = sweep_df[sweep_df["sweep_depth"] <= 0.10]  # 0-10% of ORB range
        medium = sweep_df[(sweep_df["sweep_depth"] > 0.10) & (sweep_df["sweep_depth"] <= 0.30)]
        deep = sweep_df[sweep_df["sweep_depth"] > 0.30]
        
        for label, sub in [("Shallow (0-10% ORB)", shallow), 
                           ("Medium (10-30% ORB)", medium),
                           ("Deep (30%+ ORB)", deep)]:
            n = len(sub)
            if n == 0:
                print(f"\n  {label}: 0 trades")
                continue
            wins = len(sub[sub["result"].isin(["win", "partial_win"])])
            wr = wins / n * 100
            avg_rr = sub["rr"].mean()
            pnl = sub["pnl"].sum()
            print(f"\n  {label}: {n} trades | WR {wr:.1f}% | Avg RR {avg_rr:+.2f}R | P&L ${pnl:>+,.0f}")
    
    # Direction breakdown
    print("\n" + "="*60)
    print("  DIRECTION BREAKDOWN")
    print("="*60)
    for direction in ["bullish", "bearish"]:
        dir_df = df[df["direction"] == direction]
        dir_sweep = dir_df[dir_df["sweep"] == True]
        dir_no = dir_df[dir_df["sweep"] == False]
        
        dn = len(dir_df)
        sn = len(dir_sweep)
        nn = len(dir_no)
        
        if dn > 0:
            swr = (len(dir_sweep[dir_sweep["result"].isin(["win", "partial_win"])]) / sn * 100) if sn > 0 else 0
            nwr = (len(dir_no[dir_no["result"].isin(["win", "partial_win"])]) / nn * 100) if nn > 0 else 0
            srr = dir_sweep["rr"].mean() if sn > 0 else 0
            nrr = dir_no["rr"].mean() if nn > 0 else 0
            
            print(f"\n  {direction.upper()} ({dn} total):")
            print(f"    Sweep:    {sn:3d} trades | WR {swr:5.1f}% | Avg RR {srr:+.2f}R")
            print(f"    No-sweep: {nn:3d} trades | WR {nwr:5.1f}% | Avg RR {nrr:+.2f}R")
    
    print("\n" + "="*60)


if __name__ == "__main__":
    print("Generating signals with sweep detection...\n")
    signals = generate_signals_with_sweep()
    print(f"\nTotal signals: {len(signals)}")
    
    sweep_total = sum(1 for s in signals if s["sweep"])
    print(f"Sweep signals: {sweep_total} ({sweep_total/len(signals)*100:.1f}%)")
    
    print("\nRunning portfolio backtest...")
    trades = run_portfolio_with_sweep(signals)
    print(f"Executed trades: {len(trades)}")
    
    analyze_results(trades)
