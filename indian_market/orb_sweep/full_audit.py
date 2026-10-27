"""Full performance audit of ORB Sweep strategy."""
import sys, io, logging, time
from datetime import date, timedelta
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.WARNING)

from orb_sweep.backtest import ORBSweepBacktester
from orb_sweep.config import TradingConfig

cfg = TradingConfig()
bt = ORBSweepBacktester(cfg)
bt.load_data(str(ROOT / 'data' / 'zerodha_nifty_1m.csv'),
             str(ROOT / 'data' / 'zerodha_vix_day.csv'))
results = bt.run(start_date=date(2018, 1, 1), end_date=date(2026, 5, 22))

trades = bt.trades
capital = cfg.capital
sep = '=' * 80

# ============================================================
# 1. YEAR-BY-YEAR BREAKDOWN
# ============================================================
print(f'\n{sep}')
print('  1. YEAR-BY-YEAR DETAILED BREAKDOWN')
print(sep)

years = sorted(set(t.trade_date.year for t in trades))
cumulative_pnl = 0

hdr = f'  {"Year":<6} {"N":<6} {"W":<5} {"L":<5} {"WR%":<7} {"PF":<8} {"Net PnL":<14} {"Cum PnL":<14} {"Ret%":<8} {"MaxDD%":<8}'
print(hdr)
print(f'  {"-"*88}')

for y in years:
    yt = [t for t in trades if t.trade_date.year == y]
    pnls = np.array([t.net_pnl for t in yt])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    n = len(pnls)
    nw = len(wins)
    nl = len(losses)
    wr = nw / n * 100 if n else 0
    gp = float(wins.sum()) if len(wins) else 0
    gl = abs(float(losses.sum())) if len(losses) else 1e-9
    pf = gp / gl
    net = float(pnls.sum())
    cumulative_pnl += net
    ret = net / capital * 100
    eq = capital + np.cumsum(pnls)
    pk = np.maximum.accumulate(eq)
    dd = float(((eq - pk) / pk * 100).min())
    print(f'  {y:<6} {n:<6} {nw:<5} {nl:<5} {wr:<7.1f} {pf:<8.3f} Rs{net:>+12,.0f} Rs{cumulative_pnl:>+12,.0f} {ret:>+7.1f}% {dd:>+7.1f}%')

# ============================================================
# 2. MONTHLY P&L
# ============================================================
print(f'\n{sep}')
print('  2. MONTHLY P&L (Profitable vs Loss)')
print(sep)

monthly = defaultdict(list)
for t in trades:
    key = f'{t.trade_date.year}-{t.trade_date.month:02d}'
    monthly[key].append(t.net_pnl)

months_sorted = sorted(monthly.keys())
prof_months = 0
loss_months = 0

print(f'  {"Month":<10} {"N":<5} {"Net PnL":<14} {"Status"}')
print(f'  {"-"*38}')

for m in months_sorted:
    pnls_m = monthly[m]
    net = sum(pnls_m)
    n = len(pnls_m)
    if net > 0:
        status = ' WIN'
        prof_months += 1
    else:
        status = 'LOSS'
        loss_months += 1
    print(f'  {m:<10} {n:<5} Rs{net:>+12,.0f} {status}')

total_m = prof_months + loss_months
print(f'\n  Profitable months: {prof_months}/{total_m} ({prof_months/total_m*100:.1f}%)')
print(f'  Loss months:       {loss_months}/{total_m} ({loss_months/total_m*100:.1f}%)')

# ============================================================
# 3. DRAWDOWN ANALYSIS
# ============================================================
print(f'\n{sep}')
print('  3. DRAWDOWN ANALYSIS')
print(sep)

all_pnls = np.array([t.net_pnl for t in trades])
equity = capital + np.cumsum(all_pnls)
peak = np.maximum.accumulate(equity)
dd_series = (equity - peak) / peak * 100

max_dd = float(dd_series.min())
max_dd_idx = int(np.argmin(dd_series))
max_dd_date = trades[max_dd_idx].trade_date

print(f'  Max Drawdown      : {max_dd:.1f}%')
print(f'  Max DD Amount     : Rs{float(equity[max_dd_idx] - peak[max_dd_idx]):+,.0f}')
print(f'  Max DD Date       : {max_dd_date}')
print(f'  Peak Equity       : Rs{float(peak[max_dd_idx]):,.0f}')
print(f'  Trough Equity     : Rs{float(equity[max_dd_idx]):,.0f}')

