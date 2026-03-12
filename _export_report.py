"""Export the 5-index combined report to a clean text file using saved data."""
import json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict
from io import StringIO
import sys

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import ACCOUNT_RULES

report_dir = Path(__file__).parent / "reports" / "histdata_backtest"
trades_df = pd.read_csv(report_dir / "combined_5index_trades.csv")
trades_df["date"] = pd.to_datetime(trades_df["date"])
with open(report_dir / "metrics_combined_5index.json") as f:
    results = json.load(f)

o = results["overall"]
buf = StringIO()
p = lambda *a, **kw: print(*a, **kw, file=buf)

p("=" * 85)
p("  5-INDEX COMBINED PORTFOLIO BACKTEST — FINAL REPORT")
p("  ORB Clean Breakout | 16-Year HistData | Generated: March 12, 2026")
p("=" * 85)
p()
p("  Indices  : SPX500, NDX100, GER30 (split), FRA40, US30 (Dukascopy)")
p("  Strategy : Each index uses its own default LIVE_INDEX_CONFIG rules")
p("  Data     : HistData 16yr M1 bars + US30 Dukascopy (2016-2019)")
p("  Capital  : $6,000 backtest | $100,000 FundedNext simulation")
p()

# SECTION 1
p("━" * 85)
p("  SECTION 1: PORTFOLIO SUMMARY")
p("━" * 85)
p()
p(f'  Total Trades       : {o["total_trades"]:>8}  ({o["trades_per_week"]}/wk)')
p(f'  Win Rate           : {o["win_rate"]:>7}%')
p(f'  Profit Factor      : {o["profit_factor"]:>8}')
p(f'  Avg RR             : 1:{o["actual_rr"]}')
p(f'  Sharpe Ratio       : {o["sharpe"]:>8}')
p(f'  Sortino Ratio      : {o["sortino"]:>8}')
p(f'  Calmar Ratio       : {o["calmar"]:>8}')
p(f'  Recovery Factor    : {o["recovery_factor"]:>8}')
p()
p(f'  Starting Capital   : ${o["starting_capital"]:>12,.2f}')
p(f'  Final Balance      : ${o["final_balance"]:>12,.2f}')
p(f'  Net P&L            : ${o["net_pnl"]:>12,.2f}')
p(f'  Total Return       : {o["total_return_pct"]:>8}%')
p(f'  Total Costs        : ${o["total_costs"]:>12,.2f}')
p(f'  Expectancy/Trade   : ${o["expectancy"]:>12,.2f}')
p()
p(f'  Max Drawdown       : {o["max_dd_pct"]}% (${o["max_dd_dollar"]:,.2f})')
p(f'  Peak Daily DD      : {o["peak_daily_dd"]}%')
p(f'  Peak Trailing DD   : {o["peak_trailing_dd"]}%')
p(f'  Max Consec Wins    : {o["max_consec_wins"]}')
p(f'  Max Consec Losses  : {o["max_consec_losses"]}')
p(f'  Account Resets     : 0 (10% DD terminations)')
p()

# SECTION 2
p("━" * 85)
p("  SECTION 2: PER-INDEX BREAKDOWN")
p("━" * 85)
p()
p(f'  {"Index":<10} {"Trades":>7} {"Tr/Wk":>6} {"WR":>7} {"PF":>6} {"Net PnL":>13} {"MaxDD%":>7} {"Sharpe":>7}')
p(f'  {"-"*64}')
for sym in sorted(results["per_index"].keys()):
    m = results["per_index"][sym]
    p(f'  {sym:<10} {m["total_trades"]:>7} {m["trades_per_week"]:>5.1f} '
      f'{m["win_rate"]:>6.1f}% {m["profit_factor"]:>5.2f} '
      f'${m["net_pnl"]:>12,.2f} {m["max_dd_pct"]:>6.1f}% {m["sharpe"]:>6.2f}')
p()

