"""Quick check: identify data breaks in all 3 indices from histdata."""
import pandas as pd
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import LIVE_INDEX_CONFIG

DATA_DIR = Path(__file__).parent / "data" / "raw"

SESSIONS = {
    "SPX500": (16, 30, 23, 0),
    "NDX100": (16, 30, 23, 0),
    "GER30":  (10, 0, 18, 30),
}

for symbol in ["SPX500", "NDX100", "GER30"]:
    f = DATA_DIR / f"{symbol}_10YR_M1_CLEAN.csv"
    if not f.exists():
        print(f"\n{symbol}: FILE NOT FOUND")
        continue
    
    df = pd.read_csv(f, sep="\t", index_col=0, parse_dates=True)
    print(f"\n{'='*70}")
    print(f"  {symbol}: {len(df):,} bars | {df.index[0]} → {df.index[-1]}")
    print(f"{'='*70}")
    
    # Year-by-year price ranges
    print(f"\n  Year   Bars       Start Price  End Price    Min       Max       Bars/Day")
    print(f"  {'─'*75}")
    for yr in sorted(df.index.year.unique()):
        chunk = df[df.index.year == yr]
        days = len(set(chunk.index.date))
        bpd = len(chunk) / max(1, days)
        print(f"  {yr}   {len(chunk):>9,}   {chunk['Close'].iloc[0]:>10.2f}  {chunk['Close'].iloc[-1]:>10.2f}  "
              f"{chunk['Close'].min():>10.2f}  {chunk['Close'].max():>10.2f}  {bpd:>6.0f}")
    
    # Check price jumps > 5% between consecutive bars
    pct_change = df['Close'].pct_change().abs()
    big_jumps = pct_change[pct_change > 0.05]
    if len(big_jumps) > 0:
        print(f"\n  BIG JUMPS (>5% in 1 min): {len(big_jumps)}")
        for ts, chg in big_jumps.items():
            idx = df.index.get_loc(ts)
            if idx > 0:
                prev = df['Close'].iloc[idx-1]
                curr = df['Close'].iloc[idx]
                print(f"    {ts} : {prev:.2f} → {curr:.2f} ({chg:+.1%})")
    else:
        print(f"\n  No big price jumps (>5%). Clean data.")
    
    # Session bars/day by year
    s_h, s_m, e_h, e_m = SESSIONS[symbol]
    expected = (e_h * 60 + e_m) - (s_h * 60 + s_m) + 1
    
    print(f"\n  Session ({s_h}:{s_m:02d}-{e_h}:{e_m:02d} EET, expected ~{expected} bars/day):")
    session_mask = (
        ((df.index.hour > s_h) | ((df.index.hour == s_h) & (df.index.minute >= s_m))) &
        ((df.index.hour < e_h) | ((df.index.hour == e_h) & (df.index.minute <= e_m)))
    )
    session_df = df[session_mask]
    daily_counts = session_df.groupby(session_df.index.date).size()
    
    for yr in sorted(df.index.year.unique()):
        yr_daily = daily_counts[[d for d in daily_counts.index if d.year == yr]]
        if len(yr_daily) > 0:
            avg_bars = yr_daily.mean()
            min_bars = yr_daily.min()
            low_pct = (yr_daily < expected * 0.8).sum() / len(yr_daily) * 100
            print(f"    {yr}: avg={avg_bars:.0f} bars/day | min={min_bars} | "
                  f"low(<80%) days: {low_pct:.0f}%")

print("\nDONE")
