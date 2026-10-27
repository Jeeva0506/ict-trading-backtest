"""
NR7 Positional Backtest — NSE Equities (Buy Only)
==================================================
Fetches + caches data, then runs with all filters active.
For filter ablation, run: python run_nr7_ablation.py

Run:
    python run_nr7_backtest.py          # full run (downloads + caches data)
    python run_nr7_backtest.py --cached # use cached CSVs (fast re-run)
"""
import sys, time, json
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from strategies.nr7_positional import (
    compute_indicators, generate_signals, simulate_trades,
    CAPITAL, RISK_RS, MAX_POSITIONS, FILTERS_ALL,
)

LOOKBACK_YEARS   = 3
MIN_TRADING_DAYS = 400
MIN_TRADES       = 3
NIFTY50_TOKEN    = 256265
CACHE_DIR        = ROOT / "data" / "nr7_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── Zerodha helpers ───────────────────────────────────────────────────────────
def _get_kite():
    from kiteconnect import KiteConnect
    env = {}
    for line in (ROOT.parent / "backtest_engine" / ".env.zerodha").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); env[k.strip()] = v.strip()
    kite = KiteConnect(api_key=env["ZERODHA_API_KEY"])
    kite.set_access_token(env["ZERODHA_ACCESS_TOKEN"])
    return kite


def _fetch_daily(kite, token: int, days: int) -> pd.DataFrame:
    end, start = datetime.now(), datetime.now() - timedelta(days=days)
    try:
        rows = kite.historical_data(token, start, end, "day", continuous=False, oi=False)
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date").sort_index()[["Open", "High", "Low", "Close", "Volume"]]


def _universe(kite) -> list:
    stocks = [i for i in kite.instruments("NSE")
              if i["instrument_type"] == "EQ" and i["segment"] == "NSE" and i["lot_size"] == 1]
    syms = _nifty500_symbols()
    out  = [{"token": s["instrument_token"], "symbol": s["tradingsymbol"]}
            for s in stocks if s["tradingsymbol"] in syms]
    print(f"Universe: {len(out)} stocks")
    return out


# ── Data download + cache ─────────────────────────────────────────────────────
def download_and_cache(kite, universe, lookback_days):
    print(f"Downloading {len(universe)} stocks to cache...")
    days = lookback_days
    ok = 0
    for i, stock in enumerate(universe):
        sym   = stock["symbol"]
        fpath = CACHE_DIR / f"{sym}.csv"
        if fpath.exists():
            ok += 1; continue
        df = _fetch_daily(kite, stock["token"], days)
        if len(df) >= MIN_TRADING_DAYS:
            df.to_csv(fpath)
            ok += 1
        time.sleep(0.06)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(universe)} cached...")
    print(f"Cache: {ok} stocks ready in {CACHE_DIR}")


def load_from_cache(universe) -> dict:
    out = {}
    for stock in universe:
        sym   = stock["symbol"]
        fpath = CACHE_DIR / f"{sym}.csv"
        if not fpath.exists():
            continue
        df = pd.read_csv(fpath, index_col="Date", parse_dates=True)
        if len(df) >= MIN_TRADING_DAYS:
            out[sym] = df
    return out


# ── Single filter-set run ─────────────────────────────────────────────────────
def run_one(stock_data: dict, nifty_df: pd.DataFrame,
            filters: dict) -> dict:
    """Run backtest for one filter configuration. Returns aggregate metrics."""
    all_trades = []
    for sym, df in stock_data.items():
        df_ind = compute_indicators(df)
        sigs   = generate_signals(df_ind, nifty_df=nifty_df, filters=filters)
        for s in sigs: s['symbol'] = sym
        trades = [t for t in simulate_trades(df_ind, sigs)
                  if t.get('exit_reason') != 'DATA_END']
        all_trades.extend(trades)

    if not all_trades:
        return {"n": 0, "wr": 0, "pf": 0, "net": 0, "avg_r": 0}

    wins = [t for t in all_trades if t['win']]
    loss = [t for t in all_trades if not t['win']]
    gw   = sum(t['net_rs'] for t in wins)
    gl   = sum(abs(t['net_rs']) for t in loss)
    net  = gw - gl
    pf   = gw / gl if gl > 0 else float('inf')
    wr   = len(wins) / len(all_trades) * 100
    avg_r = sum(t['r_multiple'] for t in all_trades) / len(all_trades)
    return {"n": len(all_trades), "wr": wr, "pf": pf, "net": net,
            "avg_r": avg_r, "trades": all_trades}


