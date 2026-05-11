#!/usr/bin/env python3
"""
Trail SL parameter optimizer — grid search be_trigger_r × trail_buffer_r.

Train : Feb 2023 – Feb 20, 2025  (2 years)
OOS   : Feb 20, 2025 – Feb 2026  (1 year)
Fixed 1% risk per trade (no adaptive risk) for clean parameter comparison.

Grid:
  be_trigger_r   ∈ [0.75, 1.0, 1.25, 1.5]
  trail_buffer_r ∈ [0.25, 0.5, 0.75, 1.0, 999.0]   (999 = BE-only, SL stays at entry)
  + baseline (999, 999)  = no BE, no trail — pure SL/TP system

Anti-overfitting: only flag combos where OOS PF >= Train PF × 0.85

Usage: python optimize_trail_sl.py
"""
from __future__ import annotations

import sys
import time
from datetime import date
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest_live_setup import (
    BROKER_POINT_VALUES,
    LIVE_INDEX_CONFIG,
    TRADING_COSTS,
    compute_htf_ema,
    get_htf_trend_for_day,
)
from strategy.strategy_ict_clean import StrategyICTClean

# ── Constants ──────────────────────────────────────────────────────────────
TRAIN_END = date(2025, 2, 20)
OOS_START = date(2025, 2, 20)

SYMBOLS = ["GER30", "NDX100", "SPX500", "US30", "FRA40"]

DATA_FILES = {
    "SPX500": "SPX500_3YEAR_M1.csv",
    "NDX100": "NDX100_3YEAR_M1.csv",
    "GER30":  "GER30_3YEAR_M1.csv",
    "US30":   "US30_3YEAR_M1.csv",
    "FRA40":  "FRA40_M1_CLEAN.csv",
}

FIXED_RISK_PCT   = 1.0
STARTING_BALANCE = 6000.0
MAX_OPEN_POS     = 2
MAX_DAILY_TRADES = 2
ROBUST_THRESH    = 0.85   # OOS PF >= Train PF × this → robust

BE_VALUES    = [0.75, 1.0, 1.25, 1.5]
TRAIL_VALUES = [0.25, 0.5, 0.75, 1.0, 999.0]   # 999 = BE-only (no trailing after BE)
ALL_COMBOS   = list(product(BE_VALUES, TRAIL_VALUES)) + [(999.0, 999.0)]  # +baseline

LIVE_COMBO = (1.0, 0.5)   # current live config


# ── Data loading ───────────────────────────────────────────────────────────

def load_data(symbol: str) -> pd.DataFrame | None:
    path = Path(__file__).parent / "data" / "raw" / DATA_FILES[symbol]
    if not path.exists():
        print(f"  WARNING: {DATA_FILES[symbol]} not found — skipping {symbol}")
        return None
    df = pd.read_csv(path, sep="\t")
    df["Time"] = pd.to_datetime(df["Time"], format="mixed", dayfirst=False)
    df.set_index("Time", inplace=True)
    df.columns = [c.lower() for c in df.columns]
    df.sort_index(inplace=True)
    if symbol == "FRA40":
        df = df[df.index >= "2023-02-01"]
    return df


