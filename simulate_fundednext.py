"""
FundedNext Account Simulation
─────────────────────────────
Rules implemented:
  1. 3-tier adaptive risk from INITIAL balance:
     - Default: 1.5% (DD 0-3%)
     - Tier 2:  0.75% (DD 3-5%)
     - Tier 3:  0.5%  (DD 5%+)
  2. If daily floating DD from day's PEAK balance reaches -2% →
     close all open trades at current price & stop trading for the day
  3. Max 2 open positions at any time
  4. Max 2 losses per day → stop for day
  5. Max 2 trades per day

FundedNext official limits:
  - Daily DD: 5% from day's starting balance (BREACH = fail)
  - Max DD: 10% from INITIAL capital (BREACH = fail)
  - No time limit (but we track duration vs 60-day personal goal)

When an account breaches, a NEW account starts from the next trade.
Reports how many accounts passed vs breached, plus duration stats.
"""
import pandas as pd
import numpy as np
from pathlib import Path
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS,
    FUNDEDNEXT_RULES, LEVERAGE, MAX_OPEN_POSITIONS, MAX_DAILY_LOSSES,
    load_data, get_htf_trend_for_day,
)
from strategy.strategy_ict_clean import StrategyICTClean

STARTING_BALANCE = 6000.0

# ─── 3-Tier Adaptive Risk (aligned with live config) ───
RISK_TIER1 = 1.5          # % risk per trade (DD 0-3%)
RISK_TIER2 = 0.75         # % risk per trade (DD 3-5%)
RISK_TIER3 = 0.5          # % risk per trade (DD 5%+)
DD_TIER2_THRESHOLD = 3.0  # switch to tier2 at 3% DD from initial
DD_TIER3_THRESHOLD = 5.0  # switch to tier3 at 5% DD from initial

DAILY_DD_CUTOFF = 2.0     # close all + stop when daily DD from peak >= 2%
FUNDEDNEXT_DAILY_DD = 5.0 # official daily DD breach limit
FUNDEDNEXT_MAX_DD = 10.0  # official max DD from INITIAL capital
PHASE1_TARGET_PCT = 8.0   # Phase 1 profit target
PHASE2_TARGET_PCT = 5.0   # Phase 2 profit target
MAX_DAILY_TRADES = 2      # max trades per day (aligned with live)
MAX_CHALLENGE_DAYS = 60   # max calendar days for entire challenge (P1+P2)

ALL_SYMBOLS = ["GER30", "US30", "NDX100", "SPX500"]

# Clean M1-only data files (no H1 contamination)
DATA_FILES_M1 = {
    "GER30": "GER30_M1_CLEAN.csv",
    "US30": "US30_M1_CLEAN.csv",
    "NDX100": "NDX100_M1_CLEAN.csv",
    "SPX500": "SPX500_M1_CLEAN.csv",
}

# Native H1 data from MT5 for EMA computation (goes back further for warmup)
DATA_FILES_H1 = {
    "GER30": "GER30_H1_EMA.csv",
    "US30": "US30_H1_EMA.csv",
    "NDX100": "NDX100_H1_EMA.csv",
    "SPX500": "SPX500_H1_EMA.csv",
}


def load_h1_ema(filename: str, fast: int = 20, slow: int = 50) -> pd.DataFrame:
    """Load native H1 data and compute EMA trend (no resampling from M1)."""
    from pathlib import Path
    data_path = Path(__file__).parent / "data" / "raw" / filename
    h1 = pd.read_csv(data_path, sep="\t")
    time_col = [c for c in h1.columns if c.lower() == "time"][0]
    h1[time_col] = pd.to_datetime(h1[time_col], format="mixed", dayfirst=False)
    h1 = h1.set_index(time_col).sort_index()
    h1.columns = [c.lower() for c in h1.columns]
    h1[f"ema{fast}"] = h1["close"].ewm(span=fast, adjust=False).mean()
    h1[f"ema{slow}"] = h1["close"].ewm(span=slow, adjust=False).mean()
    h1["htf_trend"] = "bullish"
    h1.loc[h1[f"ema{fast}"] < h1[f"ema{slow}"], "htf_trend"] = "bearish"
    return h1[["htf_trend", f"ema{fast}", f"ema{slow}"]]


