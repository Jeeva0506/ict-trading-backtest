"""
Fetch 3 years of historical data for Forex pairs from MT5.
Stores M1, M15, H1 timeframes in backtest_engine/forexDATA/
"""
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import sys, time

sys.path.insert(0, str(Path(__file__).parent / 'live'))
from config_fundednext import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER

SYMBOLS = ['EURUSD', 'USDJPY', 'XAUUSD']
TIMEFRAMES = {
    'M1':  mt5.TIMEFRAME_M1,
    'M15': mt5.TIMEFRAME_M15,
    'H1':  mt5.TIMEFRAME_H1,
}
YEARS = 3
OUTPUT_DIR = Path(__file__).parent / 'forexDATA'


def fetch_and_save():
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        return

    info = mt5.account_info()
    if not info or info.login != MT5_LOGIN:
        ok = mt5.login(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if not ok:
            print(f"MT5 login failed: {mt5.last_error()}")
            return

    print(f"Connected to MT5 - Account: {mt5.account_info().login}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=YEARS * 365)

    for symbol in SYMBOLS:
        # Ensure symbol is visible
        if not mt5.symbol_select(symbol, True):
            print(f"  [ERROR] Cannot select {symbol}")
            continue

        for tf_name, tf_val in TIMEFRAMES.items():
            print(f"\nFetching {symbol} {tf_name} ({YEARS}y)...", end=" ", flush=True)

            # For M1 data (huge), fetch in monthly chunks to avoid MT5 limits
            if tf_val == mt5.TIMEFRAME_M1:
                all_frames = []
                chunk_start = start_date
                month_delta = timedelta(days=30)
                while chunk_start < end_date:
                    chunk_end = min(chunk_start + month_delta, end_date)
                    rates = mt5.copy_rates_range(symbol, tf_val, chunk_start, chunk_end)
                    if rates is not None and len(rates) > 0:
                        all_frames.append(pd.DataFrame(rates))
                    chunk_start = chunk_end
                    time.sleep(0.1)  # be gentle on MT5

                if all_frames:
                    df = pd.concat(all_frames, ignore_index=True)
                    df.drop_duplicates(subset=['time'], inplace=True)
                    df.sort_values('time', inplace=True)
                else:
                    print("NO DATA")
                    continue
            else:
                rates = mt5.copy_rates_range(symbol, tf_val, start_date, end_date)
                if rates is None or len(rates) == 0:
                    print("NO DATA")
                    continue
                df = pd.DataFrame(rates)

            df['time'] = pd.to_datetime(df['time'], unit='s')
            df = df[['time', 'open', 'high', 'low', 'close', 'tick_volume', 'spread', 'real_volume']]

            filename = f"{symbol}_{YEARS}YEAR_{tf_name}.csv"
            filepath = OUTPUT_DIR / filename
            df.to_csv(filepath, index=False)

            date_range = f"{df['time'].iloc[0].strftime('%Y-%m-%d')} to {df['time'].iloc[-1].strftime('%Y-%m-%d')}"
            print(f"{len(df):,} bars | {date_range} | {filepath.name}")

    mt5.shutdown()
    print(f"\n{'='*60}")
    print(f"All data saved to: {OUTPUT_DIR}")
    print(f"{'='*60}")


if __name__ == '__main__':
    fetch_and_save()
