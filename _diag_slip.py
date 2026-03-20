"""Quick diagnostic: why are 65% of trades being skipped?"""
import pandas as pd
import sys
sys.path.insert(0, ".")
from strategy.strategy_ict_clean import StrategyICTClean
from backtest_live_setup import LIVE_INDEX_CONFIG, load_data, compute_htf_ema, get_htf_trend_for_day

cfg = LIVE_INDEX_CONFIG["SPX500"]
df = load_data("SPX500_M1_CLEAN.csv")
htf_ema = compute_htf_ema(df, fast=20, slow=50)

total, skipped_R, skipped_idx, valid = 0, 0, 0, 0
first_5_shown = 0

for day in sorted(set(df.index.date)):
    ss = pd.Timestamp(day).replace(hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
    se = pd.Timestamp(day).replace(hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
    mdf = df[(df.index >= ss) & (df.index <= se)].copy()
    if len(mdf) < 80:
        continue
    htf = get_htf_trend_for_day(htf_ema, ss)
    strat = StrategyICTClean(
        sl_buffer=cfg["sl_buffer"], min_sl_points=cfg.get("min_sl_points", 2),
        max_sl_points=cfg.get("max_sl_points", 25), orb_minutes=cfg.get("orb_minutes", 15),
        entry_hours=cfg.get("entry_hours", 3), breakout_window_minutes=cfg.get("breakout_window_minutes", 120),
        disp_mult=cfg.get("disp_mult", 1.5), body_min=cfg.get("body_min", 0.6),
        close_strength=cfg.get("close_strength", 0.3), breakout_margin=cfg.get("breakout_margin", 0.1),
        orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5), tp3_r=2.5, orb_bias_filter=False)
    sigs = strat.generate_signals(mdf, htf_trend=htf)
    if not sigs:
        continue
    sig = sigs[0]
    total += 1
    
    et = pd.Timestamp(sig.entry_time)
    idx_arr = mdf.index.get_indexer([et], method="nearest")
    sim_idx = idx_arr[0]
    entry_idx = max(0, sim_idx - 1)
    start_idx = entry_idx + 1
    
    slip_pts = 0.25 * sig.risk_points
    if sig.direction == "bullish":
        new_entry = sig.entry_price + slip_pts
        new_R = new_entry - sig.stop_loss
    else:
        new_entry = sig.entry_price - slip_pts
        new_R = sig.stop_loss - new_entry
    
    if start_idx >= len(mdf):
        skipped_idx += 1
        if first_5_shown < 5:
            print(f"SKIP(idx) {day}: entry_idx={entry_idx}, start={start_idx}, len={len(mdf)}")
            first_5_shown += 1
    elif new_R <= 0:
        skipped_R += 1
        if first_5_shown < 5:
            print(f"SKIP(R) {day}: {sig.direction} entry={sig.entry_price:.2f} SL={sig.stop_loss:.2f} "
                  f"R={sig.risk_points:.2f} slip={slip_pts:.2f} new_entry={new_entry:.2f} new_R={new_R:.2f}")
            first_5_shown += 1
    else:
        valid += 1
    
    if first_5_shown < 3 and new_R > 0:
        first_5_shown += 1
        print(f"VALID {day}: {sig.direction} entry={sig.entry_price:.2f} SL={sig.stop_loss:.2f} "
              f"R={sig.risk_points:.2f} slip={slip_pts:.2f} new_entry={new_entry:.2f} new_R={new_R:.2f}")

print(f"\nSPX500 Summary: {total} total, {valid} valid, {skipped_R} skipped(R<=0), {skipped_idx} skipped(idx)")