# ── Full backtest (all filters ON) ────────────────────────────────────────────
def run_backtest(use_cache=False):
    print("=" * 65)
    print("  NR7 POSITIONAL BACKTEST — NSE EQUITIES (BUY ONLY)")
    print(f"  Capital: Rs{CAPITAL:,}  Risk/trade: Rs{RISK_RS:,}")
    print("=" * 65)

    lookback_days = LOOKBACK_YEARS * 365 + 60
    kite = _get_kite()
    universe = _universe(kite)

    nifty_raw = _fetch_daily(kite, NIFTY50_TOKEN, days=lookback_days)
    nifty_df  = compute_indicators(nifty_raw) if not nifty_raw.empty else pd.DataFrame()
    print(f"Nifty50: {len(nifty_df)} days\n")

    if not use_cache:
        download_and_cache(kite, universe, lookback_days)

    stock_data = load_from_cache(universe)
    print(f"Loaded {len(stock_data)} stocks from cache\n")

    result = run_one(stock_data, nifty_df, filters=FILTERS_ALL)
    all_trades = result.get('trades', [])

    _print_full_report(all_trades, stock_data)

    # Save
    out_dir = ROOT / "reports" / "nr7_positional"
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(all_trades).to_csv(out_dir / "nr7_all_trades.csv", index=False)
    print(f"\nTrades saved to {out_dir}/nr7_all_trades.csv")


def _print_full_report(all_trades, stock_data):
    if not all_trades:
        print("No trades."); return

    wins  = [t for t in all_trades if t['win']]
    loss  = [t for t in all_trades if not t['win']]
    gw    = sum(t['net_rs'] for t in wins)
    gl    = sum(abs(t['net_rs']) for t in loss)
    net   = gw - gl
    pf    = gw / gl if gl else float('inf')
    wr    = len(wins) / len(all_trades) * 100
    avg_r = sum(t['r_multiple'] for t in all_trades) / len(all_trades)

    print(f"\n  Total trades    : {len(all_trades)}")
    print(f"  Win rate        : {wr:.1f}%")
    print(f"  Profit factor   : {pf:.2f}")
    print(f"  Avg R-multiple  : {avg_r:+.2f}R")
    print(f"  Net P&L         : Rs{net:+,.0f}")

    # Year breakdown
    print("\n  YEAR-BY-YEAR:")
    yd = defaultdict(lambda: {'n':0,'w':0,'net':0})
    for t in all_trades:
        yr = pd.Timestamp(t['entry_date']).year
        yd[yr]['n']   += 1
        yd[yr]['w']   += 1 if t['win'] else 0
        yd[yr]['net'] += t['net_rs']
    for yr in sorted(yd):
        s = yd[yr]
        print(f"    {yr}  n={s['n']:>4}  WR={s['w']/s['n']*100:.0f}%  Net=Rs{s['net']:+,.0f}")

    # Top stocks
    sym_stats = defaultdict(lambda: {'n':0,'w':0,'gw':0,'gl':0})
    for t in all_trades:
        s = sym_stats[t.get('symbol', 'UNKNOWN')]
        s['n'] += 1
        if t['win']: s['w'] += 1; s['gw'] += t['net_rs']
        else: s['gl'] += abs(t['net_rs'])
    top = sorted(sym_stats.items(),
                 key=lambda x: x[1]['gw']/(x[1]['gl'] or 1), reverse=True)[:15]
    print(f"\n  TOP 15 STOCKS (PF):")
    print(f"  {'Symbol':<14} {'N':>4} {'WR':>6} {'PF':>6} {'Net':>10}")
    print("  " + "-"*46)
    for sym, s in top:
        pf_s = f"{s['gw']/(s['gl'] or 0.01):.2f}"
        wr_s = s['w']/s['n']*100
        net_s = s['gw'] - s['gl']
        print(f"  {sym:<14} {s['n']:>4} {wr_s:>5.0f}%  {pf_s:>6}  Rs{net_s:>+8,.0f}")


CACHE_DIR_10YR = ROOT / "data" / "nr7_cache_10yr"
CACHE_DIR_10YR.mkdir(parents=True, exist_ok=True)


def _fetch_daily_chunked(kite, token: int, days: int) -> pd.DataFrame:
    """Fetch daily history in 1900-calendar-day chunks (Kite hard limit ~2000 days)."""
    CHUNK  = 1900
    end    = datetime.now()
    t_start = end - timedelta(days=days)
    frames, chunk_end = [], end

    while chunk_end > t_start:
        chunk_start = max(chunk_end - timedelta(days=CHUNK), t_start)
        try:
            rows = kite.historical_data(token, chunk_start, chunk_end,
                                        "day", continuous=False, oi=False)
            if rows:
                frames.append(pd.DataFrame(rows))
        except Exception:
            pass
        chunk_end = chunk_start - timedelta(days=1)
        time.sleep(0.12)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames[::-1]).rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df[["Open", "High", "Low", "Close", "Volume"]]