# SECTION 3
p("━" * 85)
p("  SECTION 3: YEARLY BREAKDOWN")
p("━" * 85)
p()
header = (f'  {"Year":<6} {"Trades":>7} {"Tr/Wk":>6} {"W":>4} {"L":>4} '
          f'{"WR":>7} {"PF":>6} {"Net PnL":>13} {"MaxDD%":>7} {"Sharpe":>7} {"MCL":>4}')
p(header)
p(f'  {"-"*82}')
for yr in sorted(results["yearly"].keys()):
    m = results["yearly"][yr]
    w = m["wins"] + m["partial_wins"]
    p(f'  {yr:<6} {m["total_trades"]:>7} {m["trades_per_week"]:>5.1f} {w:>4} '
      f'{m["losses"]:>4} {m["win_rate"]:>6.1f}% {m["profit_factor"]:>5.2f} '
      f'${m["net_pnl"]:>12,.2f} {m["max_dd_pct"]:>6.1f}% {m["sharpe"]:>6.2f} '
      f'{m["max_consec_losses"]:>4}')
p()

# Per-index yearly
for sym in sorted(results.get("per_index_yearly", {}).keys()):
    yearly = results["per_index_yearly"][sym]
    if not yearly:
        continue
    p(f"  -- {sym} YEARLY --")
    p(f'  {"Year":<6} {"Trades":>7} {"W":>4} {"L":>4} {"WR":>7} {"PF":>6} {"Net PnL":>13}')
    p(f'  {"-"*54}')
    for yr in sorted(yearly.keys()):
        m = yearly[yr]
        w = m["wins"] + m["partial_wins"]
        p(f'  {yr:<6} {m["total_trades"]:>7} {w:>4} {m["losses"]:>4} '
          f'{m["win_rate"]:>6.1f}% {m["profit_factor"]:>5.2f} ${m["net_pnl"]:>12,.2f}')
    p()

# SECTION 4
p("━" * 85)
p("  SECTION 4: EXIT REASONS")
p("━" * 85)
p()
for reason in sorted(results.get("exit_reasons", {}).keys(),
                     key=lambda x: -results["exit_reasons"][x]["count"]):
    info = results["exit_reasons"][reason]
    p(f'  {reason:<15} : {info["count"]:>5} ({info["pct"]:>5.1f}%)  '
      f'avg PnL: ${info["avg_pnl"]:>10,.2f}')
p()

# SECTION 5: Monthly PnL
p("━" * 85)
p("  SECTION 5: MONTHLY P&L BREAKDOWN (Backtest $6K account)")
p("━" * 85)
p()
trades_df["year"] = trades_df["date"].dt.year
trades_df["month"] = trades_df["date"].dt.month
month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

p(f'  {"Year":>6}', end="")
for mn in month_names:
    p(f" {mn:>8}", end="")
p(f' {"TOTAL":>11} {"Months+":>8}')
p(f'  {"-"*6}', end="")
for _ in month_names:
    p(f" {'-'*8}", end="")
p(f" {'-'*11} {'-'*8}")

years = sorted(trades_df["year"].unique())
for yr in years:
    p(f"  {yr:>6}", end="")
    yr_total = 0
    pos_m = 0
    for m in range(1, 13):
        mdf = trades_df[(trades_df["year"] == yr) & (trades_df["month"] == m)]
        if len(mdf) == 0:
            p(f' {"--":>8}', end="")
        else:
            pnl = mdf["pnl"].sum()
            yr_total += pnl
            if pnl > 0:
                pos_m += 1
            if abs(pnl) >= 1000:
                p(f" {pnl/1000:>7.1f}k", end="")
            else:
                p(f" {pnl:>8.0f}", end="")
    p(f" ${yr_total:>9,.0f} {pos_m:>5}/12")
p()

monthly_grp = trades_df.groupby(["year", "month"]).agg(
    trades=("pnl", "count"), pnl=("pnl", "sum"))
