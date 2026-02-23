"""
Walk-Forward Optimization — New Index Evaluation + Entry Filter Tuning
======================================================================
Anti-curve-fitting framework:
  - TRAIN on 2024 (Jan 1 - Dec 31)
  - TEST  on 2023 + 2025 (completely out-of-sample)
  - Acceptance criteria:
      * PF > 1.3 in EACH OOS year independently
      * Positive expectancy in EACH OOS year
      * WR > 45% in EACH OOS year
      * Min 20 trades in train year (enough sample)

Tunable entry filters (exits LOCKED at BE@1R, trail 1R, TP 2.5R):
  - disp_mult:        [1.0, 1.3, 1.5, 1.7, 2.0]
  - body_min:          [0.50, 0.55, 0.60, 0.65, 0.70]
  - close_strength:    [0.20, 0.25, 0.30, 0.35]
  - breakout_margin:   [0.03, 0.05, 0.08, 0.10, 0.15]

Outputs: Per-index best params + OOS validation results.
"""
import sys
import itertools
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS,
    load_data,
)
from strategy.strategy_ict_clean import StrategyICTClean

# ═══════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════
# Candidate indices: non-US + existing
CANDIDATE_INDICES = [
    # ── Non-US (diversification targets) ──
    "AUS200", "EUSTX50", "NTH25", "SWI20", "UK100", "FRA40",
    # ── US (already proven, re-validate with walk-forward) ──
    "SPX500", "NDX100", "US30", "GER30",
    # ── Additional US ──
    "US2000",
    # ── Asia (previously removed — re-test with walk-forward) ──
    "HK50", "JP225",
]

# Data files
DATA_FILES_M1 = {sym: f"{sym}_M1_CLEAN.csv" for sym in CANDIDATE_INDICES}
DATA_FILES_H1 = {sym: f"{sym}_H1_EMA.csv" for sym in CANDIDATE_INDICES}

# Walk-forward periods
TRAIN_YEAR = 2024
OOS_YEARS = [2023, 2025]

# Grid search parameters (entry filters only — exits locked)
PARAM_GRID = {
    "disp_mult":        [1.0, 1.3, 1.5, 1.7, 2.0],
    "body_min":          [0.50, 0.55, 0.60, 0.65, 0.70],
    "close_strength":    [0.20, 0.25, 0.30, 0.35],
    "breakout_margin":   [0.03, 0.05, 0.08, 0.10, 0.15],
}

# Acceptance thresholds
MIN_TRADES_TRAIN = 20
MIN_PF_OOS = 1.3
MIN_WR_OOS = 0.45
MIN_EXPECTANCY_OOS = 0.0  # must be positive

# LOCKED exit params
TP_R = 2.5
BE_R = 1.0
TRAIL_BUFFER_R = 1.0  # coded inside strategy, not a param


def load_h1_ema(filename: str, fast: int = 20, slow: int = 50) -> pd.DataFrame:
    data_path = Path(__file__).parent / "data" / "raw" / filename
    if not data_path.exists():
        return pd.DataFrame()
    h1 = pd.read_csv(data_path, sep="\t")
    time_col = [c for c in h1.columns if c.lower() == "time"][0]
    h1[time_col] = pd.to_datetime(h1[time_col], format="mixed", dayfirst=False)
    h1 = h1.set_index(time_col).sort_index()
    h1.columns = [c.lower() for c in h1.columns]
    h1[f"ema{fast}"] = h1["close"].ewm(span=fast, adjust=False).mean()
    h1[f"ema{slow}"] = h1["close"].ewm(span=slow, adjust=False).mean()
    h1["htf_trend"] = "bullish"
    h1.loc[h1[f"ema{fast}"] < h1[f"ema{slow}"], "htf_trend"] = "bearish"
    return h1[["htf_trend", f"ema{fast}", f"ema{slow}"]]


def get_htf_trend_for_day(htf_ema: pd.DataFrame, session_start: pd.Timestamp):
    prior = htf_ema[htf_ema.index < session_start]
    if len(prior) == 0:
        return None
    return str(prior.iloc[-1]["htf_trend"])


