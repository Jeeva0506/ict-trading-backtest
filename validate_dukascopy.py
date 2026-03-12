"""Validate downloaded Dukascopy chunks - check timestamps, prices, timezone alignment."""
import pandas as pd
from pathlib import Path
from collections import Counter

chunks_dir = Path("data/dukascopy_chunks")
existing_dir = Path("data/raw")

print("=" * 70)
print("  DATA VALIDATION - DOWNLOADED DUKASCOPY CHUNKS")
print("=" * 70)

for symbol in ["SPX500", "NDX100", "US30", "GER30", "US2000"]:
    sym_dir = chunks_dir / symbol
    if not sym_dir.exists():
        continue

    chunks = sorted(sym_dir.glob("m1_*.parquet"))
    if not chunks:
        print(f"\n--- {symbol}: No chunks yet ---")
        continue

    # Load first and last chunks
    first = pd.read_parquet(chunks[0])
    last = pd.read_parquet(chunks[-1])

    # Load a middle chunk
    mid_idx = len(chunks) // 2
    mid = pd.read_parquet(chunks[mid_idx])

    print(f"\n--- {symbol} ({len(chunks)} chunks) ---")
    print(f"  First chunk: {chunks[0].name}")
    print(f"    Date range: {first.index[0]} -> {first.index[-1]}")
    print(f"    Bars: {len(first):,}")
    print(f"    Columns: {first.columns.tolist()}")
    o, h, l, c = first["Open"].iloc[0], first["High"].iloc[0], first["Low"].iloc[0], first["Close"].iloc[0]
    print(f"    Sample prices: O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f}")
    print(f"    Price range: {first['Close'].min():.2f} - {first['Close'].max():.2f}")
    print(f"    Index tz: {first.index.tz}")
    print(f"    Index dtype: {first.index.dtype}")

    print(f"  Mid chunk: {chunks[mid_idx].name}")
    print(f"    Date range: {mid.index[0]} -> {mid.index[-1]}")
    print(f"    Sample prices: O={mid['Open'].iloc[0]:.2f} C={mid['Close'].iloc[0]:.2f}")

    print(f"  Last chunk: {chunks[-1].name}")
    print(f"    Date range: {last.index[0]} -> {last.index[-1]}")
    print(f"    Sample prices: O={last['Open'].iloc[0]:.2f} C={last['Close'].iloc[0]:.2f}")

    # Compare with existing data if available
    existing_m1 = existing_dir / f"{symbol}_M1_CLEAN.csv"
    if existing_m1.exists():
        ex = pd.read_csv(existing_m1, sep="\t", nrows=5)
        print(f"  EXISTING DATA ({symbol}_M1_CLEAN.csv):")
        print(f"    Columns: {ex.columns.tolist()}")
        print(f"    First time: {ex.iloc[0, 0]}")
        print(f"    Sample: O={ex['Open'].iloc[0]:.2f} C={ex['Close'].iloc[0]:.2f}")

print("\n" + "=" * 70)
print("  TIMEZONE & SESSION HOUR DISTRIBUTION")
print("=" * 70)

# Our sessions (EET = UTC+2):
#   SPX500/NDX100/US30/US2000: 16:30-23:00 EET = 14:30-21:00 UTC
#   GER30: 10:00-18:30 EET = 08:00-16:30 UTC
# Dukascopy serves UTC data

for symbol in ["SPX500", "GER30"]:
    sym_dir = chunks_dir / symbol
    if not sym_dir.exists():
        continue
    chunks = sorted(sym_dir.glob("m1_*.parquet"))
    if not chunks:
        continue

    # Load a recent chunk
    recent = [c for c in chunks if "2024" in c.name or "2023" in c.name]
    if recent:
        df = pd.read_parquet(recent[0])
        if df.index.tz:
            hours = df.index.tz_localize(None).hour
        else:
            hours = df.index.hour

        hour_counts = Counter(hours)
        print(f"\n  {symbol} hour distribution (from {recent[0].name}):")
        for h in sorted(hour_counts.keys()):
            bar = "#" * min(50, hour_counts[h] // 20)
            print(f"    {h:02d}:00 -> {hour_counts[h]:>5} bars  {bar}")

print("\n" + "=" * 70)
print("  OVERLAP COMPARISON WITH EXISTING MT5 DATA")
print("=" * 70)

# Find overlap period between Dukascopy and existing data
for symbol in ["SPX500", "NDX100", "US30", "GER30"]:
    existing_m1 = existing_dir / f"{symbol}_M1_CLEAN.csv"
    sym_dir = chunks_dir / symbol
    if not existing_m1.exists() or not sym_dir.exists():
        continue

    # Load existing data head
    ex = pd.read_csv(existing_m1, sep="\t", parse_dates=["Time"], nrows=100)
    ex_start = ex["Time"].iloc[0]
    ex_end_small = ex["Time"].iloc[-1]

    # Find matching Dukascopy chunk
    year = ex_start.year
    month = ex_start.month
    chunk_path = sym_dir / f"m1_{year}_{month:02d}.parquet"

    if chunk_path.exists():
        dk = pd.read_parquet(chunk_path)
        # Find overlap
        overlap_start = ex_start
        overlap_mask = (dk.index >= str(overlap_start)) & (dk.index <= str(ex_end_small))
        dk_overlap = dk[overlap_mask]

        if len(dk_overlap) > 0:
            print(f"\n  {symbol} - Overlap found at {year}-{month:02d}:")
            print(f"    Existing first bar: Time={ex_start}, O={ex['Open'].iloc[0]:.2f}, C={ex['Close'].iloc[0]:.2f}")
            # Dukascopy timestamps are UTC, existing are EET (UTC+2)
            # So dukascopy timestamp + 2h = existing timestamp
            eet_idx = dk.index + pd.Timedelta(hours=2) if dk.index.tz is None else dk.index.tz_convert("EET")
            dk_eet = dk.copy()
            dk_eet.index = dk.index + pd.Timedelta(hours=2)
            eet_mask = (dk_eet.index >= str(overlap_start)) & (dk_eet.index <= str(ex_end_small))
            dk_eet_overlap = dk_eet[eet_mask]
            if len(dk_eet_overlap) > 0:
                match_time = dk_eet_overlap.index[0]
                print(f"    Dukascopy (UTC+2): Time={match_time}, O={dk_eet_overlap['Open'].iloc[0]:.2f}, C={dk_eet_overlap['Close'].iloc[0]:.2f}")
                print(f"    Price match: {'YES' if abs(dk_eet_overlap['Open'].iloc[0] - ex['Open'].iloc[0]) < 5 else 'NO'}")
            else:
                print(f"    No overlap after EET conversion in this range")
        else:
            print(f"\n  {symbol} - No overlap bars found at {year}-{month:02d}")
    else:
        print(f"\n  {symbol} - Dukascopy chunk for {year}-{month:02d} not yet downloaded")

print("\nDone.")
