"""
Dukascopy 10-Year Data Fetcher
==============================
Downloads tick data index-by-index from Dukascopy via findatapy,
resamples to M1 and H1 bars, and saves in the format expected by
the backtest engine (tab-separated: Time, Open, High, Low, Close, Volume).

Downloads in monthly chunks with resume capability.
Computes EMA 20/50 on H1 bars for HTF filter.

Dukascopy ticker mapping:
  SPX500  -> USA500IDXUSD
  NDX100  -> USATECHIDXUSD
  US30    -> USA30IDXUSD
  GER30   -> DEUIDXEUR
  US2000  -> USSMLIDXUSD

Usage:
  python fetch_dukascopy_10yr.py SPX500
  python fetch_dukascopy_10yr.py NDX100
  python fetch_dukascopy_10yr.py ALL
"""

import sys
import time
import gc
import logging
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Suppress noisy findatapy/urllib3 logs
logging.getLogger("findatapy").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

INDEX_MAP = {
    "SPX500":  {"vendor": "USA500IDXUSD",   "divisor": 1000.0},
    "NDX100":  {"vendor": "USATECHIDXUSD",  "divisor": 1000.0},
    "US30":    {"vendor": "USA30IDXUSD",    "divisor": 1000.0},
    "GER30":   {"vendor": "DEUIDXEUR",      "divisor": 1000.0},
    "US2000":  {"vendor": "USSMLIDXUSD",    "divisor": 1000.0},
}

START_DATE = "2016-01-01"
END_DATE   = "2026-03-11"

DATA_DIR = Path(__file__).parent / "data" / "raw"
TEMP_DIR = Path(__file__).parent / "data" / "dukascopy_chunks"

EMA_FAST = 20
EMA_SLOW = 50


