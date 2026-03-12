"""
Generate monthly & yearly breakdown + comprehensive metrics document
from the 0.5R trail buffer backtest (all_trades.csv).
"""
import pandas as pd
import numpy as np
import json
from pathlib import Path

REPORT_DIR = Path(__file__).parent / "reports" / "histdata_backtest"
df = pd.read_csv(REPORT_DIR / "all_trades.csv")
df['date'] = pd.to_datetime(df['date'])
df['year'] = df['date'].dt.year
df['month'] = df['date'].dt.month
df['ym'] = df['date'].dt.to_period('M').astype(str)

with open(REPORT_DIR / "metrics_histdata_16yr.json") as f:
    metrics = json.load(f)

# ── helpers ──
def calc_stats(g):
    w = len(g[g['result'] != 'loss'])
    l = len(g[g['result'] == 'loss'])
    wr = w / len(g) * 100 if len(g) > 0 else 0
    gw = g[g['pnl'] > 0]['pnl'].sum()
    gl = abs(g[g['pnl'] < 0]['pnl'].sum())
    pf = gw / gl if gl > 0 else float('inf')
    net = g['pnl'].sum()
    avg_win = g[g['pnl'] > 0]['pnl'].mean() if len(g[g['pnl'] > 0]) > 0 else 0
    avg_loss = abs(g[g['pnl'] < 0]['pnl'].mean()) if len(g[g['pnl'] < 0]) > 0 else 1
    rr = avg_win / avg_loss if avg_loss > 0 else 0
    mdd = g['trailing_dd_pct'].max()
    costs = g['costs'].sum()
    return {
        'trades': len(g), 'wins': w, 'losses': l, 'wr': wr,
        'pf': pf, 'net': net, 'rr': rr, 'mdd': mdd, 'costs': costs,
    }


# ═══════════════════════════════════════════════════
#  PART 1: YEARLY BREAKDOWN
# ═══════════════════════════════════════════════════
print("=" * 100)
print("  YEARLY BREAKDOWN — 0.5R Trail Buffer (16-Year HistData)")
print("=" * 100)
hdr = f"  {'Year':<6}{'Trades':>7}{'W':>5}{'L':>5}{'WR':>8}{'PF':>8}{'Net PnL':>14}{'Avg RR':>8}{'MaxDD':>8}{'Costs':>12}"
print(hdr)
print("  " + "-" * 94)

yearly_data = []
for yr, g in df.groupby('year'):
    s = calc_stats(g)
    yearly_data.append({'year': yr, **s})
    pf_str = f"{s['pf']:.2f}" if s['pf'] < 100 else "inf"
    print(f"  {yr:<6}{s['trades']:>7}{s['wins']:>5}{s['losses']:>5}{s['wr']:>7.1f}%{pf_str:>8}{s['net']:>13,.0f}{s['rr']:>8.2f}{s['mdd']:>7.1f}%{s['costs']:>11,.0f}")

# Totals
tw = sum(y['wins'] for y in yearly_data)
tl = sum(y['losses'] for y in yearly_data)
tn = sum(y['net'] for y in yearly_data)
tc = sum(y['costs'] for y in yearly_data)
twr = tw / (tw + tl) * 100
print("  " + "-" * 94)
print(f"  {'TOTAL':<6}{tw+tl:>7}{tw:>5}{tl:>5}{twr:>7.1f}%{'':>8}{tn:>13,.0f}{'':>8}{'':>8}{tc:>11,.0f}")

# Profitable years
prof_yrs = sum(1 for y in yearly_data if y['net'] > 0)
print(f"\n  Profitable Years: {prof_yrs}/{len(yearly_data)}")


# ═══════════════════════════════════════════════════
#  PART 2: MONTHLY BREAKDOWN
# ═══════════════════════════════════════════════════
print("\n\n" + "=" * 100)
print("  MONTHLY BREAKDOWN — 0.5R Trail Buffer (16-Year HistData)")
print("=" * 100)
hdr = f"  {'Month':<9}{'Trades':>7}{'W':>5}{'L':>5}{'WR':>8}{'PF':>8}{'Net PnL':>14}{'Avg RR':>8}{'Costs':>12}"
print(hdr)
print("  " + "-" * 80)

monthly_data = []
for ym, g in df.groupby('ym'):
    s = calc_stats(g)
    monthly_data.append({'month': ym, **s})
    pf_str = f"{s['pf']:.2f}" if s['pf'] < 100 else "inf"
    print(f"  {ym:<9}{s['trades']:>7}{s['wins']:>5}{s['losses']:>5}{s['wr']:>7.1f}%{pf_str:>8}{s['net']:>13,.0f}{s['rr']:>8.2f}{s['costs']:>11,.0f}")

prof_mos = sum(1 for m in monthly_data if m['net'] > 0)
loss_mos = sum(1 for m in monthly_data if m['net'] < 0)
flat_mos = sum(1 for m in monthly_data if m['net'] == 0)
print(f"\n  Profitable Months: {prof_mos}/{len(monthly_data)} ({prof_mos/len(monthly_data)*100:.0f}%)")
print(f"  Losing Months:     {loss_mos}/{len(monthly_data)} ({loss_mos/len(monthly_data)*100:.0f}%)")
avg_profit_mo = np.mean([m['net'] for m in monthly_data if m['net'] > 0])
avg_loss_mo = np.mean([m['net'] for m in monthly_data if m['net'] < 0]) if loss_mos > 0 else 0
print(f"  Avg Winning Month: ${avg_profit_mo:,.0f}")
print(f"  Avg Losing Month:  ${avg_loss_mo:,.0f}")
best_mo = max(monthly_data, key=lambda x: x['net'])
worst_mo = min(monthly_data, key=lambda x: x['net'])
print(f"  Best Month:  {best_mo['month']}  ${best_mo['net']:,.0f}")
print(f"  Worst Month: {worst_mo['month']}  ${worst_mo['net']:,.0f}")


# ═══════════════════════════════════════════════════
#  PART 3: MONTHLY HEATMAP (by calendar month)
# ═══════════════════════════════════════════════════
print("\n\n" + "=" * 100)
print("  CALENDAR MONTH PERFORMANCE AVERAGE")
print("=" * 100)

cal_months = df.groupby('month').apply(lambda g: pd.Series({
    'trades': len(g),
    'wr': len(g[g['result'] != 'loss']) / len(g) * 100 if len(g) > 0 else 0,
    'net': g['pnl'].sum(),
    'avg_per_yr': g['pnl'].sum() / g['year'].nunique(),
}))

month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
print(f"  {'Month':<6}{'Trades':>7}{'WR':>8}{'Total PnL':>14}{'Avg/Year':>12}")
print("  " + "-" * 50)
for i, row in cal_months.iterrows():
    print(f"  {month_names[int(i)-1]:<6}{int(row['trades']):>7}{row['wr']:>7.1f}%{row['net']:>13,.0f}{row['avg_per_yr']:>11,.0f}")

print()
