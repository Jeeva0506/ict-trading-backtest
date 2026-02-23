"""
TP/Exit Variant Tester
─────────────────────
Tests different take profit & exit management configurations
against the SAME signal set to find the optimal exit strategy.

Core entry logic stays IDENTICAL — only exit management changes.
"""
import pandas as pd
import numpy as np
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from copy import deepcopy

from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS,
    MAX_OPEN_POSITIONS,
    load_data, get_htf_trend_for_day,
)
from strategy.strategy_ict_clean import StrategyICTClean, TradeSignalICT


# ─── Configuration ───

ALL_SYMBOLS = ["SPX500", "NDX100", "GER30", "US30"]

DATA_FILES = {
    "SPX500": "SPX500_3YEAR_M1.csv",
    "NDX100": "NDX100_3YEAR_M1.csv",
    "GER30":  "GER30_3YEAR_M1.csv",
    "US30":   "US30_3YEAR_M1.csv",
}


# ─── Exit Variants ───

VARIANTS = {
    "CURRENT": {
        "desc": "Current: BE@1R, trail 1.0R buffer, TP@2.5R",
        "be_trigger_r": 1.0,
        "tp_r": 2.5,
        "trail_buffer_r": 1.0,
        "partial_tp_r": None,    # no partial
        "partial_pct": 0.0,
    },
    "TP_2.0R": {
        "desc": "Lower TP: BE@1R, trail 1.0R buffer, TP@2.0R",
        "be_trigger_r": 1.0,
        "tp_r": 2.0,
        "trail_buffer_r": 1.0,
        "partial_tp_r": None,
        "partial_pct": 0.0,
    },
    "TP_1.8R": {
        "desc": "Aggressive TP: BE@1R, trail 1.0R buffer, TP@1.8R",
        "be_trigger_r": 1.0,
        "tp_r": 1.8,
        "trail_buffer_r": 1.0,
        "partial_tp_r": None,
        "partial_pct": 0.0,
    },
    "TRAIL_0.75": {
        "desc": "Tighter trail: BE@1R, trail 0.75R buffer, TP@2.5R",
        "be_trigger_r": 1.0,
        "tp_r": 2.5,
        "trail_buffer_r": 0.75,
        "partial_tp_r": None,
        "partial_pct": 0.0,
    },
    "PARTIAL_1.5": {
        "desc": "Partial: 50% at 1.5R, rest trails to 2.5R",
        "be_trigger_r": 1.0,
        "tp_r": 2.5,
        "trail_buffer_r": 1.0,
        "partial_tp_r": 1.5,
        "partial_pct": 0.5,
    },
    "COMBO_A": {
        "desc": "TP@2.0R + tighter trail 0.75R",
        "be_trigger_r": 1.0,
        "tp_r": 2.0,
        "trail_buffer_r": 0.75,
        "partial_tp_r": None,
        "partial_pct": 0.0,
    },
    "COMBO_B": {
        "desc": "50% partial@1.5R + trail 0.75R + TP@2.5R",
        "be_trigger_r": 1.0,
        "tp_r": 2.5,
        "trail_buffer_r": 0.75,
        "partial_tp_r": 1.5,
        "partial_pct": 0.5,
    },
    "COMBO_C": {
        "desc": "50% partial@1.2R + trail 0.75R + TP@2.0R",
        "be_trigger_r": 1.0,
        "tp_r": 2.0,
        "trail_buffer_r": 0.75,
        "partial_tp_r": 1.2,
        "partial_pct": 0.5,
    },
}


