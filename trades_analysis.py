"""
Trades Per Month & Week Analysis for All 4 Indices
"""
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
import sys
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from strategy.strategy_ict_clean import StrategyICTClean

INDEX_CONFIG = {
    'SPX500': {'local_file': 'spx500_1min.csv', 'session_start_hour': 16, 'session_start_minute': 30, 'sl_buffer': 1.0},
    'GER30': {'local_file': 'GER30_PERIOD_M1.csv', 'session_start_hour': 10, 'session_start_minute': 0, 'sl_buffer': 2.0},
    'FRA40': {'local_file': 'FRA40_PERIOD_M1.csv', 'session_start_hour': 10, 'session_start_minute': 0, 'sl_buffer': 1.0},
    'HK50': {'local_file': 'HK50_PERIOD_M1.csv', 'session_start_hour': 3, 'session_start_minute': 30, 'sl_buffer': 2.5},
}

all_trades = []

for symbol, config in INDEX_CONFIG.items():
    data_path = Path(__file__).parent / 'data' / 'raw' / config['local_file']
    if not data_path.exists():
        print(f"Skipping {symbol} - file not found")
        continue
    
    df = pd.read_csv(data_path, sep='\t', parse_dates=['Time'])
    df.set_index('Time', inplace=True)
    df.columns = [col.lower() for col in df.columns]
    
    trading_days = sorted(set(df.index.date))
    
    for day in trading_days:
        prev_day = day - timedelta(days=1)
        while prev_day.weekday() >= 5:
            prev_day -= timedelta(days=1)
        
        day_start = pd.Timestamp(day) - timedelta(hours=12)
        day_end = pd.Timestamp(day) + timedelta(hours=23, minutes=59)
        
        day_df = df[(df.index >= day_start) & (df.index <= day_end)].copy()
        if len(day_df) < 100:
            continue
        
        prev_day_data = df[df.index.date == prev_day]
        pdh = prev_day_data['high'].max() if len(prev_day_data) > 0 else None
        pdl = prev_day_data['low'].min() if len(prev_day_data) > 0 else None
        
        current_day_data = day_df[day_df.index.date == day]
        if len(current_day_data) == 0:
            continue
        
        session_start = pd.Timestamp(day).replace(hour=config['session_start_hour'], minute=config['session_start_minute'])
        market_hours_data = current_day_data[current_day_data.index >= session_start]
        
        if len(market_hours_data) > 0:
            day_high = market_hours_data['high'].max()
            day_low = market_hours_data['low'].min()
        else:
            continue
        
        strategy = StrategyICTClean(
            lot_size=1, sl_buffer=config['sl_buffer'], min_rr=2.0, bos_required=2,
            capital=100000, risk_percent=1.0, session_start_hour=config['session_start_hour'],
            session_start_minute=config['session_start_minute']
        )
        
        signals = strategy.generate_signals(day_df, day_high=day_high, day_low=day_low, pdh=pdh, pdl=pdl, debug=False)
        
        for signal in signals:
            week_num = day.isocalendar()[1]
            year_week = f'{day.year}-W{week_num:02d}'
            month = day.strftime('%Y-%m')
            all_trades.append({
                'date': day, 'symbol': symbol, 'month': month, 'week': year_week,
                'pnl': signal.realized_pnl, 'tp1_hit': signal.tp1_hit
            })

print('=' * 80)
print('   TRADES PER MONTH & WEEK ANALYSIS (ALL 4 INDICES)')
print('=' * 80)
print()

# Monthly breakdown by index
print('=== MONTHLY BREAKDOWN ===')
monthly = defaultdict(lambda: defaultdict(list))
for t in all_trades:
    monthly[t['month']][t['symbol']].append(t)

months = sorted(monthly.keys())
header = f"{'Month':<10} | {'SPX500':>8} | {'GER30':>8} | {'FRA40':>8} | {'HK50':>8} | {'TOTAL':>8} | {'P&L':>12}"
print(header)
print('-' * 80)

