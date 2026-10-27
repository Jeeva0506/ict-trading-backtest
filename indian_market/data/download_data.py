"""
Download 12 years of daily data for Indian options backtesting.

Sources (via yfinance):
  ^NSEI      → Nifty 50 spot
  ^NSEBANK   → BankNifty spot
  ^INDIAVIX  → India VIX

Saves CSVs to data/ directory.  Run once; subsequent runs use cached files.
"""
import os
import pandas as pd
import yfinance as yf
from datetime import date

DATA_DIR = os.path.dirname(__file__)
START    = "2012-01-01"
END      = str(date.today())

TICKERS = {
    "nifty"    : "^NSEI",
    "banknifty": "^NSEBANK",
    "indiavix" : "^INDIAVIX",
}

def download_all(force: bool = False) -> dict[str, pd.DataFrame]:
    frames = {}
    for name, ticker in TICKERS.items():
        path = os.path.join(DATA_DIR, f"{name}.csv")
        if os.path.exists(path) and not force:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            print(f"  Loaded {name} from cache ({len(df)} rows)")
        else:
            print(f"  Downloading {ticker} …")
            raw = yf.download(ticker, start=START, end=END,
                              auto_adjust=True, progress=False)
            if raw.empty:
                raise RuntimeError(f"No data returned for {ticker}")
            # Flatten MultiIndex columns if present
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.index.name = "Date"
            df.to_csv(path)
            print(f"  Saved {name}.csv ({len(df)} rows)")
        frames[name] = df
    return frames


def load_all() -> dict[str, pd.DataFrame]:
    frames = {}
    for name in TICKERS:
        path = os.path.join(DATA_DIR, f"{name}.csv")
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} not found — run download_all() first")
        frames[name] = pd.read_csv(path, index_col=0, parse_dates=True)
    return frames


if __name__ == "__main__":
    print("Downloading Indian market data …")
    data = download_all(force=False)
    for name, df in data.items():
        print(f"  {name}: {df.index[0].date()} → {df.index[-1].date()} ({len(df)} rows)")
    print("Done.")
