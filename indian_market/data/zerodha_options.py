"""
Zerodha real options premium fetcher.

Downloads 1-minute OHLCV for NIFTY ATM options (CE + PE) for active/recent
instruments. Used to:
  1. Validate synthetic BS pricing against real market premiums
  2. Build forward-looking real premium database for live backtesting

Limitation: Zerodha only provides instrument tokens for currently listed
contracts. Expired contracts are removed from the instruments list, so
historical backtesting beyond ~2 months is not feasible with real data.

Usage:
    python data/zerodha_options.py                    # fetch recent ATM data
    python data/zerodha_options.py --validate         # compare real vs BS
"""
import os, sys, time, json
from pathlib import Path
from datetime import datetime, timedelta, date

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
ROOT_DIR = DATA_DIR.parent
CACHE_DIR = DATA_DIR / "options_cache"

sys.path.insert(0, str(ROOT_DIR))
from data.zerodha_data import _get_kite, _download_chunks, _filter_market_hours


def get_nifty_option_instruments(kite=None):
    """Fetch all currently listed NIFTY option instruments from Zerodha."""
    if kite is None:
        kite = _get_kite()
    instruments = kite.instruments("NFO")
    nifty_opts = [
        i for i in instruments
        if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")
    ]
    df = pd.DataFrame(nifty_opts)
    df["expiry"] = pd.to_datetime(df["expiry"])
    return df, kite


def find_atm_options(instruments_df: pd.DataFrame, spot: float,
                     expiry_date=None, strike_step: int = 50):
    """Find ATM call and put for a given spot level and expiry."""
    atm_strike = round(spot / strike_step) * strike_step
    df = instruments_df

    if expiry_date is not None:
        df = df[df["expiry"] == pd.Timestamp(expiry_date)]

    ce = df[(df["strike"] == atm_strike) & (df["instrument_type"] == "CE")]
    pe = df[(df["strike"] == atm_strike) & (df["instrument_type"] == "PE")]

    if ce.empty or pe.empty:
        return None, None, atm_strike

    return ce.iloc[0], pe.iloc[0], atm_strike


def download_option_1m(kite, instrument_token: int, start: str, end: str,
                       pause: float = 0.4) -> pd.DataFrame:
    """Download 1-minute data for a specific option instrument."""
    return _download_chunks(kite, instrument_token, "minute", start, end,
                            chunk_days=30, pause=pause)


def fetch_atm_premiums(spot_level: float = None, n_expiries: int = 3):
    """
    Fetch real 1m premium data for ATM options across nearest expiries.
    Returns dict of {expiry_date: {ce_df, pe_df, strike}}.
    """
    inst_df, kite = get_nifty_option_instruments()

    if spot_level is None:
        nifty_ltp = kite.ltp("NSE:NIFTY 50")
        spot_level = nifty_ltp["NSE:NIFTY 50"]["last_price"]
        print(f"  Current NIFTY spot: {spot_level:.2f}")

    expiries = sorted(inst_df["expiry"].unique())[:n_expiries]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    results = {}

    for exp in expiries:
        exp_date = exp.date() if hasattr(exp, "date") else exp
        print(f"\n  Expiry: {exp_date}")

        ce_inst, pe_inst, atm_strike = find_atm_options(inst_df, spot_level, exp)
        if ce_inst is None:
            print(f"    SKIP: No ATM instruments found for strike {atm_strike}")
            continue

        ce_token = int(ce_inst["instrument_token"])
        pe_token = int(pe_inst["instrument_token"])
        ce_symbol = ce_inst["tradingsymbol"]
        pe_symbol = pe_inst["tradingsymbol"]
        print(f"    ATM Strike: {atm_strike}")
        print(f"    CE: {ce_symbol} (token {ce_token})")
        print(f"    PE: {pe_symbol} (token {pe_token})")

        start_str = (datetime.today() - timedelta(days=60)).strftime("%Y-%m-%d")
        end_str = datetime.today().strftime("%Y-%m-%d")

        print(f"    Downloading CE 1m data...")
        ce_df = download_option_1m(kite, ce_token, start_str, end_str)
        ce_df = _filter_market_hours(ce_df) if not ce_df.empty else ce_df

        print(f"    Downloading PE 1m data...")
        pe_df = download_option_1m(kite, pe_token, start_str, end_str)
        pe_df = _filter_market_hours(pe_df) if not pe_df.empty else pe_df

        ce_path = CACHE_DIR / f"{ce_symbol}_1m.csv"
        pe_path = CACHE_DIR / f"{pe_symbol}_1m.csv"
        if not ce_df.empty:
            ce_df.to_csv(ce_path)
            print(f"    Saved: {ce_path.name} ({len(ce_df)} bars)")
        if not pe_df.empty:
            pe_df.to_csv(pe_path)
            print(f"    Saved: {pe_path.name} ({len(pe_df)} bars)")

        results[str(exp_date)] = {
            "strike": atm_strike,
            "ce_symbol": ce_symbol,
            "pe_symbol": pe_symbol,
            "ce_bars": len(ce_df),
            "pe_bars": len(pe_df),
            "ce_df": ce_df,
            "pe_df": pe_df,
        }

    return results


