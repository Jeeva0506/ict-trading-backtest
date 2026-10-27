"""
Zerodha intraday data downloader for PO3 backtesting.

Downloads:
  - Nifty 50 3-min OHLCV  (up to 60 days, Zerodha limit)
  - Nifty 50 15-min OHLCV (up to 200 days)
  - India VIX daily OHLCV (up to 2000 days)

Saves CSV files to indian_researcher/data/:
  nifty_3min.csv
  nifty_15min.csv
  india_vix_daily.csv

Usage:
    python data/fetch_po3_data.py
    (Zerodha access_token must be valid in .env.zerodha)
"""
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live.paper_trader import _load_env, _get_kite

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Zerodha instrument token for Nifty 50 spot
NIFTY_TOKEN  = 256265
NIFTY_SYMBOL = "NSE:NIFTY 50"

# India VIX instrument token
VIX_TOKEN    = 264969
VIX_SYMBOL   = "NSE:INDIA VIX"


def _fetch_candles(kite, instrument_token: int, interval: str,
                   from_date: date, to_date: date,
                   chunk_days: int = 60) -> pd.DataFrame:
    """
    Fetch historical candles from Zerodha in chunks (API limit per call).
    Returns DataFrame with DatetimeIndex columns [open, high, low, close, volume].
    """
    all_chunks = []
    cur = from_date

    while cur <= to_date:
        end = min(cur + timedelta(days=chunk_days - 1), to_date)
        print(f"  Fetching {interval} {cur} → {end} ...", end=" ", flush=True)
        try:
            data = kite.historical_data(
                instrument_token,
                from_date=cur,
                to_date=end,
                interval=interval,
                continuous=False,
                oi=False,
            )
            if data:
                all_chunks.extend(data)
                print(f"{len(data)} bars")
            else:
                print("0 bars")
            time.sleep(0.35)   # respect Zerodha rate limit
        except Exception as e:
            print(f"ERROR: {e}")
        cur = end + timedelta(days=1)

    if not all_chunks:
        return pd.DataFrame()

    df = pd.DataFrame(all_chunks)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df.columns = [c.capitalize() for c in df.columns]
    df = df.rename(columns={"Open": "Open", "High": "High",
                             "Low": "Low", "Close": "Close", "Volume": "Volume"})
    # Keep only trading hours (9:00–15:30 IST)
    if interval in ("3minute", "5minute", "15minute"):
        df = df.between_time("09:00", "15:30")
    return df


def fetch_all(days_3m: int = 60, days_15m: int = 180, days_vix: int = 500):
    """Download all required data and save to CSV."""
    print("Connecting to Zerodha...")
    kite = _get_kite()

    today      = date.today()

    # ── 3-min Nifty ────────────────────────────────────────────────────────────
    print(f"\nFetching Nifty 3-min ({days_3m} days)...")
    from_3m = today - timedelta(days=days_3m)
    df_3m   = _fetch_candles(kite, NIFTY_TOKEN, "3minute", from_3m, today, chunk_days=60)
    if not df_3m.empty:
        out = DATA_DIR / "nifty_3min.csv"
        df_3m.to_csv(out)
        print(f"  Saved {len(df_3m):,} bars → {out}")
    else:
        print("  WARNING: No 3-min data received")

    # ── 15-min Nifty ───────────────────────────────────────────────────────────
    print(f"\nFetching Nifty 15-min ({days_15m} days)...")
    from_15m = today - timedelta(days=days_15m)
    df_15m   = _fetch_candles(kite, NIFTY_TOKEN, "15minute", from_15m, today, chunk_days=60)
    if not df_15m.empty:
        out = DATA_DIR / "nifty_15min.csv"
        df_15m.to_csv(out)
        print(f"  Saved {len(df_15m):,} bars → {out}")
    else:
        print("  WARNING: No 15-min data received")

    # ── India VIX daily ────────────────────────────────────────────────────────
    print(f"\nFetching India VIX daily ({days_vix} days)...")
    from_vix = today - timedelta(days=days_vix)
    df_vix   = _fetch_candles(kite, VIX_TOKEN, "day", from_vix, today, chunk_days=365)
    if not df_vix.empty:
        out = DATA_DIR / "india_vix_daily.csv"
        df_vix.to_csv(out)
        print(f"  Saved {len(df_vix):,} bars → {out}")
    else:
        print("  WARNING: No VIX data received")

    print("\nDone. Run:  python backtest/po3_backtest.py")


def load_po3_data() -> tuple:
    """
    Load saved CSVs and return (df_3m, df_15m, vix_df).
    Raises FileNotFoundError if files missing (run fetch_all() first).
    """
    files = {
        "3min" : DATA_DIR / "nifty_3min.csv",
        "15min": DATA_DIR / "nifty_15min.csv",
        "vix"  : DATA_DIR / "india_vix_daily.csv",
    }
    missing = [str(f) for f in files.values() if not f.exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing data files: {missing}\nRun: python data/fetch_po3_data.py"
        )

    df_3m  = pd.read_csv(files["3min"],  index_col=0, parse_dates=True)
    df_15m = pd.read_csv(files["15min"], index_col=0, parse_dates=True)
    vix_df = pd.read_csv(files["vix"],   index_col=0, parse_dates=True)
    return df_3m, df_15m, vix_df


if __name__ == "__main__":
    fetch_all()
