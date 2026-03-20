"""
Drift Guard Simulation — Find optimal max_drift_R threshold
============================================================
Market order + v3.14.1 TP recalc + REJECT trades where drift > threshold.

Uses actual per-trade drift measured from the data (not uniform like the
slippage backtest). Each signal has its real drift_r from entry_price to
sim_start bar's open.
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from strategy.strategy_ict_clean import StrategyICTClean
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, load_data,
    compute_htf_ema, get_htf_trend_for_day,
)
from run_slippage_realistic import simulate_trade_with_slippage


def run():
    SYMBOLS = ["SPX500", "NDX100", "GER30", "US30", "FRA40", "US2000"]
    THRESHOLDS = [0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 999]  # 999 = no guard

    # ── CLI flag: --10yr to prefer 10-year data ───────────────────
    use_10yr = "--10yr" in sys.argv

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
    # Upgrade to 10yr data if available (prefer {sym}_10YR_M1_CLEAN.csv)
    for sym in SYMBOLS:
        for alt in [f"{sym}_10YR_M1_CLEAN.csv", f"{sym}_M1_10YR.csv"]:
            if (data_dir / alt).exists():
                if use_10yr or not (data_dir / M1_FILES[sym]).exists():
                    M1_FILES[sym] = alt
                    # Also upgrade H1
                    h1_alt = f"{sym}_10YR_H1_EMA.csv"
                    if (data_dir / h1_alt).exists():
                        H1_FILES[sym] = h1_alt
                break

    available = [s for s in SYMBOLS if (data_dir / M1_FILES[s]).exists()]

    print("=" * 80)
    print("DRIFT GUARD SIMULATION — Market Order + v3.14.1 TP Recalc")
    print("=" * 80)
    print(f"  Indices: {', '.join(available)}")
    print(f"  Thresholds (max |drift| in R): {[t for t in THRESHOLDS if t < 100]}")
    print(f"  Each trade uses its ACTUAL drift from data")
    print(f"  Trades beyond threshold → SKIPPED")
    print()

    # Collect all signals with their actual drift
    all_signals = []

    for sym in available:
        print(f"  Loading {sym}...", end=" ", flush=True)
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
            count += 1

            entry_ts = pd.Timestamp(sig.entry_time)
            entry_idx_arr = market_df.index.get_indexer([entry_ts], method="nearest")
            if len(entry_idx_arr) == 0:
                continue
            sim_start_idx = entry_idx_arr[0]
            entry_idx = max(0, sim_start_idx - 1)

            # Reconstruct initial SL
            if sig.direction == "bullish":
                sl_init = sig.entry_price - sig.risk_points
            else:
                sl_init = sig.entry_price + sig.risk_points

            # Measure actual drift at sim_start bar open
            if sim_start_idx < len(market_df):
                current_price = float(market_df.iloc[sim_start_idx]["open"])
                if sig.direction == "bullish":
                    drift_pts = current_price - sig.entry_price
                else:
                    drift_pts = sig.entry_price - current_price
                drift_r = drift_pts / sig.risk_points if sig.risk_points > 0 else 0
            else:
                drift_pts = 0
                drift_r = 0

            all_signals.append({
                "symbol": sym,
                "date": str(day),
                "direction": sig.direction,
                "entry_price": sig.entry_price,
                "sl_init": sl_init,
                "R_orig": sig.risk_points,
                "entry_idx": entry_idx,
                "sim_start_idx": sim_start_idx,
                "market_df": market_df,
                "cutoff_close": cutoff_close,
                "drift_r": drift_r,
                "drift_pts": drift_pts,
                "abs_drift_r": abs(drift_r),
                # IDEAL result
                "ideal_pnl": sig.pnl_points,
                "ideal_R_result": sig.pnl_points / sig.risk_points if sig.risk_points > 0 else 0,
                "ideal_exit": sig.exit_reason,
                "ideal_win": sig.pnl_points > 0,
            })

        print(f"{count} signals")

    total = len(all_signals)
    print(f"\n  Total signals: {total}")

    # ── Per-threshold simulation ─────────────────────────────────
    print(f"\n{'═' * 80}")
    print(f"  DRIFT GUARD RESULTS — Market Order + TP Recalc + Drift Rejection")
    print(f"{'═' * 80}")

    # IDEAL baseline
    ideal_r = sum(s["ideal_R_result"] for s in all_signals)
    ideal_wins = sum(1 for s in all_signals if s["ideal_win"])
    print(f"\n  IDEAL BACKTEST: {total} trades | WR {ideal_wins/total*100:.1f}% | {ideal_r:+.1f}R")
    print()

    header = (f"  {'Threshold':<12} {'Taken':>6} {'Skip':>5} {'Skip%':>6} "
              f"{'WR%':>6} {'TotalR':>8} {'AvgR':>7} "
              f"{'SkipR':>8} {'SkipWR':>6}")
    print(header)
    print(f"  {'─' * 76}")

    best_r = -9999
    best_thresh = None

    for threshold in THRESHOLDS:
        taken = []
        skipped = []

        for sig_info in all_signals:
            if sig_info["abs_drift_r"] > threshold:
                skipped.append(sig_info)
                continue

            # Simulate with actual drift
            actual_drift_r = sig_info["drift_r"]

            if abs(actual_drift_r) < 0.01:
                # Near-zero drift — use IDEAL result
                taken.append({
                    "pnl_r": sig_info["ideal_R_result"],
                    "win": sig_info["ideal_win"],
                })
            else:
                trade = simulate_trade_with_slippage(
                    df=sig_info["market_df"],
                    entry_idx=sig_info["entry_idx"],
                    direction=sig_info["direction"],
                    entry_price=sig_info["entry_price"],
                    sl_init=sig_info["sl_init"],
                    R_orig=sig_info["R_orig"],
                    slip_r=actual_drift_r,
                    tp_r=2.5,
                    trail_buffer_r=0.5,
                    be_trigger_r=1.0,
                    cutoff_time=sig_info["cutoff_close"],
                )
                if trade is None:
                    skipped.append(sig_info)
                    continue
                pnl_r = trade["pnl_points"] / sig_info["R_orig"] if sig_info["R_orig"] > 0 else 0
                taken.append({"pnl_r": pnl_r, "win": trade["pnl_points"] > 0})

        n_taken = len(taken)
        n_skip = total - n_taken
        skip_pct = n_skip / total * 100

        if n_taken > 0:
            wins = sum(1 for t in taken if t["win"])
            wr = wins / n_taken * 100
            total_r = sum(t["pnl_r"] for t in taken)
            avg_r = total_r / n_taken
        else:
            wr = total_r = avg_r = 0

        # What we skipped — their IDEAL value
        skip_ideal_r = sum(s["ideal_R_result"] for s in skipped)
        skip_ideal_wins = sum(1 for s in skipped if s["ideal_win"])
        skip_wr = skip_ideal_wins / len(skipped) * 100 if skipped else 0

        label = f"±{threshold:.2f}R" if threshold < 100 else "NO GUARD"
        print(f"  {label:<12} {n_taken:>6} {n_skip:>5} {skip_pct:>5.1f}% "
              f"{wr:>5.1f}% {total_r:>+8.1f} {avg_r:>+7.3f} "
              f"{skip_ideal_r:>+8.1f} {skip_wr:>5.1f}%")

        if total_r > best_r:
            best_r = total_r
            best_thresh = threshold

    print(f"\n  ★ Best threshold: ±{best_thresh:.2f}R → {best_r:+.1f}R")

    # ── Detailed per-symbol for best threshold ───────────────────
    best_t = best_thresh
    if best_t >= 100:
        best_t = 999

    print(f"\n{'═' * 80}")
    print(f"  PER-SYMBOL @ ±{best_thresh:.2f}R GUARD")
    print(f"{'═' * 80}")
    print(f"  {'Symbol':<10} {'Total':>6} {'Taken':>6} {'Skip':>5} "
          f"{'WR%':>6} {'GuardR':>8} {'IdealR':>8} {'Δ':>8}")

    for sym in available:
        sym_sigs = [s for s in all_signals if s["symbol"] == sym]
        if not sym_sigs:
            continue

        sym_taken = []
        for sig_info in sym_sigs:
            if sig_info["abs_drift_r"] > best_t:
                continue
            actual_drift_r = sig_info["drift_r"]
            if abs(actual_drift_r) < 0.01:
                sym_taken.append({"pnl_r": sig_info["ideal_R_result"], "win": sig_info["ideal_win"]})
            else:
                trade = simulate_trade_with_slippage(
                    df=sig_info["market_df"],
                    entry_idx=sig_info["entry_idx"],
                    direction=sig_info["direction"],
                    entry_price=sig_info["entry_price"],
                    sl_init=sig_info["sl_init"],
                    R_orig=sig_info["R_orig"],
                    slip_r=actual_drift_r,
                    tp_r=2.5, trail_buffer_r=0.5, be_trigger_r=1.0,
                    cutoff_time=sig_info["cutoff_close"],
                )
                if trade is None:
                    continue
                pnl_r = trade["pnl_points"] / sig_info["R_orig"] if sig_info["R_orig"] > 0 else 0
                sym_taken.append({"pnl_r": pnl_r, "win": trade["pnl_points"] > 0})

        n_total = len(sym_sigs)
        n_taken = len(sym_taken)
        n_skip = n_total - n_taken
        ideal_r_sym = sum(s["ideal_R_result"] for s in sym_sigs)

        if n_taken > 0:
            guard_r = sum(t["pnl_r"] for t in sym_taken)
            wr = sum(1 for t in sym_taken if t["win"]) / n_taken * 100
        else:
            guard_r = wr = 0

        delta = guard_r - ideal_r_sym
        print(f"  {sym:<10} {n_total:>6} {n_taken:>6} {n_skip:>5} "
              f"{wr:>5.1f}% {guard_r:>+8.1f} {ideal_r_sym:>+8.1f} {delta:>+8.1f}")

    # ── VERDICT ──────────────────────────────────────────────────
    print(f"\n{'═' * 80}")
    print(f"  RECOMMENDATION")
    print(f"{'═' * 80}")
    print(f"""
  ★ max_drift_R = {best_thresh:.2f}
  
  How it works in live:
  1. Signal detected → entry_price from past bar open
  2. Bot reads live bid/ask → expected_price
  3. drift_R = |expected_price - entry_price| / risk_points
  4. If drift_R > {best_thresh:.2f} → SKIP trade, log warning
  5. If drift_R ≤ {best_thresh:.2f} → MARKET ORDER + v3.14.1 TP recalc
  
  This protects against extreme momentum drift (like SPX +1.65R)
  while keeping all normal trades.
""")


if __name__ == "__main__":
    run()
