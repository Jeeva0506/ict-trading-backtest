"""
HistData.com 10-Year M1 Data Downloader & Processor
=====================================================
Downloads continuous M1 bar data from histdata.com for index CFDs:
  - SPX/USD → SPX500  (S&P 500)
  - NSX/USD → NDX100  (Nasdaq 100)
  - GRX/EUR → GER30   (DAX 30/40)
  - FRX/EUR → FRA40   (CAC 40)
  - UKX/GBP → UK100   (FTSE 100)

Data characteristics:
  - Continuous minute bars (no gaps like Dukascopy)
  - Available: Nov 2010 → present (16+ years)
  - Format: Semicolon-separated, EST (UTC-5 fixed, no DST)
  - Session gap fill NOT needed (bars are naturally continuous)

Pipeline:
  1. Download yearly ZIP files (2010-2024) + monthly ZIPs (2025)
  2. Extract & parse semicolon-separated M1 CSV
  3. Convert EST (UTC-5 fixed) → EET/EEST (Europe/Bucharest)
  4. Save as tab-separated CSV: {SYMBOL}_10YR_M1_CLEAN.csv
  5. Build H1 bars with EMA: {SYMBOL}_10YR_H1_EMA.csv

Usage:
  python fetch_histdata_10yr.py                # Download + process all
  python fetch_histdata_10yr.py --process-only # Process existing ZIPs only
  python fetch_histdata_10yr.py --validate     # Validate processed data
"""

import os
import sys
import time
import zipfile
import argparse
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

import pytz

# histdata library
from histdata.api import download_hist_data, TimeFrame, Platform

# ────────────────────────────────────────────────────────────
# CONFIGURATION
# ────────────────────────────────────────────────────────────

# HistData pair → our symbol mapping
PAIR_MAP = {
    "spxusd": "SPX500",
    "nsxusd": "NDX100",
    "grxeur": "GER30",
    "frxeur": "FRA40",
    "ukxgbp": "UK100",
}

# Year ranges (index CFDs available from ~2010 on histdata.com)
START_YEAR = 2010
END_YEAR = datetime.now().year  # 2025

# Directories
BASE_DIR = Path(__file__).parent
ZIP_DIR = BASE_DIR / "data" / "histdata_zips"
RAW_DIR = BASE_DIR / "data" / "histdata_raw"
OUT_DIR = BASE_DIR / "data" / "raw"

# EST fixed offset (histdata uses EST with no DST)
EST_FIXED = pytz.FixedOffset(-300)  # UTC-5 = -300 minutes

EMA_FAST = 20
EMA_SLOW = 50

# Expected price ranges for validation
EXPECTED_PRICE_RANGES = {
    "SPX500": (800, 7000),      # 2010: ~1100, 2025: ~6000
    "NDX100": (1500, 25000),    # 2010: ~2000, 2025: ~22000
    "GER30":  (4000, 25000),    # 2010: ~5500, 2025: ~23000
    "FRA40":  (2500, 9000),     # 2010: ~3500, 2025: ~8100
    "UK100":  (4500, 9000),     # 2010: ~5500, 2025: ~8500
}


# ────────────────────────────────────────────────────────────
# DOWNLOAD
# ────────────────────────────────────────────────────────────

