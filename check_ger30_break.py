"""Find exact GER30 scaling break point."""
import pandas as pd
from pathlib import Path

df = pd.read_csv('data/raw/GER30_10YR_M1_CLEAN.csv', sep='\t', index_col=0, parse_dates=True)

# Monthly median close
monthly = df['Close'].resample('ME').median()
print("Month       Median Close")
print("-" * 35)
for dt, price in monthly.items():
    if dt.year >= 2019:
        print(f"  {dt.year}-{dt.month:02d}   {price:>12.2f}")

# Find the exact transition point: check where year-over-year ratio changes
print("\n\nDAILY CLOSE AROUND KNOWN BREAK POINTS:")

# Check around mid-2020 (entry ratio change)
print("\n--- Around 2020-06 ---")
chunk = df.loc["2020-06-12":"2020-06-16"]
daily = chunk.resample('D').last().dropna()
for dt, row in daily.iterrows():
    print(f"  {dt.date()}: Close={row['Close']:.2f}")

# Check around 2023-12 (exit ratio change)
print("\n--- Around 2023-12 ---")
chunk = df.loc["2023-12-01":"2023-12-05"]
daily = chunk.resample('D').last().dropna()
for dt, row in daily.iterrows():
    print(f"  {dt.date()}: Close={row['Close']:.2f}")

# Are 2010-2020-06-12 prices correct? Check 2018 vs real DAX
print("\n\nVALIDATION: 2018 monthly median (real DAX was ~11000-13500):")
m2018 = df.loc["2018"].resample('ME')['Close'].median()
for dt, p in m2018.items():
    print(f"  {dt.year}-{dt.month:02d}: {p:.2f}")

print("\n\nVALIDATION: 2024 monthly median (real DAX was ~16000-20000):")
m2024 = df.loc["2024"].resample('ME')['Close'].median()
for dt, p in m2024.items():
    print(f"  {dt.year}-{dt.month:02d}: {p:.2f}")

# The conclusion: GER30 data is in TWO segments
# Segment 1: 2010-11 to 2020-06-12 — correct DAX prices
# Segment 2: 2020-06-15 to 2023-12-03 — scaled down (~3.8x)
# Segment 3: 2023-12-04 to present — correct DAX prices again
# 
# For backtest: run segments 1 and 3 separately, skip segment 2

print("\n\nSEGMENT SIZES:")
seg1 = df.loc[:"2020-06-12"]
seg2 = df.loc["2020-06-15":"2023-12-03"]
seg3 = df.loc["2023-12-04":]
print(f"  Seg1 (good): {seg1.index[0].date()} → {seg1.index[-1].date()} | {len(seg1):,} bars | {len(set(seg1.index.date))} days")
print(f"  Seg2 (BAD):  {seg2.index[0].date()} → {seg2.index[-1].date()} | {len(seg2):,} bars | {len(set(seg2.index.date))} days")
print(f"  Seg3 (good): {seg3.index[0].date()} → {seg3.index[-1].date()} | {len(seg3):,} bars | {len(set(seg3.index.date))} days")