monthly_totals = []
for month in months:
    spx = len(monthly[month].get('SPX500', []))
    ger = len(monthly[month].get('GER30', []))
    fra = len(monthly[month].get('FRA40', []))
    hk = len(monthly[month].get('HK50', []))
    total = spx + ger + fra + hk
    pnl = sum(t['pnl'] for sym in monthly[month].values() for t in sym)
    monthly_totals.append({'month': month, 'trades': total, 'pnl': pnl})
    print(f'{month:<10} | {spx:>8} | {ger:>8} | {fra:>8} | {hk:>8} | {total:>8} | ${pnl:>10,.0f}')

total_trades = len(all_trades)
total_months = len(months)
avg_per_month = total_trades / total_months if total_months > 0 else 0
print('-' * 80)
print(f"{'AVERAGE':<10} | {'':>8} | {'':>8} | {'':>8} | {'':>8} | {avg_per_month:>8.1f} | trades/month")

# Weekly breakdown
print()
print('=== WEEKLY BREAKDOWN ===')
weekly = defaultdict(lambda: defaultdict(list))
for t in all_trades:
    weekly[t['week']][t['symbol']].append(t)

weeks = sorted(weekly.keys())
header = f"{'Week':<10} | {'SPX500':>6} | {'GER30':>6} | {'FRA40':>6} | {'HK50':>6} | {'TOTAL':>6} | {'P&L':>10}"
print(header)
print('-' * 70)

weekly_totals = []
for week in weeks:
    spx = len(weekly[week].get('SPX500', []))
    ger = len(weekly[week].get('GER30', []))
    fra = len(weekly[week].get('FRA40', []))
    hk = len(weekly[week].get('HK50', []))
    total = spx + ger + fra + hk
    pnl = sum(t['pnl'] for sym in weekly[week].values() for t in sym)
    weekly_totals.append({'week': week, 'trades': total, 'pnl': pnl})
    print(f'{week:<10} | {spx:>6} | {ger:>6} | {fra:>6} | {hk:>6} | {total:>6} | ${pnl:>9,.0f}')

total_weeks = len(weeks)
avg_per_week = total_trades / total_weeks if total_weeks > 0 else 0
print('-' * 70)
print(f"{'AVERAGE':<10} | {'':>6} | {'':>6} | {'':>6} | {'':>6} | {avg_per_week:>6.1f} | trades/week")

# Summary
print()
print('=' * 80)
print('   SUMMARY')
print('=' * 80)
print(f'Total Trades: {total_trades}')
print(f'Total Months: {total_months}')
print(f'Total Weeks: {total_weeks}')
print(f'Avg Trades/Month: {avg_per_month:.1f}')
print(f'Avg Trades/Week: {avg_per_week:.1f}')
print(f"Total P&L: ${sum(t['pnl'] for t in all_trades):,.0f}")

# By index
print()
print('=== BY INDEX ===')
for sym in ['SPX500', 'GER30', 'FRA40', 'HK50']:
    sym_trades = [t for t in all_trades if t['symbol'] == sym]
    tp1_rate = sum(1 for t in sym_trades if t['tp1_hit']) / len(sym_trades) * 100 if sym_trades else 0
    print(f"{sym}: {len(sym_trades)} trades, TP1: {tp1_rate:.1f}%, P&L: ${sum(t['pnl'] for t in sym_trades):,.0f}")

# Distribution stats
print()
print('=== TRADE FREQUENCY DISTRIBUTION ===')
trade_counts = [w['trades'] for w in weekly_totals]
print(f"Min trades/week: {min(trade_counts)}")
print(f"Max trades/week: {max(trade_counts)}")
print(f"Weeks with 0 trades: {sum(1 for c in trade_counts if c == 0)}")
print(f"Weeks with 5+ trades: {sum(1 for c in trade_counts if c >= 5)}")
print(f"Weeks with 10+ trades: {sum(1 for c in trade_counts if c >= 10)}")
