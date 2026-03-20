"""
10-Year Drift-Adjusted Backtest — Year-by-Year + FundedNext Sim + Weekly Freq
==============================================================================
Uses actual per-trade drift + ±1.0R drift guard + v3.14.1 TP recalc.
This is the REALISTIC live performance model.
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from strategy.strategy_ict_clean import StrategyICTClean
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS,
    load_data, get_htf_trend_for_day,
)
from run_slippage_realistic import simulate_trade_with_slippage

# ── Config ──────────────────────────────────────────────────────
MAX_DRIFT_R = 1.00       # drift guard threshold (validated on 2.8yr + 10yr)
STARTING_BALANCE = 6000.0

# FundedNext rules
RISK_TIER1 = 1.0         # DD 0-4%
RISK_TIER2 = 0.5         # DD 4%+
DD_TIER2 = 4.0
DAILY_DD_CUTOFF = 2.0    # stop trading when daily DD from peak >= 2%
FN_DAILY_DD = 5.0        # breach
FN_MAX_DD = 10.0         # breach from initial
PHASE1_TARGET = 8.5
PHASE2_TARGET = 5.5
MAX_DAILY_TRADES = 2
MAX_DAILY_LOSSES = 2
MAX_OPEN_POS = 2
MIN_TRADING_DAYS = 5
COAST_RISK = 0.1        # 0.1% risk while completing min trading days
PHASE1_COAST = 8.0      # coast when P1 profit >= 8% (target 8.5%)
PHASE2_COAST = 5.0      # coast when P2 profit >= 5% (target 5.5%)

SYMBOLS = ["SPX500", "NDX100", "GER30", "US30", "FRA40"]  # US2000 no 10yr data


def load_h1_ema(filename, data_dir):
    h1 = pd.read_csv(data_dir / filename, sep="\t")
    time_col = [c for c in h1.columns if c.lower() == "time"][0]
    h1[time_col] = pd.to_datetime(h1[time_col], format="mixed", dayfirst=False)
    h1 = h1.set_index(time_col).sort_index()
    h1.columns = [c.lower() for c in h1.columns]
    h1["ema20"] = h1["close"].ewm(span=20, adjust=False).mean()
    h1["ema50"] = h1["close"].ewm(span=50, adjust=False).mean()
    h1["htf_trend"] = "bullish"
    h1.loc[h1["ema20"] < h1["ema50"], "htf_trend"] = "bearish"
    return h1[["htf_trend", "ema20", "ema50"]]


def generate_all_signals_with_drift():
    """Generate all signals with actual drift measurement."""
    data_dir = Path(__file__).parent / "data" / "raw"

    # Prefer 10yr data
    M1_FILES = {}
    H1_FILES = {}
    for sym in SYMBOLS:
        for m1 in [f"{sym}_10YR_M1_CLEAN.csv", f"{sym}_M1_CLEAN.csv"]:
            if (data_dir / m1).exists():
                M1_FILES[sym] = m1
                break
        for h1 in [f"{sym}_10YR_H1_EMA.csv", f"{sym}_H1_EMA.csv"]:
            if (data_dir / h1).exists():
                H1_FILES[sym] = h1
                break

    available = [s for s in SYMBOLS if s in M1_FILES]
    all_signals = []

    for sym in available:
        print(f"  Loading {sym} ({M1_FILES[sym]})...", end=" ", flush=True)
        cfg = LIVE_INDEX_CONFIG[sym]
        df = load_data(M1_FILES[sym])
        htf_ema = load_h1_ema(H1_FILES[sym], data_dir)

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

            # Measure actual drift
            if sim_start_idx < len(market_df):
                current_price = float(market_df.iloc[sim_start_idx]["open"])
                if sig.direction == "bullish":
                    drift_pts = current_price - sig.entry_price
                else:
                    drift_pts = sig.entry_price - current_price
                drift_r = drift_pts / sig.risk_points if sig.risk_points > 0 else 0
            else:
                drift_r = 0

            all_signals.append({
                "symbol": sym,
                "date": day,
                "year": day.year,
                "week": entry_ts.isocalendar()[1],
                "direction": sig.direction,
                "entry_price": sig.entry_price,
                "sl_init": sl_init,
                "R_orig": sig.risk_points,
                "entry_idx": entry_idx,
                "sim_start_idx": sim_start_idx,
                "market_df": market_df,
                "cutoff_close": cutoff_close,
                "point_value": BROKER_POINT_VALUES.get(sym, 10.0),
                "drift_r": drift_r,
                "abs_drift_r": abs(drift_r),
                "entry_time": entry_ts,
                "exit_time": pd.Timestamp(sig.exit_time) if sig.exit_time else entry_ts + pd.Timedelta(hours=4),
                # IDEAL
                "ideal_pnl_pts": sig.pnl_points,
                "ideal_R_result": sig.pnl_points / sig.risk_points if sig.risk_points > 0 else 0,
                "ideal_exit": sig.exit_reason,
                "ideal_win": sig.pnl_points > 0,
            })

        print(f"{count} signals")

    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals


def simulate_with_drift_guard(all_signals):
    """Apply drift guard + simulate trades with actual drift + TP recalc."""
    results = []

    for sig_info in all_signals:
        if sig_info["abs_drift_r"] > MAX_DRIFT_R:
            # Drift guard: SKIP
            results.append({**sig_info, "status": "SKIPPED", "pnl_r": 0, "win": False,
                            "exit_reason": "DRIFT_GUARD", "pnl_pts": 0})
            continue

        drift_r = sig_info["drift_r"]

        if abs(drift_r) < 0.01:
            # Near-zero drift — IDEAL
            results.append({**sig_info, "status": "TAKEN", 
                            "pnl_r": sig_info["ideal_R_result"],
                            "win": sig_info["ideal_win"],
                            "exit_reason": sig_info["ideal_exit"],
                            "pnl_pts": sig_info["ideal_pnl_pts"]})
        else:
            trade = simulate_trade_with_slippage(
                df=sig_info["market_df"],
                entry_idx=sig_info["entry_idx"],
                direction=sig_info["direction"],
                entry_price=sig_info["entry_price"],
                sl_init=sig_info["sl_init"],
                R_orig=sig_info["R_orig"],
                slip_r=drift_r,
                tp_r=2.5, trail_buffer_r=0.5, be_trigger_r=1.0,
                cutoff_time=sig_info["cutoff_close"],
            )
            if trade is None:
                results.append({**sig_info, "status": "SKIPPED", "pnl_r": 0, "win": False,
                                "exit_reason": "R_NEGATIVE", "pnl_pts": 0})
            else:
                pnl_r = trade["pnl_points"] / sig_info["R_orig"] if sig_info["R_orig"] > 0 else 0
                results.append({**sig_info, "status": "TAKEN",
                                "pnl_r": pnl_r,
                                "win": trade["pnl_points"] > 0,
                                "exit_reason": trade["exit_reason"],
                                "pnl_pts": trade["pnl_points"]})

    return results


def print_year_by_year(results):
    """Print year-by-year breakdown."""
    taken = [r for r in results if r["status"] == "TAKEN"]

    years = sorted(set(r["year"] for r in taken))
    syms = sorted(set(r["symbol"] for r in taken))

    print(f"\n{'═' * 90}")
    print(f"  YEAR-BY-YEAR RESULTS (Drift Guard ±{MAX_DRIFT_R}R + v3.14.1 TP Recalc)")
    print(f"{'═' * 90}")

    # Portfolio summary per year
    print(f"\n  {'Year':<6} {'Trades':>7} {'Skip':>5} {'WR%':>6} "
          f"{'TotalR':>8} {'AvgR':>7} {'TP%':>5} {'SL%':>5} {'BE%':>5}")
    print(f"  {'─' * 78}")

    cumulative_r = 0
    for year in years:
        yr_taken = [r for r in taken if r["year"] == year]
        yr_all = [r for r in results if r["year"] == year]
        yr_skip = len(yr_all) - len(yr_taken)
        n = len(yr_taken)
        if n == 0:
            continue

        wins = sum(1 for r in yr_taken if r["win"])
        wr = wins / n * 100
        total_r = sum(r["pnl_r"] for r in yr_taken)
        avg_r = total_r / n
        cumulative_r += total_r

        tp = sum(1 for r in yr_taken if r["exit_reason"] == "TP")
        sl = sum(1 for r in yr_taken if r["exit_reason"] in ("SL_HIT", "SL_BE"))
        be = sum(1 for r in yr_taken if r["exit_reason"] == "SL_BE")

        print(f"  {year:<6} {n:>7} {yr_skip:>5} {wr:>5.1f}% "
              f"{total_r:>+8.1f} {avg_r:>+7.3f} {tp/n*100:>4.1f}% {sl/n*100:>4.1f}% {be/n*100:>4.1f}%")

    # Total
    total_n = len(taken)
    total_skip = len(results) - total_n
    total_wins = sum(1 for r in taken if r["win"])
    total_r_all = sum(r["pnl_r"] for r in taken)
    print(f"  {'─' * 78}")
    print(f"  {'TOTAL':<6} {total_n:>7} {total_skip:>5} {total_wins/total_n*100:>5.1f}% "
          f"{total_r_all:>+8.1f} {total_r_all/total_n:>+7.3f}")

    # Per-symbol per-year matrix
    print(f"\n  TOTAL R PER SYMBOL PER YEAR:")
    print(f"  {'Year':<6}", end="")
    for sym in syms:
        print(f" {sym:>10}", end="")
    print(f" {'TOTAL':>10}")
    print(f"  {'─' * (6 + 11 * (len(syms) + 1))}")

    for year in years:
        print(f"  {year:<6}", end="")
        yr_total = 0
        for sym in syms:
            sr = [r for r in taken if r["year"] == year and r["symbol"] == sym]
            sym_r = sum(r["pnl_r"] for r in sr)
            yr_total += sym_r
            print(f" {sym_r:>+10.1f}", end="")
        print(f" {yr_total:>+10.1f}")

    print(f"  {'─' * (6 + 11 * (len(syms) + 1))}")
    print(f"  {'TOTAL':<6}", end="")
    grand = 0
    for sym in syms:
        sr = [r for r in taken if r["symbol"] == sym]
        sym_r = sum(r["pnl_r"] for r in sr)
        grand += sym_r
        print(f" {sym_r:>+10.1f}", end="")
    print(f" {grand:>+10.1f}")

    # IDEAL comparison per year
    print(f"\n  DRIFT IMPACT PER YEAR (IDEAL vs DRIFT-ADJUSTED):")
    print(f"  {'Year':<6} {'IDEAL_R':>9} {'DRIFT_R':>9} {'Δ':>8} {'Retain%':>8}")
    print(f"  {'─' * 42}")
    for year in years:
        yr_all = [r for r in results if r["year"] == year]
        ideal_r = sum(r["ideal_R_result"] for r in yr_all)
        drift_r = sum(r["pnl_r"] for r in yr_all if r["status"] == "TAKEN")
        delta = drift_r - ideal_r
        retain = drift_r / ideal_r * 100 if ideal_r != 0 else 0
        print(f"  {year:<6} {ideal_r:>+9.1f} {drift_r:>+9.1f} {delta:>+8.1f} {retain:>7.1f}%")


def print_weekly_frequency(results):
    """Print weekly trade frequency analysis."""
    taken = [r for r in results if r["status"] == "TAKEN"]

    print(f"\n{'═' * 70}")
    print(f"  WEEKLY TRADE FREQUENCY")
    print(f"{'═' * 70}")

    # Group by (year, week)
    weekly = defaultdict(list)
    for r in taken:
        yr_wk = (r["year"], r["week"])
        weekly[yr_wk].append(r)

    counts = [len(v) for v in weekly.values()]
    total_weeks = len(weekly)

    print(f"\n  Total trading weeks: {total_weeks}")
    print(f"  Total trades taken:  {len(taken)}")
    print(f"\n  Avg trades/week:  {np.mean(counts):.1f}")
    print(f"  Median trades/wk: {np.median(counts):.1f}")
    print(f"  Min trades/wk:    {np.min(counts)}")
    print(f"  Max trades/wk:    {np.max(counts)}")

    # Distribution
    print(f"\n  Weekly trade count distribution:")
    max_count = max(counts)
    for c in range(1, min(max_count + 1, 16)):
        n = sum(1 for x in counts if x == c)
        pct = n / total_weeks * 100
        bar = "█" * int(pct / 2)
        print(f"    {c:>2} trades/wk: {n:>4} weeks ({pct:>5.1f}%) {bar}")
    if max_count > 15:
        n = sum(1 for x in counts if x > 15)
        pct = n / total_weeks * 100
        print(f"   >15 trades/wk: {n:>4} weeks ({pct:>5.1f}%)")

    # Per-year average
    years = sorted(set(r["year"] for r in taken))
    print(f"\n  Per-year weekly average:")
    print(f"  {'Year':<6} {'Weeks':>6} {'Trades':>7} {'Avg/wk':>7} {'R/wk':>8}")
    print(f"  {'─' * 38}")
    for year in years:
        yr_wks = {k: v for k, v in weekly.items() if k[0] == year}
        yr_trades = sum(len(v) for v in yr_wks.values())
        yr_r = sum(r["pnl_r"] for wk in yr_wks.values() for r in wk)
        n_wks = len(yr_wks)
        print(f"  {year:<6} {n_wks:>6} {yr_trades:>7} {yr_trades/n_wks:>7.1f} {yr_r/n_wks:>+8.2f}")


def simulate_fundednext(results):
    """Run FundedNext challenge simulation with drift-adjusted trades."""
    taken = [r for r in results if r["status"] == "TAKEN"]

    print(f"\n{'═' * 70}")
    print(f"  FUNDEDNEXT STELLAR CHALLENGE SIMULATION")
    print(f"  (Drift Guard ±{MAX_DRIFT_R}R + v3.14.1 TP Recalc)")
    print(f"{'═' * 70}")
    print(f"  Starting Balance : ${STARTING_BALANCE:,.0f}")
    print(f"  Phase 1 Target   : {PHASE1_TARGET}% (${STARTING_BALANCE * PHASE1_TARGET / 100:,.0f})")
    print(f"  Phase 2 Target   : {PHASE2_TARGET}% (${STARTING_BALANCE * PHASE2_TARGET / 100:,.0f})")
    print(f"  Risk: T1={RISK_TIER1}% (DD<{DD_TIER2}%) | T2={RISK_TIER2}% (DD>={DD_TIER2}%)")
    print(f"  Coast: P1 >= {PHASE1_COAST}% → {COAST_RISK}% risk | P2 >= {PHASE2_COAST}% → {COAST_RISK}% risk")
    print(f"  Daily cutoff: {DAILY_DD_CUTOFF}% | Max DD: {FN_MAX_DD}% | Daily DD breach: {FN_DAILY_DD}%")
    print()

    challenges = []
    cnum = 1
    phase = 1
    target = PHASE1_TARGET

    bal = STARTING_BALANCE
    init_bal = STARTING_BALANCE
    peak_bal = STARTING_BALANCE
    phase_start = None
    phase_trades = []
    phase_days = set()
    breached = False
    passed = False
    breach_reason = None

    ch_start = None
    ch_phases = []

    cur_day = None
    day_start_bal = STARTING_BALANCE
    day_peak_bal = STARTING_BALANCE
    daily_losses = 0
    daily_trades_count = 0
    day_stopped = False

    open_pos = []

    def make_rec():
        end_d = phase_trades[-1]["date"] if phase_trades else cur_day
        min_b = min(t["bal"] for t in phase_trades) if phase_trades else bal
        return {
            "phase": phase, "passed": passed, "breached": breached,
            "breach_reason": breach_reason,
            "start": phase_start, "end": end_d,
            "trades": len(phase_trades), "days": len(phase_days),
            "final_bal": bal, "peak": peak_bal,
            "net_pnl": bal - init_bal,
            "return_pct": (bal - init_bal) / init_bal * 100,
            "max_dd": (peak_bal - min_b) / peak_bal * 100 if peak_bal > 0 else 0,
            "log": phase_trades,
        }

    def reset_phase(new_phase, new_target):
        nonlocal phase, target, bal, init_bal, peak_bal, phase_start, phase_trades
        nonlocal phase_days, breached, passed, breach_reason
        nonlocal day_start_bal, day_peak_bal, daily_losses, daily_trades_count, day_stopped, open_pos
        phase = new_phase
        target = new_target
        bal = STARTING_BALANCE
        init_bal = STARTING_BALANCE
        peak_bal = STARTING_BALANCE
        phase_start = None
        phase_trades = []
        phase_days = set()
        breached = False
        passed = False
        breach_reason = None
        day_start_bal = STARTING_BALANCE
        day_peak_bal = STARTING_BALANCE
        daily_losses = 0
        daily_trades_count = 0
        day_stopped = False
        open_pos = []

    for sig in taken:
        trade_day = sig["date"]
        entry_time = sig["entry_time"]
        exit_time = sig["exit_time"]
        symbol = sig["symbol"]
        point_value = sig["point_value"]

        if phase_start is None:
            phase_start = trade_day
        if ch_start is None:
            ch_start = trade_day

        # New day
        if trade_day != cur_day:
            cur_day = trade_day
            day_start_bal = bal
            day_peak_bal = bal
            daily_losses = 0
            daily_trades_count = 0
            day_stopped = False

        day_peak_bal = max(day_peak_bal, bal)

        if day_stopped:
            continue

        # Handle passed
        if passed:
            rec = make_rec()
            ch_phases.append(rec)
            if phase == 1:
                reset_phase(2, PHASE2_TARGET)
                phase_start = trade_day
            else:
                challenges.append({"num": cnum, "status": "PASSED",
                                   "start": ch_start, "end": rec["end"], "phases": ch_phases})
                cnum += 1
                ch_start = trade_day
                ch_phases = []
                reset_phase(1, PHASE1_TARGET)
                phase_start = trade_day

        # Handle breached
        if breached:
            rec = make_rec()
            ch_phases.append(rec)
            challenges.append({"num": cnum, "status": "BREACHED",
                               "start": ch_start, "end": rec["end"], "phases": ch_phases,
                               "breach_phase": phase, "breach_reason": breach_reason})
            cnum += 1
            ch_start = trade_day
            ch_phases = []
            reset_phase(1, PHASE1_TARGET)
            phase_start = trade_day

        # Daily checks
        daily_dd = (day_peak_bal - bal) / day_peak_bal * 100 if day_peak_bal > 0 else 0
        if daily_dd >= DAILY_DD_CUTOFF:
            day_stopped = True
            continue
        if daily_losses >= MAX_DAILY_LOSSES:
            continue
        if daily_trades_count >= MAX_DAILY_TRADES:
            continue

        # Open positions
        open_pos = [p for p in open_pos if p["exit"] > entry_time]
        if len(open_pos) >= MAX_OPEN_POS:
            continue

        # Risk sizing — coast mode near target if min days not met
        coast_thresh = PHASE1_COAST if phase == 1 else PHASE2_COAST
        profit_pre = (bal - init_bal) / init_bal * 100
        coasting = profit_pre >= coast_thresh and len(phase_days) < MIN_TRADING_DAYS

        if coasting:
            risk_pct = COAST_RISK
        else:
            dd_from_init = max(0, (init_bal - bal) / init_bal * 100)
            if dd_from_init >= DD_TIER2:
                risk_pct = RISK_TIER2
            else:
                risk_pct = RISK_TIER1

        risk_amount = bal * (risk_pct / 100)
        risk_pts = sig["R_orig"]
        if risk_pts <= 0:
            continue

        lots = risk_amount / (risk_pts * point_value)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        # Trade P&L with costs
        pnl_pts = sig["pnl_pts"]
        slippage = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
        spread = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        half_spread = spread / 2.0
        entry_cost = slippage + half_spread
        exit_cost = half_spread if sig["exit_reason"] == "TP" else slippage + half_spread
        total_cost_pts = entry_cost + exit_cost
        commission = TRADING_COSTS["commission_per_lot"].get(symbol, 0.0) * lots

        pnl = (pnl_pts - total_cost_pts) * lots * point_value - commission

        bal += pnl
        if pnl > 0:
            day_peak_bal = max(day_peak_bal, bal)
        peak_bal = max(peak_bal, bal)

        is_loss = pnl < 0
        if is_loss:
            daily_losses += 1
        daily_trades_count += 1

        open_pos.append({"symbol": symbol, "exit": exit_time})
        phase_trades.append({
            "symbol": symbol, "date": trade_day, "pnl": pnl, "bal": bal,
            "risk_pct": risk_pct, "coast": coasting,
        })
        phase_days.add(trade_day)

        # Post-trade checks
        daily_dd_post = (day_peak_bal - bal) / day_peak_bal * 100 if day_peak_bal > 0 else 0
        if daily_dd_post >= DAILY_DD_CUTOFF:
            day_stopped = True

        fn_daily = (day_start_bal - bal) / day_start_bal * 100 if day_start_bal > 0 else 0
        if fn_daily >= FN_DAILY_DD:
            breached = True
            breach_reason = f"Daily DD {fn_daily:.1f}%"
            continue

        fn_max = (init_bal - bal) / init_bal * 100 if init_bal > 0 else 0
        if fn_max >= FN_MAX_DD:
            breached = True
            breach_reason = f"Max DD {fn_max:.1f}%"
            continue

        profit_pct = (bal - init_bal) / init_bal * 100
        if profit_pct >= target and len(phase_days) >= MIN_TRADING_DAYS:
            passed = True

    # Finalize
    if phase_trades:
        rec = make_rec()
        ch_phases.append(rec)
        status = "PASSED" if passed else ("BREACHED" if breached else f"ACTIVE_P{phase}")
        challenges.append({"num": cnum, "status": status,
                           "start": ch_start, "end": rec["end"], "phases": ch_phases})

    # ── Report ─────────────────────────────────────────────────
    passed_full = [c for c in challenges if c["status"] == "PASSED"]
    breached_all = [c for c in challenges if c["status"] == "BREACHED"]
    total_completed = len(passed_full) + len(breached_all)

    print(f"  {'═' * 58}")
    print(f"  FULLY PASSED (both phases) : {len(passed_full)}")
    print(f"  BREACHED                   : {len(breached_all)}")
    if total_completed > 0:
        print(f"  PASS RATE                  : {len(passed_full)/total_completed*100:.0f}%")

    if passed_full:
        durations = [(c["end"] - c["start"]).days for c in passed_full]
        print(f"  AVG PASS DURATION          : {np.mean(durations):.0f} days")
        print(f"  FASTEST PASS               : {min(durations)} days")
        print(f"  SLOWEST PASS               : {max(durations)} days")
        within_60 = sum(1 for d in durations if d <= 60)
        print(f"  WITHIN 60 DAYS             : {within_60}/{len(passed_full)} ({within_60/len(passed_full)*100:.0f}%)")
    print(f"  {'═' * 58}")

    # Per-challenge details
    for ch in challenges:
        print(f"\n  Challenge #{ch['num']}  [{ch['status']}]")
        if ch.get("breach_reason"):
            print(f"    Breach: {ch['breach_reason']}")
        print(f"    Period: {ch['start']} → {ch['end']}  ({(ch['end'] - ch['start']).days} days)")

        for p in ch["phases"]:
            tgt = PHASE1_TARGET if p["phase"] == 1 else PHASE2_TARGET
            status = "✓ PASSED" if p["passed"] else ("✗ BREACH" if p["breached"] else "…")
            print(f"    Phase {p['phase']} ({tgt}%): {status}")
            print(f"      Trades: {p['trades']}  |  Days: {p['days']}  |  "
                  f"Bal: ${p['final_bal']:,.0f}  |  PnL: {p['return_pct']:+.1f}%  |  Max DD: {p['max_dd']:.1f}%")
            if p["breach_reason"]:
                print(f"      Reason: {p['breach_reason']}")

    # Summary
    all_trades = sum(p["trades"] for c in challenges for p in c["phases"])
    print(f"\n  {'─' * 58}")
    print(f"  TOTAL TRADES: {all_trades} across {len(challenges)} challenge attempts")

    if passed_full:
        print(f"\n  PAYOUT POTENTIAL (80% profit split):")
        for ch in passed_full:
            p2 = [p for p in ch["phases"] if p["phase"] == 2]
            if p2:
                profit = p2[0]["net_pnl"]
                payout = profit * 0.80
                print(f"    Challenge #{ch['num']}: ${profit:,.0f} profit → ${payout:,.0f} payout "
                      f"({(ch['end'] - ch['start']).days} days)")

    return challenges


def main():
    print("=" * 70)
    print("  10-YEAR DRIFT-ADJUSTED BACKTEST")
    print(f"  Drift Guard: ±{MAX_DRIFT_R}R  |  v3.14.1 TP Recalc  |  Market Orders")
    print("=" * 70)
    print()

    # Generate signals
    print("  Generating signals from 10yr data...")
    all_signals = generate_all_signals_with_drift()
    print(f"\n  Total raw signals: {len(all_signals)}")

    # Apply drift guard + simulate
    print(f"\n  Applying drift guard (±{MAX_DRIFT_R}R) + simulating trades...")
    results = simulate_with_drift_guard(all_signals)

    taken = [r for r in results if r["status"] == "TAKEN"]
    skipped = [r for r in results if r["status"] == "SKIPPED"]
    print(f"  Taken: {len(taken)} | Skipped: {len(skipped)} ({len(skipped)/len(results)*100:.1f}%)")

    # 1. Year-by-year
    print_year_by_year(results)

    # 2. Weekly frequency
    print_weekly_frequency(results)

    # 3. FundedNext simulation
    simulate_fundednext(results)

    # Clean up market_df references to free memory
    for r in results:
        r.pop("market_df", None)
    for s in all_signals:
        s.pop("market_df", None)


if __name__ == "__main__":
    main()