def build_sessions(symbol: str, df: pd.DataFrame) -> list[dict]:
    """Pre-slice all trading session windows for a symbol (done once, reused per combo)."""
    cfg = LIVE_INDEX_CONFIG[symbol]
    htf_ema = compute_htf_ema(df, fast=20, slow=50)
    pv = BROKER_POINT_VALUES[symbol]
    sessions = []
    for day in sorted(set(df.index.date)):
        ss = pd.Timestamp(day).replace(
            hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
        se = pd.Timestamp(day).replace(
            hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
        mdf = df[(df.index >= ss) & (df.index <= se)].copy()
        if len(mdf) < 80:
            continue
        sessions.append({
            "day": day, "df": mdf, "symbol": symbol, "pv": pv,
            "cfg": cfg, "htf": get_htf_trend_for_day(htf_ema, ss),
        })
    return sessions


# ── Signal generation ──────────────────────────────────────────────────────

def run_strategy(sessions: list[dict], be_r: float, trail_r: float) -> list[dict]:
    """Generate all trade signals for a set of sessions with given trail params."""
    signals = []
    for sess in sessions:
        cfg = sess["cfg"]
        strat = StrategyICTClean(
            sl_buffer=cfg["sl_buffer"],
            min_sl_points=cfg.get("min_sl_points", 2.0),
            max_sl_points=cfg.get("max_sl_points", 25.0),
            orb_minutes=cfg.get("orb_minutes", 15),
            entry_hours=cfg.get("entry_hours", 2),
            breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
            disp_mult=cfg.get("disp_mult", 1.5),
            body_min=cfg.get("body_min", 0.60),
            close_strength=cfg.get("close_strength", 0.25),
            breakout_margin=cfg.get("breakout_margin", 0.10),
            orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
            tp1_r=be_r,
            tp3_r=2.5,
            trail_buffer_r=trail_r,
        )
        for s in strat.generate_signals(sess["df"], htf_trend=sess["htf"]):
            signals.append({
                "symbol": sess["symbol"], "pv": sess["pv"], "signal": s,
                "entry_time": pd.Timestamp(s.entry_time),
                "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
            })
    return signals


# ── Portfolio simulation ───────────────────────────────────────────────────

def simulate_portfolio(signals: list[dict]) -> dict:
    """Execute signals with portfolio constraints. Fixed 1% risk per trade."""
    signals = sorted(signals, key=lambda x: x["entry_time"])

    balance      = STARTING_BALANCE
    peak         = STARTING_BALANCE
    open_pos     = []
    daily_trades = {}
    trades       = []

    for sig in signals:
        s          = sig["signal"]
        symbol     = sig["symbol"]
        pv         = sig["pv"]
        entry_time = sig["entry_time"]
        exit_time  = sig["exit_time"]
        trade_day  = entry_time.date()

        # Portfolio constraints
        if daily_trades.get(trade_day, 0) >= MAX_DAILY_TRADES:
            continue
        open_pos = [p for p in open_pos if p["exit_time"] > entry_time]
        if len(open_pos) >= MAX_OPEN_POS:
            continue

        risk_pts = float(s.risk_points)
        if risk_pts <= 0:
            continue

        lots = (balance * FIXED_RISK_PCT / 100.0) / (risk_pts * pv)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        slip    = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
        half_sp = TRADING_COSTS["spread_points"].get(symbol, 1.0) / 2.0
        ex_rsn  = getattr(s, "exit_reason", None)
        exit_cost = half_sp if ex_rsn == "TP" else (slip + half_sp)
        cost_pts  = (slip + half_sp) + exit_cost

        pnl_pts = float(getattr(s, "pnl_points", 0.0))
        pnl     = (pnl_pts - cost_pts) * lots * pv

        balance += pnl
        peak     = max(peak, balance)

        daily_trades[trade_day] = daily_trades.get(trade_day, 0) + 1
        if exit_time:
            open_pos.append({"exit_time": exit_time})

        trades.append({"pnl": pnl, "pnl_pts": pnl_pts, "ex_rsn": ex_rsn, "bal": balance})

    if not trades:
        return {"n": 0, "pf": 0.0, "wr": 0.0, "net": 0.0, "mdd": 0.0, "tp_pct": 0.0, "sl_pct": 0.0, "trail_pct": 0.0}

    df = pd.DataFrame(trades)
    gp  = df[df.pnl > 0].pnl.sum()
    gl  = df[df.pnl < 0].pnl.sum()
    pf  = round(abs(gp / gl) if gl != 0 else float("inf"), 3)
    wr  = round(len(df[df.pnl > 0]) / len(df) * 100, 1)
    net = round(df.pnl.sum(), 2)

    eq  = df.bal.values
    pk  = np.maximum.accumulate(eq)
    mdd = round(((pk - eq) / pk * 100).max(), 2)

    n     = len(df)
    tp_c  = (df.ex_rsn == "TP").sum()
    sl_c  = (df.ex_rsn == "SL_HIT").sum()
    be_c  = (df.ex_rsn == "SL_BE").sum()
    ses_c = (df.ex_rsn == "SESSION_CLOSE").sum()

    return {
        "n": n, "pf": pf, "wr": wr, "net": net, "mdd": mdd,
        "tp_pct":    round(tp_c  / n * 100, 1),
        "sl_pct":    round(sl_c  / n * 100, 1),
        "be_pct":    round(be_c  / n * 100, 1),
        "ses_pct":   round(ses_c / n * 100, 1),
    }


# ── Label helper ───────────────────────────────────────────────────────────

def combo_label(be_r: float, trail_r: float, is_live: bool) -> str:
    if be_r >= 900:
        base = "NO_BE_NO_TRAIL"
    elif trail_r >= 900:
        base = f"BE={be_r}R  BE-ONLY"
    else:
        base = f"BE={be_r}R  TRAIL={trail_r}R"
    return base + ("  [LIVE]" if is_live else "")


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 100)
    print("  TRAIL SL OPTIMIZER — Grid Search (be_trigger_r × trail_buffer_r)")
    print(f"  Train: Feb 2023 – Feb 20, 2025  |  OOS: Feb 20, 2025 – Feb 2026")
    print(f"  Symbols: {', '.join(SYMBOLS)}")
    print(f"  Fixed risk: {FIXED_RISK_PCT}%  |  Starting capital: ${STARTING_BALANCE:,.0f}")
    print(f"  Robust filter: OOS PF >= Train PF × {ROBUST_THRESH}")
    print(f"  Current live config: BE={LIVE_COMBO[0]}R, TRAIL={LIVE_COMBO[1]}R")
    print("=" * 100)

    # Load + pre-slice sessions (done once, reused per combo)
    print("\nLoading and slicing session data...")
    all_sessions: dict[str, dict] = {}
    for sym in SYMBOLS:
        df = load_data(sym)
        if df is None:
            continue
        sessions = build_sessions(sym, df)
        train_s = [s for s in sessions if s["day"] <  TRAIN_END]
        oos_s   = [s for s in sessions if s["day"] >= OOS_START]
        all_sessions[sym] = {"train": train_s, "oos": oos_s}
        train_range = f"{min(s['day'] for s in train_s)} to {max(s['day'] for s in train_s)}" if train_s else "none"
        oos_range   = f"{min(s['day'] for s in oos_s)} to {max(s['day'] for s in oos_s)}"   if oos_s   else "none"
        print(f"  {sym:7}: {len(train_s):3d} train days ({train_range})  |  {len(oos_s):3d} OOS days ({oos_range})")

    if not all_sessions:
        print("No data loaded. Exiting.")
        return

    print(f"\nRunning {len(ALL_COMBOS)} parameter combos × 2 periods. Please wait...\n")

    results = []
    total_t0 = time.time()

    for i, (be_r, trail_r) in enumerate(ALL_COMBOS, 1):
        t0 = time.time()
        is_live = (be_r, trail_r) == LIVE_COMBO

        train_sigs: list[dict] = []
        oos_sigs:   list[dict] = []
        for data in all_sessions.values():
            train_sigs.extend(run_strategy(data["train"], be_r, trail_r))
            oos_sigs.extend(run_strategy(data["oos"],   be_r, trail_r))

        tm = simulate_portfolio(train_sigs)
        om = simulate_portfolio(oos_sigs)

        # Robust: OOS PF >= Train PF * threshold (need >= 15 OOS trades to count)
        if om["n"] >= 15 and tm["pf"] > 0:
            robust = (om["pf"] != float("inf")) and (om["pf"] >= tm["pf"] * ROBUST_THRESH)
        else:
            robust = False

        label = combo_label(be_r, trail_r, is_live)
        elapsed = time.time() - t0

        flag = "ROBUST" if robust else "      "
        live = "[LIVE]" if is_live else "      "
        print(f"  [{i:02d}/{len(ALL_COMBOS)}] {live} {flag} {label:<30}  "
              f"Train: n={tm['n']:3d} PF={tm['pf']:.2f} WR={tm['wr']:.0f}%  "
              f"OOS: n={om['n']:3d} PF={om['pf']:.2f} WR={om['wr']:.0f}%  "
              f"({elapsed:.1f}s)")

        results.append({
            "be_r": be_r, "trail_r": trail_r, "is_live": is_live,
            "robust": robust, "label": label, "train": tm, "oos": om,
        })

    total_elapsed = time.time() - total_t0
    print(f"\nTotal time: {total_elapsed:.0f}s")

    # ── Summary table ──────────────────────────────────────────────────────
    print("\n" + "=" * 120)
    print("  RESULTS TABLE  (sorted: robust first, then by OOS Profit Factor)")
    print("=" * 120)
    hdr = (f"  {'Parameters':<28} {'Flag':<6} | "
           f"{'---- TRAIN (2yr) ----':^46} | "
           f"{'---- OOS (1yr) ----':^46} |")
    sub = (f"  {'':28} {'':6} | "
           f"{'n':>5} {'WR%':>5} {'PF':>5} {'Net$':>9} {'MDD%':>6} {'TP%':>5} {'BE%':>5} {'SL%':>5} | "
           f"{'n':>5} {'WR%':>5} {'PF':>5} {'Net$':>9} {'MDD%':>6} {'TP%':>5} {'BE%':>5} {'SL%':>5} |")
    print(hdr)
    print(sub)
    print("  " + "-" * 118)

    results_sorted = sorted(results, key=lambda r: (-int(r["robust"]), -(r["oos"]["pf"] if r["oos"]["pf"] != float("inf") else 99)))

    for r in results_sorted:
        t = r["train"]
        o = r["oos"]
        flag = "ROBUST" if r["robust"] else "      "
        live = "[LIVE]" if r["is_live"] else "      "
        label_clean = r["label"].replace("  [LIVE]", "").strip()
        pf_t = f"{t['pf']:.2f}" if t["pf"] != float("inf") else " inf"
        pf_o = f"{o['pf']:.2f}" if o["pf"] != float("inf") else " inf"
        row = (f"  {label_clean:<28} {live} | "
               f"{t['n']:>5} {t['wr']:>5.1f} {pf_t:>5} ${t['net']:>8,.0f} {t['mdd']:>5.1f}% "
               f"{t.get('tp_pct',0):>4.0f}% {t.get('be_pct',0):>4.0f}% {t.get('sl_pct',0):>4.0f}% | "
               f"{o['n']:>5} {o['wr']:>5.1f} {pf_o:>5} ${o['net']:>8,.0f} {o['mdd']:>5.1f}% "
               f"{o.get('tp_pct',0):>4.0f}% {o.get('be_pct',0):>4.0f}% {o.get('sl_pct',0):>4.0f}% | "
               f"{flag}")
        print(row)

    print("=" * 120)
    print("  Legend: WR% = win rate  |  PF = profit factor  |  MDD% = max drawdown")
    print("          TP% = hit TP    |  BE% = stopped at breakeven  |  SL% = full stop loss")

    # ── Recommendation ────────────────────────────────────────────────────
    robust_with_trades = [r for r in results if r["robust"] and r["oos"]["n"] >= 15]
    live_r = next((r for r in results if r["is_live"]), None)

    print("\n" + "=" * 100)
    print("  RECOMMENDATION")
    print("=" * 100)

    if robust_with_trades:
        best = max(robust_with_trades, key=lambda r: (r["oos"]["pf"] if r["oos"]["pf"] != float("inf") else 99))
        print(f"\n  Best robust combo: BE={best['be_r']}R, TRAIL={best['trail_r']}R")
        print(f"    Train: n={best['train']['n']}, WR={best['train']['wr']}%, "
              f"PF={best['train']['pf']:.2f}, Net=${best['train']['net']:,.0f}, MDD={best['train']['mdd']:.1f}%")
        print(f"    OOS  : n={best['oos']['n']}, WR={best['oos']['wr']}%, "
              f"PF={best['oos']['pf']:.2f}, Net=${best['oos']['net']:,.0f}, MDD={best['oos']['mdd']:.1f}%")

        if live_r:
            print(f"\n  Current live: BE={live_r['be_r']}R, TRAIL={live_r['trail_r']}R")
            print(f"    Train: n={live_r['train']['n']}, WR={live_r['train']['wr']}%, "
                  f"PF={live_r['train']['pf']:.2f}, Net=${live_r['train']['net']:,.0f}")
            print(f"    OOS  : n={live_r['oos']['n']}, WR={live_r['oos']['wr']}%, "
                  f"PF={live_r['oos']['pf']:.2f}, Net=${live_r['oos']['net']:,.0f}")
            live_is_best = (best["be_r"] == live_r["be_r"] and best["trail_r"] == live_r["trail_r"])
            if live_is_best:
                print("\n  VERDICT: Current live config is the best robust combo. No change needed.")
            else:
                oos_pf_gain = ((best["oos"]["pf"] - live_r["oos"]["pf"]) / max(live_r["oos"]["pf"], 0.01)) * 100
                print(f"\n  VERDICT: Consider switching to BE={best['be_r']}R / TRAIL={best['trail_r']}R")
                print(f"           OOS PF improvement: {oos_pf_gain:+.1f}%")
                print(f"           Change in live config:")
                print(f"             strategy_ict_clean.py: tp1_r={best['be_r']}  trail_buffer_r={best['trail_r']}")
                print(f"           NOTE: Only switch if OOS PF improvement is > 10% and MDD stays acceptable.")
    else:
        print("\n  No robust combos found with >= 15 OOS trades.")
        if live_r:
            print(f"  Current live config (BE={live_r['be_r']}R, TRAIL={live_r['trail_r']}R) remains in place.")

    print()


if __name__ == "__main__":
    main()