pay_mo = len(monthly_grp[monthly_grp["pnl"] > 0])
tot_mo = len(monthly_grp)
avg_mo = monthly_grp["pnl"].mean()
avg_tr = monthly_grp["trades"].mean()
p(f"  Payout months: {pay_mo}/{tot_mo} ({pay_mo/tot_mo*100:.0f}%) | "
  f"Avg trades/month: {avg_tr:.1f} | Avg month PnL: ${avg_mo:,.0f}")
p()

# SECTION 6: Data Gaps
p("━" * 85)
p("  SECTION 6: DATA GAPS & COVERAGE")
p("━" * 85)
p()
gaps = {
    "SPX500": "Full coverage: Nov 2010 - Feb 2026 (no gaps).",
    "NDX100": "Full coverage: Nov 2010 - Feb 2026 (no gaps).",
    "GER30": ("2020-06-15 to 2023-12-03: Price scaling break (~3.7x too low). SKIPPED.\n"
              "             Two clean segments: Period A (2010-2020) + Period B (2024-2026)."),
    "FRA40": "Full coverage: Nov 2010 - Feb 2026 (no gaps).",
    "US30": "Only 2016-01 to 2019-06 (Dukascopy gap-filled). Missing: 2010-2015 and 2019-07+.",
}
for sym, gap in gaps.items():
    p(f"  {sym:<8}: {gap}")
p()

# SECTION 7: FundedNext Compounding
p("━" * 85)
p("  SECTION 7: FUNDEDNEXT $100K COMPOUNDING SIMULATION")
p("  50% of monthly payout reinvested to new accounts")
p("━" * 85)
p()

BACKTEST_CAPITAL = ACCOUNT_RULES["starting_balance"]
funded_balance = 100_000
payout_split = 0.90
reinvest_pct = 0.50

monthly_groups2 = trades_df.groupby(["year", "month"])
monthly_rets = []
for (yr, mo), grp in monthly_groups2:
    mpnl = grp["pnl"].sum()
    start_bal = grp["balance"].iloc[0] - grp["pnl"].iloc[0]
    ret = mpnl / start_bal if start_bal > 0 else 0
    monthly_rets.append((yr, mo, ret))

sim_results = []
total_payouts = 0
total_reinvested = 0
total_kept = 0
reinvest_pool = 0
num_accounts = 1
accounts_list = [{"size": funded_balance, "opened": "Initial"}]

for yr, mo, ret_pct in monthly_rets:
    gross = funded_balance * ret_pct * num_accounts
    trader_pnl = gross * payout_split if gross > 0 else gross
    payout = max(0, trader_pnl)
    reinvest = payout * reinvest_pct
    keep = payout - reinvest
    total_payouts += payout
    total_reinvested += reinvest
    total_kept += keep
    reinvest_pool += reinvest
    new_acct = False
    while reinvest_pool >= funded_balance:
        reinvest_pool -= funded_balance
        num_accounts += 1
        accounts_list.append({"size": funded_balance, "opened": f"{yr}-{mo:02d}"})
        new_acct = True
    sim_results.append({
        "year": yr, "month": mo, "ret": ret_pct * 100, "gross": gross,
        "payout": payout, "reinvest": reinvest, "keep": keep, "pool": reinvest_pool,
        "cumul_kept": total_kept, "accts": num_accounts, "new": new_acct,
    })

p(f"  Starting: 1x ${funded_balance:,.0f} FundedNext Stellar-2 account")
p(f"  Profit split: 90/10 | Reinvest: 50% of payout")
p(f"  New account opened when reinvestment pool reaches ${funded_balance:,.0f}")
p(f"  Monthly return % derived from backtest (not raw PnL scaling)")
p()

# Annual summary
by_year = defaultdict(lambda: {"gross": 0, "payout": 0, "keep": 0, "reinvest": 0,
                               "start": 0, "end": 0})
