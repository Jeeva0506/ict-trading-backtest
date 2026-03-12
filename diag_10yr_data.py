"""
Diagnose why 10-year Dukascopy data produces far fewer trades than 3-year MT5 data.
Check: session hour coverage, price scaling, bar counts per session, timezone alignment.
"""
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data" / "raw"
CHUNKS_DIR = Path(__file__).parent / "data" / "dukascopy_chunks"

# Session times (EET) from backtest_live_setup.py
SESSIONS = {
    "SPX500": (16, 30, 23, 0),   # 16:30-23:00 EET
    "NDX100": (16, 30, 23, 0),
    "US30":   (16, 30, 23, 0),
    "GER30":  (10, 0, 18, 30),   # 10:00-18:30 EET
}

print("=" * 80)
print("  DIAGNOSIS: Why are trade counts so low?")
print("=" * 80)

# 1. Compare 3-year vs 10-year file sizes and bar counts
print("\n── 1. BAR COUNT COMPARISON ──")
for sym in ["SPX500", "NDX100", "US30", "GER30"]:
    files_to_check = [
        f"{sym}_3YEAR_M1.csv",
        f"{sym}_M1_CLEAN.csv",
        f"{sym}_10YR_M1_CLEAN.csv",
    ]
    for fname in files_to_check:
        fpath = DATA_DIR / fname
        if fpath.exists():
            df = pd.read_csv(fpath, sep="\t", parse_dates=["Time"], index_col="Time")
            days = len(set(df.index.date))
            bars_per_day = len(df) / max(1, days)
            print(f"  {fname:<30} {len(df):>10,} bars | {days:>5} days | {bars_per_day:>6.0f} bars/day | {df.index[0].date()} → {df.index[-1].date()}")

# 2. Check session hour coverage (are bars present during trading hours?)
print("\n── 2. SESSION HOUR COVERAGE (BARS DURING SESSION) ──")
for sym in ["SPX500", "NDX100", "US30", "GER30"]:
    s_h, s_m, e_h, e_m = SESSIONS[sym]
    
    # 10-year data
    f10 = DATA_DIR / f"{sym}_10YR_M1_CLEAN.csv"
    if not f10.exists():
        continue
    df10 = pd.read_csv(f10, sep="\t", parse_dates=["Time"], index_col="Time")
    
    # 3-year or existing data
    for ref_name in [f"{sym}_3YEAR_M1.csv", f"{sym}_M1_CLEAN.csv"]:
        fref = DATA_DIR / ref_name
        if fref.exists():
            dfref = pd.read_csv(fref, sep="\t", parse_dates=["Time"], index_col="Time")
            break
    else:
        dfref = None
    
    print(f"\n  {sym} (session {s_h}:{s_m:02d}-{e_h}:{e_m:02d} EET):")
    
    # Count bars within session for 10yr
    session_mask_10 = (
        ((df10.index.hour > s_h) | ((df10.index.hour == s_h) & (df10.index.minute >= s_m))) &
        ((df10.index.hour < e_h) | ((df10.index.hour == e_h) & (df10.index.minute <= e_m)))
    )
    session_bars_10 = df10[session_mask_10]
    session_days_10 = len(set(session_bars_10.index.date))
    bars_per_session_day_10 = len(session_bars_10) / max(1, session_days_10)
    
    print(f"    10YR: {len(session_bars_10):,} session bars / {len(df10):,} total | "
          f"{session_days_10} session days | {bars_per_session_day_10:.0f} bars/session/day")
    
    if dfref is not None:
        session_mask_ref = (
            ((dfref.index.hour > s_h) | ((dfref.index.hour == s_h) & (dfref.index.minute >= s_m))) &
            ((dfref.index.hour < e_h) | ((dfref.index.hour == e_h) & (dfref.index.minute <= e_m)))
        )
        session_bars_ref = dfref[session_mask_ref]
        session_days_ref = len(set(session_bars_ref.index.date))
        bars_per_session_day_ref = len(session_bars_ref) / max(1, session_days_ref)
        
        print(f"    REF:  {len(session_bars_ref):,} session bars / {len(dfref):,} total | "
              f"{session_days_ref} session days | {bars_per_session_day_ref:.0f} bars/session/day")