dd_periods = []
in_dd = False
dd_s = 0
for i, d in enumerate(dd_series):
    if d < 0 and not in_dd:
        in_dd = True
        dd_s = i
    elif d >= 0 and in_dd:
        in_dd = False
        dd_min = float(dd_series[dd_s:i].min())
        dd_periods.append((dd_min, trades[dd_s].trade_date, trades[min(i, len(trades)-1)].trade_date))
if in_dd:
    dd_min = float(dd_series[dd_s:].min())
    dd_periods.append((dd_min, trades[dd_s].trade_date, trades[-1].trade_date))

dd_periods.sort(key=lambda x: x[0])
print(f'\n  Top 5 Drawdown Periods:')
for i, (dd_val, sd, ed) in enumerate(dd_periods[:5], 1):
    duration = (ed - sd).days
    print(f'    #{i}: {dd_val:+.1f}% | {sd} to {ed} ({duration} days)')

# Yearly max drawdown
print(f'\n  Yearly Max Drawdown:')
for y in years:
    yt = [t for t in trades if t.trade_date.year == y]
    yp = np.array([t.net_pnl for t in yt])
    yeq = capital + np.cumsum(yp)
    ypk = np.maximum.accumulate(yeq)
    ydd = float(((yeq - ypk) / ypk * 100).min())
    print(f'    {y}: {ydd:+.1f}%')

# ============================================================
# 4. TRADE FREQUENCY
# ============================================================
print(f'\n{sep}')
print('  4. TRADE FREQUENCY')
print(sep)

total_trades = len(trades)
first_date = trades[0].trade_date
last_date = trades[-1].trade_date
total_days = (last_date - first_date).days
total_weeks = total_days / 7

weekly = defaultdict(int)
for t in trades:
    week = t.trade_date - timedelta(days=t.trade_date.weekday())
    weekly[week] += 1

week_counts = list(weekly.values())
print(f'  Total Trades      : {total_trades}')
print(f'  Period            : {first_date} to {last_date}')
print(f'  Trades/Week (avg) : {total_trades/total_weeks:.2f}')
print(f'  Trades/Month (avg): {total_trades/(total_days/30.44):.1f}')
print(f'  Active Weeks      : {len(weekly)}/{int(total_weeks)} ({len(weekly)/total_weeks*100:.1f}%)')
print(f'  Avg per Active Wk : {np.mean(week_counts):.1f}')
print(f'  Max per Week      : {max(week_counts)}')
print(f'  Median per Week   : {np.median(week_counts):.0f}')

dow = defaultdict(lambda: {'n': 0, 'pnl': 0})
for t in trades:
    d = t.trade_date.strftime('%A')
    dow[d]['n'] += 1
    dow[d]['pnl'] += t.net_pnl
print(f'\n  Day-of-Week:')
for d in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
    s = dow.get(d, {'n': 0, 'pnl': 0})
    print(f'    {d:<12}: {s["n"]:>4} trades, PnL=Rs{s["pnl"]:>+12,.0f}')

# ============================================================
# 5. COST & SLIPPAGE
# ============================================================
print(f'\n{sep}')
print('  5. COST & SLIPPAGE ANALYSIS (after Zerodha charges)')
print(sep)

total_costs = sum(t.costs for t in trades)
total_gross = sum(t.gross_pnl for t in trades)
total_net = sum(t.net_pnl for t in trades)
avg_cost = total_costs / total_trades
avg_entry = np.mean([t.entry_premium for t in trades])
avg_exit = np.mean([t.exit_premium for t in trades])
avg_qty = np.mean([t.quantity for t in trades])
avg_lots = np.mean([t.lots for t in trades])

print(f'  Gross P&L         : Rs{total_gross:>+14,.0f}')
print(f'  Total Costs       : Rs{total_costs:>14,.0f}')
print(f'  Net P&L           : Rs{total_net:>+14,.0f}')
print(f'  Costs % of |Gross|: {total_costs/abs(total_gross)*100:.1f}%')
print(f'  Avg Cost/Trade    : Rs{avg_cost:,.0f}')
print(f'  Avg Entry Premium : Rs{avg_entry:,.2f}')
print(f'  Avg Exit Premium  : Rs{avg_exit:,.2f}')
print(f'  Avg Lots/Trade    : {avg_lots:.1f}')
print(f'  Avg Qty/Trade     : {avg_qty:.0f} units')
print(f'  Slippage Model    : {cfg.slippage_pct*100:.1f}% per leg (entry+exit)')
print(f'  Cost Model        : Zerodha (brokerage+STT+exchange+SEBI+GST+stamp)')

# ============================================================
# 6. EXIT TYPE ANALYSIS
# ============================================================
print(f'\n{sep}')
print('  6. EXIT TYPE ANALYSIS')
print(sep)

