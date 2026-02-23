"""Quick check of date ranges for all candidate indices."""
import pandas as pd
from pathlib import Path

data_dir = Path(__file__).parent / "data" / "raw"
indices = ['AUS200', 'EUSTX50', 'NTH25', 'SWI20', 'UK100', 'US2000', 'FRA40', 'HK50', 'JP225',
           'GER30', 'US30', 'NDX100', 'SPX500']

print(f"{'Symbol':<10} {'Start':<12} {'End':<12} {'Days':>6} {'Bars':>10}")
print("-" * 55)
for sym in indices:
    f = data_dir / f"{sym}_M1_CLEAN.csv"
    if not f.exists():
        print(f"{sym:<10} FILE NOT FOUND")
        continue
    df = pd.read_csv(f, sep="\t", usecols=["Time"])
    df["Time"] = pd.to_datetime(df["Time"], format="mixed")
    days = df["Time"].dt.date.nunique()
    start = df["Time"].min().strftime("%Y-%m-%d")
    end = df["Time"].max().strftime("%Y-%m-%d")
    print(f"{sym:<10} {start:<12} {end:<12} {days:>6} {len(df):>10,}")
