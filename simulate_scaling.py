"""
FundedNext Stellar-2 — Compounding Multi-Account Scaling Simulation
═══════════════════════════════════════════════════════════════════

After passing the first $6K challenge and getting funded:
  1. Payouts from funded accounts go into a "wallet"
  2. Once the wallet has enough, buy another challenge (largest affordable)
  3. Pass that challenge → another funded account
  4. Repeat until TOTAL FUNDED CAPITAL >= $500K

FundedNext Account Tiers (Stellar-2):
  $6,000   →  $59 fee
  $15,000  →  $119 fee
  $25,000  →  $199 fee
  $50,000  →  $299 fee
  $100,000 →  $519 fee
  $200,000 →  $999 fee

Each funded account trades independently on the SAME signals.
Each has its own balance/DD/payout tracking.

Usage:
  python simulate_scaling.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from copy import deepcopy
from collections import defaultdict

# ─── FundedNext Stellar-2 Account Tiers ───
ACCOUNT_TIERS = [
    {"capital": 6_000,   "fee": 59},
    {"capital": 15_000,  "fee": 119},
    {"capital": 25_000,  "fee": 199},
    {"capital": 50_000,  "fee": 299},
    {"capital": 100_000, "fee": 519},
    {"capital": 200_000, "fee": 999},
]

# Challenge rules
CHALLENGE_RISK_T1 = 1.5    # % (DD 0-3%)
CHALLENGE_RISK_T2 = 0.75   # % (DD 3-5%)
CHALLENGE_RISK_T3 = 0.5    # % (DD 5%+)
DD_TIER2 = 3.0
DD_TIER3 = 5.0
PHASE1_TARGET = 8.5
PHASE2_TARGET = 5.5
MIN_TRADING_DAYS = 5

# Funded phase rules
FUNDED_RISK_NORMAL = 0.5
FUNDED_RISK_DEEP_DD = 0.25
FUNDED_DD_TIER = 4.0

# Payout
PAYOUT_INTERVAL_DAYS = 14
PAYOUT_MIN_PROFIT_PCT = 1.0
PAYOUT_SPLIT = 0.80  # 80% to trader

# Breach limits
DAILY_DD_LIMIT = 5.0
MAX_DD_LIMIT = 10.0
DAILY_DD_CUTOFF = 2.0

# Operational
MAX_DAILY_TRADES = 2
MAX_DAILY_LOSSES = 2

# Scaling target
TARGET_FUNDED_CAPITAL = 500_000

# Initial investment
INITIAL_WALLET = 59  # enough for first $6K challenge


class Account:
    """Represents a single FundedNext account (challenge or funded)."""
    
    _next_id = 1
    
    def __init__(self, capital, fee, buy_date):
        self.id = Account._next_id
        Account._next_id += 1
        self.capital = capital
        self.fee = fee
        self.buy_date = buy_date
        
        # Challenge state
        self.state = "CHALLENGE_P1"  # CHALLENGE_P1, CHALLENGE_P2, FUNDED, BREACHED, DONE
        self.balance = float(capital)
        self.initial_balance = float(capital)
        self.peak_balance = float(capital)
        
        # Phase tracking
        self.phase_start_date = None
        self.phase_trades = []
        self.phase_trading_days = set()
        self.challenge_start_date = buy_date
        
        # Funded tracking
        self.funded_start_date = None
        self.last_payout_date = None
        self.total_payouts = 0.0
        self.payout_count = 0
        self.payout_log = []
        
        # Daily state
        self.current_day = None
        self.day_start_balance = float(capital)
        self.day_peak_balance = float(capital)
        self.daily_losses = 0
        self.daily_trades = 0
        self.day_stopped = False
        
        # Stats
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.gross_pnl = 0.0
        
        # History
        self.phase_history = []  # records of passed/breached phases
        self.pass_date = None
        self.breach_date = None
        self.breach_reason = None
        self.fee_refunded = False
    
    def reset_daily(self, trade_day):
        self.current_day = trade_day
        self.day_start_balance = self.balance
        self.day_peak_balance = self.balance
        self.daily_losses = 0
        self.daily_trades = 0
        self.day_stopped = False
    
    def get_risk_pct(self):
        dd = max(0.0, (self.initial_balance - self.balance) / self.initial_balance * 100)
        if self.state == "FUNDED":
            return FUNDED_RISK_DEEP_DD if dd >= FUNDED_DD_TIER else FUNDED_RISK_NORMAL
        else:
            if dd >= DD_TIER3:
                return CHALLENGE_RISK_T3
            elif dd >= DD_TIER2:
                return CHALLENGE_RISK_T2
            return CHALLENGE_RISK_T1
    
    def can_take_trade(self):
        if self.state in ("BREACHED", "DONE"):
            return False
        if self.day_stopped:
            return False
        if self.daily_losses >= MAX_DAILY_LOSSES:
            return False
        if self.daily_trades >= MAX_DAILY_TRADES:
            return False
        # Daily DD cutoff (2% safety)
        if self.day_peak_balance > 0:
            dd = (self.day_peak_balance - self.balance) / self.day_peak_balance * 100
            if dd >= DAILY_DD_CUTOFF:
                self.day_stopped = True
                return False
        return True
    
    def apply_trade(self, row):
        """Apply a trade, rescaling PnL for this account's risk level."""
        risk_pct = self.get_risk_pct()
        original_risk_amount = row["risk_amount"]
        if original_risk_amount <= 0:
            return 0.0
        
        new_risk_amount = self.balance * (risk_pct / 100.0)
        scale = new_risk_amount / original_risk_amount
        pnl = row["pnl"] * scale
        
        is_loss = row["result"] == "loss"
        
        self.balance += pnl
        if pnl > 0:
            self.day_peak_balance = max(self.day_peak_balance, self.balance)
        self.peak_balance = max(self.peak_balance, self.balance)
        
        if is_loss:
            self.daily_losses += 1
            self.losses += 1
        else:
            self.wins += 1
        self.daily_trades += 1
        self.total_trades += 1
        self.gross_pnl += pnl
        
        trade_day = row["date"].date() if hasattr(row["date"], 'date') else row["date"]
        self.phase_trading_days.add(trade_day)
        
        return pnl
    
    def check_breach(self, trade_day):
        """Check FundedNext breach conditions. Returns breach_reason or None."""
        # Daily DD (5% from day start)
        if self.day_start_balance > 0:
            dd = (self.day_start_balance - self.balance) / self.day_start_balance * 100
            if dd >= DAILY_DD_LIMIT:
                return f"Daily DD {dd:.1f}% >= {DAILY_DD_LIMIT}%"
        
        # Max DD (10% from initial)
        if self.initial_balance > 0:
            dd = (self.initial_balance - self.balance) / self.initial_balance * 100
            if dd >= MAX_DD_LIMIT:
                return f"Max DD {dd:.1f}% from initial >= {MAX_DD_LIMIT}%"
        
        # Post-trade daily cutoff
        if self.day_peak_balance > 0:
            dd = (self.day_peak_balance - self.balance) / self.day_peak_balance * 100
            if dd >= DAILY_DD_CUTOFF:
                self.day_stopped = True
        
        return None
    
    def check_challenge_pass(self):
        """Check if challenge target is hit. Returns True if passed."""
        if self.state not in ("CHALLENGE_P1", "CHALLENGE_P2"):
            return False
        
        target = PHASE1_TARGET if self.state == "CHALLENGE_P1" else PHASE2_TARGET
        profit_pct = (self.balance - self.initial_balance) / self.initial_balance * 100
        
        if profit_pct >= target and len(self.phase_trading_days) >= MIN_TRADING_DAYS:
            return True
        return False
    
    def advance_phase(self, trade_day):
        """Advance from P1→P2 or P2→FUNDED."""
        profit_pct = (self.balance - self.initial_balance) / self.initial_balance * 100
        max_dd = 0
        if self.phase_trades:
            min_bal = min(self.phase_trades)
            max_dd = (self.peak_balance - min_bal) / self.peak_balance * 100
        
        self.phase_history.append({
            "phase": self.state,
            "status": "PASSED",
            "trades": self.total_trades if self.state == "CHALLENGE_P1" else self.total_trades - sum(p.get("trade_count", 0) for p in self.phase_history),
            "trading_days": len(self.phase_trading_days),
            "return_pct": profit_pct,
            "start_date": self.phase_start_date,
            "end_date": trade_day,
        })
        
        if self.state == "CHALLENGE_P1":
            self.state = "CHALLENGE_P2"
            self.balance = float(self.capital)
            self.initial_balance = float(self.capital)
            self.peak_balance = float(self.capital)
            self.phase_start_date = trade_day
            self.phase_trades = []
            self.phase_trading_days = set()
        elif self.state == "CHALLENGE_P2":
            self.state = "FUNDED"
            self.balance = float(self.capital)
            self.initial_balance = float(self.capital)
            self.peak_balance = float(self.capital)
            self.phase_start_date = trade_day
            self.phase_trades = []
            self.phase_trading_days = set()
            self.funded_start_date = trade_day
            self.last_payout_date = trade_day
            self.pass_date = trade_day
            self.fee_refunded = True
            self.total_trades = 0
            self.wins = 0
            self.losses = 0
            self.gross_pnl = 0.0
    
    def check_payout(self, trade_day):
        """Check bi-weekly payout eligibility. Returns payout amount (trader share) or 0."""
        if self.state != "FUNDED":
            return 0.0
        if self.last_payout_date is None:
            self.last_payout_date = self.funded_start_date or trade_day
        
        days_since = (trade_day - self.last_payout_date).days
        if days_since < PAYOUT_INTERVAL_DAYS:
            return 0.0
        
        profit = self.balance - self.initial_balance
        profit_pct = profit / self.initial_balance * 100
        
        if profit_pct >= PAYOUT_MIN_PROFIT_PCT:
            trader_share = profit * PAYOUT_SPLIT
            self.payout_log.append({
                "date": trade_day,
                "balance": self.balance,
                "profit": profit,
                "trader_share": trader_share,
                "profit_pct": profit_pct,
            })
            self.total_payouts += trader_share
            self.payout_count += 1
            self.balance = self.initial_balance
            self.peak_balance = self.initial_balance
            self.last_payout_date = trade_day
            return trader_share
        else:
            self.last_payout_date = trade_day
            return 0.0
    
    def mark_breached(self, trade_day, reason):
        self.state = "BREACHED"
        self.breach_date = trade_day
        self.breach_reason = reason