def download_and_cache_dir(kite, universe, lookback_days,
                           cache_dir=None, min_rows=1500):
    """Download to cache_dir using chunked fetch; skips stocks already >= min_rows."""
    cdir = Path(cache_dir) if cache_dir else CACHE_DIR_10YR
    cdir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {len(universe)} stocks -> {cdir.name} ...")
    ok = 0
    for i, stock in enumerate(universe):
        sym   = stock["symbol"]
        fpath = cdir / f"{sym}.csv"
        if fpath.exists():
            try:
                n = sum(1 for _ in open(fpath)) - 1
                if n >= min_rows:
                    ok += 1; continue
            except Exception:
                pass
        df = _fetch_daily_chunked(kite, stock["token"], lookback_days)
        if len(df) >= MIN_TRADING_DAYS:
            df.to_csv(fpath)
            ok += 1
        time.sleep(0.06)
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(universe)} done...")
    print(f"Cache: {ok}/{len(universe)} stocks ready in {cdir.name}")


def load_from_cache_dir(universe, cache_dir=None) -> dict:
    cdir = Path(cache_dir) if cache_dir else CACHE_DIR_10YR
    out = {}
    for stock in universe:
        sym   = stock["symbol"]
        fpath = cdir / f"{sym}.csv"
        if not fpath.exists():
            continue
        df = pd.read_csv(fpath, index_col="Date", parse_dates=True)
        if len(df) >= MIN_TRADING_DAYS:
            out[sym] = df
    return out