def simulate_trade_variant(
    df: pd.DataFrame,
    start_idx: int,
    direction: str,
    entry: float,
    sl_init: float,
    R: float,
    cutoff_time: pd.Timestamp,
    variant: dict,
) -> Tuple[float, str, float]:
    """
    Re-simulate a single trade with a different exit config.
    Returns (pnl_in_R, exit_reason, max_favorable_R).

    For partial TP: returns blended PnL accounting for the partial close.
    """
    be_trigger_r = variant["be_trigger_r"]
    tp_r = variant["tp_r"]
    trail_buffer_r = variant["trail_buffer_r"]
    partial_tp_r = variant.get("partial_tp_r")
    partial_pct = variant.get("partial_pct", 0.0)

    if direction == "bullish":
        tp_price = entry + R * tp_r
        be_price = entry + R * be_trigger_r
        partial_price = entry + R * partial_tp_r if partial_tp_r else None
    else:
        tp_price = entry - R * tp_r
        be_price = entry - R * be_trigger_r
        partial_price = entry - R * partial_tp_r if partial_tp_r else None

    current_sl = sl_init
    hit_be = False
    max_favorable = 0.0
    partial_filled = False
    partial_pnl_r = 0.0  # locked PnL from partial (in R)
    remaining_pct = 1.0   # remaining position percentage

    for i in range(start_idx, len(df)):
        c = df.iloc[i]
        ts = df.index[i]
        hi = float(c.high)
        lo = float(c.low)

        # Force close near session end
        if ts >= cutoff_time:
            if direction == "bullish":
                pnl_r = (float(c.close) - entry) / R
            else:
                pnl_r = (entry - float(c.close)) / R
            total_pnl_r = partial_pnl_r + pnl_r * remaining_pct
            return total_pnl_r, "SESSION_CLOSE", max_favorable

        # --- SL check ---
        if direction == "bullish" and lo <= current_sl:
            pnl_r = (current_sl - entry) / R
            total_pnl_r = partial_pnl_r + pnl_r * remaining_pct
            reason = "SL_BE" if hit_be else "SL_HIT"
            return total_pnl_r, reason, max_favorable

        if direction == "bearish" and hi >= current_sl:
            pnl_r = (entry - current_sl) / R
            total_pnl_r = partial_pnl_r + pnl_r * remaining_pct
            reason = "SL_BE" if hit_be else "SL_HIT"
            return total_pnl_r, reason, max_favorable

        # --- Full TP check ---
        if direction == "bullish" and hi >= tp_price:
            pnl_r = tp_r
            total_pnl_r = partial_pnl_r + pnl_r * remaining_pct
            return total_pnl_r, "TP", max_favorable

        if direction == "bearish" and lo <= tp_price:
            pnl_r = tp_r
            total_pnl_r = partial_pnl_r + pnl_r * remaining_pct
            return total_pnl_r, "TP", max_favorable

        # --- Partial TP check ---
        if partial_price and not partial_filled:
            if (direction == "bullish" and hi >= partial_price) or \
               (direction == "bearish" and lo <= partial_price):
                partial_filled = True
                partial_pnl_r = partial_tp_r * partial_pct
                remaining_pct = 1.0 - partial_pct

        # --- BE trigger ---
        if not hit_be:
            if (direction == "bullish" and hi >= be_price) or \
               (direction == "bearish" and lo <= be_price):
                hit_be = True
                current_sl = entry

        # --- Trailing after BE ---
        if hit_be:
            current_r = ((hi - entry) / R) if direction == "bullish" else ((entry - lo) / R)
            if current_r > max_favorable:
                max_favorable = current_r

            trail_r = max(0.0, max_favorable - trail_buffer_r)
            if direction == "bullish":
                new_sl = entry + trail_r * R
                if new_sl > current_sl:
                    current_sl = new_sl
            else:
                new_sl = entry - trail_r * R
                if new_sl < current_sl:
                    current_sl = new_sl

    # Fallback
    last = df.iloc[-1]
    if direction == "bullish":
        pnl_r = (float(last.close) - entry) / R
    else:
        pnl_r = (entry - float(last.close)) / R
    total_pnl_r = partial_pnl_r + pnl_r * remaining_pct
    return total_pnl_r, "SESSION_CLOSE", max_favorable


def compute_cost_r(symbol: str, R: float, point_value: float, exit_reason: str) -> float:
    """Convert trading costs to R-multiple."""
    slippage = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
    spread = TRADING_COSTS["spread_points"].get(symbol, 1.0)
    half_spread = spread / 2.0

    entry_cost = slippage + half_spread
    if exit_reason == "TP":
        exit_cost = half_spread
    else:
        exit_cost = slippage + half_spread

    total_cost_pts = entry_cost + exit_cost
    # Commission as R fraction (approximate)
    cost_r = total_cost_pts / R if R > 0 else 0
    return cost_r