for r in sim_results:
    yr = r["year"]
    by_year[yr]["gross"] += r["gross"]
    by_year[yr]["payout"] += r["payout"]
    by_year[yr]["keep"] += r["keep"]
    by_year[yr]["reinvest"] += r["reinvest"]
    by_year[yr]["end"] = r["accts"]
    if by_year[yr]["start"] == 0:
        by_year[yr]["start"] = r["accts"] - (1 if r["new"] else 0)

p(f'  {"Year":>6} {"Accts":>6} {"Gross PnL":>12} {"Payouts":>12} '
  f'{"Kept":>12} {"Reinvested":>12} {"End Accts":>10}')
p(f"  {'-'*72}")
for yr in sorted(by_year.keys()):
    d = by_year[yr]
    p(f'  {yr:>6} {d["start"]:>5}x ${d["gross"]:>11,.0f} ${d["payout"]:>11,.0f} '
      f'${d["keep"]:>11,.0f} ${d["reinvest"]:>11,.0f} {d["end"]:>9}x')

tot_g = sum(r["gross"] for r in sim_results)
tot_p = sum(r["payout"] for r in sim_results)
tot_k = sum(r["keep"] for r in sim_results)
tot_r = sum(r["reinvest"] for r in sim_results)
fin_a = sim_results[-1]["accts"]
p(f"  {'-'*72}")
p(f'  {"TOTAL":>6} {"":>6} ${tot_g:>11,.0f} ${tot_p:>11,.0f} '
  f'${tot_k:>11,.0f} ${tot_r:>11,.0f} {fin_a:>9}x')
p()

# Monthly detail
p("  -- MONTHLY PAYOUT DETAIL --")
p(f'  {"Month":>8} {"Ret%":>7} {"Gross":>12} {"Payout":>12} '
  f'{"Keep":>12} {"Reinvest":>12} {"Pool":>10} {"Accts":>6}')
p(f"  {'-'*82}")
for r in sim_results:
    marker = " <NEW" if r["new"] else ""
    p(f'  {r["year"]}-{r["month"]:02d}  {r["ret"]:>6.1f}% ${r["gross"]:>11,.0f} '
      f'${r["payout"]:>11,.0f} ${r["keep"]:>11,.0f} ${r["reinvest"]:>11,.0f} '
      f'${r["pool"]:>9,.0f} {r["accts"]:>5}{marker}')
p()

# Accounts list
p("  -- ACCOUNTS OPENED --")
for i, a in enumerate(accounts_list):
    p(f'    Account #{i+1}: ${a["size"]:,.0f} -- opened {a["opened"]}')
p()

# Projections
recent = [r for r in sim_results if r["year"] >= sim_results[-1]["year"] - 2]
if recent:
    avg_k = sum(r["keep"] for r in recent) / len(recent)
    avg_p = sum(r["payout"] for r in recent) / len(recent)
    p(f"  Avg monthly payout (last 3yr)       : ${avg_p:,.0f}")
    p(f"  Avg monthly income kept (last 3yr)   : ${avg_k:,.0f}")
    p(f"  Projected annual income kept         : ${avg_k*12:,.0f}")
    p()

pos_m = sum(1 for r in sim_results if r["ret"] > 0)
neg_m = sum(1 for r in sim_results if r["ret"] <= 0)
avg_pos = np.mean([r["ret"] for r in sim_results if r["ret"] > 0]) if pos_m else 0
avg_neg = np.mean([r["ret"] for r in sim_results if r["ret"] <= 0]) if neg_m else 0
p(f"  Monthly stats: {pos_m} positive ({avg_pos:.1f}% avg) | "
  f"{neg_m} negative ({avg_neg:.1f}% avg)")
p()
p("=" * 85)
p("  END OF REPORT")
p("=" * 85)

# Write
out_path = report_dir / "FundedNext_5Index_Final_Report.txt"
with open(out_path, "w", encoding="utf-8", encoding="utf-8") as f:
    f.write(buf.getvalue())
print(f"Report saved to: {out_path}")
print(f"Lines: {len(buf.getvalue().splitlines())}")
