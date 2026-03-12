"""
FundedNext Stellar-2 Realistic Funded Trader Lifecycle Simulation
═════════════════════════════════════════════════════════════════

Uses pre-generated trades from run_histdata_backtest.py (all_trades.csv).

Lifecycle:
  1. CHALLENGE Phase 1: 8.5% target, 1.5% risk (3-tier adaptive), 5% daily DD / 10% max DD
  2. CHALLENGE Phase 2: 5.5% target, same rules as P1
  3. FUNDED PHASE: 0.5% risk per trade (0.25% after 4% DD from initial)
     - Bi-weekly payout: if balance >= initial + 1%, withdraw profit, reset to initial
     - 5% daily DD / 10% max DD breach → lose account → restart from Phase 1
  4. On breach at any point → restart entire lifecycle from Phase 1

Reports:
  - Full challenge pass/breach history
  - Funded phase payout history
  - Total net payouts (real money extracted)
  - Compare challenge vs funded risk profiles

Usage:
  python simulate_funded_lifecycle.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import timedelta
from collections import defaultdict

# ─── FundedNext Stellar-2 Rules ───
STARTING_BALANCE = 6000.0

# Challenge phase risk (3-tier adaptive from initial balance)
CHALLENGE_RISK_T1 = 1.5    # % risk (DD 0-3%)
CHALLENGE_RISK_T2 = 0.75   # % risk (DD 3-5%)
CHALLENGE_RISK_T3 = 0.5    # % risk (DD 5%+)
DD_TIER2 = 3.0
DD_TIER3 = 5.0

# Funded phase risk
FUNDED_RISK_NORMAL = 0.5    # % risk per trade
FUNDED_RISK_DEEP_DD = 0.25  # % risk after 4% DD from initial
FUNDED_DD_TIER = 4.0        # switch to deep DD risk at 4%

# Payout rules
PAYOUT_INTERVAL_DAYS = 14   # bi-weekly
PAYOUT_MIN_PROFIT_PCT = 1.0 # must have >= 1% profit from initial to withdraw
PAYOUT_SPLIT = 0.80         # 80% to trader (FundedNext Stellar keeps 20%)

# Breach limits
DAILY_DD_LIMIT = 5.0        # official daily DD breach (from day start)
MAX_DD_LIMIT = 10.0         # max DD from initial capital
DAILY_DD_CUTOFF = 2.0       # our safety: stop trading at 2% daily DD

# Challenge targets
PHASE1_TARGET = 8.5
PHASE2_TARGET = 5.5

# Operational
MAX_DAILY_TRADES = 2
MAX_DAILY_LOSSES = 2
MIN_TRADING_DAYS = 5

# Challenge fee (Stellar-2 $6K = $59 fee, refunded on pass)
CHALLENGE_FEE = 59.0


def load_trades():
    """Load pre-computed trades from the backtest."""
    csv_path = Path(__file__).parent / "reports" / "histdata_backtest" / "all_trades.csv"
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def get_challenge_risk(dd_from_initial_pct):
    """3-tier adaptive risk for challenge phases."""
    dd = max(0.0, dd_from_initial_pct)
    if dd >= DD_TIER3:
        return CHALLENGE_RISK_T3
    elif dd >= DD_TIER2:
        return CHALLENGE_RISK_T2
    else:
        return CHALLENGE_RISK_T1


def get_funded_risk(dd_from_initial_pct):
    """Funded phase risk: 0.5% normal, 0.25% after 4% DD."""
    dd = max(0.0, dd_from_initial_pct)
    if dd >= FUNDED_DD_TIER:
        return FUNDED_RISK_DEEP_DD
    else:
        return FUNDED_RISK_NORMAL


def rescale_trade_pnl(row, new_risk_pct, current_balance):
    """
    Rescale a trade's PnL based on new risk % and current balance.
    
    The original trade was sized at some risk_percent / balance.
    We rescale proportionally to maintain the same R-multiple outcome.
    """
    original_risk_amount = row["risk_amount"]
    if original_risk_amount <= 0:
        return 0.0, 0.0
    
    new_risk_amount = current_balance * (new_risk_pct / 100.0)
    scale_factor = new_risk_amount / original_risk_amount
    
    # PnL and costs scale with position size
    new_pnl = row["pnl"] * scale_factor
    new_costs = row["costs"] * scale_factor
    
    return new_pnl, new_costs


def simulate():
    trades_df = load_trades()
    print(f"  Loaded {len(trades_df)} trades from backtest")
    print(f"  Date range: {trades_df['date'].iloc[0].date()} → {trades_df['date'].iloc[-1].date()}")
    print()
    
    # ─── State tracking ───
    lifecycle_events = []     # all challenge/funded phase records
    payouts = []              # all payout events
    all_challenge_attempts = []
    
    # Current state
    state = "CHALLENGE_P1"    # CHALLENGE_P1, CHALLENGE_P2, FUNDED
    balance = STARTING_BALANCE
    initial_balance = STARTING_BALANCE
    peak_balance = STARTING_BALANCE
    
    # Phase tracking
    phase_start_idx = 0
    phase_start_date = None
    phase_trades = []
    phase_trading_days = set()
    
    # Funded tracking
    funded_start_date = None
    funded_total_payouts = 0.0
    funded_payout_count = 0
    last_payout_date = None
    funded_trades_count = 0
    funded_wins = 0
    funded_losses = 0
    funded_gross_pnl = 0.0
    
    # Challenge tracking
    challenge_num = 1
    challenge_start_date = None
    challenge_fees_paid = 0.0
    challenge_phases_log = []
    
    # Daily state
    current_day = None
    day_start_balance = STARTING_BALANCE
    day_peak_balance = STARTING_BALANCE
    daily_losses_count = 0
    daily_trade_count = 0
    day_stopped = False
    
    # Stats
    total_challenges_passed = 0
    total_challenges_breached = 0
    total_funded_breaches = 0
    total_trade_count = 0
    
    def reset_for_new_challenge():
        nonlocal state, balance, initial_balance, peak_balance
        nonlocal phase_start_date, phase_trades, phase_trading_days
        nonlocal challenge_start_date, challenge_phases_log, challenge_fees_paid
        nonlocal funded_start_date, funded_total_payouts, funded_payout_count
        nonlocal last_payout_date, funded_trades_count, funded_wins, funded_losses
        nonlocal funded_gross_pnl, challenge_num
        
        challenge_num += 1
        state = "CHALLENGE_P1"
        balance = STARTING_BALANCE
        initial_balance = STARTING_BALANCE
        peak_balance = STARTING_BALANCE
        phase_start_date = None
        phase_trades = []
        phase_trading_days = set()
        challenge_start_date = None
        challenge_phases_log = []
        challenge_fees_paid = 0.0
        funded_start_date = None
        last_payout_date = None
        funded_trades_count = 0
        funded_wins = 0
        funded_losses = 0
        funded_gross_pnl = 0.0
    
    def reset_daily(trade_day):
        nonlocal current_day, day_start_balance, day_peak_balance
        nonlocal daily_losses_count, daily_trade_count, day_stopped
        current_day = trade_day
        day_start_balance = balance
        day_peak_balance = balance
        daily_losses_count = 0
        daily_trade_count = 0
        day_stopped = False
    
    def get_target():
        if state == "CHALLENGE_P1":
            return PHASE1_TARGET
        elif state == "CHALLENGE_P2":
            return PHASE2_TARGET
        return None
    
    def check_payout_eligible(trade_date):
        """Check if bi-weekly payout is due and profitable."""
        nonlocal balance, last_payout_date, funded_total_payouts, funded_payout_count
        
        if state != "FUNDED":
            return
        
        if last_payout_date is None:
            last_payout_date = funded_start_date
        
        days_since_last = (trade_date - last_payout_date).days
        if days_since_last < PAYOUT_INTERVAL_DAYS:
            return
        
        profit = balance - initial_balance
        profit_pct = (profit / initial_balance) * 100
        
        if profit_pct >= PAYOUT_MIN_PROFIT_PCT:
            trader_share = profit * PAYOUT_SPLIT
            payouts.append({
                "payout_num": funded_payout_count + 1,
                "date": trade_date,
                "balance_before": balance,
                "profit": profit,
                "trader_share": trader_share,
                "fn_share": profit - trader_share,
                "profit_pct": profit_pct,
            })
            funded_total_payouts += trader_share
            funded_payout_count += 1
            balance = initial_balance  # reset to initial after payout
            peak_balance = initial_balance
            last_payout_date = trade_date
        else:
            # No payout but reset the timer
            last_payout_date = trade_date
    
    # ─── Main simulation loop ───
    for idx, row in trades_df.iterrows():
        trade_day = row["date"].date()
        
        # Initialize dates
        if phase_start_date is None:
            phase_start_date = trade_day
        if challenge_start_date is None:
            challenge_start_date = trade_day
            challenge_fees_paid += CHALLENGE_FEE
        if state == "FUNDED" and funded_start_date is None:
            funded_start_date = trade_day
            last_payout_date = trade_day
        
        # New day
        if trade_day != current_day:
            # Before resetting, check payout on day boundary
            if state == "FUNDED" and current_day is not None:
                check_payout_eligible(trade_day)
            reset_daily(trade_day)
        
        day_peak_balance = max(day_peak_balance, balance)
        
        if day_stopped:
            continue
        
        # ─── Pre-trade filters ───
        # Daily DD cutoff (our 2% safety)
        if day_peak_balance > 0:
            daily_dd = (day_peak_balance - balance) / day_peak_balance * 100
            if daily_dd >= DAILY_DD_CUTOFF:
                day_stopped = True
                continue
        
        if daily_losses_count >= MAX_DAILY_LOSSES:
            continue
        if daily_trade_count >= MAX_DAILY_TRADES:
            continue
        
        # ─── Determine risk ───
        dd_from_initial = max(0.0, (initial_balance - balance) / initial_balance * 100) if initial_balance > 0 else 0
        
        if state in ("CHALLENGE_P1", "CHALLENGE_P2"):
            risk_pct = get_challenge_risk(dd_from_initial)
        else:  # FUNDED
            risk_pct = get_funded_risk(dd_from_initial)
        
        # ─── Rescale trade PnL ───
        pnl, costs = rescale_trade_pnl(row, risk_pct, balance)
        
        # Determine result
        is_loss = row["result"] == "loss"
        
        # ─── Apply trade ───
        balance += pnl
        if pnl > 0:
            day_peak_balance = max(day_peak_balance, balance)
        peak_balance = max(peak_balance, balance)
        
        if is_loss:
            daily_losses_count += 1
        daily_trade_count += 1
        total_trade_count += 1
        
        phase_trades.append({
            "date": row["date"],
            "symbol": row["symbol"],
            "direction": row["direction"],
            "result": row["result"],
            "exit_reason": row["exit_reason"],
            "risk_pct": risk_pct,
            "pnl": pnl,
            "costs": costs,
            "balance": balance,
        })
        phase_trading_days.add(trade_day)
        
        if state == "FUNDED":
            funded_trades_count += 1
            funded_gross_pnl += pnl
            if not is_loss:
                funded_wins += 1
            else:
                funded_losses += 1
        
        # ─── Post-trade checks ───
        
        # Daily DD cutoff (2% safety)
        if day_peak_balance > 0:
            daily_dd_post = (day_peak_balance - balance) / day_peak_balance * 100
            if daily_dd_post >= DAILY_DD_CUTOFF:
                day_stopped = True
        
        # FundedNext DAILY DD breach (5% from day start)
        if day_start_balance > 0:
            fn_daily_dd = (day_start_balance - balance) / day_start_balance * 100
            if fn_daily_dd >= DAILY_DD_LIMIT:
                breach_reason = f"Daily DD {fn_daily_dd:.1f}% >= {DAILY_DD_LIMIT}%"
                _handle_breach(
                    state, breach_reason, lifecycle_events, all_challenge_attempts,
                    challenge_num, challenge_start_date, trade_day, challenge_phases_log,
                    phase_trades, phase_trading_days, balance, initial_balance, peak_balance,
                )
                if state == "FUNDED":
                    total_funded_breaches += 1
                else:
                    total_challenges_breached += 1
                reset_for_new_challenge()
                reset_daily(trade_day)
                continue
        
        # FundedNext MAX DD breach (10% from initial)
        if initial_balance > 0:
            fn_max_dd = (initial_balance - balance) / initial_balance * 100
            if fn_max_dd >= MAX_DD_LIMIT:
                breach_reason = f"Max DD {fn_max_dd:.1f}% from initial >= {MAX_DD_LIMIT}%"
                _handle_breach(
                    state, breach_reason, lifecycle_events, all_challenge_attempts,
                    challenge_num, challenge_start_date, trade_day, challenge_phases_log,
                    phase_trades, phase_trading_days, balance, initial_balance, peak_balance,
                )
                if state == "FUNDED":
                    total_funded_breaches += 1
                else:
                    total_challenges_breached += 1
                reset_for_new_challenge()
                reset_daily(trade_day)
                continue
        
        # ─── Challenge target check ───
        if state in ("CHALLENGE_P1", "CHALLENGE_P2"):
            target = get_target()
            profit_pct = (balance - initial_balance) / initial_balance * 100 if initial_balance > 0 else 0
            
            if profit_pct >= target and len(phase_trading_days) >= MIN_TRADING_DAYS:
                # Phase passed!
                phase_record = {
                    "type": state,
                    "status": "PASSED",
                    "challenge_num": challenge_num,
                    "start_date": phase_start_date,
                    "end_date": trade_day,
                    "trades": len(phase_trades),
                    "trading_days": len(phase_trading_days),
                    "final_balance": balance,
                    "initial_balance": initial_balance,
                    "net_pnl": balance - initial_balance,
                    "return_pct": profit_pct,
                    "max_dd_pct": (peak_balance - min(t["balance"] for t in phase_trades)) / peak_balance * 100 if phase_trades else 0,
                    "wins": sum(1 for t in phase_trades if t["result"] != "loss"),
                    "losses": sum(1 for t in phase_trades if t["result"] == "loss"),
                }
                challenge_phases_log.append(phase_record)
                lifecycle_events.append(phase_record)
                
                if state == "CHALLENGE_P1":
                    # Advance to Phase 2
                    state = "CHALLENGE_P2"
                    balance = STARTING_BALANCE
                    initial_balance = STARTING_BALANCE
                    peak_balance = STARTING_BALANCE
                    phase_start_date = None
                    phase_trades = []
                    phase_trading_days = set()
                    reset_daily(trade_day)
                    
                elif state == "CHALLENGE_P2":
                    # Challenge fully passed! Enter funded phase
                    total_challenges_passed += 1
                    all_challenge_attempts.append({
                        "challenge_num": challenge_num,
                        "status": "PASSED",
                        "start_date": challenge_start_date,
                        "end_date": trade_day,
                        "duration_days": (trade_day - challenge_start_date).days,
                        "phases": list(challenge_phases_log),
                        "fee": CHALLENGE_FEE,
                        "fee_refunded": True,
                    })
                    
                    # Enter funded phase
                    state = "FUNDED"
                    balance = STARTING_BALANCE
                    initial_balance = STARTING_BALANCE
                    peak_balance = STARTING_BALANCE
                    phase_start_date = None
                    phase_trades = []
                    phase_trading_days = set()
                    funded_start_date = None
                    last_payout_date = None
                    funded_trades_count = 0
                    funded_wins = 0
                    funded_losses = 0
                    funded_gross_pnl = 0.0
                    challenge_phases_log = []
                    reset_daily(trade_day)
    
    # ─── Final payout check ───
    if state == "FUNDED" and balance > initial_balance:
        profit = balance - initial_balance
        profit_pct = profit / initial_balance * 100
        if profit_pct >= PAYOUT_MIN_PROFIT_PCT:
            trader_share = profit * PAYOUT_SPLIT
            payouts.append({
                "payout_num": funded_payout_count + 1,
                "date": trades_df["date"].iloc[-1].date(),
                "balance_before": balance,
                "profit": profit,
                "trader_share": trader_share,
                "fn_share": profit - trader_share,
                "profit_pct": profit_pct,
            })
            funded_total_payouts += trader_share
            funded_payout_count += 1
    
    # ═══════════════════ GENERATE REPORT ═══════════════════
    
    report_lines = []
    def p(line=""):
        report_lines.append(line)
    
    p("=" * 80)
    p("     FUNDEDNEXT STELLAR-2 — REALISTIC FUNDED TRADER LIFECYCLE SIMULATION")
    p("     Using 16-Year HistData Backtest Trades (SPX500, NDX100, GER30)")
    p(f"     Generated: {pd.Timestamp.now().strftime('%B %d, %Y')}")
    p("=" * 80)
    
    p()
    p("━" * 70)
    p("SECTION 1: SIMULATION RULES")
    p("━" * 70)
    p()
    p(f"  Starting Capital     : ${STARTING_BALANCE:,.0f}")
    p(f"  Challenge Fee        : ${CHALLENGE_FEE:.0f} (refunded on pass)")
    p()
    p("  CHALLENGE PHASES:")
    p(f"    Phase 1 Target     : {PHASE1_TARGET}% (${STARTING_BALANCE * PHASE1_TARGET / 100:,.0f})")
    p(f"    Phase 2 Target     : {PHASE2_TARGET}% (${STARTING_BALANCE * PHASE2_TARGET / 100:,.0f})")
    p(f"    Risk Tier 1        : {CHALLENGE_RISK_T1}% (DD 0-{DD_TIER2}%)")
    p(f"    Risk Tier 2        : {CHALLENGE_RISK_T2}% (DD {DD_TIER2}-{DD_TIER3}%)")
    p(f"    Risk Tier 3        : {CHALLENGE_RISK_T3}% (DD {DD_TIER3}%+)")
    p(f"    Min Trading Days   : {MIN_TRADING_DAYS}")
    p()
    p("  FUNDED PHASE:")
    p(f"    Risk (normal)      : {FUNDED_RISK_NORMAL}% per trade")
    p(f"    Risk (DD > {FUNDED_DD_TIER}%)    : {FUNDED_RISK_DEEP_DD}% per trade")
    p(f"    Payout Interval    : Every {PAYOUT_INTERVAL_DAYS} days (bi-weekly)")
    p(f"    Payout Threshold   : >= {PAYOUT_MIN_PROFIT_PCT}% profit from initial")
    p(f"    Profit Split       : {PAYOUT_SPLIT*100:.0f}% trader / {(1-PAYOUT_SPLIT)*100:.0f}% FundedNext")
    p()
    p("  BREACH LIMITS (both phases):")
    p(f"    Daily DD           : {DAILY_DD_LIMIT}% from day start → BREACH")
    p(f"    Max DD             : {MAX_DD_LIMIT}% from initial → BREACH")
    p(f"    Daily Cutoff       : {DAILY_DD_CUTOFF}% from day peak → stop trading")
    p(f"    Max Daily Trades   : {MAX_DAILY_TRADES}")
    p(f"    Max Daily Losses   : {MAX_DAILY_LOSSES}")
    
    # ─── Section 2: Challenge Summary ───
    p()
    p("━" * 70)
    p("SECTION 2: CHALLENGE PASS / BREACH HISTORY")
    p("━" * 70)
    p()
    
    passed_challenges = [c for c in all_challenge_attempts if c["status"] == "PASSED"]
    breached_challenges = [c for c in all_challenge_attempts if c["status"] == "BREACHED"]
    
    total_attempts = len(all_challenge_attempts)
    p(f"  Total Challenge Attempts : {total_attempts}")
    p(f"  Fully Passed (P1+P2)     : {len(passed_challenges)}")
    p(f"  Breached                 : {len(breached_challenges)}")
    if total_attempts > 0:
        p(f"  Pass Rate                : {len(passed_challenges)/total_attempts*100:.0f}%")
    
    total_fees = sum(c.get("fee", CHALLENGE_FEE) for c in all_challenge_attempts)
    refunded_fees = sum(c.get("fee", CHALLENGE_FEE) for c in passed_challenges if c.get("fee_refunded"))
    net_fees = total_fees - refunded_fees
    p(f"  Total Fees Paid          : ${total_fees:,.0f}")
    p(f"  Fees Refunded (passes)   : ${refunded_fees:,.0f}")
    p(f"  Net Fee Cost             : ${net_fees:,.0f}")
    
    if passed_challenges:
        durations = [c["duration_days"] for c in passed_challenges]
        p(f"\n  Pass Duration Stats:")
        p(f"    Average              : {sum(durations)/len(durations):.0f} days")
        p(f"    Fastest              : {min(durations)} days")
        p(f"    Slowest              : {max(durations)} days")
    
    p()
    for ch in all_challenge_attempts:
        status_icon = "✓" if ch["status"] == "PASSED" else "✗"
        p(f"  [{status_icon}] Challenge #{ch['challenge_num']}  {ch['status']}")
        p(f"      Period: {ch['start_date']} → {ch['end_date']}  ({ch['duration_days']} days)")
        for phase in ch.get("phases", []):
            phase_name = "P1" if "P1" in phase["type"] else "P2"
            target = PHASE1_TARGET if phase_name == "P1" else PHASE2_TARGET
            p(f"      {phase_name}: {phase['trades']} trades, {phase['trading_days']} days, "
              f"{phase['return_pct']:+.1f}% (target {target}%), DD {phase['max_dd_pct']:.1f}%, "
              f"WR {phase['wins']/(phase['wins']+phase['losses'])*100:.0f}% — {phase['status']}")
            if phase.get("breach_reason"):
                p(f"          Breach: {phase['breach_reason']}")
    
    # ─── Section 3: Funded Phase ───
    p()
    p("━" * 70)
    p("SECTION 3: FUNDED PHASE PERFORMANCE")
    p("━" * 70)
    p()
    
    funded_events = [e for e in lifecycle_events if e.get("type") == "FUNDED"]
    
    if funded_trades_count > 0 or payouts:
        p(f"  Times Reached Funded     : {total_challenges_passed}")
        p(f"  Funded Account Breaches  : {total_funded_breaches}")
        p(f"  Total Funded Trades      : {funded_trades_count}")
        if funded_trades_count > 0:
            p(f"  Funded Win Rate          : {funded_wins/funded_trades_count*100:.1f}%")
        p(f"  Funded Gross PnL (acct)  : ${funded_gross_pnl:,.2f}")
    else:
        p("  (No funded phase trades in this simulation)")
    
    # ─── Section 4: Payouts ───
    p()
    p("━" * 70)
    p("SECTION 4: PAYOUT HISTORY")
    p("━" * 70)
    p()
    
    if payouts:
        p(f"  Total Payouts            : {len(payouts)}")
        p(f"  Total Withdrawn (80%)    : ${sum(p['trader_share'] for p in payouts):,.2f}")
        p(f"  FundedNext Share (20%)   : ${sum(p['fn_share'] for p in payouts):,.2f}")
        p(f"  Avg Payout               : ${sum(p['trader_share'] for p in payouts)/len(payouts):,.2f}")
        p()
        p(f"  ┌──────┬────────────┬────────────┬──────────────┬──────────────┬────────┐")
        p(f"  │  #   │    Date    │  Balance   │   Profit     │ Your Share   │  Pct   │")
        p(f"  ├──────┼────────────┼────────────┼──────────────┼──────────────┼────────┤")
        for pay in payouts:
            p(f"  │ {pay['payout_num']:4d} │ {pay['date']} │ ${pay['balance_before']:>8,.2f} │ "
              f"${pay['profit']:>10,.2f} │ ${pay['trader_share']:>10,.2f} │ {pay['profit_pct']:5.1f}% │")
        p(f"  └──────┴────────────┴────────────┴──────────────┴──────────────┴────────┘")
    else:
        p("  No payouts made (never reached funded phase or never hit payout threshold)")
    
    # ─── Section 5: Net Financial Summary ───
    p()
    p("━" * 70)
    p("SECTION 5: NET FINANCIAL SUMMARY — REAL MONEY")
    p("━" * 70)
    p()
    
    total_payouts_amount = sum(pay["trader_share"] for pay in payouts)
    
    p(f"  Total Payouts Received   : ${total_payouts_amount:,.2f}")
    p(f"  Challenge Fees (net)     : -${net_fees:,.0f}")
    p(f"  ─────────────────────────────────")
    net_profit = total_payouts_amount - net_fees
    p(f"  NET REAL PROFIT          : ${net_profit:,.2f}")
    p()
    
    if total_payouts_amount > 0:
        roi_on_fees = (net_profit / total_fees * 100) if total_fees > 0 else float('inf')
        p(f"  ROI on Fees Paid         : {roi_on_fees:,.0f}%")
    
    # Time span
    first_date = trades_df["date"].iloc[0].date()
    last_date = trades_df["date"].iloc[-1].date()
    total_years = (last_date - first_date).days / 365.25
    if total_years > 0 and total_payouts_amount > 0:
        annual_payout = total_payouts_amount / total_years
        p(f"  Annualized Payouts       : ${annual_payout:,.2f}/year")
        monthly_payout = annual_payout / 12
        p(f"  Monthly Average          : ${monthly_payout:,.2f}/month")
    
    p(f"\n  Simulation Period        : {first_date} → {last_date} ({total_years:.1f} years)")
    p(f"  Total Trades Processed   : {total_trade_count}")
    
    # ─── Section 6: Lifecycle Timeline ───
    p()
    p("━" * 70)
    p("SECTION 6: LIFECYCLE TIMELINE")
    p("━" * 70)
    p()
    
    timeline = []
    for ch in all_challenge_attempts:
        for phase in ch.get("phases", []):
            timeline.append({
                "start": phase["start_date"],
                "end": phase["end_date"],
                "type": phase["type"].replace("CHALLENGE_", ""),
                "status": phase["status"],
                "ch_num": ch["challenge_num"],
            })
    for pay in payouts:
        timeline.append({
            "start": pay["date"],
            "end": pay["date"],
            "type": "PAYOUT",
            "status": f"${pay['trader_share']:,.0f}",
            "ch_num": None,
        })
    
    timeline.sort(key=lambda x: x["start"])
    
    for t in timeline:
        if t["type"] == "PAYOUT":
            p(f"  {t['start']}  💰 PAYOUT: {t['status']}")
        else:
            icon = "✓" if t["status"] == "PASSED" else "✗"
            p(f"  {t['start']} → {t['end']}  [{icon}] Ch#{t['ch_num']} {t['type']} — {t['status']}")
    
    # Current state at end
    p()
    p(f"  Current State: {state}")
    if state == "FUNDED":
        p(f"  Funded Balance: ${balance:,.2f}")
        p(f"  Funded Trades: {funded_trades_count}")
    else:
        profit_pct = (balance - initial_balance) / initial_balance * 100 if initial_balance > 0 else 0
        p(f"  Balance: ${balance:,.2f} ({profit_pct:+.1f}%)")
    
    p()
    p("=" * 80)
    p("  END OF SIMULATION")
    p("=" * 80)
    
    # ─── Print & save report ───
    report_text = "\n".join(report_lines)
    print(report_text)
    
    report_path = Path(__file__).parent / "reports" / "histdata_backtest" / "FundedNext_Lifecycle_Report.txt"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"\n  Report saved: {report_path}")
    
    return report_text


def _handle_breach(state, breach_reason, lifecycle_events, all_challenge_attempts,
                   challenge_num, challenge_start_date, trade_day, challenge_phases_log,
                   phase_trades, phase_trading_days, balance, initial_balance, peak_balance):
    """Record a breach event."""
    min_bal = min(t["balance"] for t in phase_trades) if phase_trades else balance
    max_dd = (peak_balance - min_bal) / peak_balance * 100 if peak_balance > 0 else 0
    wins = sum(1 for t in phase_trades if t["result"] != "loss")
    losses = sum(1 for t in phase_trades if t["result"] == "loss")
    
    phase_record = {
        "type": state,
        "status": "BREACHED",
        "breach_reason": breach_reason,
        "challenge_num": challenge_num,
        "start_date": phase_trades[0]["date"].date() if phase_trades else trade_day,
        "end_date": trade_day,
        "trades": len(phase_trades),
        "trading_days": len(phase_trading_days),
        "final_balance": balance,
        "initial_balance": initial_balance,
        "net_pnl": balance - initial_balance,
        "return_pct": (balance - initial_balance) / initial_balance * 100 if initial_balance > 0 else 0,
        "max_dd_pct": max_dd,
        "wins": wins,
        "losses": losses,
    }
    
    challenge_phases_log.append(phase_record)
    lifecycle_events.append(phase_record)
    
    all_challenge_attempts.append({
        "challenge_num": challenge_num,
        "status": "BREACHED",
        "start_date": challenge_start_date if challenge_start_date else trade_day,
        "end_date": trade_day,
        "duration_days": (trade_day - challenge_start_date).days if challenge_start_date else 0,
        "phases": list(challenge_phases_log),
        "breach_reason": breach_reason,
        "breach_phase": state,
        "fee": CHALLENGE_FEE,
        "fee_refunded": False,
    })


if __name__ == "__main__":
    simulate()
