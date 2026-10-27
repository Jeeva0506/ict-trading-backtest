"""
Zerodha Kite Connect — historical data downloader.

Downloads 1m OHLCV for NIFTY 50 and NIFTY BANK indices,
saves to CSV cache, and provides loaders compatible with
the existing backtest pipeline.

Usage:
    python data/zerodha_data.py              # download / refresh cache
    from data.zerodha_data import load_zerodha_1m
"""
import os, sys, time
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd

# ── Paths ──────────────────────────────────────────────────────────────────────
DATA_DIR  = Path(__file__).resolve().parent
ROOT_DIR  = DATA_DIR.parent
ENV_FILE  = ROOT_DIR.parent / "backtest_engine" / ".env.zerodha"

CACHE_NIFTY       = DATA_DIR / "zerodha_nifty_1m.csv"
CACHE_BANKNIFTY   = DATA_DIR / "zerodha_banknifty_1m.csv"
CACHE_FINNIFTY    = DATA_DIR / "zerodha_finnifty_1m.csv"
CACHE_NIFTY_DAY   = DATA_DIR / "zerodha_nifty_day.csv"
CACHE_VIX_DAY     = DATA_DIR / "zerodha_vix_day.csv"

# ── Instrument tokens (NSE indices) ───────────────────────────────────────────
TOKEN_NIFTY     = 256265   # NSE:NIFTY 50
TOKEN_BANKNIFTY = 260105   # NSE:NIFTY BANK
TOKEN_FINNIFTY  = 257801   # NSE:NIFTY FIN SERVICE
TOKEN_VIX       = 264969   # NSE:INDIA VIX

MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"


# ── Env loader ─────────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env = {}
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"envzerodha not found at {ENV_FILE}")
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _get_kite():
    from kiteconnect import KiteConnect
    env = _load_env()
    api_key      = env.get("ZERODHA_API_KEY", "")
    access_token = env.get("ZERODHA_ACCESS_TOKEN", "")
    if not api_key or api_key == "your_api_key_here":
        raise ValueError("Set ZERODHA_API_KEY in envzerodha. Run: python data/zerodha_auth.py")
    if not access_token:
        raise ValueError("No ZERODHA_ACCESS_TOKEN found. Run: python data/zerodha_auth.py")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


# ── Chunked download ───────────────────────────────────────────────────────────
def _download_chunks(kite, token: int, interval: str,
                     start: str, end: str,
                     chunk_days: int = 60,
                     pause: float = 0.4) -> pd.DataFrame:
    """
    Download historical data in date chunks to respect API limits.
    interval: "minute", "5minute", "15minute", "60minute", "day"
    """
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt   = datetime.strptime(end,   "%Y-%m-%d")
    all_rows = []
    cur = start_dt

    while cur <= end_dt:
        nxt = min(cur + timedelta(days=chunk_days), end_dt)
        try:
            rows = kite.historical_data(token, cur, nxt, interval, continuous=False, oi=False)
            all_rows.extend(rows)
            print(f"    {cur.date()} to {nxt.date()} : {len(rows)} bars")
        except Exception as e:
            print(f"    WARN {cur.date()} to {nxt.date()}: {e}")
        cur = nxt + timedelta(days=1)
        time.sleep(pause)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df = df.rename(columns={"date": "datetime", "open": "Open", "high": "High",
                             "low": "Low", "close": "Close", "volume": "Volume"})
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime")
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    df.index = df.index.tz_localize(None)  # strip tz, keep IST-naive
    return df.sort_index()


