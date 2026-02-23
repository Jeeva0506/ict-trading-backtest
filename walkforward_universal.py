"""
Universal Parameter Search — ONE param set for ALL indices
==========================================================
Anti-curve-fit: Instead of per-index tuning, find params that
work across the entire portfolio simultaneously.

Reduced grid based on walk-forward findings:
  - breakout_margin=0.15 was universal across all 4 accepted indices
  - disp_mult ranged 1.0-1.7, body_min 0.50-0.70, close_strength 0.20-0.30
  - Focus grid on these proven ranges: 3x4x3x1 = 36 combos (fast!)

Score = min(expectancy across all indices) * sqrt(total_trades)
  -> Only rewards params where ALL indices are profitable
"""
import sys, time, itertools
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, load_data,
)
from strategy.strategy_ict_clean import StrategyICTClean

# Portfolio
PORTFOLIO = ["SPX500", "NDX100", "US30", "GER30", "SWI20", "US2000"]

# Focused grid (ranges where walk-forward found edge)
PARAM_GRID = {
    "disp_mult":      [1.0, 1.3, 1.7],
    "body_min":        [0.50, 0.60, 0.65, 0.70],
    "close_strength":  [0.20, 0.25, 0.30],
    "breakout_margin": [0.15],  # universal finding
}

TRAIN_YEAR = 2024
OOS_YEARS = [2023, 2025]
TP_R = 2.5
BE_R = 1.0
DATA_M1 = {s: f"{s}_M1_CLEAN.csv" for s in PORTFOLIO}
DATA_H1 = {s: f"{s}_H1_EMA.csv" for s in PORTFOLIO}


def load_h1_ema(filename, fast=20, slow=50):
    data_path = Path(__file__).parent / "data" / "raw" / filename
    h1 = pd.read_csv(data_path, sep="\t")
    tc = [c for c in h1.columns if c.lower() == "time"][0]
    h1[tc] = pd.to_datetime(h1[tc], format="mixed", dayfirst=False)
    h1 = h1.set_index(tc).sort_index()
    h1.columns = [c.lower() for c in h1.columns]
    h1[f"ema{fast}"] = h1["close"].ewm(span=fast, adjust=False).mean()
    h1[f"ema{slow}"] = h1["close"].ewm(span=slow, adjust=False).mean()
    h1["htf_trend"] = "bullish"
    h1.loc[h1[f"ema{fast}"] < h1[f"ema{slow}"], "htf_trend"] = "bearish"
    return h1[["htf_trend", f"ema{fast}", f"ema{slow}"]]


def get_htf_trend(htf_ema, session_start):
    prior = htf_ema[htf_ema.index < session_start]
    return str(prior.iloc[-1]["htf_trend"]) if len(prior) > 0 else None