def fetch_month_ticks(symbol: str, vendor_ticker: str,
                      year: int, month: int) -> pd.DataFrame | None:
    """Download one month of tick data from Dukascopy."""
    from findatapy.market import Market, MarketDataRequest, MarketDataGenerator

    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)

    start_str = start.strftime("%d %b %Y")
    end_str = end.strftime("%d %b %Y")

    md_request = MarketDataRequest(
        start_date=start_str,
        finish_date=end_str,
        data_source="dukascopy",
        freq="tick",
        fields=["bid", "ask"],
        tickers=[symbol],
        vendor_tickers=[vendor_ticker],
        vendor_fields=["bid", "ask"],
    )

    market = Market(market_data_generator=MarketDataGenerator())

    max_retries = 3
    for attempt in range(max_retries):
        try:
            df = market.fetch_market(md_request)
            if df is not None and not df.empty:
                return df
            else:
                log.warning(f"  No data for {year}-{month:02d} (attempt {attempt+1})")
                if attempt < max_retries - 1:
                    time.sleep(5)
        except Exception as e:
            log.warning(f"  Error {year}-{month:02d} attempt {attempt+1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(10)

    return None


def ticks_to_m1(ticks: pd.DataFrame, symbol: str, divisor: float) -> pd.DataFrame:
    """Convert tick data to M1 OHLC bars."""
    bid_col = f"{symbol}.bid"
    ask_col = f"{symbol}.ask"

    if bid_col not in ticks.columns:
        # Try to find the columns
        bid_cols = [c for c in ticks.columns if "bid" in c.lower()]
        ask_cols = [c for c in ticks.columns if "ask" in c.lower()]
        if bid_cols and ask_cols:
            bid_col, ask_col = bid_cols[0], ask_cols[0]
        else:
            raise ValueError(f"Cannot find bid/ask columns in {ticks.columns.tolist()}")

    # Mid price, scaled
    mid = ((ticks[bid_col] + ticks[ask_col]) / 2.0) / divisor

    # Resample to M1
    ohlc = mid.resample("1min").ohlc()
    ohlc.columns = ["Open", "High", "Low", "Close"]

    # Volume = tick count per minute
    vol = mid.resample("1min").count()
    ohlc["Volume"] = vol

    # Drop empty bars
    ohlc = ohlc.dropna(subset=["Open"])
    ohlc.index.name = "Time"

    return ohlc


def m1_to_h1(m1: pd.DataFrame) -> pd.DataFrame:
    """Resample M1 bars to H1 bars."""
    h1 = pd.DataFrame()
    h1["Open"] = m1["Open"].resample("1h").first()
    h1["High"] = m1["High"].resample("1h").max()
    h1["Low"] = m1["Low"].resample("1h").min()
    h1["Close"] = m1["Close"].resample("1h").last()
    h1["Volume"] = m1["Volume"].resample("1h").sum()
    h1 = h1.dropna(subset=["Open"])
    h1.index.name = "Time"
    return h1


def add_emas(h1: pd.DataFrame) -> pd.DataFrame:
    """Add EMA fast/slow columns to H1 data."""
    h1 = h1.copy()
    h1[f"EMA_{EMA_FAST}"] = h1["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    h1[f"EMA_{EMA_SLOW}"] = h1["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    return h1


def get_month_list(start: str, end: str) -> list[tuple[int, int]]:
    """Generate list of (year, month) tuples between start and end dates."""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    months = []
    current = s.replace(day=1)
    while current <= e:
        months.append((current.year, current.month))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def fetch_index(symbol: str):
    """Fetch all data for one index, month by month with resume."""
    cfg = INDEX_MAP[symbol]
    vendor = cfg["vendor"]
    divisor = cfg["divisor"]

    chunk_dir = TEMP_DIR / symbol
    chunk_dir.mkdir(parents=True, exist_ok=True)

    months = get_month_list(START_DATE, END_DATE)
    total_months = len(months)

    log.info(f"{'='*60}")
    log.info(f"  FETCHING {symbol} ({vendor})")
    log.info(f"  Period: {START_DATE} -> {END_DATE}")
    log.info(f"  Months: {total_months}")
    log.info(f"  Divisor: {divisor}")
    log.info(f"{'='*60}")

    all_m1_parts = []
    skipped = 0
    downloaded = 0
    failed = 0

    for i, (year, month) in enumerate(months):
        chunk_file = chunk_dir / f"m1_{year}_{month:02d}.parquet"

        # Resume: skip if already downloaded
        if chunk_file.exists():
            try:
                m1_chunk = pd.read_parquet(chunk_file)
                all_m1_parts.append(m1_chunk)
                skipped += 1
                continue
            except Exception:
                chunk_file.unlink()  # Corrupted, re-download

        log.info(f"  [{i+1}/{total_months}] Downloading {year}-{month:02d}...")
        t0 = time.time()

        ticks = fetch_month_ticks(symbol, vendor, year, month)

        if ticks is None or ticks.empty:
            log.warning(f"  [{i+1}/{total_months}] No data for {year}-{month:02d}")
            failed += 1
            continue

        # Convert to M1 immediately
        m1_chunk = ticks_to_m1(ticks, symbol, divisor)
        dt = time.time() - t0

        if len(m1_chunk) == 0:
            log.warning(f"  [{i+1}/{total_months}] Empty M1 for {year}-{month:02d}")
            failed += 1
            continue

        # Save chunk for resume
        m1_chunk.to_parquet(chunk_file)
        all_m1_parts.append(m1_chunk)
        downloaded += 1

        tick_count = len(ticks)
        del ticks
        gc.collect()

        log.info(f"  [{i+1}/{total_months}] {year}-{month:02d}: "
                 f"{tick_count:,} ticks -> {len(m1_chunk):,} M1 bars "
                 f"({dt:.0f}s)")

        # Small delay between months to avoid rate-limiting
        if downloaded % 3 == 0:
            time.sleep(2)

    if skipped > 0:
        log.info(f"  Resumed: {skipped} months from cache")
    log.info(f"  Downloaded: {downloaded} | Failed: {failed} | Total: {total_months}")

    if not all_m1_parts:
        log.error(f"  No data collected for {symbol}!")
        return

    # Combine all months
    log.info(f"  Combining {len(all_m1_parts)} monthly chunks...")
    m1_all = pd.concat(all_m1_parts)
    m1_all = m1_all.sort_index()
    m1_all = m1_all[~m1_all.index.duplicated(keep="first")]

    # Remove timezone info to match existing format
    if m1_all.index.tz is not None:
        m1_all.index = m1_all.index.tz_localize(None)

    log.info(f"  Total M1 bars: {len(m1_all):,}")
    log.info(f"  Date range: {m1_all.index[0]} -> {m1_all.index[-1]}")

    # Generate H1
    h1_all = m1_to_h1(m1_all)
    h1_ema = add_emas(h1_all)
    log.info(f"  Total H1 bars: {len(h1_all):,}")

    # Save files
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    m1_file = DATA_DIR / f"{symbol}_10YR_M1_CLEAN.csv"
    h1_file = DATA_DIR / f"{symbol}_10YR_H1_EMA.csv"

    m1_all.to_csv(m1_file, sep="\t", float_format="%.1f")
    h1_ema.to_csv(h1_file, sep="\t", float_format="%.1f")

    log.info(f"  Saved: {m1_file.name} ({len(m1_all):,} bars)")
    log.info(f"  Saved: {h1_file.name} ({len(h1_ema):,} bars)")

    # Quick stats
    log.info(f"\n  SUMMARY FOR {symbol}:")
    log.info(f"    M1 bars:  {len(m1_all):,}")
    log.info(f"    H1 bars:  {len(h1_ema):,}")
    log.info(f"    Period:   {m1_all.index[0]} -> {m1_all.index[-1]}")
    log.info(f"    Price:    {m1_all['Close'].min():.1f} - {m1_all['Close'].max():.1f}")
    log.info(f"    Avg bars/day: {len(m1_all) / ((m1_all.index[-1] - m1_all.index[0]).days + 1):.0f}")

    return m1_all, h1_ema


def main():
    if len(sys.argv) < 2:
        print("Usage: python fetch_dukascopy_10yr.py <SYMBOL|ALL>")
        print(f"  Available: {', '.join(INDEX_MAP.keys())}, ALL")
        sys.exit(1)

    target = sys.argv[1].upper()

    if target == "ALL":
        order = ["SPX500", "NDX100", "US30", "GER30", "US2000"]
    elif target in INDEX_MAP:
        order = [target]
    else:
        print(f"Unknown symbol: {target}")
        print(f"Available: {', '.join(INDEX_MAP.keys())}")
        sys.exit(1)

    t_start = time.time()

    for symbol in order:
        fetch_index(symbol)
        gc.collect()

    total_time = time.time() - t_start
    hours = int(total_time // 3600)
    mins = int((total_time % 3600) // 60)
    log.info(f"\nTotal time: {hours}h {mins}m")


if __name__ == "__main__":
    main()