def generate_all_signals():
    """Generate all raw signals across all indices using clean M1 + native H1 data."""
    all_signals = []
    all_trading_days = set()

    for sym in ALL_SYMBOLS:
        cfg = LIVE_INDEX_CONFIG[sym]
        # Load clean M1 data (actual 1-min bars only)
        data_file = DATA_FILES_M1[sym]
        df = load_data(data_file)
        # Load native H1 from MT5 for EMA (not resampled from M1)
        h1_file = DATA_FILES_H1[sym]
        htf_ema = load_h1_ema(h1_file, fast=20, slow=50)
        trading_days = sorted(set(df.index.date))
        all_trading_days.update(trading_days)
        point_value = BROKER_POINT_VALUES[sym]
        print(f"  {sym}: {len(df):,} M1 bars, {len(htf_ema):,} H1 bars, "
              f"{len(trading_days)} trading days ({trading_days[0]} -> {trading_days[-1]})")

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
                all_signals.append({
                    "symbol": sym, "signal": s, "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                    "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                })

    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals, all_trading_days


def compute_trade_pnl(sig, lots):
    """Compute P&L with realistic costs."""
    s = sig["signal"]
    symbol = sig["symbol"]
    point_value = sig["point_value"]

    exit_reason = getattr(s, "exit_reason", None)
    pnl_points = float(getattr(s, "pnl_points", 0.0))

    slippage_pts = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
    spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
    half_spread = spread_pts / 2.0

    entry_cost_pts = slippage_pts + half_spread
    if exit_reason == "TP":
        exit_cost_pts = half_spread
    else:
        exit_cost_pts = slippage_pts + half_spread

    total_cost_pts = entry_cost_pts + exit_cost_pts
    commission_cost = TRADING_COSTS["commission_per_lot"].get(symbol, 0.0) * lots

    pnl_raw = pnl_points * lots * point_value
    total_costs = total_cost_pts * lots * point_value + commission_cost
    pnl = pnl_raw - total_costs

    adjusted_pnl_points = pnl_points - total_cost_pts
    if adjusted_pnl_points > 0:
        result = "win" if exit_reason == "TP" else "partial_win"
    elif adjusted_pnl_points == 0:
        result = "breakeven"
    else:
        result = "loss"

    return pnl, total_costs, result, exit_reason


