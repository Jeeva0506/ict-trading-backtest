"""
Fast Dukascopy 10-Year Data Fetcher (Async)
============================================
Downloads .bi5 tick files directly from Dukascopy CDN using async HTTP,
decodes them, and resamples to M1/H1 bars.

All 5 indices download in parallel with controlled concurrency.
Resume-capable via chunk caching.

Usage:
  python fetch_dukascopy_fast.py          # All 5 indices in parallel
  python fetch_dukascopy_fast.py SPX500   # Single index
"""

import sys
import time
import struct
import lzma
import gc
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import pandas as pd
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

INDEX_MAP = {
    "SPX500":  {"vendor": "USA500IDXUSD",   "point_divisor": 1.0},
    "NDX100":  {"vendor": "USATECHIDXUSD",  "point_divisor": 1.0},
    "US30":    {"vendor": "USA30IDXUSD",    "point_divisor": 1.0},
    "GER30":   {"vendor": "DEUIDXEUR",      "point_divisor": 1.0},
    "US2000":  {"vendor": "USSMLIDXUSD",    "point_divisor": 1.0},
}

# Dukascopy stores prices as integers. The actual divisor depends on the
# instrument's pip size. For indices, it's typically 1.0 (prices in points).
# We'll auto-detect from the first download.

START_DATE = datetime(2016, 1, 1)
END_DATE = datetime(2026, 3, 11)

DATA_DIR = Path(__file__).parent / "data" / "raw"
TEMP_DIR = Path(__file__).parent / "data" / "dukascopy_chunks"

EMA_FAST = 20
EMA_SLOW = 50

# Concurrency controls
MAX_CONCURRENT_REQUESTS = 100    # Max simultaneous HTTP requests (across all indices)
MAX_PER_INDEX = 25               # Max concurrent requests per index
RETRY_COUNT = 3
REQUEST_TIMEOUT = 30             # seconds per request

BASE_URL = "https://datafeed.dukascopy.com/datafeed"


# ═══════════════════════════════════════════════════════════════
# BI5 DECODING
# ═══════════════════════════════════════════════════════════════

def decode_bi5(data: bytes, hour_start: datetime, point_divisor: float) -> list:
    """
    Decode a Dukascopy .bi5 (LZMA compressed) tick file.
    
    Each tick is 20 bytes:
      - 4 bytes uint32: millisecond offset from hour start
      - 4 bytes uint32: ask price (integer)
      - 4 bytes uint32: bid price (integer)  
      - 4 bytes float32: ask volume
      - 4 bytes float32: bid volume
    """
    if not data or len(data) < 5:
        return []
    
    try:
        raw = lzma.decompress(data)
    except Exception:
        return []
    
    tick_size = 20
    n_ticks = len(raw) // tick_size
    if n_ticks == 0:
        return []
    
    ticks = []
    for i in range(n_ticks):
        offset = i * tick_size
        chunk = raw[offset:offset + tick_size]
        if len(chunk) < tick_size:
            break
        
        ms_offset, ask_int, bid_int, ask_vol, bid_vol = struct.unpack(
            '>IIIff', chunk
        )
        
        ts = hour_start + timedelta(milliseconds=ms_offset)
        # Dukascopy index prices: the integer IS the price × some factor
        # For indices like SPX500, prices are stored as actual_price * 1
        # (e.g., 5943.5 stored as 5943500 with divisor 1000, or 59435 with divisor 10)
        ask = ask_int / point_divisor
        bid = bid_int / point_divisor
        mid = (ask + bid) / 2.0
        
        ticks.append((ts, mid))
    
    return ticks


def auto_detect_divisor(data: bytes, hour_start: datetime) -> float:
    """
    Auto-detect the price divisor by looking at raw integer prices
    and comparing with expected ranges for indices.
    """
    if not data or len(data) < 5:
        return 1.0
    
    try:
        raw = lzma.decompress(data)
    except Exception:
        return 1.0
    
    if len(raw) < 20:
        return 1.0
    
    # Read first tick
    _, ask_int, bid_int, _, _ = struct.unpack('>IIIff', raw[:20])
    
    # Expected price ranges for indices:
    # SPX500: 1800-6500, NDX100: 4000-22000, US30: 16000-45000
    # GER30: 8000-23000, US2000: 1000-2500
    
    avg_int = (ask_int + bid_int) / 2.0
    
    # Try different divisors and see which gives a reasonable price
    for div in [1.0, 10.0, 100.0, 1000.0]:
        price = avg_int / div
        if 500 < price < 50000:  # Reasonable index price range
            return div
    
    return 1.0  # fallback


