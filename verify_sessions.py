"""Verify session hours: compare configured vs actual data timestamps."""
import pandas as pd
from pathlib import Path

data_dir = Path(__file__).parent / "data" / "raw"

# Include SPX500 as known-good reference
indices = {
    "SPX500":  ("SPX500_M1_CLEAN.csv",  16, 30, 23,  0),
    "GER30":   ("GER30_M1_CLEAN.csv",   10,  0, 18, 30),
    "AUS200":  ("AUS200_M1_CLEAN.csv",   1,  0,  7,  0),
    "EUSTX50": ("EUSTX50_M1_CLEAN.csv", 10,  0, 18, 30),
    "FRA40":   ("FRA40_M1_CLEAN.csv",   10,  0, 18, 30),
    "HK50":    ("HK50_M1_CLEAN.csv",     3, 30, 10,  0),
    "JP225":   ("JP225_M1_CLEAN.csv",    2,  0,  8,  0),
    "NTH25":   ("NTH25_M1_CLEAN.csv",   10,  0, 18, 30),
    "SWI20":   ("SWI20_M1_CLEAN.csv",   10,  0, 18, 30),
    "UK100":   ("UK100_M1_CLEAN.csv",   10,  0, 18, 30),
    "US2000":  ("US2000_M1_CLEAN.csv",  16, 30, 23,  0),
}

print("SESSION HOURS VERIFICATION")
print("MT5 Server = FundedNext (EET / UTC+2)")
print("=" * 110)

for sym, (fname, sh, sm, eh, em) in indices.items():
    fpath = data_dir / fname
    if not fpath.exists():
        print(f"{sym}: FILE NOT FOUND")
        continue

    df = pd.read_csv(fpath, sep="\t", nrows=300000)
    df["Time"] = pd.to_datetime(df["Time"])
    df["date"] = df["Time"].dt.date
    df["hour"] = df["Time"].dt.hour

    # Hourly bar distribution
    hourly = df.groupby("hour").size()

    # Find where volume concentrates (>5% of bars per hour = active)
    total = len(df)
    active_hours = [h for h, c in hourly.items() if c / total > 0.04]
    active_hours.sort()

    # Per-day first/last bar
    daily_first = df.groupby("date")["Time"].min()
    daily_last = df.groupby("date")["Time"].max()

    # Most common first bar hour (market open)
    first_hours = daily_first.dt.hour.value_counts()
    most_common_open = first_hours.index[0]
    last_hours = daily_last.dt.hour.value_counts()
    most_common_close = last_hours.index[0]

    # Bars in configured session
    session_start = sh * 60 + sm
    session_end = eh * 60 + em
    df["mins"] = df["Time"].dt.hour * 60 + df["Time"].dt.minute
    if session_start < session_end:
        in_session = df[(df["mins"] >= session_start) & (df["mins"] < session_end)]
    else:  # crosses midnight
        in_session = df[(df["mins"] >= session_start) | (df["mins"] < session_end)]
    pct = len(in_session) / total * 100

    config = f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
    active_str = f"{min(active_hours):02d}-{max(active_hours):02d}" if active_hours else "N/A"

    print(f"\n{sym}:")
    print(f"  Config session:     {config} EET")
    print(f"  Data open hour:     {most_common_open:02d}:00  (most common 1st bar of day)")
    print(f"  Data close hour:    {most_common_close:02d}:00  (most common last bar of day)")
    print(f"  Active hours:       {active_str}  (hours with >4% of bars)")
    print(f"  Bars in session:    {len(in_session):,} / {total:,} ({pct:.0f}%)")
    print(f"  All hours:          {dict(hourly.sort_index())}")
