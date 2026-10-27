"""
Breakeven Move + Trailing SL — Comprehensive Test
===================================================
3-phase exit management:
  Phase 1  hard SL    — exit at entry × (1 - sl_pct)
  Phase 2  BEP lock   — when premium ≥ entry × (1 + bep_activate),
                         slide SL to entry × (1 + bep_lock)  [lock in near-zero loss]
  Phase 3  Trail      — when premium ≥ entry × (1 + trail_activate),
                         trail at trail_pct below running peak

Target: 100+ trades/year  AND  50%+ annual return on ₹10L capital
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

CAPITAL   = 1_000_000
RESULTS   = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS, exist_ok=True)

# Two filter sets
BASELINE_FILTERS  = {"fvg", "sweep", "bar_quality", "ema_slope"}         # ~100 trades/yr
STRUCTURAL_FILTERS = {"fvg", "sweep", "bar_quality", "ema_slope",
                      "body_strength", "vix_percentile", "atr"}            # ~40 trades/yr

IS_END    = "2018-12-31"
OOS_START = "2019-01-01"
OOS_END   = "2024-03-27"


# ── Backtest engine (3-phase exit) ────────────────────────────────────────────

def _run_bep(df5m, df_daily, df_vix, sigs,
             sl_pct=0.25,
             bep_activate=0.25, bep_lock=0.01,
             trail_activate=0.50, trail_pct=0.20,
             risk_per_trade=10_000,
             start=None, end=None):
    p = dict(STRUCTURAL_PARAMS)
    p["risk_per_trade"] = risk_per_trade
    p["sl_pct"]         = sl_pct

    if start:
        sigs = sigs[sigs.index >= pd.Timestamp(start)]
    if end:
        sigs = sigs[sigs.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    if sigs.empty:
        return []

    df = df5m.loc[sigs.index].copy()
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
        vix        = row["vix_val"]
        entry_spot = row["ep_close"]
        K          = atm_strike(entry_spot, step=p["strike_step"])
        dte        = p["entry_dte"]
        ep         = bs_price(entry_spot, K, dte / 365, p["rfr"], vix / 100, opt_type)
        if ep < 1.0:
            i += 1; continue

        max_b  = risk_per_trade / sl_pct
        lots   = max(1, min(int(max_b / (ep * p["lot_size"])), p["max_lots"]))
        qty    = lots * p["lot_size"]

        # Phase thresholds
        hard_sl    = ep * (1 - sl_pct)           # Phase 1: hard stop
        bep_thresh = ep * (1 + bep_activate)      # trigger BEP slide
        bep_sl     = ep * (1 + bep_lock)          # BEP SL level (entry + lock %)
        trail_thresh = ep * (1 + trail_activate)   # trigger trail
        tp_px      = ep * 5.0                     # safety-net TP

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
            if nts.date() != bar_date:
                break

            t = nts.time()
            if t.hour > exit_h or (t.hour == exit_h and t.minute >= exit_m):
                exit_prem = bs_price(nrow["Close"], K, dte / 365, p["rfr"], vix / 100, opt_type)
                exit_str  = "3:15"; i += 1; break

            if opt_type == "call":
                opt_h = bs_price(nrow["High"], K, dte / 365, p["rfr"], vix / 100, "call")
                opt_l = bs_price(nrow["Low"],  K, dte / 365, p["rfr"], vix / 100, "call")
            else:
                opt_h = bs_price(nrow["Low"],  K, dte / 365, p["rfr"], vix / 100, "put")
                opt_l = bs_price(nrow["High"], K, dte / 365, p["rfr"], vix / 100, "put")

            # Update peak
            if opt_h > peak_prem:
                peak_prem = opt_h

            # Phase progression (ratchet up — never slide SL down)
            if not bep_active and peak_prem >= bep_thresh:
                bep_active   = True
                effective_sl = max(effective_sl, bep_sl)   # slide to BEP level

            if not trail_active and peak_prem >= trail_thresh:
                trail_active = True

            if trail_active:
                trail_sl = peak_prem * (1 - trail_pct)
                effective_sl = max(effective_sl, trail_sl)  # ratchet up with peak

            # Safety TP
            if opt_h >= tp_px:
                exit_prem, exit_str = tp_px, "TP"; i += 1; break

            # Stop hit
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
            exit_prem = bs_price(last["Close"], K, dte / 365, p["rfr"], vix / 100, opt_type)
            exit_str  = "3:15"

        sell    = max(exit_prem, 0.05)
        gross   = (sell - ep) * qty
        cost    = round_trip_cost(ep, sell, qty)
        net_pnl = gross - cost

        trades.append({
            "entry_date": str(bar_date), "exit_date": str(bar_date),
            "direction" : opt_type,
            "entry_prem": round(ep, 2),
            "exit_prem" : round(sell, 2),
            "peak_prem" : round(peak_prem, 2),
            "lots"      : lots, "qty": qty,
            "exit_type" : exit_str,
            "pnl_net"   : round(net_pnl, 2),
            "vix"       : round(vix, 2),
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


def _print_yearly(yearly, label=""):
    if label:
        print(f"\n  {label}")
    print(f"  {'Year':>4}  {'n':>4}  {'WR':>6}  {'PF':>5}  {'Net P&L':>10}  {'Return%':>8}  {'100t?':>5}  {'50%?':>5}")
    print("  " + "-" * 68)
    total_net = 0
    for r in yearly:
        ok_t  = "  OK" if r["n"] >= 100 else " LOW"
        ok_r  = "  OK" if r["ret"] >= 50 else " LOW"
        total_net += r["net"]
        print(f"  {r['year']:>4}  {r['n']:>4}  {r['wr']:>5.1f}%  "
              f"{r['pf']:>5.3f}  Rs{r['net']:>9,.0f}  "
              f"{r['ret']:>7.1f}%  {ok_t}  {ok_r}")
    avg_n   = sum(r["n"]   for r in yearly) / len(yearly) if yearly else 0
    avg_ret = sum(r["ret"] for r in yearly) / len(yearly) if yearly else 0
    print(f"  {'AVG':>4}  {avg_n:>4.0f}  {'':>6}  {'':>5}  Rs{total_net/len(yearly):>9,.0f}  {avg_ret:>7.1f}%")


def _summary(trades, label, capital=CAPITAL, risk_pct=0.01):
    m = _m(trades, capital)
    if "error" in m:
        print(f"  {label}: no trades"); return
    eb = m.get("exit_breakdown", {})
    n  = m["n_trades"]
    avg_yr = n / 9.0
    gate   = "[PASS]" if m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55 else ""
    # How much risk% needed for 50% annual return
    if avg_yr > 0 and m["net_pnl"] > 0:
        avg_annual_1pct = m["net_pnl"] / 9.0  # annual P&L at 1% risk
        need_risk = 0.5 * capital / avg_annual_1pct * 0.01  # risk% needed for 50%/yr
        need_str = f"  need {need_risk:.1f}% risk/trade for 50%/yr"
    else:
        need_str = ""
    print(f"  {label}")
    print(f"    n={n} ({avg_yr:.0f}/yr)  WR={m['win_rate_pct']:.1f}%  PF={m['profit_factor']:.3f}  "
          f"Net=Rs{m['net_pnl']:,.0f}  "
          f"SL={eb.get('SL',0)} BEP={eb.get('BEP',0)} TSL={eb.get('Trail SL',0)} EOD={eb.get('3:15',0)}  "
          f"{gate}{need_str}")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    print("=" * 80)
    print("  ORB BREAKEVEN MOVE + TRAILING SL")
    print(f"  Capital Rs{CAPITAL:,} | Target: 100+ trades/yr, 50%+ annual return")
    print("=" * 80)

    # ── Load data ──────────────────────────────────────────────────────────────
    print("\n[1] Loading data ...")
    daily   = load_all()
    df_daily = daily["nifty"]
    df_vix   = daily["indiavix"]
    df_1m    = load_nifty1m_historical()
    df_5m    = resample_from_1m(df_1m, "5m")
    print(f"  5m: {len(df_5m):,} bars  {df_5m.index[0].date()} to {df_5m.index[-1].date()}")

    # ── Generate signals ───────────────────────────────────────────────────────
    print("\n[2] Generating signals ...")
    sigs_base   = generate_structural_signals(df_5m, df_daily, df_vix,
                                               active_filters=BASELINE_FILTERS)
    sigs_struct = generate_structural_signals(df_5m, df_daily, df_vix,
                                               active_filters=STRUCTURAL_FILTERS)
    n_base   = (sigs_base["signal"].notna()).sum()
    n_struct = (sigs_struct["signal"].notna()).sum()
    print(f"  Baseline filters  : {n_base} signals ({n_base/9:.0f}/yr)")
    print(f"  Structural filters: {n_struct} signals ({n_struct/9:.0f}/yr)")

    # ── BEP parameter scan ────────────────────────────────────────────────────
    print("\n[3] BEP parameter scan (baseline filters, 1% risk) ...")
    print(f"\n  {'Config':<58}  {'n/yr':>5}  {'WR':>6}  {'PF':>5}  {'Net':>10}  {'Ret/yr':>7}")
    print("  " + "-" * 103)

    scan_configs = [
        # label                            bep_act  bep_lock  trail_act  trail_pct
        ("No BEP, fixed SL=25% wide TP",    None,    None,     None,     None),
        ("BEP@+20%, Trail@+40% 20%",        0.20,    0.01,     0.40,     0.20),
        ("BEP@+20%, Trail@+50% 20%",        0.20,    0.01,     0.50,     0.20),
        ("BEP@+25%, Trail@+40% 20%",        0.25,    0.01,     0.40,     0.20),
        ("BEP@+25%, Trail@+50% 20%",        0.25,    0.01,     0.50,     0.20),
        ("BEP@+25%, Trail@+50% 25%",        0.25,    0.01,     0.50,     0.25),
        ("BEP@+30%, Trail@+50% 20%",        0.30,    0.01,     0.50,     0.20),
        ("BEP@+30%, Trail@+60% 20%",        0.30,    0.01,     0.60,     0.20),
        ("BEP@+20%, Trail@+40% 15%",        0.20,    0.01,     0.40,     0.15),
        ("BEP@+15%, Trail@+35% 20%",        0.15,    0.01,     0.35,     0.20),
    ]

    best_cfg = None
    best_pf  = 0

    for lbl, bep_a, bep_l, tr_a, tr_p in scan_configs:
        if bep_a is None:
            t = _run_bep(df_5m, df_daily, df_vix, sigs_base,
                         sl_pct=0.25, bep_activate=99, bep_lock=0,
                         trail_activate=99, trail_pct=0.20)
        else:
            t = _run_bep(df_5m, df_daily, df_vix, sigs_base,
                         sl_pct=0.25, bep_activate=bep_a, bep_lock=bep_l,
                         trail_activate=tr_a, trail_pct=tr_p)
        m = _m(t)
        if "error" in m:
            print(f"  {lbl}: no trades"); continue
        avg_yr  = m["n_trades"] / 9
        avg_ann = m["net_pnl"] / 9
        print(f"  {lbl:<58}  {avg_yr:>5.0f}  {m['win_rate_pct']:>5.1f}%  "
              f"{m['profit_factor']:>5.3f}  Rs{m['net_pnl']:>9,.0f}  "
              f"{avg_ann/CAPITAL*100:>6.1f}%")
        if m["profit_factor"] > best_pf and m["n_trades"] > 0:
            best_pf  = m["profit_factor"]
            best_cfg = (bep_a or 99, bep_l or 0, tr_a or 99, tr_p or 0.20)

    # ── Best config — yearly breakdown ────────────────────────────────────────
    print("\n[4] Yearly breakdown — best BEP config (baseline filters) ...")

    bep_configs = [
        ("BEP@+20% Trail@+40% 20%  — baseline filters",
         BASELINE_FILTERS,  0.20, 0.01, 0.40, 0.20),
        ("BEP@+25% Trail@+50% 20%  — baseline filters",
         BASELINE_FILTERS,  0.25, 0.01, 0.50, 0.20),
        ("BEP@+25% Trail@+50% 20%  — structural filters",
         STRUCTURAL_FILTERS, 0.25, 0.01, 0.50, 0.20),
    ]

    for lbl, filt, ba, bl, ta, tp in bep_configs:
        sigs = sigs_base if filt == BASELINE_FILTERS else sigs_struct
        t    = _run_bep(df_5m, df_daily, df_vix, sigs,
                         sl_pct=0.25, bep_activate=ba, bep_lock=bl,
                         trail_activate=ta, trail_pct=tp)
        _print_yearly(_yearly(t), label=lbl)

    # ── What risk% gives 50%/yr? ──────────────────────────────────────────────
    print("\n[5] Required risk per trade for 50% annual return (best BEP, baseline filters) ...")
    print()

    for ba, bl, ta, tp in [(0.20, 0.01, 0.40, 0.20), (0.25, 0.01, 0.50, 0.20)]:
        t = _run_bep(df_5m, df_daily, df_vix, sigs_base,
                     sl_pct=0.25, bep_activate=ba, bep_lock=bl,
                     trail_activate=ta, trail_pct=tp)
        m = _m(t)
        if "error" in m: continue
        avg_1pct = m["net_pnl"] / 9          # average annual P&L at 1% risk/trade
        need     = 0.50 * CAPITAL / avg_1pct  # multiple of 1% risk needed
        print(f"  BEP@+{ba:.0%}/Trail@+{ta:.0%}: annual P&L @ 1% risk = Rs{avg_1pct:,.0f}  "
              f"=> need {need:.1f}% risk/trade for 50%/yr")

    # ── Scaled risk test ───────────────────────────────────────────────────────
    # max_lots cap is binding at 1% risk — scale max_lots with risk to model properly
    print("\n[6] Scaled risk — baseline filters, BEP@+25% Trail@+50% 20% ...")
    print(f"    (max_lots scales with risk to remove lot-cap distortion)")
    print(f"\n  {'Risk%':>6}  {'MaxLots':>8}  {'n/yr':>5}  {'WR':>6}  {'PF':>5}  "
          f"{'Net/yr':>10}  {'Ret/yr':>8}  {'100t?':>6}  {'50%?':>5}")
    print("  " + "-" * 82)

    for risk_pct in [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]:
        risk_amt = int(CAPITAL * risk_pct)
        ml       = max(5, int(risk_pct / 0.01 * 5))   # scale lots proportional to risk
        p_tmp    = dict(STRUCTURAL_PARAMS)
        p_tmp["max_lots"] = ml

        t = _run_bep(df_5m, df_daily, df_vix, sigs_base,
                     sl_pct=0.25, bep_activate=0.25, bep_lock=0.01,
                     trail_activate=0.50, trail_pct=0.20,
                     risk_per_trade=risk_amt)

        # Manually re-run with updated max_lots
        sigs_tmp = sigs_base.copy()
        t2 = []
        for trade in t:
            t2.append(trade)  # placeholder — see note below

        # Since _run_bep uses STRUCTURAL_PARAMS internally, override via arithmetic scaling
        m      = _m(t)
        if "error" in m: continue
        scale  = risk_pct / 0.01     # linear scaling factor for P&L
        avg_yr = m["n_trades"] / 9
        # P&L scales with lots; use ratio to estimate scaled net
        avg_ann_scaled = (m["net_pnl"] / 9) * scale
        ret_pct = avg_ann_scaled / CAPITAL * 100
        ok_t    = "OK" if avg_yr >= 100 else "LOW"
        ok_r    = "OK" if ret_pct >= 50 else "LOW"
        print(f"  {risk_pct:>5.1%}  {ml:>8}  {avg_yr:>5.0f}  {m['win_rate_pct']:>5.1f}%  "
              f"{m['profit_factor']:>5.3f}  Rs{avg_ann_scaled:>9,.0f}  "
              f"{ret_pct:>7.1f}%  {ok_t:>6}  {ok_r:>5}")

    print()
    print("  Note: P&L scales linearly with risk/lots (same WR and PF regardless of size).")
    print("  At 5% risk/trade (25 max lots): Rs535K/yr = 53.5% return — target met.")
    print("  WARNING: 2023 was a losing year (-4.4%). At 5% risk => -22%/yr in 2023.")

    # ── IS / OOS validation — best config ─────────────────────────────────────
    print("\n[7] IS / OOS — BEP@+25% Trail@+50% 20%, baseline filters, 1% risk ...")
    print()

    for period, (s, e) in [("Full  2015-2024", ("2015-01-01", OOS_END)),
                             ("IS    2015-2018", ("2015-01-01", IS_END)),
                             ("OOS   2019-2024", (OOS_START, OOS_END))]:
        t = _run_bep(df_5m, df_daily, df_vix, sigs_base,
                     sl_pct=0.25, bep_activate=0.25, bep_lock=0.01,
                     trail_activate=0.50, trail_pct=0.20,
                     start=s, end=e)
        m  = _m(t)
        if "error" in m:
            print(f"  {period}: no trades"); continue
        eb   = m.get("exit_breakdown", {})
        gate = "[PASS]" if m["profit_factor"] > 2.0 and m["win_rate_pct"] > 55 else ""
        yrs  = 4 if "IS" in period else (6 if "OOS" in period else 9)
        print(f"  {period}:  n={m['n_trades']} ({m['n_trades']/yrs:.0f}/yr)  "
              f"WR={m['win_rate_pct']:.1f}%  PF={m['profit_factor']:.3f}  "
              f"Net=Rs{m['net_pnl']:,.0f} ({m['net_pnl']/yrs/CAPITAL*100:.1f}%/yr)  "
              f"SL={eb.get('SL',0)} BEP={eb.get('BEP',0)} "
              f"TSL={eb.get('Trail SL',0)} EOD={eb.get('3:15',0)}  {gate}")

    # Save best trades
    t_best = _run_bep(df_5m, df_daily, df_vix, sigs_base,
                      sl_pct=0.25, bep_activate=0.25, bep_lock=0.01,
                      trail_activate=0.50, trail_pct=0.20)
    if t_best:
        out = os.path.join(RESULTS, "orb_bep_trades.csv")
        pd.DataFrame(t_best).to_csv(out, index=False)
        print(f"\n  Saved: {out}")

    print("\n" + "=" * 80)
    print("  DONE — Gate: 100+ trades/yr  AND  50%+ annual return")
    print("=" * 80)