# ═══════════════════════════════════════════════════════════════
# ASYNC DOWNLOAD ENGINE
# ═══════════════════════════════════════════════════════════════

def generate_hours(start: datetime, end: datetime) -> list[datetime]:
    """Generate all hourly timestamps between start and end."""
    hours = []
    current = start.replace(minute=0, second=0, microsecond=0)
    while current < end:
        # Skip weekends (Sat=5, Sun=6) — Dukascopy has no data
        if current.weekday() < 5:  # Mon-Fri
            hours.append(current)
        elif current.hour == 0 and current.weekday() == 5:
            # Saturday 00:00 might have late Friday data
            hours.append(current)
        current += timedelta(hours=1)
    return hours


async def download_hour(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    vendor_ticker: str,
    hour_dt: datetime,
) -> tuple[datetime, bytes | None]:
    """Download a single hour's .bi5 file."""
    # Dukascopy URL format: month is 0-indexed!
    url = (
        f"{BASE_URL}/{vendor_ticker}/"
        f"{hour_dt.year}/{hour_dt.month - 1:02d}/{hour_dt.day:02d}/"
        f"{hour_dt.hour:02d}h_ticks.bi5"
    )
    
    async with semaphore:
        for attempt in range(RETRY_COUNT):
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 0:
                            return (hour_dt, data)
                        return (hour_dt, None)
                    elif resp.status == 404:
                        return (hour_dt, None)  # No data for this hour
                    else:
                        if attempt < RETRY_COUNT - 1:
                            await asyncio.sleep(2 ** attempt)
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if attempt < RETRY_COUNT - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return (hour_dt, None)
    
    return (hour_dt, None)


async def download_month_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    symbol: str,
    vendor_ticker: str,
    point_divisor: float,
    year: int,
    month: int,
) -> pd.DataFrame | None:
    """Download all hours for one month and return M1 bars."""
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    
    hours = generate_hours(start, end)
    if not hours:
        return None
    
    # Download all hours concurrently
    tasks = [
        download_hour(session, semaphore, vendor_ticker, h)
        for h in hours
    ]
    results = await asyncio.gather(*tasks)
    
    # Decode all ticks
    all_ticks = []
    for hour_dt, data in results:
        if data:
            ticks = decode_bi5(data, hour_dt, point_divisor)
            all_ticks.extend(ticks)
    
    if not all_ticks:
        return None
    
    # Build M1 bars from ticks
    df = pd.DataFrame(all_ticks, columns=["Time", "Mid"])
    df = df.set_index("Time").sort_index()
    
    m1 = df["Mid"].resample("1min").ohlc()
    m1.columns = ["Open", "High", "Low", "Close"]
    m1["Volume"] = df["Mid"].resample("1min").count()
    m1 = m1.dropna(subset=["Open"])
    m1.index.name = "Time"
    
    return m1


