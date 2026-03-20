"""
Realistic Slippage Backtest (v3.14.1 validation)
=====================================================
Instead of delaying by full bars (too pessimistic), this adds
FIXED SLIPPAGE in R-multiples to the entry price.

Live drift data analysis (Stellar-2 logs):
  GER30 Mar16:  sigR=38.2  liveR=36.1  drift=-5.5%  = -0.05R
  GER30 Mar17:  sigR=40.7  liveR=52.3  drift=+28.5%  = +0.28R
  SPX500 Mar12: sigR=5.6   liveR=16.6  drift=+195%   = +1.96R  (extreme)
  SPX500 Mar20: sigR=7.9   liveR=20.9  drift=+165%   = +1.65R  (extreme)
  NDX100 Mar12: sigR=33.3  liveR=101.3 drift=+204%   = +2.04R  (extreme)
  NDX100 Mar20: sigR=39.8  liveR=72.3  drift=+81%    = +0.81R
  SPX500 Mar12b:sigR=7.7   liveR=7.7   drift=~0%     = 0.00R   (clean)
  US30 Mar03:   sigR=28.5  liveR=28.5  drift=~0%     = 0.00R   (clean)

Typical: 0.0-0.3R drift (normal days)
Worst:   1.0-2.0R drift (fast markets like Mar 12, Mar 20)

Scenarios:
  A) IDEAL (0 slip)     — Original backtest
  B) 0.25R favorable    — Typical live drift
  C) 0.50R favorable    — Moderate drift
  D) 1.00R favorable    — Worst case seen in live
  E) 0.25R adverse      — Price moved against before fill

With v3.14.1 TP recalc: TP = fill ± |fill-SL| × 2.5
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
)


def simulate_trade_with_slippage(df, entry_idx, direction, entry_price, sl_init, R_orig,
                                  slip_r, tp_r=2.5, trail_buffer_r=0.5, be_trigger_r=1.0,
                                  cutoff_time=None):
    """
    Simulate a trade with fixed R-based slippage.
    
    slip_r > 0 = favorable drift (price moved in trade direction)
    slip_r < 0 = adverse drift (price moved against trade direction)
    
    For bearish (sell): favorable = price went DOWN → entry is lower
    For bullish (buy):  favorable = price went UP → entry is higher
    """
    # Apply slippage to entry
    slip_pts = slip_r * R_orig
    if direction == "bullish":
        new_entry = entry_price + slip_pts  # favorable = higher entry for buy
    else:
        new_entry = entry_price - slip_pts  # favorable = lower entry for sell
    
    # Recalculate R from slipped entry to original SL
    if direction == "bullish":
        new_R = new_entry - sl_init
    else:
        new_R = sl_init - new_entry
    
    if new_R <= 0:
        return None  # SL crossed — would be rejected by post-fill guard
    
    # TP recalculated from fill (v3.14.1)
    if direction == "bullish":
        tp_price = new_entry + new_R * tp_r
    else:
        tp_price = new_entry - new_R * tp_r
    
    if cutoff_time is None:
        cutoff_time = df.index[-1] - pd.Timedelta(minutes=15)
    
    # Simulate from the bar AFTER entry (same as ideal)
    # entry_idx is the actual entry bar. Simulation starts from entry_idx + 1.
    start_idx = entry_idx + 1
    if start_idx >= len(df):
        return None
    
    hit_be = False
    max_favorable = 0.0
    current_sl = sl_init  # SL stays at structural level
    
    result = {
        "entry_price": new_entry,
        "entry_time": df.index[entry_idx],
        "sl_init": sl_init,
        "tp": tp_price,
        "R": new_R,
        "R_orig": R_orig,
        "direction": direction,
        "slip_r": slip_r,
        "slip_pts": slip_pts,
    }
    
    for i in range(start_idx, len(df)):
        bar = df.iloc[i]
        ts = df.index[i]
        hi = float(bar["high"])
        lo = float(bar["low"])
        
        if ts >= cutoff_time:
            exit_price = float(bar["close"])
            pnl_pts = (exit_price - new_entry) if direction == "bullish" else (new_entry - exit_price)
            result.update({"exit_price": exit_price, "exit_time": ts,
                          "pnl_points": pnl_pts, "exit_reason": "SESSION_CLOSE"})
            return result
        
        # SL check
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
        
        # Unrealized R for BE/trail
        if direction == "bullish":
            unrealized_r = (hi - new_entry) / new_R if new_R > 0 else 0
        else:
            unrealized_r = (new_entry - lo) / new_R if new_R > 0 else 0
        
        if not hit_be and unrealized_r >= 1.0:
            hit_be = True
            current_sl = new_entry
        
        if hit_be:
            if unrealized_r > max_favorable:
                max_favorable = unrealized_r
            trail_r = max(0.0, max_favorable - trail_buffer_r)
            if direction == "bullish":
                new_sl = new_entry + trail_r * new_R
                if new_sl > current_sl:
                    current_sl = new_sl
            else:
                new_sl = new_entry - trail_r * new_R
                if new_sl < current_sl:
                    current_sl = new_sl
    
    # Fallback
    last = df.iloc[-1]
    exit_price = float(last["close"])
    pnl_pts = (exit_price - new_entry) if direction == "bullish" else (new_entry - exit_price)
    result.update({"exit_price": exit_price, "exit_time": df.index[-1],
                  "pnl_points": pnl_pts, "exit_reason": "SESSION_CLOSE"})
    return result


def run_realistic_slippage():
    """Run comparison with R-based slippage levels."""
    
    SYMBOLS = ["SPX500", "NDX100", "GER30", "US30", "FRA40", "US2000"]
    SCENARIOS = [
        (0.00, "IDEAL"),
        (0.25, "+0.25R FAV"),
        (0.50, "+0.50R FAV"),
        (1.00, "+1.00R FAV"),
        (-0.25, "-0.25R ADV"),
    ]
    
    data_dir = Path(__file__).parent / "data" / "raw"
    
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
    
    # 10yr fallback
    for sym in SYMBOLS:
        if not (data_dir / M1_FILES[sym]).exists():
            alt = f"{sym}_M1_10YR.csv"
            if (data_dir / alt).exists():
                M1_FILES[sym] = alt
    
    available = [s for s in SYMBOLS if (data_dir / M1_FILES[s]).exists()]
    
    print("=" * 80)
    print("REALISTIC SLIPPAGE BACKTEST — R-Based Drift Simulation (v3.14.1)")
    print("=" * 80)
    print(f"  Indices: {', '.join(available)}")
    print(f"  Scenarios: {', '.join(n for _, n in SCENARIOS)}")
    print(f"  TP recalc: YES (v3.14.1 — TP = fill ± new_R × 2.5)")
    print(f"  SL: Structural (unchanged)")
    print(f"  BE: at 1R from fill | Trail: 0.5R buffer from fill")
    print()
    
    # results[slip_r] = list of all trades
    results = {s: [] for s, _ in SCENARIOS}
    
    for sym in available:
        print(f"  Processing {sym}...", end=" ", flush=True)
        
        cfg = LIVE_INDEX_CONFIG[sym]
        df = load_data(M1_FILES[sym])
        
        h1_file = H1_FILES.get(sym)
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
        sig_count = 0
        
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
                orb_bias_filter=False,
            )
            
            signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
            if not signals:
                continue
            
            sig = signals[0]
            sig_count += 1
            
            # sig.entry_time = df.index[start_idx] = bar AFTER entry.
            # The actual entry bar is one bar BEFORE that.
            entry_ts = pd.Timestamp(sig.entry_time)
            entry_idx_arr = market_df.index.get_indexer([entry_ts], method="nearest")
            if len(entry_idx_arr) == 0:
                continue
            sim_start_idx = entry_idx_arr[0]  # first simulation bar
            entry_idx = max(0, sim_start_idx - 1)  # actual entry bar
            
            point_value = BROKER_POINT_VALUES.get(sym, 10.0)
            
            # Reconstruct initial SL from entry ± R
            # sig.stop_loss is MUTATED during backtest simulation (BE/trail)
            if sig.direction == "bullish":
                sl_init_orig = sig.entry_price - sig.risk_points
            else:
                sl_init_orig = sig.entry_price + sig.risk_points
            
            for slip_r, name in SCENARIOS:
                if slip_r == 0:
                    # IDEAL: use original signal
                    results[slip_r].append({
                        "symbol": sym,
                        "date": str(day),
                        "direction": sig.direction,
                        "entry_price": sig.entry_price,
                        "sl_init": sl_init_orig,
                        "tp": sig.tp3,
                        "R": sig.risk_points,
                        "R_orig": sig.risk_points,
                        "exit_price": sig.exit_price,
                        "exit_reason": sig.exit_reason,
                        "pnl_points": sig.pnl_points,
                        "slip_r": 0,
                        "point_value": point_value,
                    })
                else:
                    trade = simulate_trade_with_slippage(
                        df=market_df,
                        entry_idx=entry_idx,
                        direction=sig.direction,
                        entry_price=sig.entry_price,
                        sl_init=sl_init_orig,
                        R_orig=sig.risk_points,
                        slip_r=slip_r,
                        tp_r=2.5,
                        trail_buffer_r=0.5,
                        be_trigger_r=1.0,
                        cutoff_time=cutoff_close,
                    )
                    if trade is None:
                        results[slip_r].append({
                            "symbol": sym, "date": str(day),
                            "direction": sig.direction,
                            "entry_price": 0, "sl_init": sl_init_orig,
                            "tp": 0, "R": 0, "R_orig": sig.risk_points,
                            "exit_price": 0, "exit_reason": "SKIPPED_DRIFT",
                            "pnl_points": 0, "slip_r": slip_r,
                            "point_value": point_value,
                        })
                    else:
                        trade["symbol"] = sym
                        trade["point_value"] = point_value
                        results[slip_r].append(trade)
        
        print(f"{sig_count} signals")
    
    # ════════════════════════════════════════════════════════════════
    #  RESULTS
    # ════════════════════════════════════════════════════════════════
    
    # Per-symbol table
    for sym in available:
        print(f"\n{'━' * 70}")
        print(f"  {sym}")
        print(f"{'━' * 70}")
        print(f"  {'Scenario':<14} {'Trades':>6} {'Wins':>5} {'WR%':>6} "
              f"{'TotalR':>8} {'AvgR':>7} {'TP%':>5} {'SL%':>5} {'BE%':>5} {'Skip':>5}")
        
        for slip_r, name in SCENARIOS:
            sym_trades = [t for t in results[slip_r] if t["symbol"] == sym]
            valid = [t for t in sym_trades if t["exit_reason"] != "SKIPPED_DRIFT"]
            skipped = len(sym_trades) - len(valid)
            n = len(valid)
            if n == 0:
                print(f"  {name:<14} {'—':>6} {'—':>5} {'—':>6} {'—':>8} {'—':>7} {'—':>5} {'—':>5} {'—':>5} {skipped:>5}")
                continue
            
            wins = sum(1 for t in valid if t["pnl_points"] > 0)
            wr = wins / n * 100
            
            pnl_r = []
            for t in valid:
                R = t.get("R", 0)
                pnl_r.append(t["pnl_points"] / R if R > 0 else 0)
            total_r = sum(pnl_r)
            avg_r = total_r / n
            
            tp_pct = sum(1 for t in valid if t["exit_reason"] == "TP") / n * 100
            sl_pct = sum(1 for t in valid if t["exit_reason"] == "SL_HIT") / n * 100
            be_pct = sum(1 for t in valid if t["exit_reason"] == "SL_BE") / n * 100
            
            print(f"  {name:<14} {n:>6} {wins:>5} {wr:>5.1f}% "
                  f"{total_r:>+8.1f} {avg_r:>+6.3f} {tp_pct:>4.1f}% {sl_pct:>4.1f}% "
                  f"{be_pct:>4.1f}% {skipped:>5}")
    
    # Portfolio summary
    print(f"\n\n{'═' * 80}")
    print("PORTFOLIO SUMMARY (all indices combined)")
    print(f"{'═' * 80}")
    print(f"  {'Scenario':<14} {'Trades':>6} {'Wins':>5} {'WR%':>6} "
          f"{'TotalR':>8} {'Profit':>7} {'Expectancy':>11} {'Skip':>5}")
    
    ideal_total_r = None
    
    for slip_r, name in SCENARIOS:
        valid = [t for t in results[slip_r] if t["exit_reason"] != "SKIPPED_DRIFT"]
        skipped = len(results[slip_r]) - len(valid)
        n = len(valid)
        if n == 0:
            continue
        
        wins = sum(1 for t in valid if t["pnl_points"] > 0)
        wr = wins / n * 100
        
        pnl_r = []
        for t in valid:
            R = t.get("R", 0)
            pnl_r.append(t["pnl_points"] / R if R > 0 else 0)
        total_r = sum(pnl_r)
        avg_r = total_r / n
        
        gross_win = sum(r for r in pnl_r if r > 0)
        gross_loss = abs(sum(r for r in pnl_r if r < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
        
        win_r = [r for r in pnl_r if r > 0]
        loss_r = [r for r in pnl_r if r < 0]
        avg_win = np.mean(win_r) if win_r else 0
        avg_loss = abs(np.mean(loss_r)) if loss_r else 0
        expectancy = (avg_win * wr/100) - (avg_loss * (1 - wr/100))
        
        if slip_r == 0:
            ideal_total_r = total_r
        
        print(f"  {name:<14} {n:>6} {wins:>5} {wr:>5.1f}% "
              f"{total_r:>+8.1f} {pf:>6.2f}x {expectancy:>+10.4f}R {skipped:>5}")
    
    # Delta table
    print(f"\n{'─' * 80}")
    print("DELTA vs IDEAL")
    print(f"{'─' * 80}")
    
    for slip_r, name in SCENARIOS:
        if slip_r == 0:
            continue
        valid = [t for t in results[slip_r] if t["exit_reason"] != "SKIPPED_DRIFT"]
        n = len(valid)
        wins = sum(1 for t in valid if t["pnl_points"] > 0)
        wr = wins / n * 100 if n else 0
        pnl_r = [t["pnl_points"] / t["R"] if t.get("R", 0) > 0 else 0 for t in valid]
        total_r = sum(pnl_r)
        
        ideal_valid = [t for t in results[0] if t["exit_reason"] != "SKIPPED_DRIFT"]
        ideal_wins = sum(1 for t in ideal_valid if t["pnl_points"] > 0)
        ideal_wr = ideal_wins / len(ideal_valid) * 100
        
        r_delta = total_r - ideal_total_r if ideal_total_r else 0
        wr_delta = wr - ideal_wr
        r_pct = (r_delta / ideal_total_r * 100) if ideal_total_r else 0
        skip = len(results[slip_r]) - n
        
        print(f"  {name:<14}: ΔR = {r_delta:>+8.1f} ({r_pct:>+6.1f}%)  |  "
              f"ΔWR = {wr_delta:>+5.1f}%  |  Skipped: {skip}")
    
    # R-shift analysis
    print(f"\n{'─' * 80}")
    print("R-VALUE SHIFT ANALYSIS (how drift changes risk distance)")
    print(f"{'─' * 80}")
    for slip_r, name in SCENARIOS:
        if slip_r == 0:
            continue
        valid = [t for t in results[slip_r] if t.get("R", 0) > 0 and t.get("R_orig", 0) > 0]
        if not valid:
            continue
        r_ratios = [t["R"] / t["R_orig"] for t in valid]
        avg_ratio = np.mean(r_ratios)
        min_ratio = np.min(r_ratios)
        max_ratio = np.max(r_ratios)
        print(f"  {name:<14}: R_new/R_orig = {avg_ratio:.2f}x avg "
              f"(min {min_ratio:.2f}x, max {max_ratio:.2f}x)")
    
    print(f"\n{'═' * 80}")
    print("INTERPRETATION:")
    print("  • +0.25R FAV = typical live execution (bot fills within seconds)")
    print("  • +0.50R FAV = moderate momentum / minor scan delay")
    print("  • +1.00R FAV = extreme fast market (like SPX Mar 20: +1.65R)")
    print("  • -0.25R ADV = adverse fill (price bounced before order)")
    print("  • Favorable drift: R grows, TP further, SL unchanged → lower WR")
    print("  • Adverse drift: R shrinks, TP closer, risk of R<0 → could be rejected")
    print(f"{'═' * 80}")


if __name__ == "__main__":
    run_realistic_slippage()
