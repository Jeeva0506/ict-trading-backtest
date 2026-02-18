"""Check data availability for all FundedNext indices."""
import MetaTrader5 as mt5
from datetime import datetime

mt5.initialize()

indices = ['AUS200', 'EUSTX50', 'FRA40', 'HK50', 'JP225', 'NTH25', 'SWI20', 'UK100', 'US2000', 'VIX']

print('DATA AVAILABILITY (M15 timeframe):')
print('-' * 80)

for name in indices:
    # Use copy_rates_range for proper date range fetch
    rates = mt5.copy_rates_range(name, mt5.TIMEFRAME_M15, datetime(2015, 1, 1), datetime(2026, 12, 31))
    if rates is not None and len(rates) > 0:
        first = datetime.fromtimestamp(rates[0]['time']).strftime('%Y-%m-%d')
        last = datetime.fromtimestamp(rates[-1]['time']).strftime('%Y-%m-%d')
        count = len(rates)
        print(f'{name:<12} From: {first}  To: {last}  Bars: {count:>8,}')
    else:
        print(f'{name:<12} NO DATA')

mt5.shutdown()