async def fetch_index_async(
    session: aiohttp.ClientSession,
    global_semaphore: asyncio.Semaphore,
    symbol: str,
    progress_tracker: dict,
) -> tuple[str, pd.DataFrame | None, pd.DataFrame | None]:
    """Fetch all data for one index."""
    cfg = INDEX_MAP[symbol]
    vendor = cfg["vendor"]
    
    chunk_dir = TEMP_DIR / symbol
    chunk_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate month list
    months = []
    current = START_DATE.replace(day=1)
    while current <= END_DATE:
        months.append((current.year, current.month))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    
    total_months = len(months)
    progress_tracker[symbol] = {"total": total_months, "done": 0, "status": "starting"}
    
    # Auto-detect divisor from first available data
    point_divisor = cfg["point_divisor"]
    divisor_detected = False
    
    all_m1_parts = []
    
    for i, (year, month) in enumerate(months):
        chunk_file = chunk_dir / f"m1_{year}_{month:02d}.parquet"
        
        # Resume: skip if already downloaded
        if chunk_file.exists():
            try:
                m1_chunk = pd.read_parquet(chunk_file)
                all_m1_parts.append(m1_chunk)
                
                # Auto-detect divisor from cached data if needed
                if not divisor_detected and len(m1_chunk) > 0:
                    sample_price = m1_chunk["Close"].iloc[0]
                    if sample_price > 50000:
                        point_divisor = 10.0
                    elif sample_price > 500000:
                        point_divisor = 100.0
                    divisor_detected = True
                
                progress_tracker[symbol]["done"] = i + 1
                continue
            except Exception:
                chunk_file.unlink()
        
        # Download this month
        t0 = time.time()
        m1_chunk = await download_month_async(
            session, global_semaphore,
            symbol, vendor, point_divisor,
            year, month,
        )
        dt = time.time() - t0
        
        if m1_chunk is None or len(m1_chunk) == 0:
            progress_tracker[symbol]["done"] = i + 1
            continue
        
        # Auto-detect divisor from first real data
        if not divisor_detected and len(m1_chunk) > 0:
            sample_price = m1_chunk["Close"].iloc[0]
            # Expected ranges: SPX500 1800-6500, NDX 4000-22000, US30 16000-45000
            if sample_price > 100000:
                # Prices are too large, need adjustment
                # Try to figure out the right divisor
                if sample_price > 10000000:
                    point_divisor = 10000.0
                elif sample_price > 1000000:
                    point_divisor = 1000.0
                elif sample_price > 100000:
                    point_divisor = 100.0
                
                log.info(f"  {symbol}: Auto-detected divisor={point_divisor} "
                         f"(raw price={sample_price:.0f})")
                
                # Re-download with correct divisor
                m1_chunk = await download_month_async(
                    session, global_semaphore,
                    symbol, vendor, point_divisor,
                    year, month,
                )
                if m1_chunk is None or len(m1_chunk) == 0:
                    progress_tracker[symbol]["done"] = i + 1
                    continue
            
            divisor_detected = True
            log.info(f"  {symbol}: Divisor={point_divisor}, "
                     f"sample price={m1_chunk['Close'].iloc[0]:.2f}")
        
        # Save chunk
        m1_chunk.to_parquet(chunk_file)
        all_m1_parts.append(m1_chunk)
        
        progress_tracker[symbol]["done"] = i + 1
        
        if (i + 1) % 6 == 0 or i == 0:
            log.info(f"  {symbol}: [{i+1}/{total_months}] {year}-{month:02d} "
                     f"-> {len(m1_chunk):,} M1 bars ({dt:.1f}s)")
    
    progress_tracker[symbol]["status"] = "combining"
    
    if not all_m1_parts:
        log.error(f"  {symbol}: No data collected!")
        progress_tracker[symbol]["status"] = "FAILED"
        return (symbol, None, None)
    
    # Combine all months
    m1_all = pd.concat(all_m1_parts)
    m1_all = m1_all.sort_index()
    m1_all = m1_all[~m1_all.index.duplicated(keep="first")]
    
    # Remove timezone if present
    if m1_all.index.tz is not None:
        m1_all.index = m1_all.index.tz_localize(None)
    
    # Generate H1
    h1 = pd.DataFrame()
    h1["Open"] = m1_all["Open"].resample("1h").first()
    h1["High"] = m1_all["High"].resample("1h").max()
    h1["Low"] = m1_all["Low"].resample("1h").min()
    h1["Close"] = m1_all["Close"].resample("1h").last()
    h1["Volume"] = m1_all["Volume"].resample("1h").sum()
    h1 = h1.dropna(subset=["Open"])
    h1.index.name = "Time"
    
    # Add EMAs
    h1[f"EMA_{EMA_FAST}"] = h1["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    h1[f"EMA_{EMA_SLOW}"] = h1["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    
    # Save
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    m1_file = DATA_DIR / f"{symbol}_10YR_M1_CLEAN.csv"
    h1_file = DATA_DIR / f"{symbol}_10YR_H1_EMA.csv"
    
    m1_all.to_csv(m1_file, sep="\t", float_format="%.2f")
    h1.to_csv(h1_file, sep="\t", float_format="%.2f")
    
    progress_tracker[symbol]["status"] = "DONE"
    
    log.info(f"\n  ✓ {symbol} COMPLETE:")
    log.info(f"    M1 bars:  {len(m1_all):,}")
    log.info(f"    H1 bars:  {len(h1):,}")
    log.info(f"    Period:   {m1_all.index[0]} → {m1_all.index[-1]}")
    log.info(f"    Price:    {m1_all['Close'].min():.2f} – {m1_all['Close'].max():.2f}")
    log.info(f"    Saved:    {m1_file.name}, {h1_file.name}")
    
    return (symbol, m1_all, h1)


async def progress_reporter(tracker: dict, interval: int = 30):
    """Print progress every N seconds."""
    while True:
        await asyncio.sleep(interval)
        lines = []
        all_done = True
        for sym in ["SPX500", "NDX100", "US30", "GER30", "US2000"]:
            if sym in tracker:
                info = tracker[sym]
                done = info["done"]
                total = info["total"]
                pct = (done / total * 100) if total > 0 else 0
                status = info["status"]
                lines.append(f"    {sym:<8} {done:>3}/{total} ({pct:>5.1f}%) [{status}]")
                if status not in ("DONE", "FAILED"):
                    all_done = False
            else:
                lines.append(f"    {sym:<8} waiting...")
                all_done = False
        
        log.info("  ─── PROGRESS ───\n" + "\n".join(lines))
        
        if all_done:
            break


async def main_async(symbols: list[str]):
    """Main async entry point."""
    global_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    progress_tracker = {}
    
    connector = aiohttp.TCPConnector(
        limit=MAX_CONCURRENT_REQUESTS,
        limit_per_host=MAX_CONCURRENT_REQUESTS,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
        keepalive_timeout=30,
    )
    
    timeout = aiohttp.ClientTimeout(total=60, connect=15)
    
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as session:
        # Start progress reporter
        reporter = asyncio.create_task(progress_reporter(progress_tracker))
        
        # Launch all indices in parallel
        tasks = [
            fetch_index_async(session, global_semaphore, sym, progress_tracker)
            for sym in symbols
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Cancel reporter
        reporter.cancel()
        try:
            await reporter
        except asyncio.CancelledError:
            pass
    
    # Summary
    log.info("\n" + "=" * 60)
    log.info("  FINAL SUMMARY")
    log.info("=" * 60)
    
    for result in results:
        if isinstance(result, Exception):
            log.error(f"  ERROR: {result}")
            continue
        symbol, m1, h1 = result
        if m1 is not None:
            log.info(f"  ✓ {symbol}: {len(m1):,} M1 bars, "
                     f"{len(h1):,} H1 bars, "
                     f"{m1.index[0].strftime('%Y-%m-%d')} → "
                     f"{m1.index[-1].strftime('%Y-%m-%d')}")
        else:
            log.info(f"  ✗ {symbol}: FAILED")


def main():
    if len(sys.argv) > 1:
        target = sys.argv[1].upper()
        if target == "ALL":
            symbols = ["SPX500", "NDX100", "US30", "GER30", "US2000"]
        elif target in INDEX_MAP:
            symbols = [target]
        else:
            print(f"Unknown: {target}. Available: {', '.join(INDEX_MAP.keys())}, ALL")
            sys.exit(1)
    else:
        symbols = ["SPX500", "NDX100", "US30", "GER30", "US2000"]
    
    log.info("=" * 60)
    log.info("  DUKASCOPY FAST DOWNLOADER (Async)")
    log.info(f"  Indices: {', '.join(symbols)}")
    log.info(f"  Period: {START_DATE.date()} → {END_DATE.date()}")
    log.info(f"  Concurrency: {MAX_CONCURRENT_REQUESTS} requests")
    log.info("=" * 60)
    
    t0 = time.time()
    
    # Run on Windows requires this policy
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main_async(symbols))
    
    elapsed = time.time() - t0
    hours = int(elapsed // 3600)
    mins = int((elapsed % 3600) // 60)
    secs = int(elapsed % 60)
    log.info(f"\n  Total time: {hours}h {mins}m {secs}s")


if __name__ == "__main__":
    main()