def get_best_affordable_tier(wallet):
    """Get the largest account tier the wallet can afford."""
    affordable = [t for t in ACCOUNT_TIERS if t["fee"] <= wallet]
    if not affordable:
        return None
    return max(affordable, key=lambda t: t["capital"])


def load_trades():
    csv_path = Path(__file__).parent / "reports" / "histdata_backtest" / "all_trades.csv"
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def simulate():
    trades_df = load_trades()
    print(f"  Loaded {len(trades_df)} trades")
    print(f"  Date range: {trades_df['date'].iloc[0].date()} → {trades_df['date'].iloc[-1].date()}")
    print()
    
    # ─── State ───
    wallet = 0.0
    total_invested = INITIAL_WALLET  # personal money put in
    wallet -= 0  # fee paid from initial investment
    
    accounts = []          # all accounts ever created
    active_accounts = []   # currently active (challenge or funded)
    
    all_payouts = []       # all payout events across all accounts
    purchase_log = []      # all challenge purchases
    
    # Stats
    total_funded_capital = 0
    target_reached = False
    target_reached_date = None
    target_reached_trade_idx = None
    
    # Buy first challenge
    first_tier = ACCOUNT_TIERS[0]  # $6K
    first_date = trades_df["date"].iloc[0].date()
    acc = Account(first_tier["capital"], first_tier["fee"], first_date)
    acc.phase_start_date = first_date
    accounts.append(acc)
    active_accounts.append(acc)
    purchase_log.append({
        "date": first_date,
        "capital": first_tier["capital"],
        "fee": first_tier["fee"],
        "account_id": acc.id,
        "wallet_after": wallet,
        "source": "initial_investment",
    })
    
    current_day = None
    
    # ─── Main loop ───
    for idx, row in trades_df.iterrows():
        trade_day = row["date"].date() if hasattr(row["date"], 'date') else row["date"]
        
        # New day processing
        if trade_day != current_day:
            current_day = trade_day
            
            # Check payouts for all funded accounts
            for acc in active_accounts:
                if acc.state == "FUNDED":
                    payout = acc.check_payout(trade_day)
                    if payout > 0:
                        wallet += payout
                        all_payouts.append({
                            "date": trade_day,
                            "account_id": acc.id,
                            "capital": acc.capital,
                            "amount": payout,
                            "wallet_after": wallet,
                        })
            
            # Check if wallet can buy a new challenge
            # Stop buying once funded capital already >= target
            funded_capital_now = sum(a.capital for a in active_accounts if a.state == "FUNDED")
            buying_allowed = funded_capital_now < TARGET_FUNDED_CAPITAL
            if buying_allowed:
                tier = get_best_affordable_tier(wallet)
                if tier is not None:
                    new_acc = Account(tier["capital"], tier["fee"], trade_day)
                    new_acc.phase_start_date = trade_day
                    wallet -= tier["fee"]
                    accounts.append(new_acc)
                    active_accounts.append(new_acc)
                    purchase_log.append({
                        "date": trade_day,
                        "capital": tier["capital"],
                        "fee": tier["fee"],
                        "account_id": new_acc.id,
                        "wallet_after": wallet,
                        "source": "payouts",
                    })
            
            # Reset daily state for all active accounts
            for acc in active_accounts:
                if acc.state not in ("BREACHED", "DONE"):
                    acc.reset_daily(trade_day)
            
            # Check total funded capital
            funded_capital = sum(a.capital for a in active_accounts if a.state == "FUNDED")
            if funded_capital >= TARGET_FUNDED_CAPITAL and not target_reached:
                target_reached = True
                target_reached_date = trade_day
                target_reached_trade_idx = idx
                total_funded_capital = funded_capital
        
        # Apply trade to each active account
        for acc in list(active_accounts):
            if not acc.can_take_trade():
                continue
            
            # Initialize phase start date
            if acc.phase_start_date is None:
                acc.phase_start_date = trade_day
            
            pnl = acc.apply_trade(row)
            acc.phase_trades.append(acc.balance)
            
            # Breach check
            breach = acc.check_breach(trade_day)
            if breach:
                acc.mark_breached(trade_day, breach)
                continue
            
            # Challenge pass check
            if acc.check_challenge_pass():
                acc.advance_phase(trade_day)
                if acc.state == "FUNDED":
                    # Fee refunded to wallet
                    wallet += acc.fee
    
    # ─── Final payouts for all funded accounts ───
    last_date = trades_df["date"].iloc[-1].date()
    for acc in active_accounts:
        if acc.state == "FUNDED" and acc.balance > acc.initial_balance:
            profit = acc.balance - acc.initial_balance
            profit_pct = profit / acc.initial_balance * 100
            if profit_pct >= PAYOUT_MIN_PROFIT_PCT:
                trader_share = profit * PAYOUT_SPLIT
                wallet += trader_share
                acc.total_payouts += trader_share
                acc.payout_count += 1
                all_payouts.append({
                    "date": last_date,
                    "account_id": acc.id,
                    "capital": acc.capital,
                    "amount": trader_share,
                    "wallet_after": wallet,
                })
    
    # ═══════════════════ GENERATE REPORT ═══════════════════
    
    lines = []
    def p(line=""):
        lines.append(line)
    
    p("=" * 85)
    p("   FUNDEDNEXT STELLAR-2 — MULTI-ACCOUNT COMPOUNDING SCALING SIMULATION")
    p(f"   Target: ${TARGET_FUNDED_CAPITAL:,} Total Funded Capital")
    p(f"   Using 16-Year HistData Backtest Trades (SPX500, NDX100, GER30)")
    p(f"   Generated: {pd.Timestamp.now().strftime('%B %d, %Y')}")
    p("=" * 85)
    
    # ─── Section 1: Rules ───
    p()
    p("━" * 75)
    p("SECTION 1: SIMULATION RULES")
    p("━" * 75)
    p()
    p(f"  Initial Investment       : ${INITIAL_WALLET} (first challenge fee)")
    p(f"  Starting Account         : ${ACCOUNT_TIERS[0]['capital']:,}")
    p(f"  Target Funded Capital    : ${TARGET_FUNDED_CAPITAL:,}")
    p()
    p("  ACCOUNT TIERS:")
    for t in ACCOUNT_TIERS:
        p(f"    ${t['capital']:>7,}  →  ${t['fee']:>3} fee")
    p()
    p("  SCALING STRATEGY:")
    p("    1. Pass challenge → get funded account")
    p("    2. Funded payouts → wallet")
    p("    3. Wallet >= fee → buy LARGEST affordable challenge")
    p("    4. Repeat until total funded capital >= $500K")
    p()
    p("  CHALLENGE: 1.5% risk (3-tier adaptive), 8.5%/5.5% targets")
    p("  FUNDED:    0.5% risk (0.25% after 4% DD)")
    p("  PAYOUTS:   Bi-weekly, >= 1% profit, 80/20 split")
    
    # ─── Section 2: Scaling Journey ───
    p()
    p("━" * 75)
    p("SECTION 2: SCALING JOURNEY — ACCOUNT PURCHASES")
    p("━" * 75)
    p()
    
    funded_accounts = [a for a in accounts if a.state == "FUNDED" or a.pass_date is not None]
    breached_accounts = [a for a in accounts if a.state == "BREACHED"]
    challenge_active = [a for a in accounts if a.state in ("CHALLENGE_P1", "CHALLENGE_P2")]
    
    total_fees = sum(p["fee"] for p in purchase_log)
    refunded_fees = sum(a.fee for a in accounts if a.fee_refunded)
    
    p(f"  Total Challenges Purchased : {len(accounts)}")
    p(f"  Challenges Passed          : {len(funded_accounts)}")
    p(f"  Challenges Breached        : {len(breached_accounts)}")
    p(f"  Challenges In Progress     : {len(challenge_active)}")
    p(f"  Pass Rate                  : {len(funded_accounts)/max(1,len(funded_accounts)+len(breached_accounts))*100:.0f}%")
    p()
    p(f"  Total Fees Paid            : ${total_fees:,.0f}")
    p(f"  Fees Refunded              : ${refunded_fees:,.0f}")
    p(f"  Net Fee Cost               : ${total_fees - refunded_fees:,.0f}")
    p()
    
    p(f"  ┌──────┬────────────┬──────────┬──────┬─────────────┬───────────────────────┐")
    p(f"  │  #   │    Date    │ Capital  │ Fee  │  Source     │ Status                │")
    p(f"  ├──────┼────────────┼──────────┼──────┼─────────────┼───────────────────────┤")
    for i, pur in enumerate(purchase_log):
        acc_obj = next(a for a in accounts if a.id == pur["account_id"])
        if acc_obj.pass_date:
            status = f"PASSED → FUNDED ({acc_obj.pass_date})"
        elif acc_obj.state == "BREACHED":
            status = f"BREACHED ({acc_obj.breach_date})"
        elif acc_obj.state in ("CHALLENGE_P1", "CHALLENGE_P2"):
            status = f"IN PROGRESS ({acc_obj.state})"
        else:
            status = acc_obj.state
        src = "💰 initial" if pur["source"] == "initial_investment" else "📈 payouts"
        p(f"  │ {i+1:4d} │ {pur['date']} │ ${pur['capital']:>6,} │ ${pur['fee']:>3} │ {src:11s} │ {status:<21s} │")
    p(f"  └──────┴────────────┴──────────┴──────┴─────────────┴───────────────────────┘")
    
    # ─── Section 3: Funded Accounts Summary ───
    p()
    p("━" * 75)
    p("SECTION 3: FUNDED ACCOUNTS SUMMARY")
    p("━" * 75)
    p()
    
    active_funded = [a for a in active_accounts if a.state == "FUNDED"]
    all_funded_ever = [a for a in accounts if a.pass_date is not None]
    
    total_funded_now = sum(a.capital for a in active_funded)
    p(f"  Active Funded Accounts     : {len(active_funded)}")
    p(f"  Total Funded Capital (now) : ${total_funded_now:,}")
    p(f"  Funded Accounts Breached   : {sum(1 for a in all_funded_ever if a.state == 'BREACHED')}")
    p()
    
    if target_reached:
        p(f"  🎯 TARGET ${TARGET_FUNDED_CAPITAL:,} REACHED: {target_reached_date}")
    else:
        p(f"  ⚠️  Target ${TARGET_FUNDED_CAPITAL:,} NOT yet reached")
        p(f"      Current funded capital: ${total_funded_now:,}")
    p()
    
    if active_funded:
        p(f"  ┌──────┬──────────┬───────────┬────────┬─────────┬───────────┬─────────┐")
        p(f"  │  ID  │ Capital  │  Funded   │ Trades │   WR    │  Payouts  │ Tot Paid│")
        p(f"  ├──────┼──────────┼───────────┼────────┼─────────┼───────────┼─────────┤")
        for acc in sorted(active_funded, key=lambda a: a.capital, reverse=True):
            wr = acc.wins / max(1, acc.total_trades) * 100
            p(f"  │ {acc.id:4d} │ ${acc.capital:>6,} │ {acc.funded_start_date} │ {acc.total_trades:6d} │ {wr:5.1f}%  │ {acc.payout_count:4d} pay  │${acc.total_payouts:>7,.0f}│")
        p(f"  └──────┴──────────┴───────────┴────────┴─────────┴───────────┴─────────┘")
    
    # ─── Section 4: All Payouts ───
    p()
    p("━" * 75)
    p("SECTION 4: PAYOUT SUMMARY")
    p("━" * 75)
    p()
    
    total_payout_amount = sum(pay["amount"] for pay in all_payouts)
    p(f"  Total Payouts              : {len(all_payouts)}")
    p(f"  Total Withdrawn (80%)      : ${total_payout_amount:,.2f}")
    p(f"  Average Payout             : ${total_payout_amount/max(1,len(all_payouts)):,.2f}")
    p()
    
    # Group payouts by year
    if all_payouts:
        payout_df = pd.DataFrame(all_payouts)
        payout_df["year"] = pd.to_datetime(payout_df["date"].astype(str)).dt.year
        yearly = payout_df.groupby("year").agg(
            count=("amount", "count"),
            total=("amount", "sum"),
        )
        p(f"  ┌──────┬────────┬──────────────┐")
        p(f"  │ Year │ Count  │    Total     │")
        p(f"  ├──────┼────────┼──────────────┤")
        for year, r in yearly.iterrows():
            p(f"  │ {year} │ {r['count']:6.0f} │ ${r['total']:>10,.2f} │")
        p(f"  └──────┴────────┴──────────────┘")
    
    # Show last 20 payouts
    if len(all_payouts) > 20:
        p(f"\n  Last 20 Payouts:")
    elif all_payouts:
        p(f"\n  All Payouts:")
    
    show_payouts = all_payouts[-20:] if len(all_payouts) > 20 else all_payouts
    if show_payouts:
        p(f"  ┌──────┬────────────┬────────┬──────────────┐")
        p(f"  │  #   │    Date    │ Acct $ │   Payout     │")
        p(f"  ├──────┼────────────┼────────┼──────────────┤")
        start_num = len(all_payouts) - len(show_payouts) + 1
        for i, pay in enumerate(show_payouts):
            p(f"  │ {start_num+i:4d} │ {pay['date']} │ ${pay['capital']/1000:.0f}K    │ ${pay['amount']:>10,.2f} │")
        p(f"  └──────┴────────────┴────────┴──────────────┘")
    
    # ─── Section 5: Financial Summary ───
    p()
    p("━" * 75)
    p("SECTION 5: NET FINANCIAL SUMMARY — REAL MONEY")
    p("━" * 75)
    p()
    
    # Money in wallet currently (not yet used for challenges)
    remaining_wallet = wallet
    
    # Net fees = total fees - refunded fees
    net_fees = total_fees - refunded_fees
    
    # Financial summary:
    # wallet = all payouts received - fees paid from payouts + refunded fees
    # Net profit = wallet - initial investment
    payout_fees = sum(p["fee"] for p in purchase_log if p["source"] == "payouts")
    
    p(f"  MONEY IN:")
    p(f"    Personal Investment      : ${INITIAL_WALLET:,.0f}")
    p()
    p(f"  MONEY FLOW:")
    p(f"    Total Payouts Received   : ${total_payout_amount:,.2f}")
    p(f"    Fees Paid from Payouts   : -${payout_fees:,.0f}")
    p(f"    Fees Refunded (on pass)  : +${refunded_fees:,.0f}")
    p(f"    ─────────────────────────────────────")
    p(f"    Wallet Balance Now       : ${remaining_wallet:,.2f}")
    p()
    
    net_profit = remaining_wallet - INITIAL_WALLET
    p(f"    NET PROFIT (wallet - inv) : ${net_profit:,.2f}")
    p()
    
    roi = net_profit / INITIAL_WALLET * 100
    p(f"  ROI on ${INITIAL_WALLET} investment    : {roi:,.0f}%")
    
    first_date = trades_df["date"].iloc[0].date()
    last_date = trades_df["date"].iloc[-1].date()
    total_years = (last_date - first_date).days / 365.25
    
    if total_years > 0 and total_payout_amount > 0:
        annual = total_payout_amount / total_years
        monthly = annual / 12
        p(f"  Annualized Gross Payouts   : ${annual:,.2f}/year")
        p(f"  Monthly Average            : ${monthly:,.2f}/month")
    
    p(f"\n  Simulation Period          : {first_date} → {last_date} ({total_years:.1f} years)")
    
    # ─── Section 6: Scaling Timeline ───
    p()
    p("━" * 75)
    p("SECTION 6: SCALING TIMELINE")
    p("━" * 75)
    p()
    
    timeline = []
    for pur in purchase_log:
        timeline.append({"date": pur["date"], "type": "BUY", "detail": f"Buy ${pur['capital']:,} challenge (fee ${pur['fee']})", "sort": 0})
    for acc in accounts:
        if acc.pass_date:
            timeline.append({"date": acc.pass_date, "type": "PASS", "detail": f"Acct #{acc.id} ${acc.capital:,} FUNDED", "sort": 1})
        if acc.state == "BREACHED":
            timeline.append({"date": acc.breach_date, "type": "BREACH", "detail": f"Acct #{acc.id} ${acc.capital:,} BREACHED: {acc.breach_reason}", "sort": 2})
    
    # Aggregate payouts by date for timeline
    payout_by_date = defaultdict(float)
    for pay in all_payouts:
        payout_by_date[pay["date"]] += pay["amount"]
    for date, amt in payout_by_date.items():
        timeline.append({"date": date, "type": "PAYOUT", "detail": f"Payout ${amt:,.0f}", "sort": 3})
    
    if target_reached:
        timeline.append({"date": target_reached_date, "type": "TARGET", "detail": f"🎯 ${TARGET_FUNDED_CAPITAL:,} FUNDED CAPITAL REACHED!", "sort": 0})
    
    timeline.sort(key=lambda x: (x["date"], x["sort"]))
    
    icons = {"BUY": "🛒", "PASS": "✅", "BREACH": "❌", "PAYOUT": "💰", "TARGET": "🎯"}
    
    # Show milestones and key events
    milestones = [t for t in timeline if t["type"] in ("BUY", "PASS", "BREACH", "TARGET")]
    
    p("  KEY MILESTONES:")
    for t in milestones:
        icon = icons.get(t["type"], "•")
        p(f"  {t['date']}  {icon} {t['detail']}")
    
    # Funded capital progression
    p()
    p("  FUNDED CAPITAL PROGRESSION:")
    capital_events = []
    running_capital = 0
    for acc in sorted(accounts, key=lambda a: a.pass_date or a.buy_date):
        if acc.pass_date:
            running_capital += acc.capital
            capital_events.append((acc.pass_date, running_capital, acc.capital))
        if acc.state == "BREACHED" and acc.breach_date and acc.funded_start_date:
            # Only subtract if it was breached AFTER being funded
            running_capital -= acc.capital
            capital_events.append((acc.breach_date, running_capital, -acc.capital))
    
    for date, total, change in capital_events:
        sign = "+" if change > 0 else ""
        p(f"    {date}  {sign}${change:,}  →  Total: ${total:,}")
    
    p()
    p("=" * 85)
    p("  END OF SCALING SIMULATION")
    p("=" * 85)
    
    report_text = "\n".join(lines)
    print(report_text)
    
    report_path = Path(__file__).parent / "reports" / "histdata_backtest" / "FundedNext_Scaling_Report.txt"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"\n  Report saved: {report_path}")


if __name__ == "__main__":
    simulate()