def download_all():
    """Download M1 data from histdata.com for all pairs and years."""
    ZIP_DIR.mkdir(parents=True, exist_ok=True)

    current_year = datetime.now().year
    current_month = datetime.now().month

    total_downloaded = 0
    total_skipped = 0
    total_failed = 0

    for pair, symbol in PAIR_MAP.items():
        print(f"\n{'═' * 60}")
        print(f"  DOWNLOADING: {pair.upper()} → {symbol}")
        print(f"{'═' * 60}")

        for year in range(START_YEAR, END_YEAR + 1):
            if year < current_year:
                # Past years: download full year (month=None)
                zip_name = f"DAT_ASCII_{pair.upper()}_M1_{year}.zip"
                zip_path = ZIP_DIR / zip_name

                if zip_path.exists():
                    print(f"  {year}: Already downloaded, skipping")
                    total_skipped += 1
                    continue

                for attempt in range(3):
                    try:
                        print(f"  {year}: Downloading full year...", end=" ", flush=True)
                        result = download_hist_data(
                            year=str(year),
                            month=None,
                            pair=pair,
                            time_frame=TimeFrame.ONE_MINUTE,
                            platform=Platform.GENERIC_ASCII,
                            output_directory=str(ZIP_DIR),
                            verbose=False,
                        )
                        print(f"OK → {Path(result).name}")
                        total_downloaded += 1
                        time.sleep(3)  # Be polite to the server
                        break
                    except Exception as e:
                        if attempt < 2:
                            print(f"retry {attempt+1}...", end=" ", flush=True)
                            time.sleep(5 * (attempt + 1))
                        else:
                            print(f"FAILED: {e}")
                            total_failed += 1
                            time.sleep(3)

            else:
                # Current year: download month by month
                for month in range(1, current_month + 1):
                    zip_name = f"DAT_ASCII_{pair.upper()}_M1_{year}{str(month).zfill(2)}.zip"
                    zip_path = ZIP_DIR / zip_name

                    if zip_path.exists():
                        print(f"  {year}-{month:02d}: Already downloaded, skipping")
                        total_skipped += 1
                        continue

                    for attempt in range(3):
                        try:
                            print(f"  {year}-{month:02d}: Downloading...", end=" ", flush=True)
                            result = download_hist_data(
                                year=str(year),
                                month=str(month),
                                pair=pair,
                                time_frame=TimeFrame.ONE_MINUTE,
                                platform=Platform.GENERIC_ASCII,
                                output_directory=str(ZIP_DIR),
                                verbose=False,
                            )
                            print(f"OK → {Path(result).name}")
                            total_downloaded += 1
                            time.sleep(3)
                            break
                        except Exception as e:
                            if attempt < 2:
                                print(f"retry {attempt+1}...", end=" ", flush=True)
                                time.sleep(5 * (attempt + 1))
                            else:
                                print(f"FAILED: {e}")
                                total_failed += 1
                                time.sleep(3)

    print(f"\n{'─' * 60}")
    print(f"  Download Summary: {total_downloaded} new, {total_skipped} skipped, {total_failed} failed")
    print(f"  ZIP directory: {ZIP_DIR}")


# ────────────────────────────────────────────────────────────
# EXTRACT & PARSE
# ────────────────────────────────────────────────────────────