def _nifty500_symbols():
    return {
        "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC",
        "KOTAKBANK","LT","AXISBANK","BAJFINANCE","BHARTIARTL","ASIANPAINT",
        "MARUTI","HCLTECH","SUNPHARMA","TITAN","WIPRO","ULTRACEMCO","NESTLEIND",
        "ADANIENT","ADANIPORTS","NTPC","POWERGRID","COALINDIA","ONGC","JSWSTEEL",
        "TATAMOTORS","TATACONSUM","BAJAJFINSV","SBILIFE","HDFCLIFE","DIVISLAB",
        "CIPLA","TECHM","BRITANNIA","GRASIM","APOLLOHOSP","EICHERMOT","HINDALCO",
        "BAJAJ-AUTO","DRREDDY","M&M","SBIN","INDUSINDBK","BPCL","TATASTEEL",
        "UPL","HEROMOTOCO","SHREECEM","ADANIGREEN","AMBUJACEM","AUROPHARMA",
        "BANDHANBNK","BERGEPAINT","BIOCON","BOSCHLTD","CHOLAFIN","COLPAL",
        "CONCOR","CUMMINSIND","DABUR","DLF","GODREJCP","GODREJPROP","HAVELLS",
        "ICICIGI","ICICIPRULI","INDIGO","IOC","IRCTC","LUPIN","MCDOWELL-N",
        "MFSL","MOTHERSON","MUTHOOTFIN","NAUKRI","PAGEIND","PETRONET","PIDILITIND",
        "PIIND","RECLTD","SIEMENS","SRF","TORNTPHARM","TRENT","VEDL","VOLTAS",
        "ZOMATO","NYKAA","POLICYBZR","ABCAPITAL","ABFRL","AIAENG","AJANTPHARMA",
        "ALKEM","APLLTD","ASTRAL","ATUL","AUBANK","BATAINDIA","BAYERCROP",
        "BLUESTARCO","CANBK","CANFINHOME","CEATLTD","COFORGE","CROMPTON",
        "CSBBANK","DEEPAKNI","EDELWEISS","ENGINERSIN","ESCORTS","EXIDEIND",
        "FEDERALBNK","FORTIS","GAIL","GLENMARK","GLAXO","GMRAIRPORT","GNFC",
        "GODREJAGRO","GRANULES","GSFC","GUJGASLTD","HAPPSTMNDS","HIKAL",
        "HINDCOPPER","HINDZINC","IDFCFIRSTB","IEX","IIFL","INDHOTEL",
        "IPCALAB","JKCEMENT","JKLAKSHMI","JUBLFOOD","JUBLINGREA","KANSAINER",
        "KARURVYSYA","KEC","KEI","KPITTECH","L&TFH","LALPATHLAB","LAURUSLABS",
        "LICHSGFIN","LTTS","LUXIND","MANAPPURAM","MARICO","MAXHEALTH",
        "METROPOLIS","MPHASIS","MRF","MRPL","NATIONALUM","NBCC","NCC",
        "NETWORK18","NHPC","NMDC","OBEROIRLTY","OFSS","OIL","PERSISTENT",
        "PFIZER","PHOENIXLTD","POLYCAB","PRAJ","PRESTIGE","PRINCEPIPE",
        "PVRINOX","RAMCOCEM","RAYMOND","REDINGTON","RITES","SIEMENS","SKFINDIA",
        "SOBHA","SONACOMS","SPARC","STARCEMENT","SUPREMEIND","TANLA","TATACHEM",
        "TATACOMM","TATAELXSI","TATAPOWER","TCNSBRANDS","THERMAX","TIMKEN",
        "TTKPRESTIG","TVSMOTORS","UJJIVANSFB","UNIONBANK","V2RETAIL","VGUARD",
        "VINATIORGA","VSTIND","WELCORP","WELSPUNIND","ZEEL","AARTIIND","ABB",
        "AFFLE","ALKYLAMINE","AMARAJABAT","AMBER","ANGELONE","APTUS","ASHOKLEY",
        "ASTERDM","ATGL","BAJAJHLDNG","BALRAMCHIN","BEML","BHEL","BIKAJI",
        "BLUEDART","BSOFT","CAMPUS","CDSL","CMSINFO","CRISIL","CYIENT",
        "DATAPATTNS","DCMSHRIRAM","DEEPAKNTR","DELTACORP","DHANUKA","DMART",
        "ELECON","ELGIEQUIP","EMAMILTD","EMCURE","EPIGRAL","EQUITASBNK",
        "FINEORG","FIVESTAR","GABRIEL","GALAXYSURF","GEOJITFSL","GHCL",
        "GICRE","GLOBALHEALT","GODFRYPHLP","GRINDWELL","GUJALKALI","GUJFLUORO",
        "HEG","HEIDELBERG","HINDPETRO","HOMEFIRST","HUDCO","IGARASHI","INDIAMART",
        "INDIANB","INGERRAND","INOXGREEN","INSECTICID","INTELLECT","IONEXCHANG",
        "IRCON","IRFC","ISEC","JBCHEPHARM","JKPAPER","JYOTHYLAB","KALPATPOWR",
        "KIMS","KPIGREEN","KRSNAA","KSENG","KSOLVES","LEMONTREE","LICI","LTIM",
        "MACROTECH","MAHINDCIE","MAHLIFE","MANINFRA","MANYAVAR","MARKSANS",
        "MASTEK","MEDANTA","MEDPLUS","MIDHANI","MINDACORP","MOIL","MOTILALOFS",
        "MSTCLTD","NAVINFLUOR","NESCO","NEWGEN","NILKAMAL","NOCIL","NUCLEUS",
        "ORIENTCEM","PATELENG","PCBL","PENIND","PGHL","PNCINFRA","PNBHOUSING",
        "POLYMED","POWERMECH","PREMEXPLN","PRICOLLTD","PSPPROJECT","PUNJABCHEM",
        "QUESS","RACL","RAILTEL","RAIN","RAJRATAN","RALLIS","RBLBANK","ROSSARI",
        "ROUTE","RPGLIFE","SADBHAV","SASKEN","SBFC","SBICARD","SEQUENT",
        "SHANTIGEAR","SHILPAMED","SHOPERSTOP","SKFINDIA","SNOWMAN","SPANDANA",
        "SSWL","STLTECH","STOVEKRAFT","SUBROS","SUMICHEM","SUNDRMFAST","SUNTV",
        "SUPRAJIT","SURYAROSNI","SYMPHONY","TATAINVEST","TCIEXP","TEAMLEASE",
        "TEJASNET","TIINDIA","TITAGARH","TRIVENI","UNIPARTS","UTIAMC","VAIBHAVGBL",
        "VAKRANGEE","VEDANT","VENUSPIPES","VIP","VIRINCHI","WABCOINDIA",
        "WEBELSOLAR","WENDT","WESTLIFE","YASHO","ZENSARTECH","COLPAL","BBTC",
        "WHIRLPOOL","HINDCOPPER","RATNAMANI","CLEAN","ASTRAZEN","TTML","IOB",
        "SSWL","MOTILALOFS","JKCEMENT","POLYCAB","FORTIS","HEROMOTOCO",
        "INSECTICID","CROMPTON","SCHAEFFLER","SAFARI","RAYMOND","ROUTE",
        "LTIM","ANGELONE","CDSL","IRFC","IRCON","RAILTEL","HUDCO",
    }


if __name__ == "__main__":
    use_cache = "--cached" in sys.argv
    run_backtest(use_cache=use_cache)
