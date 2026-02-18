"""
Fetch maximum M1 and H1 data from MT5 for all new FundedNext indices.
Saves in tab-separated format matching existing data files.
"""
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
from pathlib import Path
import time

DATA_DIR = Path(__file__).parent / "data" / "raw"

# New indices to fetch (excluding SPX500, NDX100, GER30, US30 — already have data)
NEW_INDICES = ['AUS200', 'EUSTX50', 'FRA40', 'HK50', 'JP225', 'NTH25', 'SWI20', 'UK100', 'US2000']
# Skip VIX — only 5 months of data

START_DATE = datetime(2015, 1, 1)
END_DATE = datetime(2026, 12, 31)

def fetch_and_save(symbol: str, timeframe, tf_name: str, suffix: str):
    """Fetch data from MT5 and save as tab-separated CSV."""
    print(f"  Fetching {symbol} {tf_name}...", end=" ", flush=True)
    
    rates = mt5.copy_rates_range(symbol, timeframe, START_DATE, END_DATE)
    if rates is None or len(rates) == 0:
        print(f"NO DATA")
        return 0
    
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    # Format to match existing files: Time, Open, High, Low, Close, Volume
    out = df[['time', 'open', 'high', 'low', 'close', 'tick_volume']].copy()
    out.columns = ['Time', 'Open', 'High', 'Low', 'Close', 'Volume']
    
    filename = f"{symbol}_{suffix}.csv"
    filepath = DATA_DIR / filename
    out.to_csv(filepath, sep='\t', index=False)
    
    first = out['Time'].iloc[0].strftime('%Y-%m-%d')
    last = out['Time'].iloc[-1].strftime('%Y-%m-%d')
    print(f"{len(out):>8,} bars  ({first} to {last})  -> {filename}")
    return len(out)


def main():
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        return
    
    print("=" * 70)
    print("FETCHING DATA FOR NEW FUNDEDNEXT INDICES")
    print("=" * 70)
    
    total_bars = 0
    
    for symbol in NEW_INDICES:
        print(f"\n{symbol}:")
        
        # Fetch M1 data
        bars = fetch_and_save(symbol, mt5.TIMEFRAME_M1, "M1", "M1_CLEAN")
        total_bars += bars
        time.sleep(1)  # Rate limiting
        
        # Fetch H1 data (for EMA computation)
        bars = fetch_and_save(symbol, mt5.TIMEFRAME_H1, "H1", "H1_EMA")
        total_bars += bars
        time.sleep(1)
    
    print(f"\n{'=' * 70}")
    print(f"DONE — {total_bars:,} total bars fetched")
    print(f"{'=' * 70}")
    
    mt5.shutdown()


if __name__ == "__main__":
    main()