# 3. Hour-by-hour distribution for one sample day
print("\n── 3. HOURLY BAR DISTRIBUTION (SAMPLE MONTH) ──")
for sym in ["SPX500", "GER30"]:
    f10 = DATA_DIR / f"{sym}_10YR_M1_CLEAN.csv"
    if not f10.exists():
        continue
    df10 = pd.read_csv(f10, sep="\t", parse_dates=["Time"], index_col="Time")
    
    # Pick a month in 2018
    month_data = df10.loc["2018-06":"2018-06"]
    if len(month_data) == 0:
        month_data = df10.iloc[:5000]
    
    from collections import Counter
    hour_counts = Counter(month_data.index.hour)
    
    print(f"\n  {sym} - June 2018 hour distribution:")
    for h in range(24):
        count = hour_counts.get(h, 0)
        bar = "#" * min(60, count // 5)
        s_h, s_m, e_h, e_m = SESSIONS[sym]
        marker = " <<< SESSION" if s_h <= h <= e_h else ""
        print(f"    {h:02d}:00 -> {count:>5} bars  {bar}{marker}")

# 4. Check raw chunk timezone — are they really UTC?  
print("\n── 4. RAW CHUNK TIMEZONE CHECK ──")
for sym in ["SPX500", "GER30"]:
    chunk_dir = CHUNKS_DIR / sym
    chunks = sorted(chunk_dir.glob("m1_2018_*.parquet"))
    if not chunks:
        continue
    
    # Load a raw chunk (UTC, pre-conversion)
    raw = pd.read_parquet(chunks[5] if len(chunks) > 5 else chunks[0])
    raw_hours = Counter(raw.index.hour)
    
    print(f"\n  {sym} RAW CHUNK (UTC, pre-conversion): {chunks[5].name if len(chunks) > 5 else chunks[0].name}")
    print(f"  Date range: {raw.index[0]} → {raw.index[-1]}")
    for h in range(24):
        count = raw_hours.get(h, 0)
        bar = "#" * min(60, count // 5)
        print(f"    {h:02d}:00 -> {count:>5} bars  {bar}")

# 5. Check if the strategy minimum 80 bars is being met
print("\n── 5. STRATEGY 80-BAR MINIMUM CHECK ──")
for sym in ["SPX500", "NDX100", "US30", "GER30"]:
    f10 = DATA_DIR / f"{sym}_10YR_M1_CLEAN.csv"
    if not f10.exists():
        continue
    df10 = pd.read_csv(f10, sep="\t", parse_dates=["Time"], index_col="Time")
    s_h, s_m, e_h, e_m = SESSIONS[sym]
    
    trading_days = sorted(set(df10.index.date))
    enough_bars = 0
    not_enough = 0
    bar_counts = []
    
    for day in trading_days:
        session_start = pd.Timestamp(day).replace(hour=s_h, minute=s_m)
        session_end = pd.Timestamp(day).replace(hour=e_h, minute=e_m)
        market_df = df10[(df10.index >= session_start) & (df10.index <= session_end)]
        bar_counts.append(len(market_df))
        if len(market_df) >= 80:
            enough_bars += 1
        else:
            not_enough += 1
    
    bar_arr = np.array(bar_counts)
    print(f"  {sym}: {enough_bars} days with >=80 bars, {not_enough} days with <80 bars "
          f"({not_enough/(enough_bars+not_enough)*100:.0f}% rejected)")
    print(f"    Bar stats: min={bar_arr.min()}, median={np.median(bar_arr):.0f}, "
          f"max={bar_arr.max()}, mean={bar_arr.mean():.0f}")

# 6. Direct comparison: pick an overlapping date and compare
print("\n── 6. OVERLAPPING DATE COMPARISON ──")
# Find overlap between 10yr and 3yr/existing data
for sym in ["SPX500", "NDX100", "US30", "GER30"]:
    f10 = DATA_DIR / f"{sym}_10YR_M1_CLEAN.csv"
    for ref_name in [f"{sym}_3YEAR_M1.csv", f"{sym}_M1_CLEAN.csv"]:
        fref = DATA_DIR / ref_name
        if fref.exists():
            break
    else:
        continue
    
    if not f10.exists():
        continue
    
    df10 = pd.read_csv(f10, sep="\t", parse_dates=["Time"], index_col="Time")
    dfref = pd.read_csv(fref, sep="\t", parse_dates=["Time"], index_col="Time")
    
    # Find overlap
    overlap_start = max(df10.index[0], dfref.index[0])
    overlap_end = min(df10.index[-1], dfref.index[-1])
    
    if overlap_start < overlap_end:
        print(f"\n  {sym}: Overlap {overlap_start} → {overlap_end}")
        ov10 = df10[(df10.index >= overlap_start) & (df10.index <= overlap_end)]
        ovref = dfref[(dfref.index >= overlap_start) & (dfref.index <= overlap_end)]
        print(f"    10YR bars: {len(ov10):,}")
        print(f"    REF bars:  {len(ovref):,}")
        
        # Compare a sample timestamp
        if len(ov10) > 0 and len(ovref) > 0:
            sample_time = ovref.index[0]
            if sample_time in ov10.index:
                print(f"    At {sample_time}:")
                print(f"      10YR: O={ov10.loc[sample_time, 'open']:.2f} C={ov10.loc[sample_time, 'close']:.2f}")
                print(f"      REF:  O={ovref.loc[sample_time, 'open']:.2f} C={ovref.loc[sample_time, 'close']:.2f}")
            else:
                # Find nearest
                nearest_idx = ov10.index.get_indexer([sample_time], method='nearest')[0]
                nearest_time = ov10.index[nearest_idx]
                print(f"    REF timestamp {sample_time} → nearest 10YR: {nearest_time} (diff: {abs((nearest_time - sample_time).total_seconds())}s)")
    else:
        print(f"\n  {sym}: No overlap (10YR ends {df10.index[-1].date()}, REF starts {dfref.index[0].date()})")

print("\n  DONE")