def extract_and_parse_pair(pair: str, symbol: str) -> pd.DataFrame:
    """
    Extract all ZIP files for a pair and parse into a single DataFrame.

    HistData ASCII format:
      YYYYMMDD HHMMSS;Open;High;Low;Close;Volume
      20100101 170000;1115.75;1115.75;1115.75;1115.75;0

    Timezone: EST (UTC-5, fixed — no DST adjustment)
    """
    pair_upper = pair.upper()

    # Find all ZIP files for this pair
    zips = sorted(ZIP_DIR.glob(f"DAT_ASCII_{pair_upper}_M1_*.zip"))
    if not zips:
        print(f"  {symbol}: No ZIP files found for {pair_upper}")
        return pd.DataFrame()

    print(f"  {symbol}: Found {len(zips)} ZIP files")

    all_parts = []

    for zip_path in zips:
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # Each ZIP contains one CSV file
                csv_names = [n for n in zf.namelist() if n.endswith('.csv')]
                if not csv_names:
                    print(f"    WARNING: No CSV in {zip_path.name}")
                    continue

                csv_name = csv_names[0]
                with zf.open(csv_name) as csv_file:
                    df = pd.read_csv(
                        csv_file,
                        sep=';',
                        header=None,
                        names=['DateTime', 'Open', 'High', 'Low', 'Close', 'Volume'],
                        dtype={
                            'Open': float,
                            'High': float,
                            'Low': float,
                            'Close': float,
                            'Volume': float,
                        },
                    )

                    # Parse datetime: "YYYYMMDD HHMMSS" or "YYYY.MM.DD HH:MM"
                    # Handle both possible formats
                    sample = str(df['DateTime'].iloc[0]).strip()
                    if '.' in sample and ':' in sample:
                        # Format: "2010.01.01 17:00" (some histdata versions)
                        df['DateTime'] = pd.to_datetime(df['DateTime'].str.strip(), format='%Y.%m.%d %H:%M')
                    else:
                        # Format: "20100101 170000"
                        df['DateTime'] = pd.to_datetime(df['DateTime'].str.strip(), format='%Y%m%d %H%M%S')

                    df = df.set_index('DateTime')
                    all_parts.append(df)

            # Show progress every 5 files
            if len(all_parts) % 5 == 0:
                print(f"    Processed {len(all_parts)}/{len(zips)} files...")

        except Exception as e:
            print(f"    ERROR extracting {zip_path.name}: {e}")
            continue

    if not all_parts:
        return pd.DataFrame()

    # Combine all parts
    combined = pd.concat(all_parts)
    combined = combined.sort_index()
    combined = combined[~combined.index.duplicated(keep='first')]

    print(f"    Combined: {len(combined):,} M1 bars | {combined.index[0]} → {combined.index[-1]}")

    return combined


