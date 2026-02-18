"""Quick analysis: Weekly trading days & trade frequency."""
import pandas as pd

df = pd.read_csv('reports/live_simulation/portfolio_trades.csv')
df['date'] = pd.to_datetime(df['date'])
df['trade_date'] = df['date'].dt.date

# Unique trading days with trade count
trading_days = df.groupby('trade_date').agg(
    trades=('symbol', 'count'),
    symbols=('symbol', lambda x: ', '.join(sorted(set(x))))
).reset_index()
trading_days['trade_date'] = pd.to_datetime(trading_days['trade_date'])
trading_days['week_start'] = trading_days['trade_date'] - pd.to_timedelta(
    trading_days['trade_date'].dt.weekday, unit='D'
)

# Weekly summary
weekly = trading_days.groupby('week_start').agg(
    trading_days=('trade_date', 'count'),
    total_trades=('trades', 'sum'),
    avg_trades_per_day=('trades', 'mean')
).reset_index().sort_values('week_start')

date_min = df['date'].min().date()
date_max = df['date'].max().date()
n_trades = len(df)
n_tdays = len(trading_days)
n_weeks = len(weekly)

print('=' * 80)
print(f'WEEKLY TRADING ACTIVITY  |  {n_trades} trades over {n_tdays} trading days')
print(f'Date Range: {date_min} to {date_max}')
print('=' * 80)
print(f'{"Week Starting":<15} {"Trading Days":>13} {"Total Trades":>13} {"Avg Trades/Day":>15}')
print('-' * 60)
for _, r in weekly.iterrows():
    ws = r['week_start'].strftime('%Y-%m-%d')
    td = int(r['trading_days'])
    tt = int(r['total_trades'])
    avg = r['avg_trades_per_day']
    print(f'{ws:<15} {td:>13} {tt:>13} {avg:>15.1f}')

print('=' * 80)
print('OVERALL SUMMARY')
print(f'  Total Weeks:           {n_weeks}')
print(f'  Total Trading Days:    {n_tdays}')
print(f'  Total Trades:          {n_trades}')
print(f'  Avg Trading Days/Week: {n_tdays / n_weeks:.1f}')
print(f'  Avg Trades/Day:        {n_trades / n_tdays:.1f}')
print(f'  Avg Trades/Week:       {n_trades / n_weeks:.1f}')

print(f'\nTRADING DAYS PER WEEK DISTRIBUTION:')
dist = weekly['trading_days'].value_counts().sort_index()
for days, count in dist.items():
    pct = count / n_weeks * 100
    print(f'  {int(days)} day(s):  {count} weeks ({pct:.0f}%)')

print(f'\nTRADES PER DAY DISTRIBUTION:')
dist2 = trading_days['trades'].value_counts().sort_index()
for trades, count in dist2.items():
    pct = count / n_tdays * 100
    print(f'  {int(trades)} trade(s): {count} days ({pct:.0f}%)')