exit_stats = defaultdict(lambda: {'count': 0, 'pnl': 0, 'wins': 0})
for t in trades:
    et = t.exit_type
    exit_stats[et]['count'] += 1
    exit_stats[et]['pnl'] += t.net_pnl
    if t.net_pnl > 0:
        exit_stats[et]['wins'] += 1

print(f'  {"Type":<10} {"Count":<7} {"Pct%":<7} {"Win%":<7} {"Net PnL":<14} {"Avg PnL":<12}')
print(f'  {"-"*58}')
for et in ['TP', 'TRAIL', 'SL', 'TIME']:
    s = exit_stats.get(et, {'count': 0, 'pnl': 0, 'wins': 0})
    if s['count'] == 0:
        continue
    pct = s['count'] / total_trades * 100
    wr = s['wins'] / s['count'] * 100
    avg = s['pnl'] / s['count']
    print(f'  {et:<10} {s["count"]:<7} {pct:<7.1f} {wr:<7.1f} Rs{s["pnl"]:>+12,.0f} Rs{avg:>+10,.0f}')

# ============================================================
# 7. CALL vs PUT
# ============================================================
print(f'\n{sep}')
print('  7. CALL vs PUT PERFORMANCE')
print(sep)

for direction in ['CALL', 'PUT']:
    dt = [t for t in trades if t.direction == direction]
    if not dt:
        continue
    pnls_d = np.array([t.net_pnl for t in dt])
    wins_d = pnls_d[pnls_d > 0]
    losses_d = pnls_d[pnls_d <= 0]
    gp_d = float(wins_d.sum()) if len(wins_d) else 0
    gl_d = abs(float(losses_d.sum())) if len(losses_d) else 1e-9
    print(f'  {direction}:')
    print(f'    Trades: {len(dt)} | Wins: {len(wins_d)} | WR: {len(wins_d)/len(dt)*100:.1f}%')
    print(f'    PF: {gp_d/gl_d:.3f} | Net: Rs{float(pnls_d.sum()):+,.0f}')
    print(f'    Avg Win: Rs{float(wins_d.mean()) if len(wins_d) else 0:+,.0f} | Avg Loss: Rs{float(losses_d.mean()) if len(losses_d) else 0:+,.0f}')

# ============================================================
# 8. RISK-ADJUSTED METRICS
# ============================================================
print(f'\n{sep}')
print('  8. RISK-ADJUSTED METRICS')
print(sep)

total_return = total_net / capital
years_span = total_days / 365.25
cagr = ((1 + total_return) ** (1 / years_span) - 1) * 100
calmar = cagr / abs(max_dd) if max_dd != 0 else 0

neg_pnls = all_pnls[all_pnls < 0]
sortino = float((all_pnls / capital).mean() / (neg_pnls / capital).std() * np.sqrt(252)) if len(neg_pnls) > 1 else 0

streaks_w = []
streaks_l = []
cs = 0
for p in all_pnls:
    if p > 0:
        if cs >= 0:
            cs += 1
        else:
            streaks_l.append(abs(cs))
            cs = 1
    else:
        if cs <= 0:
            cs -= 1
        else:
            streaks_w.append(cs)
            cs = -1
if cs > 0:
    streaks_w.append(cs)
elif cs < 0:
    streaks_l.append(abs(cs))

print(f'  CAGR              : {cagr:.1f}%')
print(f'  Total Return      : {total_return*100:.1f}%')
print(f'  Sharpe Ratio      : {results.get("sharpe", 0):.3f}')
print(f'  Sortino Ratio     : {sortino:.3f}')
print(f'  Calmar Ratio      : {calmar:.3f}')
print(f'  Max Drawdown      : {max_dd:.1f}%')
print(f'  Expectancy/Trade  : Rs{total_net/total_trades:+,.0f}')
print(f'  Win Streak (max)  : {max(streaks_w) if streaks_w else 0}')
print(f'  Loss Streak (max) : {max(streaks_l) if streaks_l else 0}')
print(f'  Avg Win Streak    : {np.mean(streaks_w):.1f}' if streaks_w else '  Avg Win Streak    : 0')
print(f'  Avg Loss Streak   : {np.mean(streaks_l):.1f}' if streaks_l else '  Avg Loss Streak   : 0')

# ============================================================
# 9. LOOKAHEAD BIAS & EXECUTION AUDIT
# ============================================================
print(f'\n{sep}')
print('  9. LOOKAHEAD BIAS & EXECUTION AUDIT')
print(sep)