def simulate_fundednext():
    print("Generating signals across all indices...")
    all_signals, all_trading_days = generate_all_signals()
    print(f"Total raw signals: {len(all_signals)}")
    print()

    # ─── Multi-account, multi-phase simulation ───
    # Phase 1 (8% target) → Phase 2 (5% target) → FULLY PASSED
    # Breach in either phase → restart from Phase 1
    challenges = []     # list of challenge attempts (each has phase1 + optional phase2)
    challenge_num = 1
    current_phase = 1   # 1 or 2
    profit_target = PHASE1_TARGET_PCT

    current_balance = STARTING_BALANCE
    initial_balance = STARTING_BALANCE
    peak_balance = STARTING_BALANCE
    phase_start_date = None
    phase_trades = []
    phase_breached = False
    phase_passed = False
    breach_reason = None

    # Track per-challenge
    challenge_start_date = None
    challenge_phases = []  # accumulate phase records for this challenge

    # Daily state
    current_day = None
    day_start_balance = STARTING_BALANCE
    day_peak_balance = STARTING_BALANCE
    daily_losses_count = 0
    daily_trade_count = 0
    day_stopped = False

    open_positions = []

    def make_phase_record():
        end_date = phase_trades[-1]["date"].date() if phase_trades else current_day
        min_bal = min(t["balance"] for t in phase_trades) if phase_trades else current_balance
        return {
            "phase": current_phase,
            "status": "PASSED" if phase_passed else ("BREACHED" if phase_breached else "ACTIVE"),
            "breach_reason": breach_reason,
            "start_date": phase_start_date,
            "end_date": end_date,
            "trades": len(phase_trades),
            "final_balance": current_balance,
            "peak_balance": peak_balance,
            "net_pnl": current_balance - initial_balance,
            "return_pct": (current_balance - initial_balance) / initial_balance * 100,
            "max_dd_pct": (peak_balance - min_bal) / peak_balance * 100 if peak_balance > 0 else 0,
            "trade_log": phase_trades,
        }

    for sig_idx, sig in enumerate(all_signals):
        s = sig["signal"]
        symbol = sig["symbol"]
        point_value = sig["point_value"]
        entry_time = sig["entry_time"]
        exit_time = sig["exit_time"]
        trade_day = entry_time.date()

        if phase_start_date is None:
            phase_start_date = trade_day
        if challenge_start_date is None:
            challenge_start_date = trade_day

        # ─── New day reset ───
        if trade_day != current_day:
            current_day = trade_day
            day_start_balance = current_balance
            day_peak_balance = current_balance
            daily_losses_count = 0
            daily_trade_count = 0
            day_stopped = False

        day_peak_balance = max(day_peak_balance, current_balance)

        if day_stopped:
            continue

        # ─── Handle phase passed → advance to next phase or finalize challenge ───
        if phase_passed:
            rec = make_phase_record()
            challenge_phases.append(rec)

            if current_phase == 1:
                # Advance to Phase 2
                current_phase = 2
                profit_target = PHASE2_TARGET_PCT
                current_balance = STARTING_BALANCE
                initial_balance = STARTING_BALANCE
                peak_balance = STARTING_BALANCE
                phase_start_date = trade_day
                phase_trades = []
                phase_passed = False
                phase_breached = False
                breach_reason = None
                day_start_balance = STARTING_BALANCE
                day_peak_balance = STARTING_BALANCE
                daily_losses_count = 0
                daily_trade_count = 0
                day_stopped = False
                open_positions = []
            else:
                # Phase 2 passed → challenge FULLY PASSED
                challenges.append({
                    "challenge_num": challenge_num,
                    "status": "PASSED",
                    "start_date": challenge_start_date,
                    "end_date": rec["end_date"],
                    "phases": challenge_phases,
                })
                # Start fresh challenge
                challenge_num += 1
                current_phase = 1
                profit_target = PHASE1_TARGET_PCT
                challenge_start_date = trade_day
                challenge_phases = []
                current_balance = STARTING_BALANCE
                initial_balance = STARTING_BALANCE
                peak_balance = STARTING_BALANCE
                phase_start_date = trade_day
                phase_trades = []
                phase_passed = False
                phase_breached = False
                breach_reason = None
                day_start_balance = STARTING_BALANCE
                day_peak_balance = STARTING_BALANCE
                daily_losses_count = 0
                daily_trade_count = 0
                day_stopped = False
                open_positions = []

        # ─── Handle phase breached → restart entire challenge from Phase 1 ───
        if phase_breached:
            rec = make_phase_record()
            challenge_phases.append(rec)
            challenges.append({
                "challenge_num": challenge_num,
                "status": "BREACHED",
                "start_date": challenge_start_date,
                "end_date": rec["end_date"],
                "phases": challenge_phases,
                "breach_phase": current_phase,
                "breach_reason": breach_reason,
            })
            # Start fresh challenge from Phase 1
            challenge_num += 1
            current_phase = 1
            profit_target = PHASE1_TARGET_PCT
            challenge_start_date = trade_day
            challenge_phases = []
            current_balance = STARTING_BALANCE
            initial_balance = STARTING_BALANCE
            peak_balance = STARTING_BALANCE
            phase_start_date = trade_day
            phase_trades = []
            phase_passed = False
            phase_breached = False
            breach_reason = None
            day_start_balance = STARTING_BALANCE
            day_peak_balance = STARTING_BALANCE
            daily_losses_count = 0
            daily_trade_count = 0
            day_stopped = False
            open_positions = []

        # ─── Check daily -2% cutoff BEFORE entering ───
        daily_dd_from_peak = ((day_peak_balance - current_balance) / day_peak_balance * 100) if day_peak_balance > 0 else 0
        if daily_dd_from_peak >= DAILY_DD_CUTOFF:
            day_stopped = True
            continue

        # ─── Max daily losses ───
        if daily_losses_count >= MAX_DAILY_LOSSES:
            continue

        # ─── Max daily trades ───
        if daily_trade_count >= MAX_DAILY_TRADES:
            continue

        # ─── Max open positions ───
        open_positions = [p for p in open_positions if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            continue

        # ─── Risk sizing (3-tier from initial balance) ───
        dd_from_initial = ((initial_balance - current_balance) / initial_balance * 100) if initial_balance > 0 else 0
        dd_from_initial = max(0.0, dd_from_initial)  # only positive DD
        if dd_from_initial >= DD_TIER3_THRESHOLD:
            risk_pct = RISK_TIER3          # deep DD → 0.5%
        elif dd_from_initial >= DD_TIER2_THRESHOLD:
            risk_pct = RISK_TIER2          # moderate DD → 0.75%
        else:
            risk_pct = RISK_TIER1          # normal → 1.5%

        risk_amount = current_balance * (risk_pct / 100.0)
        risk_points = float(s.risk_points)
        if risk_points <= 0:
            continue

        lots = risk_amount / (risk_points * point_value)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        # ─── Execute trade ───
        pnl, costs, result, exit_reason = compute_trade_pnl(sig, lots)

        current_balance += pnl
        if pnl > 0:
            day_peak_balance = max(day_peak_balance, current_balance)
        peak_balance = max(peak_balance, current_balance)

        if result == "loss":
            daily_losses_count += 1
        daily_trade_count += 1

        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})

        phase_trades.append({
            "symbol": symbol, "date": entry_time, "direction": s.direction,
            "result": result, "risk_pct": risk_pct, "pnl": pnl, "costs": costs,
            "balance": current_balance, "exit_reason": exit_reason,
        })

        # ─── Post-trade checks ───

        # Daily -2% cutoff
        daily_dd_post = ((day_peak_balance - current_balance) / day_peak_balance * 100) if day_peak_balance > 0 else 0
        if daily_dd_post >= DAILY_DD_CUTOFF:
            day_stopped = True

        # FundedNext DAILY DD breach (5% from day start)
        fn_daily_dd = ((day_start_balance - current_balance) / day_start_balance * 100) if day_start_balance > 0 else 0
        if fn_daily_dd >= FUNDEDNEXT_DAILY_DD:
            phase_breached = True
            breach_reason = f"Daily DD {fn_daily_dd:.1f}% >= {FUNDEDNEXT_DAILY_DD}%"
            continue

        # FundedNext MAX DD breach (10% from initial capital)
        fn_max_dd = ((initial_balance - current_balance) / initial_balance * 100) if initial_balance > 0 else 0
        if fn_max_dd >= FUNDEDNEXT_MAX_DD:
            phase_breached = True
            breach_reason = f"Max DD {fn_max_dd:.1f}% from initial >= {FUNDEDNEXT_MAX_DD}%"
            continue

        # Profit target check
        profit_pct = ((current_balance - initial_balance) / initial_balance * 100) if initial_balance > 0 else 0
        if profit_pct >= profit_target:
            phase_passed = True
            continue

    # ─── Finalize last phase/challenge ───
    if phase_trades:
        rec = make_phase_record()
        challenge_phases.append(rec)
        if phase_passed:
            if current_phase == 2:
                challenges.append({
                    "challenge_num": challenge_num, "status": "PASSED",
                    "start_date": challenge_start_date,
                    "end_date": rec["end_date"], "phases": challenge_phases,
                })
            else:
                # Passed Phase 1 but no Phase 2 trades yet
                challenges.append({
                    "challenge_num": challenge_num, "status": "P1_PASSED_P2_PENDING",
                    "start_date": challenge_start_date,
                    "end_date": rec["end_date"], "phases": challenge_phases,
                })
        elif phase_breached:
            challenges.append({
                "challenge_num": challenge_num, "status": "BREACHED",
                "start_date": challenge_start_date, "end_date": rec["end_date"],
                "phases": challenge_phases, "breach_phase": current_phase,
                "breach_reason": breach_reason,
            })
        else:
            challenges.append({
                "challenge_num": challenge_num,
                "status": f"ACTIVE_P{current_phase}",
                "start_date": challenge_start_date,
                "end_date": rec["end_date"], "phases": challenge_phases,
            })

    # ═══════════════════ REPORT ═══════════════════

    passed_full = [c for c in challenges if c["status"] == "PASSED"]
    breached_all = [c for c in challenges if c["status"] == "BREACHED"]
    active_all = [c for c in challenges if "ACTIVE" in c.get("status", "") or "PENDING" in c.get("status", "")]

    print("=" * 70)
    print("  FUNDEDNEXT STELLAR CHALLENGE SIMULATION (Both Phases)")
    print("=" * 70)
    print(f"  Starting Balance : ${STARTING_BALANCE:,.0f}")
    print(f"  Phase 1 Target   : {PHASE1_TARGET_PCT}% (=${STARTING_BALANCE * PHASE1_TARGET_PCT / 100:,.0f})")
    print(f"  Phase 2 Target   : {PHASE2_TARGET_PCT}% (=${STARTING_BALANCE * PHASE2_TARGET_PCT / 100:,.0f})")
    print(f"  Risk Tier 1      : {RISK_TIER1}% (DD 0-{DD_TIER2_THRESHOLD}%)")
    print(f"  Risk Tier 2      : {RISK_TIER2}% (DD {DD_TIER2_THRESHOLD}-{DD_TIER3_THRESHOLD}%)")
    print(f"  Risk Tier 3      : {RISK_TIER3}% (DD {DD_TIER3_THRESHOLD}%+)")
    print(f"  Daily Cutoff     : {DAILY_DD_CUTOFF}% from day peak -> stop")
    print(f"  FundedNext Daily : {FUNDEDNEXT_DAILY_DD}% -> BREACH")
    print(f"  FundedNext Max DD: {FUNDEDNEXT_MAX_DD}% from initial -> BREACH")
    print(f"  Duration Goal    : {MAX_CHALLENGE_DAYS} calendar days (personal, not enforced)")
    print(f"  Max Open Pos     : {MAX_OPEN_POSITIONS}  |  Max Daily Losses: {MAX_DAILY_LOSSES}  |  Max Daily Trades: {MAX_DAILY_TRADES}")

    print(f"\n  {'='*58}")
    total_completed = len(passed_full) + len(breached_all)
    print(f"  CHALLENGES FULLY PASSED  : {len(passed_full)}")
    print(f"  CHALLENGES BREACHED      : {len(breached_all)}")
    if active_all:
        print(f"  CHALLENGES IN PROGRESS   : {len(active_all)}")
    if total_completed > 0:
        print(f"  PASS RATE                : {len(passed_full)/total_completed*100:.0f}%")
    if passed_full:
        durations = [(ch["end_date"] - ch["start_date"]).days for ch in passed_full]
        print(f"  {'─'*58}")
        print(f"  DURATION ANALYSIS (vs {MAX_CHALLENGE_DAYS}-day personal goal):")
        print(f"  AVG PASS DURATION        : {sum(durations)/len(durations):.0f} days")
        print(f"  FASTEST PASS             : {min(durations)} days")
        print(f"  SLOWEST PASS             : {max(durations)} days")
        under_goal = sum(1 for d in durations if d <= MAX_CHALLENGE_DAYS)
        print(f"  WITHIN {MAX_CHALLENGE_DAYS} DAYS            : {under_goal}/{len(passed_full)} ({under_goal/len(passed_full)*100:.0f}%)")
        over_goal = [d for d in durations if d > MAX_CHALLENGE_DAYS]
        if over_goal:
            print(f"  OVER {MAX_CHALLENGE_DAYS} DAYS             : {len(over_goal)} (avg {sum(over_goal)/len(over_goal):.0f} days)")
    print(f"  {'='*58}")

    for ch in challenges:
        status = ch["status"]
        print(f"\n  Challenge #{ch['challenge_num']}  [{status}]")
        print(f"    Period: {ch['start_date']} -> {ch['end_date']}")

        for p in ch["phases"]:
            phase_label = f"Phase {p['phase']}"
            target = PHASE1_TARGET_PCT if p["phase"] == 1 else PHASE2_TARGET_PCT
            print(f"\n    {phase_label} (target: {target}%)  [{p['status']}]")
            print(f"      Trades    : {p['trades']}")
            print(f"      Final Bal : ${p['final_balance']:,.2f}")
            print(f"      Net PnL   : ${p['net_pnl']:,.2f}  ({p['return_pct']:+.1f}%)")
            print(f"      Max DD    : {p['max_dd_pct']:.1f}%")
            if p["breach_reason"]:
                print(f"      Breach    : {p['breach_reason']}")
            if p["trades"] > 0:
                tdf = pd.DataFrame(p["trade_log"])
                wins = len(tdf[tdf["result"].isin(["win", "partial_win"])])
                losses_cnt = len(tdf[tdf["result"] == "loss"])
                wr = wins / len(tdf) * 100
                print(f"      Win Rate  : {wr:.1f}%  (W:{wins} L:{losses_cnt})")
                days_span = max(1, (p["end_date"] - p["start_date"]).days)
                weeks = max(1, days_span / 7)
                print(f"      Duration  : {days_span} days ({p['trades']/weeks:.1f} trades/wk)")

    # Overall
    total_trades = sum(p["trades"] for ch in challenges for p in ch["phases"])
    print(f"\n  {'='*58}")
    print(f"  OVERALL SUMMARY")
    print(f"  {'='*58}")
    print(f"  Total trades across all attempts: {total_trades}")
    print(f"  Total challenge attempts         : {len(challenges)}")
    print(f"  Fully PASSED (both phases)       : {len(passed_full)}")
    print(f"  BREACHED                         : {len(breached_all)}")

    if passed_full:
        for ch in passed_full:
            total_p_trades = sum(p["trades"] for p in ch["phases"])
            total_days = (ch["end_date"] - ch["start_date"]).days
            print(f"    Challenge #{ch['challenge_num']}: {total_p_trades} trades in {total_days} days")

    if breached_all:
        print(f"  Breach details:")
        for ch in breached_all:
            print(f"    Challenge #{ch['challenge_num']}: Phase {ch.get('breach_phase','?')} - {ch.get('breach_reason','?')}")

    print(f"  {'='*58}")


if __name__ == "__main__":
    simulate_fundednext()
