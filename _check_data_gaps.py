import pandas as pd

syms = [
    ("SPX500", "SPX500_10YR_M1_CLEAN.csv"),
    ("NDX100", "NDX100_10YR_M1_CLEAN.csv"),
    ("GER30",  "GER30_10YR_M1_CLEAN.csv"),
    ("US30",   "US30_10YR_M1_CLEAN.csv"),
    ("FRA40",  "FRA40_10YR_M1_CLEAN.csv"),
]

for sym, fname in syms:
    try:
        df = pd.read_csv(f"data/raw/{fname}", sep="\t", index_col="Time", parse_dates=True)
        yrs = sorted(df.index.year.unique().tolist())
        full = list(range(yrs[0], yrs[-1] + 1))
        miss = [y for y in full if y not in yrs]
        mo = df.resample("ME").size()
        sparse = mo[mo < 5000]
        print(f"{sym}: {len(df):,} bars | {df.index[0].date()} -> {df.index[-1].date()}")
        print(f"  Years: {yrs}")
        if miss:
            print(f"  MISSING YEARS: {miss}")
        if len(sparse) > 0:
            print(f"  Sparse months ({len(sparse)}):")
            for idx, cnt in sparse.items():
                print(f"    {idx.strftime('%Y-%m')}: {cnt} bars")
        # GER30 price scaling check
        if sym == "GER30":
            for y in [2020, 2021, 2022, 2023]:
                ydf = df[df.index.year == y]
                if len(ydf) > 0:
                    print(f"  GER30 {y}: close range {ydf['Close'].min():.0f}-{ydf['Close'].max():.0f}")
        print()
    except Exception as e:
        print(f"{sym}: ERROR - {e}")
        print()
