"""
Quick analysis: WHY are there so many zero-payout months?
Is it no trades? Small profits below threshold? Or losses?
"""

import pandas as pd
from pathlib import Path
from collections import defaultdict

csv_path = Path(__file__).parent / "reports" / "histdata_backtest" / "all_trades.csv"
df = pd.read_csv(csv_path)
df["date"] = pd.to_datetime(df["date"])
df["year"] = df["date"].dt.year
df["month"] = df["date"].dt.month
df["ym"] = df["date"].dt.to_period("M")

# Group by month
monthly = df.groupby("ym").agg(
    trades=("pnl", "count"),
    wins=("result", lambda x: (x == "win").sum()),
    gross_pnl=("pnl", "sum"),
    symbols=("symbol", lambda x: ", ".join(sorted(x.unique()))),
).reset_index()

print("=" * 90)
print("  MONTHLY TRADE ANALYSIS — What's behind the zero-payout months?")
print("=" * 90)
print()

# Categorise months
zero_trade_months = 0
low_trade_months = 0  # 1-2 trades
negative_months = 0
small_positive = 0    # positive but < 1% of $300K = $3000
good_months = 0

print(f"  {'Month':<10} {'Trades':>6} {'Wins':>5} {'WR':>6} {'PnL ($)':>10} {'Scaled 300K':>12} {'Indices':<30}")
print(f"  {'-'*10} {'-'*6} {'-'*5} {'-'*6} {'-'*10} {'-'*12} {'-'*30}")

all_months_list = []

for _, row in monthly.iterrows():
    ym = str(row["ym"])
    trades = row["trades"]
    win_count = row["wins"]
    wr = win_count / trades * 100 if trades > 0 else 0
    pnl = row["gross_pnl"]
    
    # Scale PnL: backtest uses ~1.5% risk on $6K ($90), funded uses 0.5% on $300K ($1500)
    # Scale factor = 1500/90 = 16.67x... but the actual PnL depends on risk_amount per trade
    # Better: the PnL in CSV is based on backtest sizing. For $300K at 0.5%, need to rescale
    # Average risk_amount in backtest is ~$90 (1.5% of $6K)
    # At $300K with 0.5% = $1500 risk → scale = 1500/90 = 16.67
    # But individual trades vary, so let this be approximate
    scaled_pnl = pnl * (1500 / 90)  # rough scale
    
    category = ""
    if trades == 0:
        zero_trade_months += 1
        category = "NO TRADES"
    elif trades <= 2:
        low_trade_months += 1
        category = "FEW TRADES"
    elif scaled_pnl < 0:
        negative_months += 1
        category = "LOSING MONTH"
    elif scaled_pnl < 3000:
        small_positive += 1
        category = "BELOW 1%"
    else:
        good_months += 1
        pct = scaled_pnl / 300000 * 100
        category = f"+{pct:.1f}%"
    
    all_months_list.append({
        "ym": ym, "trades": trades, "wins": win_count, "wr": wr,
        "pnl": pnl, "scaled": scaled_pnl, "syms": row["symbols"], "cat": category
    })
    
    print(f"  {ym:<10} {trades:>6} {win_count:>5} {wr:>5.1f}% {pnl:>+10.0f} {scaled_pnl:>+12,.0f} {row['symbols']:<30} {category}")

print()
print("=" * 90)
print("  SUMMARY")
print("=" * 90)
print()

total_months = len(monthly)
print(f"  Total months in data       : {total_months}")
print(f"  Months with 0 trades       : {zero_trade_months} ({zero_trade_months/total_months*100:.0f}%)")
print(f"  Months with 1-2 trades     : {low_trade_months} ({low_trade_months/total_months*100:.0f}%)")
print(f"  Losing months (net <0)     : {negative_months} ({negative_months/total_months*100:.0f}%)")
print(f"  Small positive (<1%)       : {small_positive} ({small_positive/total_months*100:.0f}%)")
print(f"  Payout-eligible (≥1%)      : {good_months} ({good_months/total_months*100:.0f}%)")

print()

# Check which indices contribute per period
print("  TRADES PER INDEX PER YEAR:")
yearly_by_sym = df.groupby(["year", "symbol"]).size().unstack(fill_value=0)
print(yearly_by_sym.to_string(index=True))

print()
print("  KEY INSIGHT — DATA GAPS:")
# Check for months with zero GER30
ger_months = df[df["symbol"] == "GER30"].groupby("ym").size()
spx_months = df[df["symbol"] == "SPX500"].groupby("ym").size()
ndx_months = df[df["symbol"] == "NDX100"].groupby("ym").size()

all_yms = sorted(monthly["ym"].unique())
ger_missing = [str(ym) for ym in all_yms if ym not in ger_months.index]
print(f"  GER30 missing months: {len(ger_missing)}")
if ger_missing:
    # group into ranges
    ranges = []
    start = ger_missing[0]
    prev = start
    for m in ger_missing[1:]:
        if int(m.replace("-", "")) - int(prev.replace("-", "")) > 2:
            ranges.append(f"{start} → {prev}")
            start = m
        prev = m
    ranges.append(f"{start} → {prev}")
    for r in ranges:
        print(f"    {r}")

print()
print("  WHAT WOULD US30 ADD?")
print("  ─────────────────────")
print("  US30 is NOT in the HistData backtest (no free data source)")
print("  In the 3-year MT5 backtest, US30 generated 30-40% of all trades")
print("  Adding US30 would likely fill most zero-trade months")
print()

# Monthly average PnL when we DO have trades
active_months = [m for m in all_months_list if m["trades"] > 0]
avg_trades = sum(m["trades"] for m in active_months) / len(active_months)
avg_pnl = sum(m["scaled"] for m in active_months) / len(active_months)
avg_pct = avg_pnl / 300000 * 100
print(f"  When trades exist:")
print(f"    Avg trades/month         : {avg_trades:.1f}")
print(f"    Avg scaled PnL/month     : ${avg_pnl:,.0f} ({avg_pct:.1f}%)")
print(f"    Months ≥ 2% ($6K+)       : {sum(1 for m in active_months if m['scaled'] >= 6000)} / {len(active_months)}")
print(f"    Months ≥ 4% ($12K+)      : {sum(1 for m in active_months if m['scaled'] >= 12000)} / {len(active_months)}")