def convert_est_to_eet(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert EST (UTC-5 fixed) → EET/EEST (Europe/Bucharest).

    HistData uses EST with NO DST adjustments, so:
    - Winter: EST (UTC-5) → EET (UTC+2) = +7h
    - Summer: EST (UTC-5) → EEST (UTC+3) = +8h

    We properly handle this with timezone-aware conversion.
    """
    if len(df) == 0:
        return df

    # Localize as fixed UTC-5 (EST without DST)
    df.index = df.index.tz_localize(EST_FIXED)

    # Convert to Europe/Bucharest (properly handles EET/EEST DST)
    df.index = df.index.tz_convert("Europe/Bucharest")

    # Strip timezone info (our pipeline expects naive timestamps in EET)
    df.index = df.index.tz_localize(None)

    return df


def fix_ger30_price_scaling(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fix GER30 (GRX/EUR) price scaling issue from histdata.com.
    
    HistData's GRX/EUR data has a known scaling anomaly:
      - Before 2020-06-15: Correct DAX prices (~12,000)
      - 2020-06-15 to 2023-12-03: Prices ~3.7-3.9x too low (~3,000-4,400)
      - From 2023-12-04: Correct prices again (~16,400+)
    
    Fix: Scale the affected segment using linear interpolation of the ratio
    between the boundary transitions, ensuring smooth price continuity.
    
    Boundary analysis (EST timestamps, pre-timezone conversion):
      Transition 1: 2020-06-13 close=12,001.66 → 2020-06-15 open=3,065.00
        Entry ratio: 12,001.66 / 3,065.00 = 3.916
      Transition 2: 2023-12-01 close=4,424.09 → 2023-12-04 open=16,428.87
        Exit ratio: 16,428.87 / 4,424.09 = 3.714
    """
    if len(df) == 0:
        return df
    
    # Define the scaled segment boundaries
    # (Using the original EST timestamps before timezone conversion)
    scale_start = pd.Timestamp('2020-06-15 00:00:00')
    scale_end = pd.Timestamp('2023-12-04 00:00:00')
    
    # Check if we have data in the affected range
    scaled_mask = (df.index >= scale_start) & (df.index < scale_end)
    n_affected = scaled_mask.sum()
    
    if n_affected == 0:
        return df
    
    # Get boundary prices for transition ratio calculation
    before_scale = df[df.index < scale_start]
    after_scale = df[df.index >= scale_end]
    
    if len(before_scale) == 0 or len(after_scale) == 0:
        print("    WARNING: Cannot compute GER30 scaling ratios (missing boundary data)")
        return df
    
    # Entry ratio: last correct close / first scaled open
    last_correct_close = before_scale['Close'].iloc[-1]
    first_scaled_open = df[scaled_mask]['Open'].iloc[0]
    entry_ratio = last_correct_close / first_scaled_open
    
    # Exit ratio: first correct open after / last scaled close
    last_scaled_close = df[scaled_mask]['Close'].iloc[-1]
    first_correct_open = after_scale['Open'].iloc[0]
    exit_ratio = first_correct_open / last_scaled_close
    
    print(f"    GER30 scaling fix: {n_affected:,} bars affected")
    print(f"      Entry ratio: {entry_ratio:.4f} (at {scale_start.date()})")
    print(f"      Exit ratio:  {exit_ratio:.4f} (at {scale_end.date()})")
    
    # Build linearly interpolated ratio for each bar in the scaled segment
    scaled_indices = df.index[scaled_mask]
    total_duration = (scaled_indices[-1] - scaled_indices[0]).total_seconds()
    
    if total_duration > 0:
        # Fractional position within the segment [0, 1]
        elapsed = (scaled_indices - scaled_indices[0]).total_seconds()
        frac = elapsed / total_duration
        # Linear interpolation: entry_ratio → exit_ratio
        ratios = entry_ratio + (exit_ratio - entry_ratio) * frac
    else:
        ratios = entry_ratio
    
    # Apply correction
    df = df.copy()
    price_cols = ['Open', 'High', 'Low', 'Close']
    for col in price_cols:
        df.loc[scaled_mask, col] = df.loc[scaled_mask, col] * ratios
    
    # Verify correction at boundaries
    new_first = df.loc[scaled_mask, 'Open'].iloc[0]
    new_last = df.loc[scaled_mask, 'Close'].iloc[-1]
    print(f"      Fixed range: {new_first:.2f} → {new_last:.2f}")
    print(f"      Boundary gaps: entry={abs(last_correct_close - new_first)/last_correct_close:.2%}, "
          f"exit={abs(first_correct_open - new_last)/first_correct_open:.2%}")
    
    return df


def validate_prices(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Validate and auto-correct price ranges."""
    if len(df) == 0:
        return df

    # GER30-specific scaling fix (must run on EST data, before timezone conversion)
    if symbol == "GER30":
        df = fix_ger30_price_scaling(df)

    lo, hi = EXPECTED_PRICE_RANGES[symbol]
    median_price = df['Close'].median()

    if lo * 0.5 <= median_price <= hi * 1.5:
        print(f"    Prices OK: median={median_price:.2f} (expected {lo}-{hi})")
        return df

    # Try divisors
    for divisor in [10.0, 100.0, 1000.0, 10000.0]:
        corrected = median_price / divisor
        if lo * 0.5 <= corrected <= hi * 1.5:
            print(f"    Price fix: dividing by {divisor:.0f} (median {median_price:.2f} → {corrected:.2f})")
            price_cols = ['Open', 'High', 'Low', 'Close']
            for col in price_cols:
                df[col] = df[col] / divisor
            return df

    print(f"    WARNING: Unexpected median price {median_price:.2f} for {symbol}")
    return df


def build_h1_with_ema(m1: pd.DataFrame) -> pd.DataFrame:
    """Build H1 OHLCV bars from M1 and compute 20/50 EMA."""
    h1 = pd.DataFrame()
    h1['Open'] = m1['Open'].resample('1h').first()
    h1['High'] = m1['High'].resample('1h').max()
    h1['Low'] = m1['Low'].resample('1h').min()
    h1['Close'] = m1['Close'].resample('1h').last()
    h1['Volume'] = m1['Volume'].resample('1h').sum()
    h1 = h1.dropna(subset=['Open'])

    # Compute EMAs
    h1[f'EMA_{EMA_FAST}'] = h1['Close'].ewm(span=EMA_FAST, adjust=False).mean()
    h1[f'EMA_{EMA_SLOW}'] = h1['Close'].ewm(span=EMA_SLOW, adjust=False).mean()

    h1.index.name = 'Time'
    return h1


# ────────────────────────────────────────────────────────────
# MAIN PROCESSING PIPELINE
# ────────────────────────────────────────────────────────────

def process_all():
    """Process all downloaded data: extract, convert timezone, save."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'═' * 60}")
    print(f"  PROCESSING HISTDATA → CLEAN M1 + H1 CSV FILES")
    print(f"{'═' * 60}")

    results = {}

    for pair, symbol in PAIR_MAP.items():
        print(f"\n─── {symbol} ({pair.upper()}) ───")

        # 1. Extract & parse
        m1 = extract_and_parse_pair(pair, symbol)
        if len(m1) == 0:
            print(f"  {symbol}: No data, skipping")
            results[symbol] = None
            continue

        # 2. Validate prices
        m1 = validate_prices(m1, symbol)

        # 3. Convert EST → EET/EEST
        print(f"    Converting EST → EET/EEST...")
        m1 = convert_est_to_eet(m1)
        print(f"    Timezone converted: {m1.index[0]} → {m1.index[-1]}")

        # 4. Build H1 with EMA
        h1 = build_h1_with_ema(m1)
        print(f"    H1 bars: {len(h1):,}")

        # 5. Set index name
        m1.index.name = 'Time'

        # 6. Save M1
        m1_file = OUT_DIR / f"{symbol}_10YR_M1_CLEAN.csv"
        m1.to_csv(m1_file, sep='\t', float_format='%.2f')
        m1_size_mb = m1_file.stat().st_size / 1024 / 1024
        print(f"    Saved: {m1_file.name} ({m1_size_mb:.1f} MB)")

        # 7. Save H1
        h1_file = OUT_DIR / f"{symbol}_10YR_H1_EMA.csv"
        h1.to_csv(h1_file, sep='\t', float_format='%.2f')
        print(f"    Saved: {h1_file.name}")

        # 8. Summary stats
        trading_days = len(set(m1.index.date))
        bars_per_day = len(m1) / max(1, trading_days)
        year_span = (m1.index[-1] - m1.index[0]).days / 365.25
        print(f"    Summary: {len(m1):,} M1 bars over {year_span:.1f} years")
        print(f"    Trading days: {trading_days:,} ({bars_per_day:.0f} bars/day avg)")
        print(f"    Price: {m1['Close'].min():.2f} → {m1['Close'].max():.2f}")

        results[symbol] = {
            'm1_bars': len(m1),
            'h1_bars': len(h1),
            'days': trading_days,
            'years': year_span,
            'start': str(m1.index[0]),
            'end': str(m1.index[-1]),
        }

    # Print overall summary
    print(f"\n{'═' * 60}")
    print(f"  PROCESSING COMPLETE")
    print(f"{'═' * 60}")
    for sym, info in results.items():
        if info:
            print(f"  {sym}: {info['m1_bars']:>12,} M1 bars | {info['years']:.1f} yrs | {info['start'][:10]} → {info['end'][:10]}")
        else:
            print(f"  {sym}: NO DATA")

    return results


# ────────────────────────────────────────────────────────────
# VALIDATION
# ────────────────────────────────────────────────────────────

def validate_data():
    """Validate processed data for continuity and quality."""
    print(f"\n{'═' * 60}")
    print(f"  VALIDATING HISTDATA PROCESSED FILES")
    print(f"{'═' * 60}")

    # Import session config
    sys.path.insert(0, str(BASE_DIR))
    from backtest_live_setup import LIVE_INDEX_CONFIG

    for symbol in PAIR_MAP.values():
        m1_file = OUT_DIR / f"{symbol}_10YR_M1_CLEAN.csv"
        if not m1_file.exists():
            print(f"\n  {symbol}: File not found")
            continue

        print(f"\n─── {symbol} ───")
        m1 = pd.read_csv(m1_file, sep='\t', index_col=0, parse_dates=True)

        # 1. Basic stats
        print(f"  Bars: {len(m1):,}")
        print(f"  Range: {m1.index[0]} → {m1.index[-1]}")

        # 2. Check for NaN
        na_count = m1.isna().sum().sum()
        print(f"  NaN values: {na_count}")

        # 3. Session bar density
        cfg = LIVE_INDEX_CONFIG.get(symbol, {})
        s_h = cfg.get("session_start_hour", 0)
        s_m = cfg.get("session_start_minute", 0)
        e_h = cfg.get("session_end_hour", 23)
        e_m = cfg.get("session_end_minute", 59)

        session_mask = (
            ((m1.index.hour > s_h) | ((m1.index.hour == s_h) & (m1.index.minute >= s_m))) &
            ((m1.index.hour < e_h) | ((m1.index.hour == e_h) & (m1.index.minute <= e_m)))
        )
        session_bars = m1[session_mask]
        session_days = len(set(session_bars.index.date))
        if session_days > 0:
            bars_per_day = len(session_bars) / session_days
            print(f"  Session bars/day: {bars_per_day:.0f} (expected ~{(e_h*60+e_m) - (s_h*60+s_m) + 1})")

            # Check how many days have low bar counts
            daily_counts = session_bars.groupby(session_bars.index.date).size()
            expected_bars = (e_h * 60 + e_m) - (s_h * 60 + s_m) + 1
            threshold = expected_bars * 0.8  # 80% threshold
            low_days = (daily_counts < threshold).sum()
            pct_low = low_days / len(daily_counts) * 100
            print(f"  Days < 80% bars: {low_days}/{len(daily_counts)} ({pct_low:.1f}%)")
        else:
            print(f"  WARNING: No session bars found!")

        # 4. Year-by-year breakdown
        yearly = m1.groupby(m1.index.year).size()
        print(f"  Yearly bar counts:")
        for yr, count in yearly.items():
            trading_days_yr = len(set(m1[m1.index.year == yr].index.date))
            bpd = count / max(1, trading_days_yr)
            print(f"    {yr}: {count:>10,} bars ({trading_days_yr:>3} days, {bpd:.0f} bars/day)")

        # 5. Price continuity check (identify big jumps)
        price_changes = m1['Close'].pct_change().abs()
        big_jumps = price_changes[price_changes > 0.05]  # >5% in 1 minute
        if len(big_jumps) > 0:
            print(f"  WARNING: {len(big_jumps)} bars with >5% price change in 1 minute")
            for ts, chg in big_jumps.head(5).items():
                print(f"    {ts}: {chg:.1%}")


# ────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='HistData.com 10-Year M1 Data Downloader')
    parser.add_argument('--process-only', action='store_true',
                        help='Skip download, process existing ZIPs only')
    parser.add_argument('--validate', action='store_true',
                        help='Validate processed data only')
    parser.add_argument('--pairs', nargs='+', default=None,
                        help='Specific pairs to download (e.g., spxusd nsxusd)')
    args = parser.parse_args()

    if args.validate:
        validate_data()
        return

    if args.pairs:
        # Filter to requested pairs only
        global PAIR_MAP
        PAIR_MAP = {k: v for k, v in PAIR_MAP.items() if k in args.pairs}
        if not PAIR_MAP:
            print(f"ERROR: No valid pairs in {args.pairs}")
            print(f"Available: {list(PAIR_MAP.keys())}")
            return

    if not args.process_only:
        download_all()

    process_all()

    # Auto-validate after processing
    validate_data()


if __name__ == '__main__':
    main()
