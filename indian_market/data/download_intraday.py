"""
Download intraday OHLCV data for Nifty 50.

yfinance intraday limits (free tier):
  1m  → last 7 days
  3m  → last 60 days   (not directly supported — approximated from 1m)
  5m  → last 60 days
  15m → last 60 days
  1h  → last 730 days

Historical 1m source (Jan 2015 - Mar 2024):
  backtest_engine/data/nifty50_candlestick_data.csv
  Columns: Instrument, Date (DD-MM-YYYY), Time (H:MM:SS), Open, High, Low, Close
  load_nifty1m_historical() reads this and returns a clean DataFrame.
"""
import os
import pandas as pd
import yfinance as yf

DATA_DIR = os.path.dirname(__file__)
TICKER   = "^NSEI"   # Nifty 50

# Path to the historical 1m candlestick CSV (backtest_engine data folder)
HIST_1M_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "backtest_engine", "data", "nifty50_candlestick_data.csv"
)

INTERVALS = {
    "1m" : "1m",
    "5m" : "5m",
    "15m": "15m",
    "1h" : "60m",
}


def load_nifty1m_historical(start: str = None, end: str = None) -> pd.DataFrame:
    """
    Load the historical 1m Nifty dataset from backtest_engine/data.
    Format: Instrument, Date (DD-MM-YYYY), Time (H:MM:SS), Open, High, Low, Close

    Returns DataFrame with DatetimeIndex in IST (naive), columns Open/High/Low/Close.
    Volume column is added as zeros (not present in source).
    """
    if not os.path.exists(HIST_1M_PATH):
        print(f"  Historical 1m file not found: {HIST_1M_PATH}")
        return pd.DataFrame()

    print(f"  Loading historical 1m data from nifty50_candlestick_data.csv ...")
    df = pd.read_csv(HIST_1M_PATH,
                     names=["Instrument", "Date", "Time", "Open", "High", "Low", "Close"],
                     header=0,
                     dtype={"Open": float, "High": float, "Low": float, "Close": float})

    # Parse DD-MM-YYYY + H:MM:SS into a datetime index (IST, naive)
    df["datetime"] = pd.to_datetime(
        df["Date"] + " " + df["Time"], format="%d-%m-%Y %H:%M:%S"
    )
    df = df.set_index("datetime").drop(columns=["Instrument", "Date", "Time"])
    df["Volume"] = 0

    # Keep only market hours 09:15 to 15:30 IST
    df = df.between_time("09:15", "15:30")
    df = df.sort_index()

    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]

    print(f"  Loaded {len(df):,} bars — {df.index[0].date()} to {df.index[-1].date()}")
    return df


def resample_from_1m(df_1m: pd.DataFrame, target_interval: str) -> pd.DataFrame:
    """
    Resample 1m bars to a higher timeframe (5m, 15m, 1h).
    Useful when only the 1m historical CSV is available.
    """
    rule_map = {"5m": "5min", "15m": "15min", "1h": "60min", "3m": "3min"}
    rule = rule_map.get(target_interval)
    if rule is None:
        raise ValueError(f"Unsupported target interval: {target_interval}")

    df_rs = df_1m.resample(rule, closed="left", label="left").agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low",  "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    ).dropna(subset=["Open", "Close"])

    # Re-apply market hours filter after resampling
    df_rs = df_rs.between_time("09:15", "15:30")
    return df_rs


def download_intraday(interval: str = "5m", force: bool = False) -> pd.DataFrame:
    """Download intraday data for the interval. Returns DataFrame."""
    fname = os.path.join(DATA_DIR, f"nifty_{interval}.csv")

    if os.path.exists(fname) and not force:
        df = pd.read_csv(fname, index_col=0, parse_dates=True)
        print(f"  Loaded nifty_{interval} from cache ({len(df)} bars)")
        return df

    print(f"  Downloading Nifty {interval} data from yfinance ...")

    # yfinance period limits
    if interval == "1m":
        period = "7d"
    elif interval in ("5m", "15m", "3m"):
        period = "60d"
    elif interval == "60m":
        period = "730d"
    else:
        period = "60d"

    raw = yf.download(TICKER, period=period, interval=interval,
                      auto_adjust=True, progress=False)
    if raw.empty:
        print(f"  WARNING: No {interval} data returned")
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    # NSE market hours: 09:15 to 15:30 IST = 03:45 to 10:00 UTC
    # yfinance returns ^NSEI timestamps in UTC, so filter accordingly
    if df.index.tzinfo is not None:
        df = df.between_time("03:45", "10:00")
    else:
        df = df.between_time("09:15", "15:30")
    df.to_csv(fname)
    print(f"  Saved nifty_{interval}.csv ({len(df)} bars, {df.index[0].date()} to {df.index[-1].date()})")
    return df


def load_intraday(interval: str, use_historical: bool = True) -> pd.DataFrame:
    """
    Load intraday data for the given interval.

    For 1m: prefers the historical CSV (Jan 2015 - Mar 2024) if available.
    For other intervals: checks if historical 1m is available and resamples,
    otherwise falls back to yfinance cache / live download.
    """
    # --- 1m: prefer historical CSV ---
    if interval == "1m" and use_historical and os.path.exists(HIST_1M_PATH):
        return load_nifty1m_historical()

    # --- Other intervals: try resampling from historical 1m ---
    if interval in ("5m", "15m", "3m", "1h") and use_historical and os.path.exists(HIST_1M_PATH):
        print(f"  Resampling {interval} from historical 1m data ...")
        df_1m = load_nifty1m_historical()
        if not df_1m.empty:
            iv = "1h" if interval == "1h" else interval
            df_rs = resample_from_1m(df_1m, iv)
            print(f"  Resampled to {interval}: {len(df_rs):,} bars — "
                  f"{df_rs.index[0].date()} to {df_rs.index[-1].date()}")
            return df_rs

    # --- Fallback: cached CSV or yfinance download ---
    fname = os.path.join(DATA_DIR, f"nifty_{interval}.csv")
    if not os.path.exists(fname):
        return download_intraday(interval, force=False)
    df = pd.read_csv(fname, index_col=0, parse_dates=True)
    return df


def get_daily_from_intraday(df_intraday: pd.DataFrame) -> pd.DataFrame:
    """Resample intraday to daily OHLCV (for CPR calculation)."""
    return df_intraday.resample("B").agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low",  "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    ).dropna()


if __name__ == "__main__":
    print("=== Historical 1m data ===")
    df1m = load_nifty1m_historical()
    if not df1m.empty:
        import pandas as pd
        bpd = df1m.groupby(df1m.index.date).size()
        print(f"  Bars/day: avg={bpd.mean():.0f}, min={bpd.min()}, max={bpd.max()}")

    print("\n=== Resampled 5m from historical 1m ===")
    if not df1m.empty:
        df5m = resample_from_1m(df1m, "5m")
        print(f"  {len(df5m):,} bars — {df5m.index[0].date()} to {df5m.index[-1].date()}")

    print("\n=== yfinance recent data ===")
    for iv in ["5m", "15m"]:
        df = download_intraday(iv, force=False)
        if not df.empty:
            print(f"  {iv}: {df.index[0]} to {df.index[-1]}")