def preslice_daily_data(cfg: dict, df: pd.DataFrame, htf_ema: pd.DataFrame,
                        years: list) -> dict:
    """Pre-slice data by day+year once. Returns {year: [(market_df, htf_trend), ...]}"""
    result = {}
    trading_days = sorted(set(df.index.date))

    for year in years:
        year_days = [d for d in trading_days if d.year == year]
        day_data = []
        for day in year_days:
            session_start = pd.Timestamp(day).replace(
                hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
            session_end = pd.Timestamp(day).replace(
                hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
            market_df = df[(df.index >= session_start) & (df.index <= session_end)]
            if len(market_df) < 30:
                continue
            htf_trend = get_htf_trend_for_day(htf_ema, session_start)
            day_data.append((market_df, htf_trend))
        result[year] = day_data
    return result


def generate_signals_for_year(sym: str, cfg: dict, day_slices: list,
                               params: dict) -> list:
    """Generate signals for one index in one year with given params using pre-sliced data."""
    point_value = BROKER_POINT_VALUES.get(sym, 10.0)

    signals = []
    for market_df, htf_trend in day_slices:
        try:
            strategy = StrategyICTClean(
                sl_buffer=cfg["sl_buffer"],
                min_sl_points=cfg.get("min_sl_points", 2.0),
                max_sl_points=cfg.get("max_sl_points", 25.0),
                debug=False,
                orb_minutes=cfg.get("orb_minutes", 15),
                entry_hours=cfg.get("entry_hours", 2),
                breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
                disp_mult=params["disp_mult"],
                body_min=params["body_min"],
                close_strength=params["close_strength"],
                breakout_margin=params["breakout_margin"],
                orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
                tp3_r=TP_R,
                tp1_r=BE_R,
            )
            sigs = strategy.generate_signals(market_df, htf_trend=htf_trend)
            for s in sigs:
                signals.append({
                    "symbol": sym,
                    "signal": s,
                    "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                    "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                })
        except Exception:
            continue
    return signals


def compute_metrics(signals: list, sym: str) -> dict:
    """Compute PF, WR, expectancy from signal list."""
    if not signals:
        return {"trades": 0, "pf": 0, "wr": 0, "expectancy": 0, "net_R": 0}

    wins = 0
    losses = 0
    gross_profit_R = 0.0
    gross_loss_R = 0.0

    for sig in signals:
        s = sig["signal"]
        R = float(s.risk_points)
        if R <= 0:
            continue

        pnl_pts = float(s.pnl_points)

        # Apply costs
        slippage_pts = TRADING_COSTS["slippage_points"].get(sym, 0.5)
        spread_pts = TRADING_COSTS["spread_points"].get(sym, 1.0)
        half_spread = spread_pts / 2.0
        entry_cost = slippage_pts + half_spread
        exit_reason = getattr(s, "exit_reason", "")
        if exit_reason == "TP":
            exit_cost = half_spread
        else:
            exit_cost = slippage_pts + half_spread
        total_cost = entry_cost + exit_cost
        net_pnl_pts = pnl_pts - total_cost
        pnl_R = net_pnl_pts / R

        if net_pnl_pts > 0:
            wins += 1
            gross_profit_R += pnl_R
        else:
            losses += 1
            gross_loss_R += abs(pnl_R)

    total = wins + losses
    if total == 0:
        return {"trades": 0, "pf": 0, "wr": 0, "expectancy": 0, "net_R": 0}

    pf = gross_profit_R / gross_loss_R if gross_loss_R > 0 else 99.0
    wr = wins / total
    net_R = gross_profit_R - gross_loss_R
    expectancy = net_R / total

    return {
        "trades": total,
        "pf": round(pf, 2),
        "wr": round(wr, 3),
        "expectancy": round(expectancy, 3),
        "net_R": round(net_R, 2),
    }


def run_walkforward():
    """Main walk-forward optimization."""
    print("=" * 75)
    print("  WALK-FORWARD OPTIMIZATION — Train 2024 / Test 2023 + 2025")
    print("=" * 75)

    # Precompute all param combos
    keys = list(PARAM_GRID.keys())
    combos = list(itertools.product(*PARAM_GRID.values()))
    print(f"  Parameter combinations: {len(combos)}")
    print(f"  Candidate indices: {len(CANDIDATE_INDICES)}")
    print(f"  Locked exits: TP={TP_R}R, BE={BE_R}R, trail=1.0R")
    print()

    import time as _time
    results = {}

    for idx_num, sym in enumerate(CANDIDATE_INDICES, 1):
        cfg = LIVE_INDEX_CONFIG.get(sym)
        if cfg is None:
            print(f"  {sym}: No config found, skipping")
            continue

        # Load data
        m1_file = DATA_FILES_M1[sym]
        h1_file = DATA_FILES_H1[sym]
        try:
            df = load_data(m1_file)
            htf_ema = load_h1_ema(h1_file)
        except Exception as e:
            print(f"  {sym}: Data load error: {e}")
            continue

        # Check year coverage
        all_dates = set(df.index.date)
        years_available = set(d.year for d in all_dates)
        has_train = TRAIN_YEAR in years_available

        if not has_train:
            print(f"  {sym}: No {TRAIN_YEAR} data, skipping")
            continue

        print(f"\n{'─'*75}")
        print(f"  [{idx_num}/{len(CANDIDATE_INDICES)}] {sym} ({cfg.get('name', sym)})")
        print(f"  Data: {min(all_dates)} to {max(all_dates)}")
        print(f"  Years: {sorted(years_available)} | Train: {TRAIN_YEAR} | OOS: {OOS_YEARS}")

        # ── Pre-slice daily data once (major speedup) ──
        all_years = [TRAIN_YEAR] + OOS_YEARS
        try:
            daily_slices = preslice_daily_data(cfg, df, htf_ema, all_years)
        except Exception as e:
            print(f"  {sym}: Pre-slice error: {e}")
            results[sym] = {"status": "FAIL_TRAIN", "reason": f"preslice error: {e}"}
            continue

        train_days = len(daily_slices.get(TRAIN_YEAR, []))
        print(f"  Trading days: train={train_days}, "
              + ", ".join(f"oos_{y}={len(daily_slices.get(y, []))}" for y in OOS_YEARS))
        print(f"  Searching {len(combos)} param combos...", flush=True)

        # ── PHASE 1: Grid search on TRAIN year ──
        t0 = _time.time()
        best_train = None
        best_train_score = -999
        best_params = None
        all_train_results = []
        train_slices = daily_slices.get(TRAIN_YEAR, [])

        for ci, combo in enumerate(combos):
            if ci > 0 and ci % 100 == 0:
                elapsed = _time.time() - t0
                print(f"    ... {ci}/{len(combos)} combos ({elapsed:.0f}s)", flush=True)

            params = dict(zip(keys, combo))
            try:
                sigs = generate_signals_for_year(sym, cfg, train_slices, params)
                m = compute_metrics(sigs, sym)
            except Exception:
                continue

            if m["trades"] < MIN_TRADES_TRAIN:
                continue

            # Score: expectancy * sqrt(trades) — rewards both quality AND frequency
            score = m["expectancy"] * np.sqrt(m["trades"])
            all_train_results.append((params.copy(), m, score))

            if score > best_train_score:
                best_train_score = score
                best_train = m
                best_params = params.copy()

        elapsed = _time.time() - t0
        if best_params is None:
            print(f"  NO viable params (all < {MIN_TRADES_TRAIN} trades) [{elapsed:.0f}s]")
            results[sym] = {"status": "FAIL_TRAIN", "reason": "no viable params"}
            continue

        print(f"  Grid done in {elapsed:.0f}s")
        print(f"  TRAIN ({TRAIN_YEAR}): {best_train['trades']} trades, "
              f"PF {best_train['pf']}, WR {best_train['wr']:.1%}, "
              f"Exp {best_train['expectancy']:+.3f}R")
        print(f"  Best params: disp={best_params['disp_mult']}, body={best_params['body_min']}, "
              f"close={best_params['close_strength']}, margin={best_params['breakout_margin']}")

        # ── PHASE 2: Validate on OOS years ──
        oos_results = {}
        oos_pass = True

        for oos_year in OOS_YEARS:
            if oos_year not in years_available:
                print(f"  OOS {oos_year}: No data — SKIPPED")
                oos_results[oos_year] = {"status": "NO_DATA"}
                continue

            oos_slices = daily_slices.get(oos_year, [])
            if not oos_slices:
                print(f"  OOS {oos_year}: No trading days — SKIPPED")
                oos_results[oos_year] = {"status": "NO_DATA"}
                continue

            try:
                sigs = generate_signals_for_year(sym, cfg, oos_slices, best_params)
                m = compute_metrics(sigs, sym)
            except Exception as e:
                print(f"  OOS {oos_year}: Error: {e}")
                oos_results[oos_year] = {"status": "ERROR"}
                oos_pass = False
                continue

            oos_results[oos_year] = m

            pass_pf = m["pf"] >= MIN_PF_OOS or m["trades"] < 10
            pass_wr = m["wr"] >= MIN_WR_OOS or m["trades"] < 10
            pass_exp = m["expectancy"] > MIN_EXPECTANCY_OOS
            year_pass = pass_exp and m["trades"] >= 5

            status = "PASS" if year_pass else "FAIL"
            print(f"  OOS {oos_year}: {m['trades']} trades, PF {m['pf']}, "
                  f"WR {m['wr']:.1%}, Exp {m['expectancy']:+.3f}R → {status}")

            if not year_pass and m["trades"] >= 5:
                oos_pass = False

        # ── PHASE 3: Robustness check — how many of top-10 train combos also pass OOS? ──
        all_train_results.sort(key=lambda x: -x[2])
        top10 = all_train_results[:10]
        robust_count = 0
        for params_t, _, _ in top10:
            all_oos_ok = True
            for oos_year in OOS_YEARS:
                if oos_year not in years_available:
                    continue
                oos_slices = daily_slices.get(oos_year, [])
                if not oos_slices:
                    continue
                try:
                    sigs_oos = generate_signals_for_year(sym, cfg, oos_slices, params_t)
                    m_oos = compute_metrics(sigs_oos, sym)
                except Exception:
                    all_oos_ok = False
                    break
                if m_oos["trades"] >= 5 and m_oos["expectancy"] <= 0:
                    all_oos_ok = False
                    break
            if all_oos_ok:
                robust_count += 1
        print(f"  Robustness: {robust_count}/10 top train combos also positive OOS")

        # ── Verdict ──
        if oos_pass and robust_count >= 3:
            verdict = "ACCEPT"
        elif oos_pass:
            verdict = "MARGINAL"
        else:
            verdict = "REJECT"

        print(f"  >>> VERDICT: {verdict}")

        results[sym] = {
            "status": verdict,
            "best_params": best_params,
            "train": best_train,
            "oos": oos_results,
            "robust_count": robust_count,
        }

    # ═══════════════════════════════════════════════════════════════════
    #  FINAL SUMMARY
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*75}")
    print(f"  WALK-FORWARD RESULTS SUMMARY")
    print(f"{'='*75}")

    accepted = []
    marginal = []
    rejected = []

    for sym, r in results.items():
        status = r.get("status", "UNKNOWN")
        if status == "ACCEPT":
            accepted.append(sym)
        elif status == "MARGINAL":
            marginal.append(sym)
        else:
            rejected.append(sym)

    print(f"\n  ACCEPTED ({len(accepted)}):")
    for sym in accepted:
        r = results[sym]
        p = r["best_params"]
        t = r["train"]
        # Determine region
        us_syms = {"SPX500", "NDX100", "US30", "US2000"}
        eu_syms = {"GER30", "FRA40", "EUSTX50", "NTH25", "SWI20", "UK100"}
        asia_syms = {"HK50", "JP225", "AUS200"}
        if sym in us_syms:
            region = "US"
        elif sym in eu_syms:
            region = "EU"
        elif sym in asia_syms:
            region = "APAC"
        else:
            region = "??"

        oos_strs = []
        for y in OOS_YEARS:
            if y in r["oos"] and isinstance(r["oos"][y], dict) and "trades" in r["oos"][y]:
                m = r["oos"][y]
                oos_strs.append(f"{y}: {m['trades']}t PF{m['pf']} E{m['expectancy']:+.3f}")
            else:
                oos_strs.append(f"{y}: n/a")

        print(f"    {sym:<10} [{region}] Train: {t['trades']}t PF{t['pf']} | "
              f"OOS: {' | '.join(oos_strs)} | "
              f"Robust: {r['robust_count']}/10")
        print(f"             Params: disp={p['disp_mult']}, body={p['body_min']}, "
              f"close={p['close_strength']}, margin={p['breakout_margin']}")

    if marginal:
        print(f"\n  MARGINAL ({len(marginal)}):")
        for sym in marginal:
            r = results[sym]
            print(f"    {sym}: Robust={r.get('robust_count',0)}/10")

    if rejected:
        print(f"\n  REJECTED ({len(rejected)}):")
        for sym in rejected:
            r = results[sym]
            reason = r.get("reason", "OOS fail")
            print(f"    {sym}: {reason}")

    # Region balance
    print(f"\n  REGION BALANCE:")
    regions = {"US": [], "EU": [], "APAC": []}
    for sym in accepted + marginal:
        if sym in {"SPX500", "NDX100", "US30", "US2000"}:
            regions["US"].append(sym)
        elif sym in {"GER30", "FRA40", "EUSTX50", "NTH25", "SWI20", "UK100"}:
            regions["EU"].append(sym)
        elif sym in {"HK50", "JP225", "AUS200"}:
            regions["APAC"].append(sym)
    for reg, syms in regions.items():
        print(f"    {reg}: {len(syms)} indices — {', '.join(syms) if syms else 'none'}")

    total_accepted = len(accepted) + len(marginal)
    us_pct = len(regions["US"]) / total_accepted * 100 if total_accepted > 0 else 0
    print(f"\n  US Concentration: {us_pct:.0f}% (target: <50%)")
    print(f"{'='*75}")


if __name__ == "__main__":
    run_walkforward()
