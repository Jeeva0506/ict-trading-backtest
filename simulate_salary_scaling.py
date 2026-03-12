"""
FundedNext Stellar-2 — Salary-Funded Scaling Simulation
════════════════════════════════════════════════════════
Scenario:
  1. Start with $6K challenge, pass organically
  2. Use payouts to buy more challenges initially
  3. After 3 accounts are FUNDED in live → confidence gained
  4. From that point: buy ONE $100K challenge EVERY MONTH from salary ($519/mo)
  5. Payouts from ALL funded accounts accumulate in wallet (bonus income)
  6. Target: $500K total funded capital

Usage:
  python simulate_salary_scaling.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
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
CHALLENGE_RISK_T1 = 1.5
CHALLENGE_RISK_T2 = 0.75
CHALLENGE_RISK_T3 = 0.5
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
PAYOUT_SPLIT = 0.80

# Breach limits
DAILY_DD_LIMIT = 5.0
MAX_DD_LIMIT = 10.0
DAILY_DD_CUTOFF = 2.0
MAX_DAILY_TRADES = 2
MAX_DAILY_LOSSES = 2

# ─── Salary Scaling Config ───
CONFIDENCE_THRESHOLD = 3        # funded accounts before salary buying starts
SALARY_CHALLENGE_CAPITAL = 100_000
SALARY_CHALLENGE_FEE = 519
SALARY_BUY_INTERVAL_DAYS = 30   # one per month

TARGET_FUNDED_CAPITAL = 500_000
INITIAL_WALLET = 59


class Account:
    _next_id = 1

    def __init__(self, capital, fee, buy_date, source="payouts"):
        self.id = Account._next_id
        Account._next_id += 1
        self.capital = capital
        self.fee = fee
        self.buy_date = buy_date
        self.source = source  # "initial", "payouts", "salary"

        self.state = "CHALLENGE_P1"
        self.balance = float(capital)
        self.initial_balance = float(capital)
        self.peak_balance = float(capital)

        self.phase_start_date = None
        self.phase_trades = []
        self.phase_trading_days = set()
        self.challenge_start_date = buy_date

        self.funded_start_date = None
        self.last_payout_date = None
        self.total_payouts = 0.0
        self.payout_count = 0
        self.payout_log = []

        self.current_day = None
        self.day_start_balance = float(capital)
        self.day_peak_balance = float(capital)
        self.daily_losses = 0
        self.daily_trades = 0
        self.day_stopped = False

        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.gross_pnl = 0.0

        self.phase_history = []
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
        if self.day_peak_balance > 0:
            dd = (self.day_peak_balance - self.balance) / self.day_peak_balance * 100
            if dd >= DAILY_DD_CUTOFF:
                self.day_stopped = True
                return False
        return True

    def apply_trade(self, row):
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
        if self.day_start_balance > 0:
            dd = (self.day_start_balance - self.balance) / self.day_start_balance * 100
            if dd >= DAILY_DD_LIMIT:
                return f"Daily DD {dd:.1f}%"
        if self.initial_balance > 0:
            dd = (self.initial_balance - self.balance) / self.initial_balance * 100
            if dd >= MAX_DD_LIMIT:
                return f"Max DD {dd:.1f}%"
        if self.day_peak_balance > 0:
            dd = (self.day_peak_balance - self.balance) / self.day_peak_balance * 100
            if dd >= DAILY_DD_CUTOFF:
                self.day_stopped = True
        return None

    def check_challenge_pass(self):
        if self.state not in ("CHALLENGE_P1", "CHALLENGE_P2"):
            return False
        target = PHASE1_TARGET if self.state == "CHALLENGE_P1" else PHASE2_TARGET
        profit_pct = (self.balance - self.initial_balance) / self.initial_balance * 100
        return profit_pct >= target and len(self.phase_trading_days) >= MIN_TRADING_DAYS

    def advance_phase(self, trade_day):
        profit_pct = (self.balance - self.initial_balance) / self.initial_balance * 100
        self.phase_history.append({
            "phase": self.state, "status": "PASSED",
            "trading_days": len(self.phase_trading_days),
            "return_pct": profit_pct,
            "start_date": self.phase_start_date, "end_date": trade_day,
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
                "date": trade_day, "balance": self.balance,
                "profit": profit, "trader_share": trader_share,
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

    wallet = 0.0
    total_salary_spent = SALARY_CHALLENGE_FEE * 0  # tracked separately
    total_personal_investment = INITIAL_WALLET

    accounts = []
    active_accounts = []
    all_payouts = []
    purchase_log = []

    salary_buying_active = False
    salary_buy_started_date = None
    last_salary_buy_date = None
    confidence_date = None

    # Buy first challenge
    first_tier = ACCOUNT_TIERS[0]
    first_date = trades_df["date"].iloc[0].date()
    acc = Account(first_tier["capital"], first_tier["fee"], first_date, source="initial")
    acc.phase_start_date = first_date
    accounts.append(acc)
    active_accounts.append(acc)
    purchase_log.append({
        "date": first_date, "capital": first_tier["capital"],
        "fee": first_tier["fee"], "account_id": acc.id,
        "wallet_after": wallet, "source": "initial",
    })

    current_day = None

    for idx, row in trades_df.iterrows():
        trade_day = row["date"].date() if hasattr(row["date"], 'date') else row["date"]

        if trade_day != current_day:
            current_day = trade_day

            # Check payouts
            for acc in active_accounts:
                if acc.state == "FUNDED":
                    payout = acc.check_payout(trade_day)
                    if payout > 0:
                        wallet += payout
                        all_payouts.append({
                            "date": trade_day, "account_id": acc.id,
                            "capital": acc.capital, "amount": payout,
                            "wallet_after": wallet,
                        })

            # Check confidence threshold
            funded_count = sum(1 for a in active_accounts if a.state == "FUNDED")
            if not salary_buying_active and funded_count >= CONFIDENCE_THRESHOLD:
                salary_buying_active = True
                confidence_date = trade_day
                last_salary_buy_date = trade_day  # start counting from here

            # ─── Buying logic ───
            funded_capital_now = sum(a.capital for a in active_accounts if a.state == "FUNDED")
            in_challenge_capital = sum(a.capital for a in active_accounts if a.state in ("CHALLENGE_P1", "CHALLENGE_P2"))
            pipeline_total = funded_capital_now + in_challenge_capital

            if pipeline_total < TARGET_FUNDED_CAPITAL:
                if salary_buying_active:
                    # Buy $100K from salary every ~30 days
                    if last_salary_buy_date is None or (trade_day - last_salary_buy_date).days >= SALARY_BUY_INTERVAL_DAYS:
                        new_acc = Account(SALARY_CHALLENGE_CAPITAL, SALARY_CHALLENGE_FEE, trade_day, source="salary")
                        new_acc.phase_start_date = trade_day
                        total_salary_spent += SALARY_CHALLENGE_FEE
                        total_personal_investment += SALARY_CHALLENGE_FEE
                        accounts.append(new_acc)
                        active_accounts.append(new_acc)
                        purchase_log.append({
                            "date": trade_day, "capital": SALARY_CHALLENGE_CAPITAL,
                            "fee": SALARY_CHALLENGE_FEE, "account_id": new_acc.id,
                            "wallet_after": wallet, "source": "salary",
                        })
                        last_salary_buy_date = trade_day
                else:
                    # Before confidence: use payouts to buy challenges
                    tier = get_best_affordable_tier(wallet)
                    if tier is not None:
                        new_acc = Account(tier["capital"], tier["fee"], trade_day, source="payouts")
                        new_acc.phase_start_date = trade_day
                        wallet -= tier["fee"]
                        accounts.append(new_acc)
                        active_accounts.append(new_acc)
                        purchase_log.append({
                            "date": trade_day, "capital": tier["capital"],
                            "fee": tier["fee"], "account_id": new_acc.id,
                            "wallet_after": wallet, "source": "payouts",
                        })

            # Reset daily state
            for acc in active_accounts:
                if acc.state not in ("BREACHED", "DONE"):
                    acc.reset_daily(trade_day)

        # Apply trade to each active account
        for acc in list(active_accounts):
            if not acc.can_take_trade():
                continue
            if acc.phase_start_date is None:
                acc.phase_start_date = trade_day
            pnl = acc.apply_trade(row)
            acc.phase_trades.append(acc.balance)
            breach = acc.check_breach(trade_day)
            if breach:
                acc.mark_breached(trade_day, breach)
                continue
            if acc.check_challenge_pass():
                acc.advance_phase(trade_day)
                if acc.state == "FUNDED":
                    wallet += acc.fee  # fee refund

    # Final payouts
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
                    "date": last_date, "account_id": acc.id,
                    "capital": acc.capital, "amount": trader_share,
                    "wallet_after": wallet,
                })

    # ═══════════════════ REPORT ═══════════════════

    lines = []
    def p(line=""):
        lines.append(line)

    funded_accounts = [a for a in accounts if a.pass_date is not None]
    breached_accounts = [a for a in accounts if a.state == "BREACHED"]
    challenge_active = [a for a in accounts if a.state in ("CHALLENGE_P1", "CHALLENGE_P2")]
    active_funded = [a for a in active_accounts if a.state == "FUNDED"]
    total_funded_now = sum(a.capital for a in active_funded)

    salary_accounts = [p for p in purchase_log if p["source"] == "salary"]
    payout_accounts = [p for p in purchase_log if p["source"] == "payouts"]

    p("=" * 90)
    p("   FUNDEDNEXT STELLAR-2 — SALARY-FUNDED SCALING SIMULATION")
    p(f"   Strategy: Pass 3 accounts → then buy $100K challenge/month from salary")
    p(f"   Target: ${TARGET_FUNDED_CAPITAL:,} Total Funded Capital")
    p(f"   Generated: {pd.Timestamp.now().strftime('%B %d, %Y')}")
    p("=" * 90)

    p()
    p("━" * 80)
    p("SECTION 1: SIMULATION RULES")
    p("━" * 80)
    p()
    p(f"  PHASE 1 — ORGANIC (before confidence):")
    p(f"    Start with $6K challenge (${INITIAL_WALLET} fee)")
    p(f"    Use payouts to buy more challenges")
    p(f"    Until {CONFIDENCE_THRESHOLD} accounts are FUNDED in live")
    p()
    p(f"  PHASE 2 — SALARY-FUNDED (after confidence):")
    p(f"    Buy ONE ${SALARY_CHALLENGE_CAPITAL:,} challenge every month")
    p(f"    Fee: ${SALARY_CHALLENGE_FEE}/month from salary")
    p(f"    All payouts go to wallet (pure profit)")
    p(f"    Continue until pipeline ≥ ${TARGET_FUNDED_CAPITAL:,}")
    p()
    p(f"  CHALLENGE: 1.5% risk (3-tier adaptive), 8.5%/5.5% targets")
    p(f"  FUNDED:    0.5% risk (0.25% after 4% DD)")
    p(f"  PAYOUTS:   Bi-weekly, ≥1% profit, 80/20 split")

    # ─── Section 2: Scaling Journey ───
    p()
    p("━" * 80)
    p("SECTION 2: SCALING JOURNEY")
    p("━" * 80)
    p()

    total_fees = sum(pur["fee"] for pur in purchase_log)
    refunded_fees = sum(a.fee for a in accounts if a.fee_refunded)

    p(f"  Total Challenges Purchased : {len(accounts)}")
    p(f"    └ From initial           : 1")
    p(f"    └ From payouts           : {len(payout_accounts)}")
    p(f"    └ From salary            : {len(salary_accounts)}")
    p(f"  Challenges Passed          : {len(funded_accounts)}")
    p(f"  Challenges Breached        : {len(breached_accounts)}")
    p(f"  Challenges In Progress     : {len(challenge_active)}")
    p(f"  Pass Rate                  : {len(funded_accounts)/max(1,len(funded_accounts)+len(breached_accounts))*100:.0f}%")
    p()

    if confidence_date:
        p(f"  ✅ CONFIDENCE GAINED        : {confidence_date}")
        p(f"     ({CONFIDENCE_THRESHOLD} accounts funded → started buying $100K/month from salary)")
    p()

    p(f"  INVESTMENT BREAKDOWN:")
    p(f"    Initial fee               : ${INITIAL_WALLET}")
    p(f"    Salary spent on fees      : ${total_salary_spent:,}")
    p(f"    Total personal investment : ${total_personal_investment:,}")
    p(f"    Fees refunded (on pass)   : ${refunded_fees:,}")
    p(f"    Net fee cost              : ${total_personal_investment - refunded_fees:,}")
    p()

    # Purchase table
    p(f"  ┌──────┬────────────┬──────────┬──────┬──────────┬─────────────────────────────┐")
    p(f"  │  #   │    Date    │ Capital  │ Fee  │  Source  │ Status                      │")
    p(f"  ├──────┼────────────┼──────────┼──────┼──────────┼─────────────────────────────┤")
    for i, pur in enumerate(purchase_log):
        acc_obj = next(a for a in accounts if a.id == pur["account_id"])
        if acc_obj.pass_date:
            status = f"FUNDED ({acc_obj.pass_date})"
        elif acc_obj.state == "BREACHED":
            status = f"BREACHED ({acc_obj.breach_date})"
        elif acc_obj.state in ("CHALLENGE_P1", "CHALLENGE_P2"):
            status = f"IN PROGRESS ({acc_obj.state})"
        else:
            status = acc_obj.state
        src_icon = {"initial": "🏁", "payouts": "📈", "salary": "💼"}
        icon = src_icon.get(pur["source"], "?")
        src_label = pur["source"]
        p(f"  │ {i+1:4d} │ {pur['date']} │ ${pur['capital']:>6,} │ ${pur['fee']:>3} │ {icon} {src_label:<6s} │ {status:<27s} │")
    p(f"  └──────┴────────────┴──────────┴──────┴──────────┴─────────────────────────────┘")

    # ─── Section 3: Funded Accounts ───
    p()
    p("━" * 80)
    p("SECTION 3: FUNDED ACCOUNTS")
    p("━" * 80)
    p()

    p(f"  Active Funded Accounts     : {len(active_funded)}")
    p(f"  Total Funded Capital       : ${total_funded_now:,}")
    p()

    if total_funded_now >= TARGET_FUNDED_CAPITAL:
        p(f"  🎯 TARGET ${TARGET_FUNDED_CAPITAL:,} REACHED!")
    else:
        p(f"  ⚠️  Current: ${total_funded_now:,} / ${TARGET_FUNDED_CAPITAL:,} target")
    p()

    if active_funded:
        p(f"  ┌──────┬──────────┬──────────┬───────────┬────────┬────────┬───────────┐")
        p(f"  │  ID  │ Capital  │ Source   │  Funded   │ Trades │  WR    │ Tot Paid  │")
        p(f"  ├──────┼──────────┼──────────┼───────────┼────────┼────────┼───────────┤")
        for acc in sorted(active_funded, key=lambda a: a.capital, reverse=True):
            wr = acc.wins / max(1, acc.total_trades) * 100
            p(f"  │ {acc.id:4d} │ ${acc.capital:>6,} │ {acc.source:<8s} │ {acc.funded_start_date} │ {acc.total_trades:6d} │ {wr:4.1f}%  │ ${acc.total_payouts:>7,.0f} │")
        p(f"  └──────┴──────────┴──────────┴───────────┴────────┴────────┴───────────┘")

    # ─── Section 4: Monthly Payouts ───
    p()
    p("━" * 80)
    p("SECTION 4: PAYOUT SUMMARY")
    p("━" * 80)
    p()

    total_payout_amount = sum(pay["amount"] for pay in all_payouts)
    p(f"  Total Payouts              : {len(all_payouts)}")
    p(f"  Total Withdrawn (80%)      : ${total_payout_amount:,.2f}")
    p(f"  Average Payout             : ${total_payout_amount/max(1,len(all_payouts)):,.2f}")
    p()

    if all_payouts:
        payout_df = pd.DataFrame(all_payouts)
        payout_df["year"] = pd.to_datetime(payout_df["date"].astype(str)).dt.year
        yearly = payout_df.groupby("year").agg(count=("amount", "count"), total=("amount", "sum"))

        p(f"  ┌──────┬────────┬───────────────┬───────────────┐")
        p(f"  │ Year │ Count  │   Total Paid  │   Per Month   │")
        p(f"  ├──────┼────────┼───────────────┼───────────────┤")
        for year, r in yearly.iterrows():
            p(f"  │ {year} │ {r['count']:6.0f} │ ${r['total']:>11,.2f} │ ${r['total']/12:>11,.2f} │")
        p(f"  └──────┴────────┴───────────────┴───────────────┘")

    # ─── Section 5: Financial Summary ───
    p()
    p("━" * 80)
    p("SECTION 5: NET FINANCIAL SUMMARY")
    p("━" * 80)
    p()

    remaining_wallet = wallet

    p(f"  MONEY IN (personal):                             ")
    p(f"    Initial challenge fee     : ${INITIAL_WALLET:>10,}")
    p(f"    Salary → challenge fees   : ${total_salary_spent:>10,}")
    p(f"    ─────────────────────────────────────────────── ")
    p(f"    Total Personal Invested   : ${total_personal_investment:>10,}")
    p()
    p(f"  MONEY OUT (from trading):                        ")
    p(f"    Total Payouts (80%)       : ${total_payout_amount:>10,.2f}")
    p(f"    Fee Refunds (on pass)     : ${refunded_fees:>10,}")
    p(f"    ─────────────────────────────────────────────── ")
    p(f"    Total Money Back          : ${total_payout_amount + refunded_fees:>10,.2f}")
    p()

    net_profit = total_payout_amount + refunded_fees - total_personal_investment + remaining_wallet
    # Actually net profit = wallet balance - 0 (wallet has all payouts + refunds - payout-bought fees)
    # Simple: what you got back minus what you put in
    net_profit = (total_payout_amount + refunded_fees) - total_personal_investment
    roi = net_profit / max(1, total_personal_investment) * 100

    p(f"    ═══════════════════════════════════════════════ ")
    p(f"    NET PROFIT                : ${net_profit:>10,.2f}")
    p(f"    ROI                       : {roi:,.0f}%")
    p()

    first_date = trades_df["date"].iloc[0].date()
    total_years = (last_date - first_date).days / 365.25

    if total_years > 0 and total_payout_amount > 0:
        annual = total_payout_amount / total_years
        monthly = annual / 12
        p(f"    Avg Annual Payouts        : ${annual:>10,.2f}/year")
        p(f"    Avg Monthly Payouts       : ${monthly:>10,.2f}/month")
        p()
        if total_salary_spent > 0:
            salary_months = total_salary_spent / SALARY_CHALLENGE_FEE
            p(f"    Salary investment period  : {salary_months:.0f} months (${SALARY_CHALLENGE_FEE}/mo)")
            p(f"    Payback period           : {total_personal_investment / monthly:.1f} months")

    p(f"\n    Simulation Period         : {first_date} → {last_date} ({total_years:.1f} years)")
    p(f"    Wallet Balance           : ${remaining_wallet:,.2f}")

    # ─── Section 6: Timeline ───
    p()
    p("━" * 80)
    p("SECTION 6: KEY MILESTONES")
    p("━" * 80)
    p()

    milestones = []
    for pur in purchase_log:
        src_icon = {"initial": "🏁", "payouts": "📈", "salary": "💼"}
        icon = src_icon.get(pur["source"], "?")
        milestones.append((pur["date"], 0, f"{icon} Buy ${pur['capital']:,} challenge ({pur['source']})"))

    for acc in accounts:
        if acc.pass_date:
            milestones.append((acc.pass_date, 1, f"✅ Acct #{acc.id} ${acc.capital:,} FUNDED"))
        if acc.state == "BREACHED":
            milestones.append((acc.breach_date, 2, f"❌ Acct #{acc.id} BREACHED: {acc.breach_reason}"))

    if confidence_date:
        milestones.append((confidence_date, -1, f"🔥 CONFIDENCE! {CONFIDENCE_THRESHOLD} accounts funded → salary buying starts"))

    if total_funded_now >= TARGET_FUNDED_CAPITAL:
        milestones.append((last_date, 3, f"🎯 ${TARGET_FUNDED_CAPITAL:,} FUNDED CAPITAL REACHED!"))

    milestones.sort(key=lambda x: (x[0], x[1]))
    for date, _, detail in milestones:
        p(f"  {date}  {detail}")

    # Capital progression
    p()
    p("  FUNDED CAPITAL PROGRESSION:")
    running = 0
    for acc in sorted(accounts, key=lambda a: a.pass_date or a.buy_date):
        if acc.pass_date:
            running += acc.capital
            p(f"    {acc.pass_date}  +${acc.capital:,}  →  Total: ${running:,}  ({acc.source})")
        if acc.state == "BREACHED" and acc.breach_date and acc.funded_start_date:
            running -= acc.capital
            p(f"    {acc.breach_date}  -${acc.capital:,}  →  Total: ${running:,}  (breached)")

    p()
    p("=" * 90)
    p("  END OF SALARY-FUNDED SCALING SIMULATION")
    p("=" * 90)

    report_text = "\n".join(lines)
    print(report_text)

    report_path = Path(__file__).parent / "reports" / "histdata_backtest" / "FundedNext_Salary_Scaling_Report.txt"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"\n  Report saved: {report_path}")


if __name__ == "__main__":
    simulate()