def main():
    print("=" * 75)
    print("  TP/EXIT VARIANT COMPARISON TEST")
    print("  Core strategy: Candidate #12 (ORB Clean Breakout)")
    print("  Same entries — only exit management differs")
    print("=" * 75)
    print()

    # ─── Phase 1: Collect all entry setups with raw bar data ───
    print("Loading data and generating entry setups...")

    all_setups = []

    for sym in ALL_SYMBOLS:
        cfg = LIVE_INDEX_CONFIG[sym]
        data_file = DATA_FILES[sym]
        point_value = BROKER_POINT_VALUES[sym]

        df = load_data(data_file)
        h1 = df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        h1["ema20"] = h1["close"].ewm(span=20, adjust=False).mean()
        h1["ema50"] = h1["close"].ewm(span=50, adjust=False).mean()
        h1["htf_trend"] = "bullish"
        h1.loc[h1["ema20"] < h1["ema50"], "htf_trend"] = "bearish"

        trading_days = sorted(set(df.index.date))
        print(f"  {sym}: {len(trading_days)} trading days")

        for day in trading_days:
            session_start = pd.Timestamp(day).replace(
                hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
            session_end = pd.Timestamp(day).replace(
                hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
            market_df = df[(df.index >= session_start) & (df.index <= session_end)].copy()
            if len(market_df) < 30:
                continue

            htf_trend = get_htf_trend_for_day(h1, session_start)

            # Use strategy to find entry setups (we only need entry info, not exits)
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
            )

            signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
            for s in signals:
                # Find the entry bar index in market_df
                entry_idx = market_df.index.get_indexer([pd.Timestamp(s.entry_time)], method="nearest")[0]
                cutoff_time = session_end - pd.Timedelta(minutes=15)

                all_setups.append({
                    "symbol": sym,
                    "day": day,
                    "direction": s.direction,
                    "entry": float(s.entry_price),
                    "sl_init": float(s.entry_price - s.risk_points) if s.direction == "bullish"
                              else float(s.entry_price + s.risk_points),
                    "R": float(s.risk_points),
                    "entry_idx": entry_idx,
                    "cutoff_time": cutoff_time,
                    "market_df": market_df,
                    "point_value": point_value,
                })

    print(f"\n  Total entry setups: {len(all_setups)}")
    print()

    # ─── Phase 2: Re-simulate each setup with every variant ───
    print("Running exit variants...")

    results = {}
    for vname, vcfg in VARIANTS.items():
        trades = []
        for setup in all_setups:
            pnl_r, exit_reason, mfe_r = simulate_trade_variant(
                df=setup["market_df"],
                start_idx=setup["entry_idx"],
                direction=setup["direction"],
                entry=setup["entry"],
                sl_init=setup["sl_init"],
                R=setup["R"],
                cutoff_time=setup["cutoff_time"],
                variant=vcfg,
            )
            # Subtract costs (in R)
            cost_r = compute_cost_r(setup["symbol"], setup["R"], setup["point_value"], exit_reason)
            net_pnl_r = pnl_r - cost_r

            trades.append({
                "symbol": setup["symbol"],
                "year": setup["day"].year,
                "pnl_r": net_pnl_r,
                "exit_reason": exit_reason,
                "mfe_r": mfe_r,
            })
        results[vname] = trades

    # ─── Phase 3: Compare ───
    print()
    print("=" * 100)
    print(f"  {'Variant':<14} {'Desc':<44} {'Trades':>6} {'WR':>6} {'PF':>6} {'AvgR':>7} "
          f"{'TP%':>5} {'BE%':>5} {'SL%':>5} {'ExpR':>7} {'CumR':>8}")
    print("-" * 100)

    for vname, vcfg in VARIANTS.items():
        trades = results[vname]
        n = len(trades)

        wins = sum(1 for t in trades if t["pnl_r"] > 0)
        losses = sum(1 for t in trades if t["pnl_r"] < 0)
        wr = wins / n * 100 if n > 0 else 0

        gross_profit = sum(t["pnl_r"] for t in trades if t["pnl_r"] > 0)
        gross_loss = abs(sum(t["pnl_r"] for t in trades if t["pnl_r"] < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        avg_r = sum(t["pnl_r"] for t in trades) / n if n > 0 else 0
        cum_r = sum(t["pnl_r"] for t in trades)

        tp_pct = sum(1 for t in trades if t["exit_reason"] == "TP") / n * 100 if n > 0 else 0
        be_pct = sum(1 for t in trades if t["exit_reason"] == "SL_BE") / n * 100 if n > 0 else 0
        sl_pct = sum(1 for t in trades if t["exit_reason"] == "SL_HIT") / n * 100 if n > 0 else 0

        # Expectancy in R
        exp_r = avg_r

        marker = " <-- CURRENT" if vname == "CURRENT" else ""
        print(f"  {vname:<14} {vcfg['desc']:<44} {n:>6} {wr:>5.1f}% {pf:>5.2f} {avg_r:>+6.3f} "
              f"{tp_pct:>4.1f}% {be_pct:>4.1f}% {sl_pct:>4.1f}% {exp_r:>+6.3f} {cum_r:>+8.1f}{marker}")

    print("=" * 100)

    # ─── Phase 3b: Yearly consistency (anti-curve-fitting) ───
    print()
    print("─── YEARLY CONSISTENCY (must be profitable EVERY year) ───")
    years = sorted(set(t["year"] for t in results["CURRENT"] if t["year"] < 2026))
    print(f"\n  {'Variant':<14}", end="")
    for y in years:
        print(f"  {y} PF  {y} Exp", end="")
    print(f"  {'AllYrs+?':>8}  {'MinPF':>6}")
    print(f"  {'-'*14}", end="")
    for y in years:
        print(f"  {'-'*7} {'-'*7}", end="")
    print(f"  {'-'*8}  {'-'*6}")

    for vname in VARIANTS:
        trades = results[vname]
        print(f"  {vname:<14}", end="")
        year_pfs = []
        all_positive = True
        for y in years:
            yt = [t for t in trades if t["year"] == y]
            if not yt:
                print(f"  {'N/A':>7} {'N/A':>7}", end="")
                continue
            ygp = sum(t["pnl_r"] for t in yt if t["pnl_r"] > 0)
            ygl = abs(sum(t["pnl_r"] for t in yt if t["pnl_r"] < 0))
            ypf = ygp / ygl if ygl > 0 else float('inf')
            yexp = sum(t["pnl_r"] for t in yt) / len(yt)
            year_pfs.append(ypf)
            if sum(t["pnl_r"] for t in yt) <= 0:
                all_positive = False
            print(f"  {ypf:>6.2f} {yexp:>+6.3f}", end="")
        min_pf = min(year_pfs) if year_pfs else 0
        status = 'YES' if all_positive else 'NO'
        print(f"  {status:>8}  {min_pf:>5.2f}")

    print()

    # ─── Phase 4: Detailed breakdown of best vs current ───
    print()
    print("─── EXIT REASON BREAKDOWN (avg PnL in R per exit type) ───")
    print()
    for vname in VARIANTS:
        trades = results[vname]
        print(f"  {vname}:")
        for reason in ["TP", "SL_BE", "SL_HIT", "SESSION_CLOSE"]:
            subset = [t for t in trades if t["exit_reason"] == reason]
            if subset:
                avg = sum(t["pnl_r"] for t in subset) / len(subset)
                print(f"    {reason:<15}: {len(subset):>4} trades  avg {avg:>+.3f}R")
        print()

    # ─── Phase 5: MFE distribution for current BE exits ───
    print("─── MFE ANALYSIS OF CURRENT SL_BE EXITS ───")
    current_be = [t for t in results["CURRENT"] if t["exit_reason"] == "SL_BE"]
    if current_be:
        mfes = [t["mfe_r"] for t in current_be]
        print(f"  Total SL_BE trades: {len(current_be)}")
        print(f"  MFE distribution (how far price went in your favor before reversal):")
        for threshold in [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5]:
            count = sum(1 for m in mfes if m >= threshold)
            print(f"    Reached {threshold:.2f}R: {count:>4} ({count/len(current_be)*100:>5.1f}%)")
        print(f"  Avg MFE of SL_BE exits: {sum(mfes)/len(mfes):.2f}R")
        print(f"  Median MFE of SL_BE exits: {sorted(mfes)[len(mfes)//2]:.2f}R")


if __name__ == "__main__":
    main()