def _filter_market_hours(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.between_time(MARKET_OPEN, MARKET_CLOSE)


# ── Public downloaders ─────────────────────────────────────────────────────────
def download_nifty_1m(start="2010-01-01", end=None, force=False) -> pd.DataFrame:
    if end is None:
        end = datetime.today().strftime("%Y-%m-%d")
    if CACHE_NIFTY.exists() and not force:
        print(f"  Loading Nifty 1m from cache: {CACHE_NIFTY.name}")
        df = pd.read_csv(CACHE_NIFTY, index_col=0, parse_dates=True)
        print(f"  Cached: {len(df):,} bars  {df.index[0].date()} to {df.index[-1].date()}")
        return df
    print(f"  Downloading Nifty 1m from Zerodha ({start} to {end}) ...")
    kite = _get_kite()
    df = _download_chunks(kite, TOKEN_NIFTY, "minute", start, end, chunk_days=60)
    df = _filter_market_hours(df)
    df.to_csv(CACHE_NIFTY)
    print(f"  Saved {CACHE_NIFTY.name}: {len(df):,} bars")
    return df


def download_finnifty_1m(start="2015-01-01", end=None, force=False) -> pd.DataFrame:
    if end is None:
        end = datetime.today().strftime("%Y-%m-%d")
    if CACHE_FINNIFTY.exists() and not force:
        print(f"  Loading FinNifty 1m from cache: {CACHE_FINNIFTY.name}")
        df = pd.read_csv(CACHE_FINNIFTY, index_col=0, parse_dates=True)
        print(f"  Cached: {len(df):,} bars  {df.index[0].date()} to {df.index[-1].date()}")
        return df
    print(f"  Downloading FinNifty 1m from Zerodha ({start} to {end}) ...")
    kite = _get_kite()
    df = _download_chunks(kite, TOKEN_FINNIFTY, "minute", start, end, chunk_days=60)
    df = _filter_market_hours(df)
    df.to_csv(CACHE_FINNIFTY)
    print(f"  Saved {CACHE_FINNIFTY.name}: {len(df):,} bars")
    return df


def download_banknifty_1m(start="2010-01-01", end=None, force=False) -> pd.DataFrame:
    if end is None:
        end = datetime.today().strftime("%Y-%m-%d")
    if CACHE_BANKNIFTY.exists() and not force:
        print(f"  Loading BankNifty 1m from cache: {CACHE_BANKNIFTY.name}")
        df = pd.read_csv(CACHE_BANKNIFTY, index_col=0, parse_dates=True)
        print(f"  Cached: {len(df):,} bars  {df.index[0].date()} to {df.index[-1].date()}")
        return df
    print(f"  Downloading BankNifty 1m from Zerodha ({start} to {end}) ...")
    kite = _get_kite()
    df = _download_chunks(kite, TOKEN_BANKNIFTY, "minute", start, end, chunk_days=60)
    df = _filter_market_hours(df)
    df.to_csv(CACHE_BANKNIFTY)
    print(f"  Saved {CACHE_BANKNIFTY.name}: {len(df):,} bars")
    return df


def download_nifty_daily(start="2005-01-01", end=None, force=False) -> pd.DataFrame:
    if end is None:
        end = datetime.today().strftime("%Y-%m-%d")
    if CACHE_NIFTY_DAY.exists() and not force:
        df = pd.read_csv(CACHE_NIFTY_DAY, index_col=0, parse_dates=True)
        return df
    print(f"  Downloading Nifty daily from Zerodha ({start} to {end}) ...")
    kite = _get_kite()
    df = _download_chunks(kite, TOKEN_NIFTY, "day", start, end, chunk_days=2000)
    df.to_csv(CACHE_NIFTY_DAY)
    return df


def download_vix_daily(start="2005-01-01", end=None, force=False) -> pd.DataFrame:
    if end is None:
        end = datetime.today().strftime("%Y-%m-%d")
    if CACHE_VIX_DAY.exists() and not force:
        df = pd.read_csv(CACHE_VIX_DAY, index_col=0, parse_dates=True)
        return df
    print(f"  Downloading India VIX daily from Zerodha ({start} to {end}) ...")
    kite = _get_kite()
    df = _download_chunks(kite, TOKEN_VIX, "day", start, end, chunk_days=2000)
    df.to_csv(CACHE_VIX_DAY)
    return df


# ── Loaders (for backtest pipeline) ───────────────────────────────────────────
def load_zerodha_1m(instrument: str = "nifty") -> pd.DataFrame:
    """
    Load cached 1m data. Run download_*_1m() first.
    Returns DataFrame with DatetimeIndex (IST naive), cols: Open/High/Low/Close/Volume
    """
    _cache_map = {"nifty": CACHE_NIFTY, "banknifty": CACHE_BANKNIFTY, "finnifty": CACHE_FINNIFTY}
    cache = _cache_map.get(instrument.lower(), CACHE_NIFTY)
    if not cache.exists():
        raise FileNotFoundError(
            f"{cache.name} not found. Run: python data/zerodha_data.py"
        )
    df = pd.read_csv(cache, index_col=0, parse_dates=True)
    if df.index.tzinfo is not None:
        df.index = df.index.tz_localize(None)
    return df.sort_index()


def load_zerodha_daily(instrument: str = "nifty") -> pd.DataFrame:
    """Load cached daily data."""
    cache = CACHE_NIFTY_DAY if instrument.lower() == "nifty" else CACHE_VIX_DAY
    if not cache.exists():
        raise FileNotFoundError(f"{cache.name} not found.")
    df = pd.read_csv(cache, index_col=0, parse_dates=True)
    if df.index.tzinfo is not None:
        df.index = df.index.tz_localize(None)
    return df.sort_index()


def load_zerodha_vix() -> pd.DataFrame:
    return load_zerodha_daily("vix")


# ── Incremental refresh ───────────────────────────────────────────────────────
def refresh_cache(instrument: str = "nifty", interval: str = "1m"):
    """
    Append only the missing dates to an existing cache CSV.
    Useful for daily/weekly updates without re-downloading everything.
    """
    if instrument == "nifty" and interval == "1m":
        cache = CACHE_NIFTY
        token = TOKEN_NIFTY
    elif instrument == "banknifty" and interval == "1m":
        cache = CACHE_BANKNIFTY
        token = TOKEN_BANKNIFTY
    elif instrument == "nifty" and interval == "day":
        cache = CACHE_NIFTY_DAY
        token = TOKEN_NIFTY
    elif instrument == "vix" and interval == "day":
        cache = CACHE_VIX_DAY
        token = TOKEN_VIX
    else:
        raise ValueError(f"Unsupported: {instrument} {interval}")

    kite_interval = "minute" if interval == "1m" else "day"

    if not cache.exists():
        start = "2010-01-01"
    else:
        df_old = pd.read_csv(cache, index_col=0, parse_dates=True)
        last_date = df_old.index[-1].date()
        start = (last_date + timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"  Cache has data to {last_date}. Fetching from {start} ...")

    end = datetime.today().strftime("%Y-%m-%d")
    if start > end:
        print(f"  {instrument} {interval} cache is already up to date.")
        return

    kite = _get_kite()
    df_new = _download_chunks(kite, token, kite_interval, start, end)
    if interval == "1m":
        df_new = _filter_market_hours(df_new)

    if cache.exists() and not df_new.empty:
        df_old = pd.read_csv(cache, index_col=0, parse_dates=True)
        df_combined = pd.concat([df_old, df_new])
        df_combined = df_combined[~df_combined.index.duplicated(keep="last")]
        df_combined = df_combined.sort_index()
        df_combined.to_csv(cache)
        print(f"  Updated {cache.name}: {len(df_combined):,} bars total")
    elif not df_new.empty:
        df_new.to_csv(cache)
        print(f"  Created {cache.name}: {len(df_new):,} bars")


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Zerodha historical data downloader")
    parser.add_argument("--start",       default="2010-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end",         default=None,         help="End date YYYY-MM-DD")
    parser.add_argument("--force",       action="store_true",  help="Re-download even if cache exists")
    parser.add_argument("--refresh",     action="store_true",  help="Incremental update only")
    parser.add_argument("--instrument",  default="all",        help="nifty | banknifty | all")
    args = parser.parse_args()

    print("="*70)
    print("  ZERODHA DATA DOWNLOADER")
    print("="*70)

    if args.refresh:
        print("\n[Refresh mode — incremental update]\n")
        for inst, intv in [("nifty", "1m"), ("banknifty", "1m"),
                           ("nifty", "day"), ("vix", "day")]:
            if args.instrument != "all" and inst not in args.instrument:
                continue
            print(f"\n  {inst.upper()} {intv}")
            refresh_cache(inst, intv)
    else:
        end = args.end or datetime.today().strftime("%Y-%m-%d")
        if args.instrument in ("nifty", "all"):
            print(f"\n[1/4] Nifty 1m ...")
            download_nifty_1m(args.start, end, force=args.force)

            print(f"\n[2/4] Nifty daily ...")
            download_nifty_daily(args.start, end, force=args.force)

        if args.instrument in ("banknifty", "all"):
            print(f"\n[3/4] BankNifty 1m ...")
            download_banknifty_1m(args.start, end, force=args.force)

        print(f"\n[4/4] India VIX daily ...")
        download_vix_daily("2005-01-01", end, force=args.force)

    print("\n  Done. Run run_zerodha_backtest.py to backtest with real data.")