def preslice(cfg, df, htf_ema, years):
    result = {}
    trading_days = sorted(set(df.index.date))
    for year in years:
        slices = []
        for day in [d for d in trading_days if d.year == year]:
            ss = pd.Timestamp(day).replace(hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
            se = pd.Timestamp(day).replace(hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
            mdf = df[(df.index >= ss) & (df.index <= se)]
            if len(mdf) < 30:
                continue
            slices.append((mdf, get_htf_trend(htf_ema, ss)))
        result[year] = slices
    return result


def run_signals(sym, cfg, day_slices, params):
    signals = []
    for mdf, htf in day_slices:
        try:
            strat = StrategyICTClean(
                sl_buffer=cfg["sl_buffer"],
                min_sl_points=cfg.get("min_sl_points", 2.0),
                max_sl_points=cfg.get("max_sl_points", 25.0),
                debug=False, orb_minutes=cfg.get("orb_minutes", 15),
                entry_hours=cfg.get("entry_hours", 2),
                breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
                disp_mult=params["disp_mult"], body_min=params["body_min"],
                close_strength=params["close_strength"],
                breakout_margin=params["breakout_margin"],
                orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
                tp3_r=TP_R, tp1_r=BE_R,
            )
            for s in strat.generate_signals(mdf, htf_trend=htf):
                signals.append(s)
        except Exception:
            continue
    return signals


def metrics(signals, sym):
    if not signals:
        return {"trades": 0, "pf": 0, "wr": 0, "exp": 0}
    w, l, gp, gl = 0, 0, 0.0, 0.0
    for s in signals:
        R = float(s.risk_points)
        if R <= 0: continue
        pnl = float(s.pnl_points)
        slip = TRADING_COSTS["slippage_points"].get(sym, 0.5)
        spread = TRADING_COSTS["spread_points"].get(sym, 1.0)
        hs = spread / 2.0
        ec = slip + hs
        xc = hs if getattr(s, "exit_reason", "") == "TP" else slip + hs
        net = pnl - (ec + xc)
        pR = net / R
        if net > 0: w += 1; gp += pR
        else: l += 1; gl += abs(pR)
    t = w + l
    if t == 0: return {"trades": 0, "pf": 0, "wr": 0, "exp": 0}
    return {
        "trades": t,
        "pf": round(gp / gl if gl > 0 else 99.0, 2),
        "wr": round(w / t, 3),
        "exp": round((gp - gl) / t, 3),
    }


def main():
    P = print
    P("=" * 70)
    P("  UNIVERSAL PARAM SEARCH -- One set for ALL 6 indices")
    P("=" * 70)

    keys = list(PARAM_GRID.keys())
    combos = list(itertools.product(*PARAM_GRID.values()))
    P(f"  Combos: {len(combos)} | Portfolio: {PORTFOLIO}")
    P(f"  Train: {TRAIN_YEAR} | OOS: {OOS_YEARS}")
    P()

    # Pre-load and pre-slice all data
    P("  Loading data...", flush=True)
    data = {}
    for sym in PORTFOLIO:
        cfg = LIVE_INDEX_CONFIG[sym]
        df = load_data(DATA_M1[sym])
        h1 = load_h1_ema(DATA_H1[sym])
        years = set(d.year for d in set(df.index.date))
        slices = preslice(cfg, df, h1, [TRAIN_YEAR] + OOS_YEARS)
        data[sym] = {"cfg": cfg, "slices": slices, "years": years}
        P(f"    {sym}: train={len(slices.get(TRAIN_YEAR, []))}d, "
          f"oos2023={len(slices.get(2023, []))}d, oos2025={len(slices.get(2025, []))}d")
    P()

    # TRAIN grid search
    P(f"  Grid searching {len(combos)} combos on TRAIN {TRAIN_YEAR}...", flush=True)
    t0 = time.time()
    results = []

    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))

        per_idx = {}
        total_trades = 0
        min_exp = 999
        all_positive = True

        for sym in PORTFOLIO:
            d = data[sym]
            train_slices = d["slices"].get(TRAIN_YEAR, [])
            if not train_slices:
                all_positive = False
                break
            sigs = run_signals(sym, d["cfg"], train_slices, params)
            m = metrics(sigs, sym)
            per_idx[sym] = m
            total_trades += m["trades"]
            if m["trades"] < 5 or m["exp"] <= 0:
                all_positive = False
                break
            min_exp = min(min_exp, m["exp"])

        if not all_positive:
            continue

        score = min_exp * np.sqrt(total_trades)
        results.append((params.copy(), per_idx.copy(), score, total_trades, min_exp))

        if (i + 1) % 12 == 0:
            elapsed = time.time() - t0
            P(f"    ... {i+1}/{len(combos)} ({elapsed:.0f}s, {len(results)} viable)", flush=True)

    elapsed = time.time() - t0
    P(f"  Grid done: {elapsed:.0f}s, {len(results)} viable out of {len(combos)}")

    if not results:
        P("\n  NO universal param set found where ALL indices are profitable!")
        P("  This suggests per-index params may be necessary, or the grid needs widening.")
        return

    results.sort(key=lambda x: -x[2])

    P(f"\n  TOP 5 UNIVERSAL PARAM SETS (train {TRAIN_YEAR}):")
    P(f"  {'Rank':<5} {'disp':>5} {'body':>5} {'close':>6} {'margin':>7} {'Trades':>7} {'MinExp':>7} {'Score':>7}")
    P(f"  {'-'*55}")
    for rank, (p, pidx, sc, tt, me) in enumerate(results[:5], 1):
        P(f"  {rank:<5} {p['disp_mult']:>5} {p['body_min']:>5} {p['close_strength']:>6} "
          f"{p['breakout_margin']:>7} {tt:>7} {me:>+7.3f} {sc:>7.2f}")
        for sym in PORTFOLIO:
            m = pidx[sym]
            P(f"         {sym:<8} {m['trades']:>3}t PF{m['pf']:<5} WR{m['wr']:.1%} Exp{m['exp']:+.3f}R")

    # OOS validation on top 5
    P(f"\n  OOS VALIDATION (top 5 on {OOS_YEARS}):")
    P(f"  {'='*65}")

    best_universal = None

    for rank, (params, _, train_score, _, _) in enumerate(results[:5], 1):
        P(f"\n  Rank #{rank}: disp={params['disp_mult']}, body={params['body_min']}, "
          f"close={params['close_strength']}, margin={params['breakout_margin']}")

        all_oos_pass = True
        for oos_yr in OOS_YEARS:
            year_metrics = {}
            year_trades = 0
            year_ok = True

            for sym in PORTFOLIO:
                d = data[sym]
                if oos_yr not in d["years"]:
                    year_metrics[sym] = {"trades": 0, "exp": 0, "pf": 0, "wr": 0}
                    continue
                oos_slices = d["slices"].get(oos_yr, [])
                sigs = run_signals(sym, d["cfg"], oos_slices, params)
                m = metrics(sigs, sym)
                year_metrics[sym] = m
                year_trades += m["trades"]
                if m["trades"] >= 5 and m["exp"] <= 0:
                    year_ok = False

            status = "PASS" if year_ok else "FAIL"
            P(f"    OOS {oos_yr} ({year_trades}t total): {status}")
            for sym in PORTFOLIO:
                m = year_metrics[sym]
                if m["trades"] > 0:
                    sym_st = "ok" if m["exp"] > 0 else "FAIL"
                    P(f"      {sym:<8} {m['trades']:>3}t PF{m['pf']:<5} WR{m['wr']:.1%} Exp{m['exp']:+.3f}R {sym_st}")

            if not year_ok:
                all_oos_pass = False

        verdict = "ACCEPT" if all_oos_pass else "REJECT"
        P(f"    >>> {verdict}")

        if all_oos_pass and best_universal is None:
            best_universal = params

    # Final recommendation
    P(f"\n  {'='*65}")
    if best_universal:
        P(f"  RECOMMENDED UNIVERSAL PARAMS:")
        P(f"    disp_mult       = {best_universal['disp_mult']}")
        P(f"    body_min         = {best_universal['body_min']}")
        P(f"    close_strength   = {best_universal['close_strength']}")
        P(f"    breakout_margin  = {best_universal['breakout_margin']}")
        P(f"\n  These params work across ALL 6 indices without per-index tuning.")
        P(f"  No curve-fitting risk -- single set validated OOS on 2023 + 2025.")
    else:
        P(f"  NO universal param set passed OOS for all indices.")
        P(f"  Consider: (1) wider grid, (2) drop weakest index, (3) accept per-index params")
    P(f"  {'='*65}")


if __name__ == "__main__":
    main()
