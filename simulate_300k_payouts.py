"""
FundedNext $300K Funded Account — Monthly & Yearly Payout Report
════════════════════════════════════════════════════════════════
Single $300,000 funded account starting Jan 2011.
Shows every month's payout (including $0 months) + yearly totals.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

# ─── Config ───
ACCOUNT_CAPITAL = 300_000
FUNDED_RISK_NORMAL = 0.5       # %
FUNDED_RISK_DEEP_DD = 0.25     # %
FUNDED_DD_TIER = 4.0           # % DD to switch to deep-DD risk
PAYOUT_INTERVAL_DAYS = 14
PAYOUT_MIN_PROFIT_PCT = 1.0    # % from initial to be eligible
PAYOUT_SPLIT = 0.80            # 80% to trader
DAILY_DD_LIMIT = 5.0
MAX_DD_LIMIT = 10.0
DAILY_DD_CUTOFF = 2.0
MAX_DAILY_TRADES = 2
MAX_DAILY_LOSSES = 2
START_DATE = "2011-01-01"


def load_trades():
    csv_path = Path(__file__).parent / "reports" / "histdata_backtest" / "all_trades.csv"
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # Filter from start date
    df = df[df["date"] >= START_DATE].reset_index(drop=True)
    return df


def simulate():
    trades_df = load_trades()
    
    balance = float(ACCOUNT_CAPITAL)
    initial = float(ACCOUNT_CAPITAL)
    peak = float(ACCOUNT_CAPITAL)
    
    # Daily state
    current_day = None
    day_start = balance
    day_peak = balance
    daily_losses = 0
    daily_trades = 0
    day_stopped = False
    
    # Payout tracking
    last_payout_date = trades_df["date"].iloc[0].date()
    funded_start = last_payout_date
    
    payout_log = []          # {date, profit, trader_share, balance_before, balance_after}
    monthly_payouts = defaultdict(float)   # (year, month) → total payout
    all_months = set()       # track every month we traded in
    
    # Stats
    total_trades = 0
    wins = 0
    losses = 0
    breached = False
    breach_reason = None
    breach_date = None
    
    for idx, row in trades_df.iterrows():
        if breached:
            break
            
        trade_day = row["date"].date()
        
        # ─── New day ───
        if trade_day != current_day:
            current_day = trade_day
            all_months.add((trade_day.year, trade_day.month))
            
            # Check payout
            days_since = (trade_day - last_payout_date).days
            if days_since >= PAYOUT_INTERVAL_DAYS:
                profit = balance - initial
                profit_pct = profit / initial * 100
                if profit_pct >= PAYOUT_MIN_PROFIT_PCT:
                    trader_share = profit * PAYOUT_SPLIT
                    bal_before = balance
                    balance = initial
                    peak = initial
                    payout_log.append({
                        "date": trade_day,
                        "profit": profit,
                        "trader_share": trader_share,
                        "balance_before": bal_before,
                        "profit_pct": profit_pct,
                    })
                    monthly_payouts[(trade_day.year, trade_day.month)] += trader_share
                last_payout_date = trade_day
            
            # Reset daily
            day_start = balance
            day_peak = balance
            daily_losses = 0
            daily_trades = 0
            day_stopped = False
        
        # ─── Can trade? ───
        if day_stopped or daily_losses >= MAX_DAILY_LOSSES or daily_trades >= MAX_DAILY_TRADES:
            continue
        if day_peak > 0:
            dd = (day_peak - balance) / day_peak * 100
            if dd >= DAILY_DD_CUTOFF:
                day_stopped = True
                continue
        
        # ─── Risk ───
        dd_from_initial = max(0, (initial - balance) / initial * 100)
        risk_pct = FUNDED_RISK_DEEP_DD if dd_from_initial >= FUNDED_DD_TIER else FUNDED_RISK_NORMAL
        
        # ─── Apply trade ───
        original_risk = row["risk_amount"]
        if original_risk <= 0:
            continue
        
        new_risk = balance * (risk_pct / 100.0)
        scale = new_risk / original_risk
        pnl = row["pnl"] * scale
        
        is_loss = row["result"] == "loss"
        balance += pnl
        if pnl > 0:
            day_peak = max(day_peak, balance)
        peak = max(peak, balance)
        
        if is_loss:
            daily_losses += 1
            losses += 1
        else:
            wins += 1
        daily_trades += 1
        total_trades += 1
        
        # ─── Breach check ───
        # Daily DD
        if day_start > 0:
            dd = (day_start - balance) / day_start * 100
            if dd >= DAILY_DD_LIMIT:
                breached = True
                breach_reason = f"Daily DD {dd:.1f}%"
                breach_date = trade_day
                break
        # Max DD
        dd = (initial - balance) / initial * 100
        if dd >= MAX_DD_LIMIT:
            breached = True
            breach_reason = f"Max DD {dd:.1f}%"
            breach_date = trade_day
            break
        
        # Day cutoff
        if day_peak > 0:
            dd = (day_peak - balance) / day_peak * 100
            if dd >= DAILY_DD_CUTOFF:
                day_stopped = True
    
    # ─── Final payout at end ───
    last_date = trades_df["date"].iloc[-1].date()
    all_months.add((last_date.year, last_date.month))
    profit = balance - initial
    profit_pct = profit / initial * 100
    if profit_pct >= PAYOUT_MIN_PROFIT_PCT and not breached:
        trader_share = profit * PAYOUT_SPLIT
        payout_log.append({
            "date": last_date,
            "profit": profit,
            "trader_share": trader_share,
            "balance_before": balance,
            "profit_pct": profit_pct,
        })
        monthly_payouts[(last_date.year, last_date.month)] += trader_share
        balance = initial
    
    # ═══════════════════ REPORT ═══════════════════
    
    lines = []
    def p(line=""):
        lines.append(line)
    
    p("=" * 85)
    p(f"   FUNDEDNEXT $300K FUNDED ACCOUNT — MONTHLY PAYOUT REPORT")
    p(f"   Account Capital: ${ACCOUNT_CAPITAL:,}")
    p(f"   Risk: {FUNDED_RISK_NORMAL}% normal / {FUNDED_RISK_DEEP_DD}% after {FUNDED_DD_TIER}% DD")
    p(f"   Payouts: Bi-weekly, ≥{PAYOUT_MIN_PROFIT_PCT}% profit, {int(PAYOUT_SPLIT*100)}/{int((1-PAYOUT_SPLIT)*100)} split")
    p("=" * 85)
    
    p()
    p("━" * 75)
    p("ACCOUNT SUMMARY")
    p("━" * 75)
    p()
    
    total_payout = sum(pay["trader_share"] for pay in payout_log)
    wr = wins / max(1, total_trades) * 100
    
    p(f"  Total Trades               : {total_trades}")
    p(f"  Win Rate                   : {wr:.1f}%")
    p(f"  Total Payouts              : {len(payout_log)}")
    p(f"  Total Withdrawn (80%)      : ${total_payout:,.2f}")
    p(f"  Average Payout             : ${total_payout/max(1,len(payout_log)):,.2f}")
    
    if breached:
        p(f"  ❌ BREACHED                 : {breach_date} — {breach_reason}")
    else:
        p(f"  Account Status             : ACTIVE (no breach)")
    
    first_date = trades_df["date"].iloc[0].date()
    total_years = (last_date - first_date).days / 365.25
    p(f"  Period                     : {first_date} → {last_date} ({total_years:.1f} years)")
    if total_years > 0:
        p(f"  Avg Annual Payout          : ${total_payout/total_years:,.2f}/year")
        p(f"  Avg Monthly Payout         : ${total_payout/total_years/12:,.2f}/month")
    
    # ─── Monthly Breakdown ───
    p()
    p("━" * 75)
    p("MONTHLY PAYOUT BREAKDOWN")
    p("━" * 75)
    p()
    
    # Build complete month range
    min_ym = min(all_months)
    max_ym = max(all_months)
    
    month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    
    # Group by year
    years = sorted(set(ym[0] for ym in all_months))
    
    yearly_totals = defaultdict(float)
    yearly_payout_count = defaultdict(int)
    zero_months = []
    
    for year in years:
        p(f"  ┌─────────────────────────────────────────────────┐")
        p(f"  │  {year}                                            │")
        p(f"  ├───────┬──────────────┬──────────────────────────┤")
        p(f"  │ Month │    Payout    │                          │")
        p(f"  ├───────┼──────────────┼──────────────────────────┤")
        
        year_total = 0
        year_months_with_payout = 0
        
        for m in range(1, 13):
            ym = (year, m)
            if ym < min_ym or ym > max_ym:
                continue
            
            payout = monthly_payouts.get(ym, 0.0)
            year_total += payout
            
            if payout > 0:
                bar_len = min(25, int(payout / (ACCOUNT_CAPITAL * 0.002)))  # scale bar
                bar = "█" * bar_len
                p(f"  │  {month_names[m]}  │ ${payout:>10,.2f} │ {bar:<25s}│")
                year_months_with_payout += 1
            else:
                p(f"  │  {month_names[m]}  │     $0.00    │ ░░░ no payout             │")
                zero_months.append(f"{month_names[m]} {year}")
        
        p(f"  ├───────┼──────────────┼──────────────────────────┤")
        p(f"  │ TOTAL │ ${year_total:>10,.2f} │ {year_months_with_payout} months with payouts     │")
        p(f"  └───────┴──────────────┴──────────────────────────┘")
        p()
        
        yearly_totals[year] = year_total
        yearly_payout_count[year] = year_months_with_payout
    
    # ─── Yearly Summary ───
    p("━" * 75)
    p("YEARLY PAYOUT SUMMARY")
    p("━" * 75)
    p()
    p(f"  ┌──────┬───────────────┬────────┬──────────────┬──────────────┐")
    p(f"  │ Year │  Total Payout │ Months │   Per Month  │   Per Year   │")
    p(f"  │      │   (80% share) │ w/pay  │   (average)  │    (gross)   │")
    p(f"  ├──────┼───────────────┼────────┼──────────────┼──────────────┤")
    
    for year in years:
        tot = yearly_totals[year]
        cnt = yearly_payout_count[year]
        # Count total months in this year within our data range
        months_in_year = sum(1 for m in range(1, 13) if min_ym <= (year, m) <= max_ym)
        avg_per_month = tot / max(1, months_in_year)
        p(f"  │ {year} │ ${tot:>11,.2f} │  {cnt:2d}/{months_in_year:2d}  │ ${avg_per_month:>10,.2f} │ ${tot:>10,.2f}  │")
    
    p(f"  ├──────┼───────────────┼────────┼──────────────┼──────────────┤")
    grand = sum(yearly_totals.values())
    total_months_all = sum(sum(1 for m in range(1, 13) if min_ym <= (year, m) <= max_ym) for year in years)
    total_pay_months = sum(yearly_payout_count.values())
    p(f"  │ ALL  │ ${grand:>11,.2f} │ {total_pay_months:3d}/{total_months_all:<3d} │ ${grand/max(1,total_months_all):>10,.2f} │ ${grand/max(1,len(years)):>10,.2f}  │")
    p(f"  └──────┴───────────────┴────────┴──────────────┴──────────────┘")
    
    # ─── Zero-payout months ───
    p()
    p("━" * 75)
    p("MONTHS WITH NO PAYOUT")
    p("━" * 75)
    p()
    
    if zero_months:
        p(f"  {len(zero_months)} months with $0 payout:")
        # Print in rows of 6
        for i in range(0, len(zero_months), 6):
            chunk = zero_months[i:i+6]
            p(f"    {', '.join(chunk)}")
    else:
        p("  Every month had a payout! ✅")
    
    # ─── Every individual payout ───
    p()
    p("━" * 75)
    p("ALL INDIVIDUAL PAYOUTS")
    p("━" * 75)
    p()
    p(f"  ┌──────┬────────────┬──────────────┬──────────┬──────────────┐")
    p(f"  │  #   │    Date    │    Payout    │ Profit % │  Bal Before  │")
    p(f"  ├──────┼────────────┼──────────────┼──────────┼──────────────┤")
    for i, pay in enumerate(payout_log):
        p(f"  │ {i+1:4d} │ {pay['date']} │ ${pay['trader_share']:>10,.2f} │  {pay['profit_pct']:5.2f}%  │ ${pay['balance_before']:>10,.0f} │")
    p(f"  ├──────┼────────────┼──────────────┼──────────┼──────────────┤")
    p(f"  │      │            │ ${total_payout:>10,.2f} │          │              │")
    p(f"  └──────┴────────────┴──────────────┴──────────┴──────────────┘")
    
    p()
    p("=" * 85)
    p("  END OF REPORT")
    p("=" * 85)
    
    report_text = "\n".join(lines)
    print(report_text)
    
    report_path = Path(__file__).parent / "reports" / "histdata_backtest" / "FundedNext_300K_Monthly_Report.txt"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"\n  Report saved: {report_path}")


if __name__ == "__main__":
    simulate()
