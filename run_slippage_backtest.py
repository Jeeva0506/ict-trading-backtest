"""
Worst-Case Slippage Backtest (v3.14.1 validation)
=====================================================
Simulates realistic live execution by adding entry delay (1-3 bars)
and recalculating TP from the drifted fill price.

Scenarios:
  A) IDEAL   — Original backtest (entry = next bar open after retest)
  B) 1-BAR   — Enter 1 bar later (open of bar after ideal entry)
  C) 2-BAR   — Enter 2 bars later
  D) 3-BAR   — Enter 3 bars later (worst case from live logs)

For each delayed entry:
  - New entry = open of the delayed bar
  - SL stays at structural level (retest candle extreme) — unchanged
  - R_new = |new_entry - SL|
  - TP_new = new_entry ± R_new × 2.5  (v3.14.1 behavior)
  - Trade simulated from delayed bar onwards with new TP/SL/R

Live drift data (Stellar-2 logs, Feb-Mar 2026):
  SPX500: 3-13 pts drift (sigR 7.9, liveR 7.7-20.9)
  NDX100: 33-68 pts drift (sigR 33-40, liveR 72-101)
  GER30:  22-93 pts drift (sigR 14-41, liveR 8-52)
  US30:   ~0 pts drift
  US2000: ~4 pts drift
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).parent))
from strategy.strategy_ict_clean import StrategyICTClean, TradeSignalICT
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, load_data,
    compute_htf_ema, get_htf_trend_for_day,
    ACCOUNT_RULES, MAX_OPEN_POSITIONS, MAX_DAILY_LOSSES, LEVERAGE,
)


def simulate_trade_with_delay(df, entry_idx, direction, sl_init, delay_bars, tp_r=2.5, 
                               trail_buffer_r=0.5, be_trigger_r=1.0, cutoff_time=None):
    """
    Simulate a trade with N bars of entry delay.
    Entry = open of bar (entry_idx + delay_bars).
    SL stays at sl_init (structural level).
    TP = entry ± |entry - SL| × tp_r (v3.14.1 recalc).
    """
    delayed_idx = entry_idx + delay_bars
    if delayed_idx >= len(df):
        return None
    
    new_entry = float(df.iloc[delayed_idx]["open"])
    
    # R = distance from new entry to original SL
    if direction == "bullish":
        R = new_entry - sl_init
    else:
        R = sl_init - new_entry
    
    # If R <= 0 or too small, trade would be rejected by live guards
    if R <= 0:
        return None  # price crossed SL — would be rejected
    
    # TP recalculated from fill (v3.14.1)
    if direction == "bullish":
        tp_price = new_entry + R * tp_r
        be_price = new_entry + R * be_trigger_r
    else:
        tp_price = new_entry - R * tp_r
        be_price = new_entry - R * be_trigger_r
    
    # If cutoff not provided, use last bar - 15 min
    if cutoff_time is None:
        cutoff_time = df.index[-1] - pd.Timedelta(minutes=15)
    
    # Simulate bar by bar
    hit_be = False
    max_favorable = 0.0
    current_sl = sl_init
    start_idx = delayed_idx + 1  # simulate from bar AFTER entry
    
    if start_idx >= len(df):
        return None
    
    result = {
        "entry_price": new_entry,
        "entry_time": df.index[delayed_idx],
        "sl_init": sl_init,
        "tp": tp_price,
        "R": R,
        "direction": direction,
        "delay_bars": delay_bars,
    }
    
    for i in range(start_idx, len(df)):
        bar = df.iloc[i]
        ts = df.index[i]
        hi = float(bar["high"])
        lo = float(bar["low"])
        
        # Force close near session end
        if ts >= cutoff_time:
            exit_price = float(bar["close"])
            if direction == "bullish":
                pnl_pts = exit_price - new_entry
            else:
                pnl_pts = new_entry - exit_price
            result.update({"exit_price": exit_price, "exit_time": ts,
                          "pnl_points": pnl_pts, "exit_reason": "SESSION_CLOSE"})
            return result
        
        # SL check first (conservative)
        if direction == "bullish" and lo <= current_sl:
            pnl_pts = current_sl - new_entry
            result.update({"exit_price": current_sl, "exit_time": ts,
                          "pnl_points": pnl_pts, 
                          "exit_reason": "SL_BE" if hit_be else "SL_HIT"})
            return result
        if direction == "bearish" and hi >= current_sl:
            pnl_pts = new_entry - current_sl
            result.update({"exit_price": current_sl, "exit_time": ts,
                          "pnl_points": pnl_pts,
                          "exit_reason": "SL_BE" if hit_be else "SL_HIT"})
            return result
        
        # TP check
        if direction == "bullish" and hi >= tp_price:
            pnl_pts = tp_price - new_entry
            result.update({"exit_price": tp_price, "exit_time": ts,
                          "pnl_points": pnl_pts, "exit_reason": "TP"})
            return result
        if direction == "bearish" and lo <= tp_price:
            pnl_pts = new_entry - tp_price
            result.update({"exit_price": tp_price, "exit_time": ts,
                          "pnl_points": pnl_pts, "exit_reason": "TP"})
            return result
        
        # BE trigger (at 1R)
        if direction == "bullish":
            unrealized_r = (hi - new_entry) / R if R > 0 else 0
        else:
            unrealized_r = (new_entry - lo) / R if R > 0 else 0
        
        if not hit_be and unrealized_r >= 1.0:
            hit_be = True
            current_sl = new_entry  # move to BE
        
        # Trail after BE (0.5R buffer)
        if hit_be:
            if unrealized_r > max_favorable:
                max_favorable = unrealized_r
            trail_r = max(0.0, max_favorable - trail_buffer_r)
            if direction == "bullish":
                new_sl = new_entry + trail_r * R
                if new_sl > current_sl:
                    current_sl = new_sl
            else:
                new_sl = new_entry - trail_r * R
                if new_sl < current_sl:
                    current_sl = new_sl
    
    # Session end fallback
    last = df.iloc[-1]
    exit_price = float(last["close"])
    if direction == "bullish":
        pnl_pts = exit_price - new_entry
    else:
        pnl_pts = new_entry - exit_price
    result.update({"exit_price": exit_price, "exit_time": df.index[-1],
                  "pnl_points": pnl_pts, "exit_reason": "SESSION_CLOSE"})
    return result


def run_slippage_comparison(use_10yr=False):
    """Run A/B/C/D comparison: ideal vs 1/2/3 bar delay."""
    
    SYMBOLS = ["SPX500", "NDX100", "GER30", "US30", "FRA40", "US2000"]
    DELAY_SCENARIOS = [0, 1, 2, 3]  # bars of delay
    SCENARIO_NAMES = {0: "IDEAL", 1: "1-BAR", 2: "2-BAR", 3: "3-BAR"}
    
    # Determine data files
    data_dir = Path(__file__).parent / "data" / "raw"
    
    if use_10yr:
        M1_FILES = {
            "SPX500": "SPX500_M1_10YR.csv", "NDX100": "NDX100_M1_10YR.csv",
            "GER30": "GER30_M1_10YR.csv", "US30": "US30_M1_10YR.csv",
            "FRA40": "FRA40_M1_10YR.csv", "US2000": "US2000_M1_10YR.csv",
        }
        H1_FILES = {
            "SPX500": "SPX500_H1_EMA.csv", "NDX100": "NDX100_H1_EMA.csv",
            "GER30": "GER30_H1_EMA.csv", "US30": "US30_H1_EMA.csv",
            "FRA40": "FRA40_H1_EMA.csv", "US2000": "US2000_H1_EMA.csv",
        }
    else:
        M1_FILES = {
            "SPX500": "SPX500_M1_CLEAN.csv", "NDX100": "NDX100_M1_CLEAN.csv",
            "GER30": "GER30_M1_CLEAN.csv", "US30": "US30_M1_CLEAN.csv",
            "FRA40": "FRA40_M1_CLEAN.csv", "US2000": "US2000_M1_CLEAN.csv",
        }
        H1_FILES = {
            "SPX500": "SPX500_H1_EMA.csv", "NDX100": "NDX100_H1_EMA.csv",
            "GER30": "GER30_H1_EMA.csv", "US30": "US30_H1_EMA.csv",
            "FRA40": "FRA40_H1_EMA.csv", "US2000": "US2000_H1_EMA.csv",
        }
    
    # Filter to available symbols
    available = []
    for sym in SYMBOLS:
        m1_path = data_dir / M1_FILES.get(sym, "")
        if m1_path.exists():
            available.append(sym)
        else:
            print(f"  [SKIP] {sym}: data file not found ({M1_FILES.get(sym, 'N/A')})")
    
    print("=" * 78)
    print("WORST-CASE SLIPPAGE BACKTEST — Live Execution Reality Check")
    print("=" * 78)
    print(f"  Indices: {', '.join(available)}")
    print(f"  Scenarios: {', '.join(SCENARIO_NAMES.values())}")
    print(f"  TP recalc: YES (v3.14.1 — TP = fill ± R × 2.5)")
    print(f"  SL: Structural (unchanged)")
    print()
    
    # Collect results per scenario per symbol
    # results[delay][symbol] = list of trade dicts
    results = {d: {s: [] for s in available} for d in DELAY_SCENARIOS}
    
    for sym in available:
        print(f"\n{'─' * 50}")
        print(f"  Processing {sym}...")
        
        cfg = LIVE_INDEX_CONFIG[sym]
        m1_file = M1_FILES[sym]
        h1_file = H1_FILES.get(sym)
        
        df = load_data(m1_file)
        
        # HTF EMA
        if h1_file and (data_dir / h1_file).exists():
            h1 = pd.read_csv(data_dir / h1_file, sep="\t")
            time_col = [c for c in h1.columns if c.lower() == "time"][0]
            h1[time_col] = pd.to_datetime(h1[time_col], format="mixed", dayfirst=False)
            h1 = h1.set_index(time_col).sort_index()
            h1.columns = [c.lower() for c in h1.columns]
            h1["ema20"] = h1["close"].ewm(span=20, adjust=False).mean()
            h1["ema50"] = h1["close"].ewm(span=50, adjust=False).mean()
            h1["htf_trend"] = "bullish"
            h1.loc[h1["ema20"] < h1["ema50"], "htf_trend"] = "bearish"
            htf_ema = h1[["htf_trend", "ema20", "ema50"]]
        else:
            htf_ema = compute_htf_ema(df, fast=20, slow=50)
        
        trading_days = sorted(set(df.index.date))
        
        for day in trading_days:
            session_start = pd.Timestamp(day).replace(
                hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
            session_end = pd.Timestamp(day).replace(
                hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
            market_df = df[(df.index >= session_start) & (df.index <= session_end)].copy()
            if len(market_df) < 80:
                continue
            
            htf_trend = get_htf_trend_for_day(htf_ema, session_start)
            cutoff_close = market_df.index[-1] - pd.Timedelta(minutes=15)
            
            # Generate signals using IDEAL (0-delay) strategy
            strategy = StrategyICTClean(
                sl_buffer=cfg["sl_buffer"],
                min_sl_points=cfg.get("min_sl_points", 2.0),
                max_sl_points=cfg.get("max_sl_points", 25.0),
                debug=False,
                orb_minutes=cfg.get("orb_minutes", 15),
                entry_hours=cfg.get("entry_hours", 3),
                breakout_window_minutes=cfg.get("breakout_window_minutes", 120),
                disp_mult=cfg.get("disp_mult", 1.5),
                body_min=cfg.get("body_min", 0.60),
                close_strength=cfg.get("close_strength", 0.30),
                breakout_margin=cfg.get("breakout_margin", 0.10),
                orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
                tp3_r=2.5,
                orb_bias_filter=False,  # v3.14.0
            )
            
            signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
            if not signals:
                continue
            
            sig = signals[0]  # 1 trade per day per symbol
            
            # Find the entry bar index in market_df
            entry_ts = pd.Timestamp(sig.entry_time)
            entry_idx_arr = market_df.index.get_indexer([entry_ts], method="nearest")
            if len(entry_idx_arr) == 0:
                continue
            entry_idx = entry_idx_arr[0]
            
            # For each delay scenario, simulate the trade
            for delay in DELAY_SCENARIOS:
                if delay == 0:
                    # IDEAL: use original signal results directly
                    results[delay][sym].append({
                        "date": str(day),
                        "direction": sig.direction,
                        "entry_price": sig.entry_price,
                        "sl_init": sig.stop_loss,
                        "tp": sig.tp3,
                        "R": sig.risk_points,
                        "exit_price": sig.exit_price,
                        "exit_reason": sig.exit_reason,
                        "pnl_points": sig.pnl_points,
                        "delay_bars": 0,
                        "entry_time": str(sig.entry_time),
                    })
                else:
                    # Delayed: simulate from shifted entry
                    trade = simulate_trade_with_delay(
                        df=market_df,
                        entry_idx=entry_idx,
                        direction=sig.direction,
                        sl_init=sig.stop_loss,
                        delay_bars=delay,
                        tp_r=2.5,
                        trail_buffer_r=0.5,
                        be_trigger_r=1.0,
                        cutoff_time=cutoff_close,
                    )
                    if trade is None:
                        # Trade not possible (SL crossed, out of data)
                        results[delay][sym].append({
                            "date": str(day),
                            "direction": sig.direction,
                            "entry_price": 0,
                            "sl_init": sig.stop_loss,
                            "tp": 0,
                            "R": 0,
                            "exit_price": 0,
                            "exit_reason": "SKIPPED_DRIFT",
                            "pnl_points": 0,
                            "delay_bars": delay,
                            "entry_time": str(sig.entry_time),
                        })
                    else:
                        results[delay][sym].append({
                            "date": str(day),
                            **trade,
                        })
        
        print(f"  {sym}: {len(results[0][sym])} signals across {len(trading_days)} days")
    
    # ═══════════════════════════════════════════════════════════════
    #  RESULTS COMPARISON
    # ═══════════════════════════════════════════════════════════════
    print("\n\n")
    print("=" * 78)
    print("RESULTS: IDEAL vs WORST-CASE SLIPPAGE")
    print("=" * 78)
    
    # Per-symbol summary
    for sym in available:
        print(f"\n{'━' * 60}")
        print(f"  {sym}")
        print(f"{'━' * 60}")
        print(f"  {'Scenario':<10} {'Trades':>7} {'Wins':>6} {'WR%':>7} "
              f"{'TotalR':>9} {'AvgR':>7} {'MaxDD_R':>8} {'TP%':>6} {'SL%':>6} {'BE%':>6}")
        
        for delay in DELAY_SCENARIOS:
            trades = results[delay][sym]
            if not trades:
                continue
            
            n = len(trades)
            valid = [t for t in trades if t["exit_reason"] != "SKIPPED_DRIFT"]
            n_valid = len(valid)
            
            if n_valid == 0:
                continue
            
            wins = [t for t in valid if t["pnl_points"] > 0]
            pnl_r = []
            for t in valid:
                R = t.get("R", 0)
                if R and R > 0:
                    pnl_r.append(t["pnl_points"] / R)
                else:
                    pnl_r.append(0)
            
            total_r = sum(pnl_r)
            avg_r = total_r / n_valid if n_valid else 0
            wr = len(wins) / n_valid * 100 if n_valid else 0
            
            # Max drawdown in R
            cum_r = np.cumsum(pnl_r)
            peak = np.maximum.accumulate(cum_r)
            dd = cum_r - peak
            max_dd_r = abs(min(dd)) if len(dd) > 0 else 0
            
            # Exit reason breakdown
            tp_pct = len([t for t in valid if t["exit_reason"] == "TP"]) / n_valid * 100
            sl_pct = len([t for t in valid if t["exit_reason"] == "SL_HIT"]) / n_valid * 100
            be_pct = len([t for t in valid if t["exit_reason"] == "SL_BE"]) / n_valid * 100
            skipped = n - n_valid
            
            name = SCENARIO_NAMES[delay]
            skip_info = f" ({skipped} skipped)" if skipped > 0 else ""
            print(f"  {name:<10} {n_valid:>7} {len(wins):>6} {wr:>6.1f}% "
                  f"{total_r:>+9.1f} {avg_r:>+6.3f} {max_dd_r:>8.1f} "
                  f"{tp_pct:>5.1f}% {sl_pct:>5.1f}% {be_pct:>5.1f}%{skip_info}")
    
    # Portfolio summary
    print(f"\n\n{'═' * 78}")
    print("PORTFOLIO SUMMARY (all indices combined)")
    print(f"{'═' * 78}")
    print(f"  {'Scenario':<10} {'Trades':>7} {'Wins':>6} {'WR%':>7} "
          f"{'TotalR':>9} {'AvgR':>7} {'Profit':>7} {'Expectancy':>11} {'Skipped':>8}")
    
    for delay in DELAY_SCENARIOS:
        all_trades = []
        for sym in available:
            all_trades.extend(results[delay][sym])
        
        n = len(all_trades)
        valid = [t for t in all_trades if t["exit_reason"] != "SKIPPED_DRIFT"]
        n_valid = len(valid)
        skipped = n - n_valid
        
        if n_valid == 0:
            continue
        
        wins = [t for t in valid if t["pnl_points"] > 0]
        pnl_r = []
        for t in valid:
            R = t.get("R", 0)
            if R and R > 0:
                pnl_r.append(t["pnl_points"] / R)
            else:
                pnl_r.append(0)
        
        total_r = sum(pnl_r)
        avg_r = total_r / n_valid if n_valid else 0
        wr = len(wins) / n_valid * 100 if n_valid else 0
        
        # Profit factor
        gross_win = sum(r for r in pnl_r if r > 0)
        gross_loss = abs(sum(r for r in pnl_r if r < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
        
        # Expectancy = avg win × WR - avg loss × (1-WR)
        win_r = [r for r in pnl_r if r > 0]
        loss_r = [r for r in pnl_r if r < 0]
        avg_win = np.mean(win_r) if win_r else 0
        avg_loss = abs(np.mean(loss_r)) if loss_r else 0
        expectancy = (avg_win * wr/100) - (avg_loss * (1 - wr/100))
        
        name = SCENARIO_NAMES[delay]
        print(f"  {name:<10} {n_valid:>7} {len(wins):>6} {wr:>6.1f}% "
              f"{total_r:>+9.1f} {avg_r:>+6.3f} {pf:>6.2f}x "
              f"{expectancy:>+10.4f}R {skipped:>8}")
    
    # Delta analysis
    print(f"\n{'─' * 78}")
    print("DELTA vs IDEAL")
    print(f"{'─' * 78}")
    
    ideal_total = 0
    ideal_wr = 0
    ideal_n = 0
    for sym in available:
        valid = [t for t in results[0][sym] if t["exit_reason"] != "SKIPPED_DRIFT"]
        for t in valid:
            R = t.get("R", 0)
            if R and R > 0:
                ideal_total += t["pnl_points"] / R
        ideal_n += len(valid)
    ideal_wr = sum(1 for sym in available 
                   for t in results[0][sym] 
                   if t.get("pnl_points", 0) > 0) / ideal_n * 100 if ideal_n else 0
    
    for delay in [1, 2, 3]:
        delay_total = 0
        delay_wins = 0
        delay_n = 0
        for sym in available:
            valid = [t for t in results[delay][sym] if t["exit_reason"] != "SKIPPED_DRIFT"]
            for t in valid:
                R = t.get("R", 0)
                if R and R > 0:
                    delay_total += t["pnl_points"] / R
            delay_n += len(valid)
            delay_wins += sum(1 for t in valid if t.get("pnl_points", 0) > 0)
        delay_wr = delay_wins / delay_n * 100 if delay_n else 0
        
        name = SCENARIO_NAMES[delay]
        r_delta = delay_total - ideal_total
        wr_delta = delay_wr - ideal_wr
        r_pct_change = (r_delta / ideal_total * 100) if ideal_total else 0
        
        print(f"  {name} vs IDEAL: "
              f"ΔR = {r_delta:+.1f} ({r_pct_change:+.1f}%)  |  "
              f"ΔWR = {wr_delta:+.1f}%  |  "
              f"Trades: {delay_n} (skipped: {ideal_n - delay_n})")
    
    print(f"\n{'═' * 78}")
    print("NOTE: Skipped trades = price crossed SL before delayed entry (R <= 0)")
    print("These would be rejected by the live post-fill risk guard.")
    print(f"{'═' * 78}")


if __name__ == "__main__":
    use_10yr = "--10yr" in sys.argv
    if use_10yr:
        print("Running on 10-year data...\n")
    else:
        print("Running on ~2.8-year clean data (use --10yr for full dataset)...\n")
    run_slippage_comparison(use_10yr=use_10yr)