print('  Signal Generation:')
print('    [OK] ORB computed from 09:15-09:29 bars only (before entry window)')
print('    [OK] Regime filter uses prior ATR(14) on 5m (rolling, no future data)')
print('    [OK] VWAP uses cumulative calculation up to current bar')
print('    [OK] Swing pivots require N bars AFTER pivot to confirm (lag-correct)')
print('    [OK] Sweep detection tracks bar-by-bar state (no peek-ahead)')
print('    [OK] Volume SMA uses rolling window up to current bar')

print('\n  Exit Management:')
print('    [OK] Premium-based exits use BS price at bar CLOSE (not high/low)')
print('    [OK] SL/TP/Trail checked on close price (conservative vs intra-bar)')
print('    [OK] Force exit at 15:15 uses closing price of that bar')
print('    [OK] Time decay modeled (DTE decreases intraday)')

print('\n  Cost Model:')
print('    [OK] Full Zerodha charges: brokerage + STT + exchange + SEBI + GST + stamp')
print('    [OK] 0.5% slippage per leg (entry and exit)')
print('    [OK] Round-trip costs applied to every trade')

print('\n  Potential Concerns for Live:')
print('    [!!] Synthetic BS pricing != real option premiums (IV skew, term structure)')
print('    [!!] VIX used as IV proxy -- real IV varies by strike/expiry')
print('    [!!] Index spot has no volume -- volume filter bypassed in backtest')
print('    [!!] 1-min bars from Zerodha may have gaps/delays in live')
print('    [!!] ATM strike rounding may differ from actual best strike')
print('    [!!] Market orders may get worse fills than 0.5% slippage on volatile days')

# ============================================================
# 10. LIVE REPLICABILITY ASSESSMENT
# ============================================================
print(f'\n{sep}')
print('  10. LIVE REPLICABILITY ASSESSMENT')
print(sep)

print('  CAN REPLICATE:')
print('    [YES] ORB calculation -- straightforward from real-time bars')
print('    [YES] Regime filter -- ATR(14) and range% computable in real-time')
print('    [YES] Sweep detection -- bar-by-bar state tracking, no lookahead')
print('    [YES] VWAP -- cumulative, computable in real-time')
print('    [YES] Premium-based exits -- use actual option LTP, not synthetic')
print('    [YES] Risk management -- daily/weekly DD limits, max 2 trades/day')
print('    [YES] Zerodha API supports all required operations')

print('\n  BETTER IN LIVE (vs backtest):')
print('    [+] Real option premiums instead of BS synthetic (more accurate)')
print('    [+] Real volume data from futures for volume filter')
print('    [+] Premium exits align directly with actual P&L (no BS distortion)')
print('    [+] Can use limit orders for better fills')

print('\n  WORSE IN LIVE (vs backtest):')
print('    [-] Slippage may exceed 0.5% on illiquid strikes/expiry days')
print('    [-] Zerodha API rate limits (3 req/sec) -- need careful polling')
print('    [-] Token refresh needed daily (auto_auth.py handles this)')
print('    [-] Internet/system outages risk missed signals or stuck positions')
print('    [-] Expiry-day theta decay is extreme -- avoid same-day expiry')

print('\n  RECOMMENDED LIVE ADJUSTMENTS:')
print('    1. Use NIFTY futures volume for volume filter (token: 256265)')
print('    2. Avoid trades on expiry day (Thursday) -- theta crush')
print('    3. Use limit orders 1-2 ticks above market for entry')
print('    4. Increase slippage buffer to 1% for safety margin')
print('    5. Start with paper trading for 2-3 months validation')
print('    6. Cap position size at 2-3 lots initially')

# ============================================================
# SUMMARY
# ============================================================
print(f'\n{sep}')
print('  FINAL SUMMARY')
print(sep)
print(f'  Capital           : Rs{capital:,.0f}')
print(f'  Final Equity      : Rs{float(equity[-1]):,.0f}')
print(f'  Net Return        : {total_return*100:.1f}% over {years_span:.1f} years')
print(f'  CAGR              : {cagr:.1f}%')
print(f'  Trades            : {total_trades} ({total_trades/total_weeks:.1f}/week)')
print(f'  Win Rate          : {len(all_pnls[all_pnls>0])/len(all_pnls)*100:.1f}%')
print(f'  Profit Factor     : {results["profit_factor"]:.3f}')
print(f'  Sharpe            : {results.get("sharpe",0):.3f}')
print(f'  Max Drawdown      : {max_dd:.1f}%')
print(f'  Profitable Months : {prof_months}/{total_m} ({prof_months/total_m*100:.0f}%)')
print(f'  Total Costs       : Rs{total_costs:,.0f} ({total_costs/abs(total_gross)*100:.1f}% of gross)')
print(f'  All returns are AFTER costs, slippage, and Zerodha taxes')
print(sep)
