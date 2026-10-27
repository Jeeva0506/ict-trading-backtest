"""
Dual-Instrument ORB — Nifty + BankNifty at 2.5% risk each
===========================================================
Target: 200 trades/yr combined, 50%+ annual return on Rs10L capital

BankNifty data: synthetic 1m series calibrated to real BankNifty daily OHLCV
  - Intraday shape derived from Nifty 1m (beta=1.3)
  - 30% independent noise for realistic decorrelation
  - Daily open/close anchor to real BankNifty levels
  - Note: Replace with real BankNifty 1m CSV when available

BankNifty params:
  - lot_size=15, strike_step=100
  - IV = India VIX * 1.25 (BankNifty implied vol > Nifty)
  - Same BEP/trail exit logic as Nifty
  - 2.5% risk per trade (Rs25,000)
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd

from data.download_data import load_all
from data.download_intraday import load_nifty1m_historical, resample_from_1m
from strategies.orb_structural_strategy import STRUCTURAL_PARAMS, generate_structural_signals
from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics

CAPITAL      = 1_000_000
RISK_PER_INS = 25_000     # 2.5% per instrument
RESULTS      = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS, exist_ok=True)

IS_END    = "2018-12-31"
OOS_START = "2019-01-01"
OOS_END   = "2024-03-27"

BASELINE_FILTERS = {"fvg", "sweep", "bar_quality", "ema_slope"}

# BankNifty-specific params
BN_PARAMS = dict(STRUCTURAL_PARAMS)
BN_PARAMS.update({
    "lot_size"   : 15,
    "strike_step": 100,
    "max_lots"   : 40,   # BankNifty lots are smaller; allow more
})
BN_IV_MULT = 1.25   # BankNifty implied vol ≈ 1.25× India VIX

NIFTY_PARAMS = dict(STRUCTURAL_PARAMS)
NIFTY_PARAMS["max_lots"] = 15   # allow up to 15 lots at 2.5% risk


# ── Synthetic BankNifty 1m generator ──────────────────────────────────────────

def make_banknifty_1m(df_nifty1m: pd.DataFrame,
                       df_bn_daily: pd.DataFrame,
                       beta: float = 1.3,
                       noise_frac: float = 0.30,
                       seed: int = 42) -> pd.DataFrame:
    """
    Generate synthetic BankNifty 1m OHLCV bars from Nifty 1m + BankNifty daily.

    Method per trading day:
      1. Normalize Nifty's cumulative intraday return from open.
      2. BankNifty return = beta * Nifty_return + independent_noise.
      3. Rescale so final bar matches actual BankNifty daily close.
      4. H/L derived proportionally from Nifty's bar H/L.
    """
    rng = np.random.default_rng(seed)

    bn_daily = df_bn_daily.copy()
    bn_daily.index = pd.DatetimeIndex(bn_daily.index).normalize()

    bars = []
    for date_val, nifty_day in df_nifty1m.groupby(df_nifty1m.index.date):
        ts = pd.Timestamp(date_val)
        if ts not in bn_daily.index:
            continue
        row = bn_daily.loc[ts]

        n       = len(nifty_day)
        if n < 5:
            continue

        ni_open = float(nifty_day["Open"].iloc[0])
        if ni_open <= 0:
            continue

        # Nifty: cumulative return from open & bar H/L range
        ni_cum  = (nifty_day["Close"].values - ni_open) / ni_open
        ni_hl   = (nifty_day["High"].values - nifty_day["Low"].values) / ni_open

        bn_open_d  = float(row["Open"])
        bn_close_d = float(row["Close"])
        bn_high_d  = float(row["High"])
        bn_low_d   = float(row["Low"])

        # Independent BankNifty noise (zero-mean Brownian)
        ref_vol    = max(abs(ni_cum[-1]) * noise_frac, 0.001)
        noise_step = rng.normal(0, ref_vol / np.sqrt(n), n)
        noise      = np.cumsum(noise_step)
        noise     -= noise.mean()

        # BankNifty cumulative returns
        bn_cum    = beta * ni_cum + noise
        bn_closes = bn_open_d * (1.0 + bn_cum)

        # Anchor: rescale so last bar = actual BankNifty close
        if bn_closes[-1] != 0:
            bn_closes *= bn_close_d / bn_closes[-1]

        # H/L proportional to Nifty bar range × beta, clipped to daily extremes
        hl_half  = ni_hl * bn_closes * beta / 2.0
        bn_highs = np.minimum(bn_closes + hl_half, bn_high_d)
        bn_lows  = np.maximum(bn_closes - hl_half, bn_low_d)

        # Opens: previous bar's close; first bar = daily open
        bn_opens      = np.empty(n)
        bn_opens[0]   = bn_open_d
        bn_opens[1:]  = bn_closes[:-1]

        bars.append(pd.DataFrame({
            "Open" : bn_opens,
            "High" : bn_highs,
            "Low"  : bn_lows,
            "Close": bn_closes,
            "Volume": 0,
        }, index=nifty_day.index))

    if not bars:
        return pd.DataFrame()
    df = pd.concat(bars).sort_index()
    print(f"  BankNifty synthetic 1m: {len(df):,} bars  "
          f"{df.index[0].date()} to {df.index[-1].date()}")
    return df


# ── Backtest engine — 3-phase BEP + trail exit ────────────────────────────────

def _run_bep(df_intraday, df_daily, df_vix, sigs,
             params,
             sl_pct=0.25, bep_activate=0.25, bep_lock=0.01,
             trail_activate=0.50, trail_pct=0.20,
             risk_per_trade=RISK_PER_INS,
             iv_mult=1.0,
             instrument="NIFTY",
             start=None, end=None):
    p = dict(params)
    p["risk_per_trade"] = risk_per_trade
    p["sl_pct"]         = sl_pct

    if start:
        sigs = sigs[sigs.index >= pd.Timestamp(start)]
    if end:
        sigs = sigs[sigs.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    if sigs.empty:
        return []

    df = df_intraday.loc[sigs.index].copy()
    df["signal"]   = sigs["signal"]
    df["vix_val"]  = sigs["vix_val"]
    df["ep_close"] = sigs["entry_price"]

    trades, active_date = [], None
    exit_h, exit_m = 15, 15

    i = 0
    while i < len(df):
        row      = df.iloc[i]
        bar_date = df.index[i].date()
        if bar_date == active_date:
            i += 1; continue
        if row["signal"] not in ("call", "put") or pd.isna(row["vix_val"]):
            i += 1; continue

        opt_type   = row["signal"]
        vix        = row["vix_val"] * iv_mult    # BankNifty IV adjustment
        entry_spot = row["ep_close"]
        K          = atm_strike(entry_spot, step=p["strike_step"])
        dte        = p["entry_dte"]
        ep         = bs_price(entry_spot, K, dte / 365, p["rfr"], vix / 100, opt_type)
        if ep < 1.0:
            i += 1; continue

        max_b = risk_per_trade / sl_pct
        lots  = max(1, min(int(max_b / (ep * p["lot_size"])), p["max_lots"]))
        qty   = lots * p["lot_size"]

        hard_sl      = ep * (1 - sl_pct)
        bep_thresh   = ep * (1 + bep_activate)
        bep_sl       = ep * (1 + bep_lock)
        trail_thresh = ep * (1 + trail_activate)
        tp_px        = ep * 5.0

        effective_sl = hard_sl
        bep_active   = False
        trail_active = False
        peak_prem    = ep

        active_date   = bar_date
        exit_prem     = None
        exit_str      = None

        i += 1
        while i < len(df):
            nrow = df.iloc[i]; nts = df.index[i]
            if nts.date() != bar_date: break
            t = nts.time()
            if t.hour > exit_h or (t.hour == exit_h and t.minute >= exit_m):
                exit_prem = bs_price(nrow["Close"], K, dte/365, p["rfr"], vix/100, opt_type)
                exit_str  = "3:15"; i += 1; break

            if opt_type == "call":
                opt_h = bs_price(nrow["High"], K, dte/365, p["rfr"], vix/100, "call")
                opt_l = bs_price(nrow["Low"],  K, dte/365, p["rfr"], vix/100, "call")
            else:
                opt_h = bs_price(nrow["Low"],  K, dte/365, p["rfr"], vix/100, "put")
                opt_l = bs_price(nrow["High"], K, dte/365, p["rfr"], vix/100, "put")

            if opt_h > peak_prem:
                peak_prem = opt_h

            if not bep_active and peak_prem >= bep_thresh:
                bep_active   = True
                effective_sl = max(effective_sl, bep_sl)

            if not trail_active and peak_prem >= trail_thresh:
                trail_active = True

            if trail_active:
                effective_sl = max(effective_sl, peak_prem * (1 - trail_pct))

            if opt_h >= tp_px:
                exit_prem, exit_str = tp_px, "TP"; i += 1; break
            if opt_l <= effective_sl:
                exit_prem = effective_sl
                if trail_active and effective_sl > bep_sl:
                    exit_str = "Trail SL"
                elif bep_active:
                    exit_str = "BEP"
                else:
                    exit_str = "SL"
                i += 1; break
            i += 1

        if exit_str is None:
            last = df[df.index.date == bar_date].iloc[-1]
            exit_prem = bs_price(last["Close"], K, dte/365, p["rfr"], vix/100, opt_type)
            exit_str  = "3:15"

        sell    = max(exit_prem, 0.05)
        gross   = (sell - ep) * qty
        cost    = round_trip_cost(ep, sell, qty)
        net_pnl = gross - cost

        trades.append({
            "instrument" : instrument,
            "entry_date" : str(bar_date),
            "exit_date"  : str(bar_date),
            "direction"  : opt_type,
            "entry_prem" : round(ep, 2),
            "exit_prem"  : round(sell, 2),
            "peak_prem"  : round(peak_prem, 2),
            "lots"       : lots,
            "exit_type"  : exit_str,
            "pnl_net"    : round(net_pnl, 2),
            "vix"        : round(row["vix_val"], 2),
        })

    return trades


# ── Stats helpers ─────────────────────────────────────────────────────────────

def _m(trades, capital=CAPITAL):
    if not trades:
        return {"n_trades": 0, "win_rate_pct": 0, "profit_factor": 0,
                "net_pnl": 0, "exit_breakdown": {}, "error": "no trades"}
    return compute_metrics(trades, capital)


def _yearly(trades, capital=CAPITAL):
    if not trades:
        return []
    df = pd.DataFrame(trades)
    df["year"] = pd.to_datetime(df["entry_date"]).dt.year
    rows = []
    for yr, grp in df.groupby("year"):
        t_list = grp.to_dict("records")
        m = _m(t_list, capital)
        net = m.get("net_pnl", 0)
        rows.append({
            "year": yr,
            "n"   : m.get("n_trades", 0),
            "wr"  : m.get("win_rate_pct", 0),
            "pf"  : m.get("profit_factor", 0),
            "net" : net,
            "ret" : net / capital * 100,
        })
    return rows


def _print_summary(trades, label, capital=CAPITAL, years=9):
    m = _m(trades, capital)
    if "error" in m:
        print(f"  {label}: no trades"); return
    eb   = m.get("exit_breakdown", {})
    gate = "[PASS]" if m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55 else ""
    print(f"  {label:<42} n={m['n_trades']:>4} ({m['n_trades']/years:.0f}/yr)  "
          f"WR={m['win_rate_pct']:.1f}%  PF={m['profit_factor']:.3f}  "
          f"Net=Rs{m['net_pnl']:,.0f} ({m['net_pnl']/years/capital*100:.1f}%/yr)  "
          f"SL={eb.get('SL',0)} BEP={eb.get('BEP',0)} "
          f"TSL={eb.get('Trail SL',0)} EOD={eb.get('3:15',0)}  {gate}")


def _print_yearly(yearly, label=""):
    if label:
        print(f"\n  [{label}]")
    print(f"  {'Year':>4}  {'n':>4}  {'WR':>6}  {'PF':>5}  {'Net P&L':>10}  "
          f"{'Return%':>8}  {'100t?':>6}  {'50%?':>5}")
    print("  " + "-" * 72)
    years_ok_t = years_ok_r = 0
    total_net = 0
    for r in yearly:
        ok_t = "OK" if r["n"] >= 100 else "LOW"
        ok_r = "OK" if r["ret"] >= 50 else "LOW"
        if ok_t == "OK": years_ok_t += 1
        if ok_r == "OK": years_ok_r += 1
        total_net += r["net"]
        print(f"  {r['year']:>4}  {r['n']:>4}  {r['wr']:>5.1f}%  "
              f"{r['pf']:>5.3f}  Rs{r['net']:>9,.0f}  "
              f"{r['ret']:>7.1f}%  {ok_t:>6}  {ok_r:>5}")
    avg_n   = sum(r["n"]   for r in yearly) / len(yearly) if yearly else 0
    avg_ret = sum(r["ret"] for r in yearly) / len(yearly) if yearly else 0
    n_full  = len(yearly)
    print(f"  {'AVG':>4}  {avg_n:>4.0f}  {'':>6}  {'':>5}  "
          f"Rs{total_net/n_full:>9,.0f}  {avg_ret:>7.1f}%")
    print(f"  Years with 100+ trades: {years_ok_t}/{n_full}  |  "
          f"Years with 50%+ return: {years_ok_r}/{n_full}")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    print("=" * 80)
    print("  DUAL-INSTRUMENT ORB — Nifty + BankNifty @ 2.5% risk each")
    print(f"  Capital Rs{CAPITAL:,}  |  Risk: Rs{RISK_PER_INS:,}/instrument = 5% combined max")
    print(f"  BEP@+25% | Trail 20% below peak, activates @+50%")
    print("=" * 80)

    # ── Load data ──────────────────────────────────────────────────────────────
    print("\n[1] Loading data ...")
    daily    = load_all()
    df_daily = daily["nifty"]
    df_bn_d  = daily["banknifty"]
    df_vix   = daily["indiavix"]

    df_1m  = load_nifty1m_historical()
    df_5m  = resample_from_1m(df_1m, "5m")
    print(f"  Nifty 5m: {len(df_5m):,} bars")

    # Build synthetic BankNifty 5m
    print("  Building synthetic BankNifty 1m ...")
    df_bn_1m = make_banknifty_1m(df_1m, df_bn_d, beta=1.3, noise_frac=0.30)
    df_bn_5m = resample_from_1m(df_bn_1m, "5m")
    print(f"  BankNifty 5m: {len(df_bn_5m):,} bars")

    # ── Generate signals ───────────────────────────────────────────────────────
    print("\n[2] Generating signals (baseline filters: fvg+sweep+bq+ema_slope) ...")
    sigs_nf = generate_structural_signals(df_5m,    df_daily, df_vix,
                                           active_filters=BASELINE_FILTERS)
    sigs_bn = generate_structural_signals(df_bn_5m, df_daily, df_vix,
                                           active_filters=BASELINE_FILTERS)

    n_nf = sigs_nf["signal"].notna().sum()
    n_bn = sigs_bn["signal"].notna().sum()
    print(f"  Nifty signals    : {n_nf} ({n_nf/9:.0f}/yr)")
    print(f"  BankNifty signals: {n_bn} ({n_bn/9:.0f}/yr)")

    # Days when both signal — overlap metric
    nf_days = set(sigs_nf[sigs_nf["signal"].notna()].index.date)
    bn_days = set(sigs_bn[sigs_bn["signal"].notna()].index.date)
    overlap = nf_days & bn_days
    print(f"  Same-day signals : {len(overlap)} days ({len(overlap)/9:.0f}/yr)  "
          f"overlap={len(overlap)/max(len(nf_days),1)*100:.0f}%")

    # ── Run backtests ──────────────────────────────────────────────────────────
    print("\n[3] Running backtests — BEP@+25% / Trail@+50% 20% ...")

    BEP_KW = dict(sl_pct=0.25, bep_activate=0.25, bep_lock=0.01,
                  trail_activate=0.50, trail_pct=0.20)

    t_nf = _run_bep(df_5m,    df_daily, df_vix, sigs_nf,
                    NIFTY_PARAMS, risk_per_trade=RISK_PER_INS,
                    iv_mult=1.0, instrument="NIFTY", **BEP_KW)

    t_bn = _run_bep(df_bn_5m, df_daily, df_vix, sigs_bn,
                    BN_PARAMS, risk_per_trade=RISK_PER_INS,
                    iv_mult=BN_IV_MULT, instrument="BANKNIFTY", **BEP_KW)

    t_combined = t_nf + t_bn   # merge all trades

    # ── Summary table ──────────────────────────────────────────────────────────
    print()
    print(f"  {'Strategy':<42} {'n (/yr)':>10}  {'WR':>6}  {'PF':>5}  {'Net (/yr %)':>16}  Gate")
    print("  " + "-" * 90)
    _print_summary(t_nf,       "Nifty only  @ 2.5% risk")
    _print_summary(t_bn,       "BankNifty   @ 2.5% risk (synthetic)")
    _print_summary(t_combined, "COMBINED    @ 2.5% each (5% max/day)")

    # ── Yearly breakdown ───────────────────────────────────────────────────────
    print("\n[4] Yearly breakdown — COMBINED portfolio ...")
    _print_yearly(_yearly(t_combined), "Nifty + BankNifty combined @ 2.5% risk each")

    # ── IS / OOS ──────────────────────────────────────────────────────────────
    print("\n[5] IS / OOS validation ...")
    print()
    for period, (s, e), yrs in [
        ("Full  2015-2024", ("2015-01-01", OOS_END),   9),
        ("IS    2015-2018", ("2015-01-01", IS_END),    4),
        ("OOS   2019-2024", (OOS_START,   OOS_END),    6),
    ]:
        tnf = _run_bep(df_5m,    df_daily, df_vix, sigs_nf, NIFTY_PARAMS,
                       risk_per_trade=RISK_PER_INS, iv_mult=1.0,
                       instrument="NIFTY", start=s, end=e, **BEP_KW)
        tbn = _run_bep(df_bn_5m, df_daily, df_vix, sigs_bn, BN_PARAMS,
                       risk_per_trade=RISK_PER_INS, iv_mult=BN_IV_MULT,
                       instrument="BANKNIFTY", start=s, end=e, **BEP_KW)
        tc = tnf + tbn
        m  = _m(tc)
        if "error" in m:
            print(f"  {period}: no trades"); continue
        eb   = m.get("exit_breakdown", {})
        gate = "[PASS]" if m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55 else ""
        print(f"  {period}:  n={m['n_trades']} ({m['n_trades']/yrs:.0f}/yr)  "
              f"WR={m['win_rate_pct']:.1f}%  PF={m['profit_factor']:.3f}  "
              f"Net=Rs{m['net_pnl']:,.0f} ({m['net_pnl']/yrs/CAPITAL*100:.1f}%/yr)  "
              f"SL={eb.get('SL',0)} BEP={eb.get('BEP',0)} "
              f"TSL={eb.get('Trail SL',0)} EOD={eb.get('3:15',0)}  {gate}")

    # ── Correlation analysis ───────────────────────────────────────────────────
    print("\n[6] Trade overlap analysis ...")
    df_nf = pd.DataFrame(t_nf)
    df_bn = pd.DataFrame(t_bn)
    if not df_nf.empty and not df_bn.empty:
        same_days = set(df_nf["entry_date"]) & set(df_bn["entry_date"])
        print(f"  Days with BOTH instruments trading : {len(same_days)} "
              f"({len(same_days)/9:.0f}/yr)")
        print(f"  Days with Nifty only               : "
              f"{len(set(df_nf['entry_date']) - set(df_bn['entry_date']))} "
              f"({(len(set(df_nf['entry_date']))-len(same_days))/9:.0f}/yr)")
        print(f"  Days with BankNifty only           : "
              f"{len(set(df_bn['entry_date']) - set(df_nf['entry_date']))} "
              f"({(len(set(df_bn['entry_date']))-len(same_days))/9:.0f}/yr)")

        # On same days, how often do both agree on direction?
        nf_day_dir = df_nf.groupby("entry_date")["direction"].first()
        bn_day_dir = df_bn.groupby("entry_date")["direction"].first()
        common     = nf_day_dir.index.intersection(bn_day_dir.index)
        agree_pct  = (nf_day_dir[common] == bn_day_dir[common]).mean() * 100
        print(f"  Same direction on overlap days     : {agree_pct:.0f}%")
        print(f"  Effective daily risk (both same dir): Rs{RISK_PER_INS*2:,} (5%)")

    # ── Save combined trades ───────────────────────────────────────────────────
    if t_combined:
        out = os.path.join(RESULTS, "orb_dual_trades.csv")
        pd.DataFrame(t_combined).sort_values("entry_date").to_csv(out, index=False)
        print(f"\n  Saved: {out}")

    print("\n" + "=" * 80)
    print("  DONE — Target: 200+ trades/yr combined, 50%+ annual return")
    print("=" * 80)
