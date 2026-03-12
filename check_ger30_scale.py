"""Quick check of GER30 price scaling pattern."""
import pandas as pd

m1 = pd.read_csv('data/raw/GER30_10YR_M1_CLEAN.csv', sep='\t', index_col=0, parse_dates=True)

# Check yearly median prices to find scaling pattern
print("=== Yearly GER30 price stats ===")
for year in range(2010, 2027):
    chunk = m1[m1.index.year == year]
    if len(chunk) == 0:
        continue
    med = chunk['Close'].median()
    mn = chunk['Close'].min()
    mx = chunk['Close'].max()
    print(f"  {year}: median={med:>10.2f}  range={mn:>10.2f} - {mx:>10.2f}  bars={len(chunk):>8,}")

# Find the exact transition points
print("\n=== Transition 1: around June 2020 ===")
for d in ['2020-06-12', '2020-06-13', '2020-06-14', '2020-06-15', '2020-06-16']:
    chunk = m1[m1.index.date == pd.Timestamp(d).date()]
    if len(chunk) > 0:
        first_close = chunk['Close'].iloc[0]
        last_close = chunk['Close'].iloc[-1]
        print(f"  {d}: {first_close:.2f} -> {last_close:.2f} ({len(chunk)} bars)")

print("\n=== Transition 2: around Dec 2023 ===")
for d in ['2023-12-01', '2023-12-02', '2023-12-03', '2023-12-04', '2023-12-05']:
    chunk = m1[m1.index.date == pd.Timestamp(d).date()]
    if len(chunk) > 0:
        first_close = chunk['Close'].iloc[0]
        last_close = chunk['Close'].iloc[-1]
        print(f"  {d}: {first_close:.2f} -> {last_close:.2f} ({len(chunk)} bars)")

# Check ratio between real DAX and data prices
print("\n=== Scaling Analysis ===")
# Real DAX values (approximate)
real_dax = {
    '2020-06-15': 12093,  # real DAX close
    '2020-12-31': 13719,
    '2021-06-30': 15531,
    '2022-06-30': 12783,
    '2023-06-30': 16148,
    '2023-12-01': 16215,
}
for date_str, real_val in real_dax.items():
    ts = pd.Timestamp(date_str)
    chunk = m1[m1.index.date == ts.date()]
    if len(chunk) > 0:
        data_val = chunk['Close'].median()
        ratio = real_val / data_val
        print(f"  {date_str}: data={data_val:.2f}  real~{real_val}  ratio={ratio:.3f}")
