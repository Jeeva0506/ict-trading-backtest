"""
Adaptive-Risk ORB — Nifty Only
================================
Two risk tiers based on signal quality:

  A+ setup  : baseline filters + body_strength + vix_percentile + atr_tight
               Risk = 2% = Rs20,000/trade  (max_lots = 15)

  Regular   : baseline filters only (A+ conditions NOT all met)
               Risk = 1% = Rs10,000/trade  (max_lots = 8)

Exit (same for both tiers):
  Phase 1  Hard SL at -25%
  Phase 2  BEP slide at +25%   → SL moves to entry + 1%
  Phase 3  Trail at +50%       → trail 20% below running peak
  Safety-net TP at 5x

Target: 100+ trades/yr AND 50%+ annual return
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np

from data.download_data import load_all
from data.download_intraday import load_nifty1m_historical, resample_from_1m
from strategies.orb_structural_strategy import STRUCTURAL_PARAMS, generate_structural_signals
from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics

CAPITAL = 1_000_000
RESULTS = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS, exist_ok=True)

IS_END    = "2018-12-31"
OOS_START = "2019-01-01"
OOS_END   = "2024-03-27"

# Signal filter sets
BASELINE_FILTERS = {"fvg", "sweep", "bar_quality", "ema_slope"}
APLUS_EXTRA      = {"body_strength", "vix_percentile", "atr"}      # extras that make A+
APLUS_FILTERS    = BASELINE_FILTERS | APLUS_EXTRA

# Risk configuration
RISK_APLUS   = 20_000   # 2% of Rs10L
RISK_REGULAR = 10_000   # 1% of Rs10L
LOTS_APLUS   = 15       # max lots at 2% risk
LOTS_REGULAR = 8        # max lots at 1% risk

# Exit parameters (shared)
SL_PCT         = 0.25
BEP_ACTIVATE   = 0.25
BEP_LOCK       = 0.01
TRAIL_ACTIVATE = 0.50
TRAIL_PCT      = 0.20


# ── Signal classifier ─────────────────────────────────────────────────────────

def classify_signals(df_5m, df_daily, df_vix):
    """
    Returns a single signals DataFrame with 'quality' column: 'A+' or 'regular'.

    A+ = day fires a valid signal under APLUS_FILTERS (stricter set).
    Regular = day fires under BASELINE_FILTERS but NOT all A+ conditions met.

    No lookahead: all quality conditions use data available at entry time.
    """
    # Run both generators
    sigs_base  = generate_structural_signals(df_5m, df_daily, df_vix,
                                              active_filters=BASELINE_FILTERS)
    sigs_aplus = generate_structural_signals(df_5m, df_daily, df_vix,
                                              active_filters=APLUS_FILTERS)

    # A+ days = dates where the stricter set also fires a signal
    aplus_dates = set(
        sigs_aplus[sigs_aplus["signal"].notna()].index.normalize()
    )

    # Mark quality on the baseline signal frame
    sigs_base["quality"] = None
    mask = sigs_base["signal"].notna()
    sigs_base.loc[mask, "quality"] = sigs_base[mask].index.normalize().map(
        lambda d: "A+" if d in aplus_dates else "regular"
    )

    n_aplus   = (sigs_base["quality"] == "A+").sum()
    n_regular = (sigs_base["quality"] == "regular").sum()
    print(f"  A+ signals   : {n_aplus} ({n_aplus/9:.0f}/yr)  — 2% risk")
    print(f"  Regular      : {n_regular} ({n_regular/9:.0f}/yr)  — 1% risk")
    print(f"  Total signals: {n_aplus + n_regular} ({(n_aplus+n_regular)/9:.0f}/yr)")

    return sigs_base


# ── Backtest engine — 3-phase BEP + trail, adaptive risk ─────────────────────

def _run_adaptive(df_5m, df_daily, df_vix, sigs, start=None, end=None):
    p = dict(STRUCTURAL_PARAMS)

    if start:
        sigs = sigs[sigs.index >= pd.Timestamp(start)]
    if end:
        sigs = sigs[sigs.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    if sigs.empty:
        return []

    df = df_5m.loc[sigs.index].copy()
    df["signal"]   = sigs["signal"]
    df["vix_val"]  = sigs["vix_val"]
    df["ep_close"] = sigs["entry_price"]
    df["quality"]  = sigs["quality"]

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

        quality    = row["quality"]
        risk_amt   = RISK_APLUS if quality == "A+" else RISK_REGULAR
        max_lots   = LOTS_APLUS if quality == "A+" else LOTS_REGULAR

        opt_type   = row["signal"]
        vix        = row["vix_val"]
        entry_spot = row["ep_close"]
        K          = atm_strike(entry_spot, step=p["strike_step"])
        dte        = p["entry_dte"]
        ep         = bs_price(entry_spot, K, dte / 365, p["rfr"], vix / 100, opt_type)
        if ep < 1.0:
            i += 1; continue

        max_b = risk_amt / SL_PCT
        lots  = max(1, min(int(max_b / (ep * p["lot_size"])), max_lots))
        qty   = lots * p["lot_size"]

        hard_sl      = ep * (1 - SL_PCT)
        bep_thresh   = ep * (1 + BEP_ACTIVATE)
        bep_sl       = ep * (1 + BEP_LOCK)
        trail_thresh = ep * (1 + TRAIL_ACTIVATE)
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
                effective_sl = max(effective_sl, peak_prem * (1 - TRAIL_PCT))

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
            "entry_date": str(bar_date), "exit_date": str(bar_date),
            "direction" : opt_type,
            "quality"   : quality,
            "risk_amt"  : risk_amt,
            "entry_prem": round(ep, 2),
            "exit_prem" : round(sell, 2),
            "peak_prem" : round(peak_prem, 2),
            "lots"      : lots,
            "exit_type" : exit_str,
            "pnl_net"   : round(net_pnl, 2),
            "vix"       : round(vix, 2),
        })

    return trades


# ── Stats helpers ─────────────────────────────────────────────────────────────

def _m(trades):
    if not trades:
        return {"n_trades": 0, "win_rate_pct": 0.0, "profit_factor": 0.0,
                "net_pnl": 0, "exit_breakdown": {}, "error": "no trades"}
    return compute_metrics(trades, CAPITAL)


def _tier_stats(trades, tier=None):
    """Compute metrics for a specific tier (or all)."""
    subset = [t for t in trades if tier is None or t["quality"] == tier]
    return _m(subset)


def _yearly(trades, capital=CAPITAL):
    if not trades:
        return []
    df = pd.DataFrame(trades)
    df["year"] = pd.to_datetime(df["entry_date"]).dt.year
    rows = []
    for yr, grp in df.groupby("year"):
        t_list = grp.to_dict("records")
        m  = _m(t_list)
        ap = grp[grp["quality"] == "A+"]
        re = grp[grp["quality"] == "regular"]
        rows.append({
            "year"  : yr,
            "n"     : m.get("n_trades", 0),
            "n_ap"  : len(ap),
            "n_re"  : len(re),
            "wr"    : m.get("win_rate_pct", 0),
            "pf"    : m.get("profit_factor", 0),
            "net"   : m.get("net_pnl", 0),
            "ret"   : m.get("net_pnl", 0) / capital * 100,
            "net_ap": ap["pnl_net"].sum() if len(ap) else 0,
            "net_re": re["pnl_net"].sum() if len(re) else 0,
        })
    return rows


def _print_yearly(yearly, capital=CAPITAL):
    print(f"\n  {'Year':>4}  {'n':>4}  {'A+':>4}  {'Reg':>4}  "
          f"{'WR':>6}  {'PF':>5}  {'A+ P&L':>10}  {'Reg P&L':>10}  "
          f"{'Total P&L':>10}  {'Return%':>8}  {'Gate':>5}")
    print("  " + "-" * 98)
    years_100 = years_50 = 0
    total_net = 0
    for r in yearly:
        ok_t = r["n"] >= 100
        ok_r = r["ret"] >= 50
        if ok_t: years_100 += 1
        if ok_r: years_50  += 1
        total_net += r["net"]
        gate = "PASS" if ok_t and ok_r else ("100t" if ok_t else ("50%" if ok_r else ""))
        print(f"  {r['year']:>4}  {r['n']:>4}  {r['n_ap']:>4}  {r['n_re']:>4}  "
              f"{r['wr']:>5.1f}%  {r['pf']:>5.3f}  "
              f"Rs{r['net_ap']:>8,.0f}  Rs{r['net_re']:>8,.0f}  "
              f"Rs{r['net']:>8,.0f}  {r['ret']:>7.1f}%  {gate:>5}")
    n_yr   = len(yearly)
    avg_n  = sum(r["n"]   for r in yearly) / n_yr
    avg_r  = sum(r["ret"] for r in yearly) / n_yr
    print(f"  {'AVG':>4}  {avg_n:>4.0f}  {'':>4}  {'':>4}  "
          f"{'':>6}  {'':>5}  {'':>10}  {'':>10}  "
          f"Rs{total_net/n_yr:>8,.0f}  {avg_r:>7.1f}%")
    print(f"\n  Years with 100+ trades : {years_100}/{n_yr}")
    print(f"  Years with 50%+ return : {years_50}/{n_yr}")


def _print_tier_summary(trades):
    for tier, lbl in [("A+", "A+ (2% risk)"), ("regular", "Regular (1% risk)")]:
        subset = [t for t in trades if t["quality"] == tier]
        m = _m(subset)
        if "error" in m: continue
        eb  = m.get("exit_breakdown", {})
        yrs = 9
        gate = "[PASS]" if m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55 else ""
        print(f"  {lbl:<22} n={m['n_trades']:>4} ({m['n_trades']/yrs:.0f}/yr)  "
              f"WR={m['win_rate_pct']:.1f}%  PF={m['profit_factor']:.3f}  "
              f"Net=Rs{m['net_pnl']:,.0f} ({m['net_pnl']/yrs/CAPITAL*100:.1f}%/yr)  "
              f"SL={eb.get('SL',0)} BEP={eb.get('BEP',0)} "
              f"TSL={eb.get('Trail SL',0)} EOD={eb.get('3:15',0)}  {gate}")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    print("=" * 80)
    print("  ADAPTIVE-RISK ORB — Nifty Only")
    print(f"  Capital Rs{CAPITAL:,}  |  A+ = 2% risk (Rs{RISK_APLUS:,})  |  "
          f"Regular = 1% risk (Rs{RISK_REGULAR:,})")
    print(f"  Exit: BEP@+{BEP_ACTIVATE:.0%}  Trail {TRAIL_PCT:.0%} below peak @+{TRAIL_ACTIVATE:.0%}")
    print("=" * 80)

    # ── Load data ──────────────────────────────────────────────────────────────
    print("\n[1] Loading data ...")
    daily    = load_all()
    df_daily = daily["nifty"]
    df_vix   = daily["indiavix"]
    df_1m    = load_nifty1m_historical()
    df_5m    = resample_from_1m(df_1m, "5m")
    print(f"  5m: {len(df_5m):,} bars  {df_5m.index[0].date()} to {df_5m.index[-1].date()}")

    # ── Classify signals ───────────────────────────────────────────────────────
    print("\n[2] Classifying signals ...")
    sigs = classify_signals(df_5m, df_daily, df_vix)

    # ── Run adaptive backtest ──────────────────────────────────────────────────
    print("\n[3] Running adaptive-risk backtest ...")
    trades = _run_adaptive(df_5m, df_daily, df_vix, sigs)

    # ── Tier breakdown ─────────────────────────────────────────────────────────
    print("\n[4] Performance by tier (full 2015-2024) ...")
    print()
    _print_tier_summary(trades)
    print()
    m_all = _m(trades)
    eb    = m_all.get("exit_breakdown", {})
    gate  = "[PASS]" if m_all["profit_factor"] > 2.0 and m_all["win_rate_pct"] > 55 else ""
    print(f"  {'COMBINED':<22} n={m_all['n_trades']:>4} ({m_all['n_trades']/9:.0f}/yr)  "
          f"WR={m_all['win_rate_pct']:.1f}%  PF={m_all['profit_factor']:.3f}  "
          f"Net=Rs{m_all['net_pnl']:,.0f} ({m_all['net_pnl']/9/CAPITAL*100:.1f}%/yr)  "
          f"SL={eb.get('SL',0)} BEP={eb.get('BEP',0)} "
          f"TSL={eb.get('Trail SL',0)} EOD={eb.get('3:15',0)}  {gate}")

    # ── Yearly breakdown ───────────────────────────────────────────────────────
    print("\n[5] Yearly breakdown ...")
    _print_yearly(_yearly(trades))

    # ── IS / OOS validation ────────────────────────────────────────────────────
    print("\n[6] IS / OOS validation ...")
    print()
    for period, (s, e), yrs in [
        ("Full  2015-2024", ("2015-01-01", OOS_END), 9),
        ("IS    2015-2018", ("2015-01-01", IS_END),  4),
        ("OOS   2019-2024", (OOS_START,   OOS_END),  6),
    ]:
        t = _run_adaptive(df_5m, df_daily, df_vix, sigs, start=s, end=e)
        m = _m(t)
        if "error" in m:
            print(f"  {period}: no trades"); continue
        eb   = m.get("exit_breakdown", {})
        gate = "[PASS]" if m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55 else ""
        t_ap = [x for x in t if x["quality"] == "A+"]
        t_re = [x for x in t if x["quality"] == "regular"]
        m_ap = _m(t_ap); m_re = _m(t_re)
        print(f"  {period}:")
        print(f"    A+      n={m_ap.get('n_trades',0):>4} ({m_ap.get('n_trades',0)/yrs:.0f}/yr)  "
              f"WR={m_ap.get('win_rate_pct',0):.1f}%  PF={m_ap.get('profit_factor',0):.3f}  "
              f"Net=Rs{m_ap.get('net_pnl',0):,.0f} ({m_ap.get('net_pnl',0)/yrs/CAPITAL*100:.1f}%/yr)")
        print(f"    Regular n={m_re.get('n_trades',0):>4} ({m_re.get('n_trades',0)/yrs:.0f}/yr)  "
              f"WR={m_re.get('win_rate_pct',0):.1f}%  PF={m_re.get('profit_factor',0):.3f}  "
              f"Net=Rs{m_re.get('net_pnl',0):,.0f} ({m_re.get('net_pnl',0)/yrs/CAPITAL*100:.1f}%/yr)")
        print(f"    COMBINED n={m['n_trades']:>4} ({m['n_trades']/yrs:.0f}/yr)  "
              f"WR={m['win_rate_pct']:.1f}%  PF={m['profit_factor']:.3f}  "
              f"Net=Rs{m['net_pnl']:,.0f} ({m['net_pnl']/yrs/CAPITAL*100:.1f}%/yr)  "
              f"{gate}")
        print()

    # ── Save trades ────────────────────────────────────────────────────────────
    if trades:
        out = os.path.join(RESULTS, "orb_adaptive_trades.csv")
        pd.DataFrame(trades).to_csv(out, index=False)
        print(f"  Saved: {out}")

    # ── What more risk would achieve 50%+ every year ───────────────────────────
    print("\n[7] Scaling analysis — what risk% achieves 50%/yr on average? ...")
    print()
    if trades:
        ap_net_1pct = _m([t for t in trades if t["quality"] == "A+"])["net_pnl"] / 9 / 2.0
        re_net_1pct = _m([t for t in trades if t["quality"] == "regular"])["net_pnl"] / 9 / 1.0
        print(f"  A+ expected @ 1% risk  : Rs{ap_net_1pct:,.0f}/yr")
        print(f"  Regular expected @ 1%  : Rs{re_net_1pct:,.0f}/yr")
        print()
        print(f"  {'A+%':>5}  {'Reg%':>5}  {'A+ P&L':>10}  {'Reg P&L':>10}  "
              f"{'Total':>10}  {'Return':>8}  {'Gate?':>6}")
        print("  " + "-" * 68)
        for ap_r, re_r in [(2, 1), (3, 1.5), (3, 2), (4, 2), (5, 2), (4, 1)]:
            a = ap_net_1pct * ap_r
            r = re_net_1pct * re_r
            tot = a + r
            ret = tot / CAPITAL * 100
            gate = "PASS" if ret >= 50 else ""
            print(f"  {ap_r:>4.0f}%  {re_r:>4.1f}%  Rs{a:>9,.0f}  Rs{r:>9,.0f}  "
                  f"Rs{tot:>9,.0f}  {ret:>7.1f}%  {gate:>6}")

    print("\n" + "=" * 80)
    print("  DONE")
    print("=" * 80)