def validate_bs_vs_real(results: dict):
    """
    Compare Black-Scholes synthetic premiums against real market premiums.
    Uses the spot data and VIX to compute BS prices, then compares with
    real option premiums at each minute bar.
    """
    from backtest.option_pricer import bs_price
    from data.zerodha_data import load_zerodha_1m, load_zerodha_vix

    nifty_1m = load_zerodha_1m("nifty")
    vix_daily = load_zerodha_vix()

    print("\n" + "=" * 70)
    print("  BS vs REAL PREMIUM VALIDATION")
    print("=" * 70)

    for exp_str, data in results.items():
        strike = data["strike"]
        ce_df = data["ce_df"]
        pe_df = data["pe_df"]
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()

        if ce_df.empty and pe_df.empty:
            continue

        print(f"\n  Expiry {exp_str} | Strike {strike}")

        for opt_type, real_df, label in [("call", ce_df, "CE"), ("put", pe_df, "PE")]:
            if real_df.empty:
                continue

            common_idx = nifty_1m.index.intersection(real_df.index)
            if len(common_idx) < 10:
                print(f"    {label}: Not enough overlapping bars ({len(common_idx)})")
                continue

            spots = nifty_1m.loc[common_idx, "Close"]
            reals = real_df.loc[common_idx, "Close"]

            bs_prices = []
            for ts, spot in spots.items():
                dte = max((exp_date - ts.date()).days, 0.5)
                vix_mask = vix_daily.index.date <= ts.date()
                vix_val = float(vix_daily.loc[vix_mask, "Close"].iloc[-1]) if vix_mask.any() else 15.0
                iv = vix_val / 100
                t = dte / 365
                bs_p = bs_price(spot, strike, t, 0.065, iv, opt_type)
                bs_prices.append(bs_p)

            bs_series = pd.Series(bs_prices, index=common_idx)
            error = reals - bs_series
            abs_error = error.abs()
            pct_error = (abs_error / reals.replace(0, np.nan)).dropna() * 100

            print(f"    {label} ({len(common_idx)} bars):")
            print(f"      Mean Real Premium  : Rs {reals.mean():>8.2f}")
            print(f"      Mean BS Premium    : Rs {bs_series.mean():>8.2f}")
            print(f"      Mean Abs Error     : Rs {abs_error.mean():>8.2f}")
            print(f"      Mean % Error       : {pct_error.mean():>6.1f}%")
            print(f"      Median % Error     : {pct_error.median():>6.1f}%")
            print(f"      BS Bias (real-BS)  : Rs {error.mean():>+8.2f}")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Zerodha real options data fetcher")
    parser.add_argument("--validate", action="store_true", help="Compare real vs BS premiums")
    parser.add_argument("--spot", type=float, help="Override spot level for ATM selection")
    parser.add_argument("--expiries", type=int, default=3, help="Number of expiries to fetch")
    args = parser.parse_args()

    print("=" * 70)
    print("  ZERODHA REAL OPTIONS PREMIUM FETCHER")
    print("=" * 70)

    results = fetch_atm_premiums(spot_level=args.spot, n_expiries=args.expiries)

    if args.validate and results:
        validate_bs_vs_real(results)

    print("\n  Done.")
