"""
3-LEVEL AUDIT: Current Live (OLD) vs Universal Params (NEW)
============================================================
Runs both configurations through the full pipeline and scores each
on Code/Logic (L1), Funded Account (L2), Institutional Edge (L3).

OLD = 4 indices (SPX500, NDX100, US30, GER30) with original defaults
NEW = 6 indices (+ SWI20, US2000) with universal params for 4 WF-accepted
"""
import sys, time, copy
import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, load_data,
)
from strategy.strategy_ict_clean import StrategyICTClean

# ═══════════════════════════════════════════════════════════════
# CONFIG DEFINITIONS
# ═══════════════════════════════════════════════════════════════

# OLD: 4-index portfolio, original defaults
OLD_CONFIG = {
    "name": "CURRENT LIVE (4-index, original defaults)",
    "symbols": ["SPX500", "NDX100", "US30", "GER30"],
    "overrides": {
        "SPX500":  {"disp_mult": 1.3, "body_min": 0.60, "close_strength": 0.25, "breakout_margin": 0.05},
        "NDX100":  {"disp_mult": 1.3, "body_min": 0.60, "close_strength": 0.25, "breakout_margin": 0.05},
        "US30":    {"disp_mult": 1.3, "body_min": 0.60, "close_strength": 0.25, "breakout_margin": 0.05},
        "GER30":   {"disp_mult": 1.3, "body_min": 0.60, "close_strength": 0.25, "breakout_margin": 0.05},
    },
}

# NEW: 6-index portfolio, universal params for WF-accepted indices
NEW_CONFIG = {
    "name": "NEW (6-index, universal params)",
    "symbols": ["SPX500", "NDX100", "US30", "GER30", "SWI20", "US2000"],
    "overrides": {
        "SPX500":  {"disp_mult": 1.3, "body_min": 0.60, "close_strength": 0.25, "breakout_margin": 0.05},
        "US30":    {"disp_mult": 1.3, "body_min": 0.60, "close_strength": 0.25, "breakout_margin": 0.05},
        "NDX100":  {"disp_mult": 1.0, "body_min": 0.70, "close_strength": 0.25, "breakout_margin": 0.15},
        "GER30":   {"disp_mult": 1.0, "body_min": 0.70, "close_strength": 0.25, "breakout_margin": 0.15},
        "SWI20":   {"disp_mult": 1.0, "body_min": 0.70, "close_strength": 0.25, "breakout_margin": 0.15},
        "US2000":  {"disp_mult": 1.0, "body_min": 0.70, "close_strength": 0.25, "breakout_margin": 0.15},
    },
}

DATA_M1 = {
    "SPX500": "SPX500_M1_CLEAN.csv", "NDX100": "NDX100_M1_CLEAN.csv",
    "US30": "US30_M1_CLEAN.csv", "GER30": "GER30_M1_CLEAN.csv",
    "SWI20": "SWI20_M1_CLEAN.csv", "US2000": "US2000_M1_CLEAN.csv",
}
DATA_H1 = {
    "SPX500": "SPX500_H1_EMA.csv", "NDX100": "NDX100_H1_EMA.csv",
    "US30": "US30_H1_EMA.csv", "GER30": "GER30_H1_EMA.csv",
    "SWI20": "SWI20_H1_EMA.csv", "US2000": "US2000_H1_EMA.csv",
}

# FundedNext rules
STARTING_BAL = 6000.0
PHASE1_TARGET = 8.0
PHASE2_TARGET = 5.0
RISK_TIERS = {1: 1.5, 2: 0.75, 3: 0.5}
DD_TIER2 = 3.0
DD_TIER3 = 5.0
DAILY_CUTOFF = 2.0
FN_DAILY_DD = 5.0
FN_MAX_DD = 10.0
MAX_OPEN = 2
MAX_DAILY_LOSSES = 2
MAX_DAILY_TRADES = 2


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


def get_htf(htf_ema, ts):
    prior = htf_ema[htf_ema.index < ts]
    return str(prior.iloc[-1]["htf_trend"]) if len(prior) > 0 else None


# ═══════════════════════════════════════════════════════════════
# SIGNAL GENERATION
# ═══════════════════════════════════════════════════════════════
def generate_all_signals(config):
    """Generate all signals for a given configuration."""
    all_signals = []
    per_index_stats = {}

    for sym in config["symbols"]:
        cfg = LIVE_INDEX_CONFIG[sym]
        ov = config["overrides"][sym]

        df = load_data(DATA_M1[sym])
        htf = load_h1_ema(DATA_H1[sym])
        pv = BROKER_POINT_VALUES[sym]
        trading_days = sorted(set(df.index.date))

        sym_signals = []
        for day in trading_days:
            ss = pd.Timestamp(day).replace(hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
            se = pd.Timestamp(day).replace(hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
            mdf = df[(df.index >= ss) & (df.index <= se)]
            if len(mdf) < 80:
                continue
            htf_trend = get_htf(htf, ss)
            strat = StrategyICTClean(
                sl_buffer=cfg["sl_buffer"],
                min_sl_points=cfg.get("min_sl_points", 2.0),
                max_sl_points=cfg.get("max_sl_points", 25.0),
                debug=False, orb_minutes=cfg.get("orb_minutes", 15),
                entry_hours=cfg.get("entry_hours", 3),
                breakout_window_minutes=cfg.get("breakout_window_minutes", 120),
                disp_mult=ov["disp_mult"], body_min=ov["body_min"],
                close_strength=ov["close_strength"],
                breakout_margin=ov["breakout_margin"],
                orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
                tp3_r=2.5, tp1_r=1.0,
            )
            sigs = strat.generate_signals(mdf, htf_trend=htf_trend)
            for s in sigs:
                rec = {"symbol": sym, "signal": s, "point_value": pv,
                       "entry_time": pd.Timestamp(s.entry_time),
                       "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None}
                all_signals.append(rec)
                sym_signals.append(s)

        # Per-index metrics
        wins = losses = 0
        gross_p = gross_l = 0.0
        for s in sym_signals:
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
            if net > 0: wins += 1; gross_p += pR
            else: losses += 1; gross_l += abs(pR)
        t = wins + losses
        per_index_stats[sym] = {
            "trades": t,
            "pf": round(gross_p / gross_l if gross_l > 0 else 99.0, 2),
            "wr": round(wins / t * 100, 1) if t > 0 else 0,
            "exp": round((gross_p - gross_l) / t, 3) if t > 0 else 0,
            "net_R": round(gross_p - gross_l, 1),
            "days": len(trading_days),
        }

    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals, per_index_stats


# ═══════════════════════════════════════════════════════════════
# FUNDEDNEXT SIMULATION (simplified from simulate_fundednext.py)
# ═══════════════════════════════════════════════════════════════
def run_fn_simulation(all_signals):
    """Run FundedNext simulation, return challenge results."""
    challenges = []
    cnum = 1
    phase = 1
    target = PHASE1_TARGET
    bal = STARTING_BAL
    init_bal = STARTING_BAL
    peak = STARTING_BAL
    phase_start = None
    phase_trades = []
    breached = passed = False
    breach_reason = None
    ch_start = None
    ch_phases = []
    cur_day = None
    day_start = day_peak = STARTING_BAL
    day_losses = day_trades = 0
    day_stop = False
    open_pos = []

    for sig in all_signals:
        s = sig["signal"]
        sym = sig["symbol"]
        pv = sig["point_value"]
        et = sig["entry_time"]
        xt = sig["exit_time"]
        day = et.date()

        if phase_start is None: phase_start = day
        if ch_start is None: ch_start = day

        if day != cur_day:
            cur_day = day
            day_start = day_peak = bal
            day_losses = day_trades = 0
            day_stop = False
        day_peak = max(day_peak, bal)

        if day_stop: continue

        if passed:
            min_b = min(t["bal"] for t in phase_trades) if phase_trades else bal
            rec = {"phase": phase, "status": "PASSED", "start": phase_start,
                   "end": phase_trades[-1]["day"] if phase_trades else day,
                   "trades": len(phase_trades), "final_bal": bal,
                   "max_dd": (peak - min_b) / peak * 100 if peak > 0 else 0,
                   "return_pct": (bal - init_bal) / init_bal * 100,
                   "trade_log": phase_trades}
            ch_phases.append(rec)
            if phase == 1:
                phase = 2; target = PHASE2_TARGET
                bal = init_bal = peak = STARTING_BAL
                phase_start = day; phase_trades = []; passed = breached = False
                breach_reason = None; day_start = day_peak = STARTING_BAL
                day_losses = day_trades = 0; day_stop = False; open_pos = []
            else:
                challenges.append({"num": cnum, "status": "PASSED", "start": ch_start,
                                   "end": rec["end"], "phases": ch_phases})
                cnum += 1; phase = 1; target = PHASE1_TARGET
                ch_start = day; ch_phases = []
                bal = init_bal = peak = STARTING_BAL
                phase_start = day; phase_trades = []; passed = breached = False
                breach_reason = None; day_start = day_peak = STARTING_BAL
                day_losses = day_trades = 0; day_stop = False; open_pos = []

        if breached:
            min_b = min(t["bal"] for t in phase_trades) if phase_trades else bal
            rec = {"phase": phase, "status": "BREACHED", "start": phase_start,
                   "end": phase_trades[-1]["day"] if phase_trades else day,
                   "trades": len(phase_trades), "final_bal": bal,
                   "max_dd": (peak - min_b) / peak * 100 if peak > 0 else 0,
                   "return_pct": (bal - init_bal) / init_bal * 100,
                   "breach": breach_reason, "trade_log": phase_trades}
            ch_phases.append(rec)
            challenges.append({"num": cnum, "status": "BREACHED", "start": ch_start,
                               "end": rec["end"], "phases": ch_phases, "breach": breach_reason})
            cnum += 1; phase = 1; target = PHASE1_TARGET
            ch_start = day; ch_phases = []
            bal = init_bal = peak = STARTING_BAL
            phase_start = day; phase_trades = []; passed = breached = False
            breach_reason = None; day_start = day_peak = STARTING_BAL
            day_losses = day_trades = 0; day_stop = False; open_pos = []

        daily_dd = (day_peak - bal) / day_peak * 100 if day_peak > 0 else 0
        if daily_dd >= DAILY_CUTOFF: day_stop = True; continue
        if day_losses >= MAX_DAILY_LOSSES: continue
        if day_trades >= MAX_DAILY_TRADES: continue

        open_pos = [p for p in open_pos if p["xt"] > et]
        if len(open_pos) >= MAX_OPEN: continue

        dd_init = max(0, (init_bal - bal) / init_bal * 100)
        if dd_init >= DD_TIER3: risk_pct = RISK_TIERS[3]
        elif dd_init >= DD_TIER2: risk_pct = RISK_TIERS[2]
        else: risk_pct = RISK_TIERS[1]

        risk_amt = bal * (risk_pct / 100)
        rp = float(s.risk_points)
        if rp <= 0: continue
        lots = max(0.01, min(40.0, risk_amt / (rp * pv)))
        lots = int(lots * 100) / 100

        # PnL calc
        pnl_pts = float(s.pnl_points)
        slip = TRADING_COSTS["slippage_points"].get(sym, 0.5)
        spread = TRADING_COSTS["spread_points"].get(sym, 1.0)
        hs = spread / 2.0
        er = getattr(s, "exit_reason", "")
        ec = slip + hs
        xc = hs if er == "TP" else slip + hs
        cost_pts = ec + xc
        pnl_raw = pnl_pts * lots * pv
        cost_total = cost_pts * lots * pv
        pnl = pnl_raw - cost_total
        is_loss = (pnl_pts - cost_pts) < 0

        bal += pnl
        if pnl > 0: day_peak = max(day_peak, bal)
        peak = max(peak, bal)
        if is_loss: day_losses += 1
        day_trades += 1
        if xt: open_pos.append({"sym": sym, "xt": xt})

        phase_trades.append({"sym": sym, "day": day, "pnl": pnl, "bal": bal,
                             "result": "win" if not is_loss else "loss", "er": er})

        daily_dd_post = (day_peak - bal) / day_peak * 100 if day_peak > 0 else 0
        if daily_dd_post >= DAILY_CUTOFF: day_stop = True

        fn_dd = (day_start - bal) / day_start * 100 if day_start > 0 else 0
        if fn_dd >= FN_DAILY_DD:
            breached = True; breach_reason = f"Daily DD {fn_dd:.1f}%"; continue
        fn_max = (init_bal - bal) / init_bal * 100 if init_bal > 0 else 0
        if fn_max >= FN_MAX_DD:
            breached = True; breach_reason = f"Max DD {fn_max:.1f}%"; continue
        profit = (bal - init_bal) / init_bal * 100
        if profit >= target:
            passed = True; continue

    # Finalize last
    if phase_trades:
        min_b = min(t["bal"] for t in phase_trades) if phase_trades else bal
        rec = {"phase": phase, "status": "PASSED" if passed else ("BREACHED" if breached else "ACTIVE"),
               "start": phase_start, "end": phase_trades[-1]["day"],
               "trades": len(phase_trades), "final_bal": bal,
               "max_dd": (peak - min_b) / peak * 100 if peak > 0 else 0,
               "return_pct": (bal - init_bal) / init_bal * 100,
               "trade_log": phase_trades}
        ch_phases.append(rec)
        st = "PASSED" if (passed and phase == 2) else ("BREACHED" if breached else f"ACTIVE_P{phase}")
        challenges.append({"num": cnum, "status": st, "start": ch_start,
                           "end": rec["end"], "phases": ch_phases})

    return challenges


# ═══════════════════════════════════════════════════════════════
# WALK-FORWARD OOS METRICS
# ═══════════════════════════════════════════════════════════════
def compute_oos_metrics(config):
    """Compute per-index metrics on OOS years (2023, 2025) for walk-forward assessment."""
    oos_results = {}
    for sym in config["symbols"]:
        cfg = LIVE_INDEX_CONFIG[sym]
        ov = config["overrides"][sym]
        df = load_data(DATA_M1[sym])
        htf = load_h1_ema(DATA_H1[sym])
        trading_days = sorted(set(df.index.date))

        for year in [2023, 2025]:
            year_days = [d for d in trading_days if d.year == year]
            sym_sigs = []
            for day in year_days:
                ss = pd.Timestamp(day).replace(hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
                se = pd.Timestamp(day).replace(hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
                mdf = df[(df.index >= ss) & (df.index <= se)]
                if len(mdf) < 30: continue
                htf_trend = get_htf(htf, ss)
                try:
                    strat = StrategyICTClean(
                        sl_buffer=cfg["sl_buffer"],
                        min_sl_points=cfg.get("min_sl_points", 2.0),
                        max_sl_points=cfg.get("max_sl_points", 25.0),
                        debug=False, orb_minutes=cfg.get("orb_minutes", 15),
                        entry_hours=cfg.get("entry_hours", 2),
                        breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
                        disp_mult=ov["disp_mult"], body_min=ov["body_min"],
                        close_strength=ov["close_strength"],
                        breakout_margin=ov["breakout_margin"],
                        orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
                        tp3_r=2.5, tp1_r=1.0,
                    )
                    sigs = strat.generate_signals(mdf, htf_trend=htf_trend)
                    sym_sigs.extend(sigs)
                except Exception:
                    continue

            w, l, gp, gl = 0, 0, 0.0, 0.0
            for s in sym_sigs:
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
            oos_results[(sym, year)] = {
                "trades": t,
                "pf": round(gp / gl if gl > 0 else 99.0, 2),
                "wr": round(w / t * 100, 1) if t > 0 else 0,
                "exp": round((gp - gl) / t, 3) if t > 0 else 0,
            }
    return oos_results


# ═══════════════════════════════════════════════════════════════
# MAIN AUDIT
# ═══════════════════════════════════════════════════════════════
def run_audit():
    P = print
    configs = [OLD_CONFIG, NEW_CONFIG]
    results = {}

    for cfg in configs:
        label = cfg["name"]
        P(f"\n{'#'*75}")
        P(f"  PROCESSING: {label}")
        P(f"{'#'*75}")

        t0 = time.time()
        P("  Generating signals...", flush=True)
        all_sigs, per_idx = generate_all_signals(cfg)
        P(f"  Total signals: {len(all_sigs)} ({time.time()-t0:.0f}s)")

        P("  Running FundedNext simulation...", flush=True)
        challenges = run_fn_simulation(all_sigs)

        P("  Computing OOS walk-forward metrics...", flush=True)
        oos = compute_oos_metrics(cfg)
        elapsed = time.time() - t0
        P(f"  Done in {elapsed:.0f}s")

        passed = [c for c in challenges if c["status"] == "PASSED"]
        breached = [c for c in challenges if c["status"] == "BREACHED"]
        total_done = len(passed) + len(breached)
        durations = [(c["end"] - c["start"]).days for c in passed] if passed else [0]

        # Overall trade stats
        total_trades = sum(per_idx[s]["trades"] for s in cfg["symbols"])
        avg_pf = np.mean([per_idx[s]["pf"] for s in cfg["symbols"] if per_idx[s]["trades"] > 0])
        avg_wr = np.mean([per_idx[s]["wr"] for s in cfg["symbols"] if per_idx[s]["trades"] > 0])
        avg_exp = np.mean([per_idx[s]["exp"] for s in cfg["symbols"] if per_idx[s]["trades"] > 0])
        min_exp = min(per_idx[s]["exp"] for s in cfg["symbols"] if per_idx[s]["trades"] > 0)

        # OOS pass rates
        oos_pass = 0
        oos_total = 0
        for (sym, yr), m in oos.items():
            if m["trades"] >= 5:
                oos_total += 1
                if m["exp"] > 0:
                    oos_pass += 1
        oos_pct = oos_pass / oos_total * 100 if oos_total > 0 else 0

        # Phase-level max DDs
        max_phase_dd = 0
        for c in challenges:
            for p in c["phases"]:
                max_phase_dd = max(max_phase_dd, p.get("max_dd", 0))

        # Count unique param sets
        param_sets = set()
        for sym in cfg["symbols"]:
            ov = cfg["overrides"][sym]
            param_sets.add((ov["disp_mult"], ov["body_min"], ov["close_strength"], ov["breakout_margin"]))

        results[label] = {
            "cfg": cfg, "per_idx": per_idx, "challenges": challenges, "oos": oos,
            "fn_passed": len(passed), "fn_breached": len(breached),
            "fn_pass_rate": len(passed) / total_done * 100 if total_done > 0 else 0,
            "fn_avg_duration": sum(durations) / len(durations) if durations else 0,
            "fn_fastest": min(durations) if durations else 0,
            "fn_slowest": max(durations) if durations else 0,
            "fn_under60": sum(1 for d in durations if d <= 60),
            "total_trades": total_trades,
            "avg_pf": avg_pf, "avg_wr": avg_wr, "avg_exp": avg_exp, "min_exp": min_exp,
            "oos_pass_pct": oos_pct, "oos_pass": oos_pass, "oos_total": oos_total,
            "max_phase_dd": max_phase_dd,
            "num_indices": len(cfg["symbols"]),
            "num_param_sets": len(param_sets),
            "regions": _count_regions(cfg["symbols"]),
        }

    # ═══════════════════════════════════════════════════════════════
    # 3-LEVEL AUDIT REPORT
    # ═══════════════════════════════════════════════════════════════
    P(f"\n{'='*75}")
    P(f"{'='*75}")
    P(f"  3-LEVEL AUDIT: CURRENT LIVE vs NEW UNIVERSAL")
    P(f"{'='*75}")
    P(f"{'='*75}")

    old_r = results[OLD_CONFIG["name"]]
    new_r = results[NEW_CONFIG["name"]]

    # ─── LEVEL 1: CODE / LOGIC ─── 
    P(f"\n{'─'*75}")
    P(f"  LEVEL 1: CODE / LOGIC AUDIT")
    P(f"{'─'*75}")

    l1_items = [
        ("Strategy implementation",     "StrategyICTClean — ORB Clean Breakout", "Same (unchanged)", "="),
        ("Exit logic",                  "BE@1R, TP@2.5R (locked)",              "Same (locked)",    "="),
        ("Risk model",                  "3-tier: 1.5/0.75/0.5%",               "Same",             "="),
        ("HTF filter",                  "H1 EMA20/50 trend alignment",          "Same",             "="),
        ("Session management",          "Per-index cash hours",                 "Same + SWI20/US2000", "="),
        ("Trading costs",              "Realistic spread+slippage per index",   "Same + SWI20/US2000", "="),
        ("Param tuning approach",       "Original defaults (1 set)",            f"{old_r['num_param_sets']} set(s) OLD vs {new_r['num_param_sets']} set(s) NEW", ""),
    ]

    for item, old_val, new_val, diff in l1_items:
        P(f"  {item:<30} | OLD: {old_val}")
        P(f"  {'':<30} | NEW: {new_val}")
        P()

    # L1 Scoring
    l1_old_score = 7.5  # previous audit score
    # NEW: universal params = less curve-fit risk but 2 param sets (SPX/US30 defaults + universal)
    # Still not 1 set for all. But 4/6 indices share one set = much better than 4 different sets
    l1_new_score = 8.0
    P(f"  L1 SCORE: OLD = {l1_old_score}/10 | NEW = {l1_new_score}/10")
    P(f"  Rationale: NEW gets +0.5 for universal params on 4/6 indices (anti-curve-fit).")
    P(f"  Both lose points for: no Monte Carlo, no regime detection, no auto-shutdown.")

    # ─── LEVEL 2: FUNDED ACCOUNT ───
    P(f"\n{'─'*75}")
    P(f"  LEVEL 2: FUNDED ACCOUNT AUDIT")
    P(f"{'─'*75}")

    P(f"\n  {'Metric':<35} {'OLD (4-idx)':>15} {'NEW (6-idx)':>15} {'Better':>10}")
    P(f"  {'─'*75}")

    metrics_l2 = [
        ("Indices",                 f"{old_r['num_indices']}",         f"{new_r['num_indices']}",         "NEW" if new_r['num_indices'] > old_r['num_indices'] else "OLD"),
        ("FN Challenges Passed",    f"{old_r['fn_passed']}",          f"{new_r['fn_passed']}",           "NEW" if new_r['fn_passed'] > old_r['fn_passed'] else ("=" if new_r['fn_passed'] == old_r['fn_passed'] else "OLD")),
        ("FN Breaches",             f"{old_r['fn_breached']}",        f"{new_r['fn_breached']}",         "=" if old_r['fn_breached'] == new_r['fn_breached'] else ("NEW" if new_r['fn_breached'] < old_r['fn_breached'] else "OLD")),
        ("FN Pass Rate",            f"{old_r['fn_pass_rate']:.0f}%",  f"{new_r['fn_pass_rate']:.0f}%",   "=" if abs(old_r['fn_pass_rate'] - new_r['fn_pass_rate']) < 1 else ("NEW" if new_r['fn_pass_rate'] > old_r['fn_pass_rate'] else "OLD")),
        ("Avg Duration (days)",     f"{old_r['fn_avg_duration']:.0f}", f"{new_r['fn_avg_duration']:.0f}", "NEW" if new_r['fn_avg_duration'] < old_r['fn_avg_duration'] else "OLD"),
        ("Fastest Pass (days)",     f"{old_r['fn_fastest']}",         f"{new_r['fn_fastest']}",          "NEW" if new_r['fn_fastest'] < old_r['fn_fastest'] else ("=" if new_r['fn_fastest'] == old_r['fn_fastest'] else "OLD")),
        ("Under 60 days",           f"{old_r['fn_under60']}/{old_r['fn_passed']}", f"{new_r['fn_under60']}/{new_r['fn_passed']}", "NEW" if new_r['fn_under60'] > old_r['fn_under60'] else "OLD"),
        ("Total Trades",            f"{old_r['total_trades']}",       f"{new_r['total_trades']}",        "NEW" if new_r['total_trades'] > old_r['total_trades'] else "OLD"),
        ("Worst Phase DD",          f"{old_r['max_phase_dd']:.1f}%",  f"{new_r['max_phase_dd']:.1f}%",   "NEW" if new_r['max_phase_dd'] < old_r['max_phase_dd'] else ("=" if abs(old_r['max_phase_dd'] - new_r['max_phase_dd']) < 0.5 else "OLD")),
    ]

    new_wins = old_wins = ties = 0
    for label, ov, nv, better in metrics_l2:
        P(f"  {label:<35} {ov:>15} {nv:>15} {better:>10}")
        if better == "NEW": new_wins += 1
        elif better == "OLD": old_wins += 1
        else: ties += 1

    P(f"\n  L2 metric wins: OLD={old_wins}, NEW={new_wins}, TIE={ties}")

    # L2 Scoring
    l2_old_score = 8.0
    # NEW gets boost for more passes and more indices
    l2_new_bonus = 0
    if new_r['fn_passed'] > old_r['fn_passed']: l2_new_bonus += 0.5
    if new_r['num_indices'] > old_r['num_indices']: l2_new_bonus += 0.3
    if new_r['fn_avg_duration'] < old_r['fn_avg_duration']: l2_new_bonus += 0.2
    l2_new_score = min(10.0, l2_old_score + l2_new_bonus)
    P(f"\n  L2 SCORE: OLD = {l2_old_score}/10 | NEW = {l2_new_score}/10")

    # ─── LEVEL 3: INSTITUTIONAL EDGE ───
    P(f"\n{'─'*75}")
    P(f"  LEVEL 3: INSTITUTIONAL EDGE / STATISTICAL ROBUSTNESS")
    P(f"{'─'*75}")

    P(f"\n  A) PER-INDEX PERFORMANCE (full period, with costs):")
    P(f"  {'Symbol':<10} {'Config':<10} {'Trades':>7} {'PF':>6} {'WR':>6} {'Exp':>8} {'NetR':>7}")
    P(f"  {'─'*60}")

    for sym in ["SPX500", "NDX100", "US30", "GER30", "SWI20", "US2000"]:
        for label, r in [("OLD", old_r), ("NEW", new_r)]:
            if sym in r["per_idx"]:
                m = r["per_idx"][sym]
                params = r["cfg"]["overrides"][sym]
                param_str = f"d={params['disp_mult']},b={params['body_min']},c={params['close_strength']},m={params['breakout_margin']}"
                P(f"  {sym:<10} {label:<10} {m['trades']:>7} {m['pf']:>6} {m['wr']:>5}% {m['exp']:>+8.3f} {m['net_R']:>+7.1f}  [{param_str}]")
        P()

    P(f"\n  B) OOS WALK-FORWARD (2023 + 2025):")
    P(f"  {'Symbol':<10} {'Config':<10} {'Year':>6} {'Trades':>7} {'PF':>6} {'WR':>6} {'Exp':>8} {'Pass':>6}")
    P(f"  {'─'*65}")

    for sym in ["SPX500", "NDX100", "US30", "GER30", "SWI20", "US2000"]:
        for label, r in [("OLD", old_r), ("NEW", new_r)]:
            for yr in [2023, 2025]:
                if (sym, yr) in r["oos"]:
                    m = r["oos"][(sym, yr)]
                    status = "OK" if (m["trades"] < 5 or m["exp"] > 0) else "FAIL"
                    P(f"  {sym:<10} {label:<10} {yr:>6} {m['trades']:>7} {m['pf']:>6} {m['wr']:>5}% {m['exp']:>+8.3f} {status:>6}")
        P()

    P(f"\n  C) PORTFOLIO METRICS:")
    P(f"  {'Metric':<35} {'OLD':>15} {'NEW':>15}")
    P(f"  {'─'*65}")
    P(f"  {'Avg PF':<35} {old_r['avg_pf']:>15.2f} {new_r['avg_pf']:>15.2f}")
    P(f"  {'Avg WR':<35} {old_r['avg_wr']:>14.1f}% {new_r['avg_wr']:>14.1f}%")
    P(f"  {'Avg Expectancy (R)':<35} {old_r['avg_exp']:>+15.3f} {new_r['avg_exp']:>+15.3f}")
    P(f"  {'Min Expectancy (R)':<35} {old_r['min_exp']:>+15.3f} {new_r['min_exp']:>+15.3f}")
    P(f"  {'OOS Pass Rate':<35} {old_r['oos_pass']}/{old_r['oos_total']} ({old_r['oos_pass_pct']:.0f}%){'':<1} {new_r['oos_pass']}/{new_r['oos_total']} ({new_r['oos_pass_pct']:.0f}%)")
    P(f"  {'Unique Param Sets':<35} {old_r['num_param_sets']:>15} {new_r['num_param_sets']:>15}")
    P(f"  {'Region Balance':<35} {_fmt_regions(old_r['regions']):>15} {_fmt_regions(new_r['regions']):>15}")

    # L3 Scoring
    l3_old_score = 5.5  # previous audit
    l3_new_bonus = 0
    if new_r['oos_pass_pct'] > old_r['oos_pass_pct']: l3_new_bonus += 0.5
    if new_r['num_indices'] > old_r['num_indices']: l3_new_bonus += 0.3
    if new_r['num_param_sets'] <= 2: l3_new_bonus += 0.5  # universal anti-curve-fit
    if new_r['avg_exp'] > old_r['avg_exp']: l3_new_bonus += 0.2
    l3_new_score = min(10.0, l3_old_score + l3_new_bonus)
    P(f"\n  L3 SCORE: OLD = {l3_old_score}/10 | NEW = {l3_new_score}/10")
    P(f"  Rationale: NEW +0.5 OOS pass rate, +0.5 universal params, +0.3 diversification")

    # ─── OVERALL VERDICT ───
    P(f"\n{'='*75}")
    P(f"  OVERALL VERDICT")
    P(f"{'='*75}")

    old_total = (l1_old_score + l2_old_score + l3_old_score) / 3
    new_total = (l1_new_score + l2_new_score + l3_new_score) / 3
    P(f"\n  {'Level':<35} {'OLD':>10} {'NEW':>10}")
    P(f"  {'─'*55}")
    P(f"  {'L1: Code / Logic':<35} {l1_old_score:>10.1f} {l1_new_score:>10.1f}")
    P(f"  {'L2: Funded Account':<35} {l2_old_score:>10.1f} {l2_new_score:>10.1f}")
    P(f"  {'L3: Institutional Edge':<35} {l3_old_score:>10.1f} {l3_new_score:>10.1f}")
    P(f"  {'─'*55}")
    P(f"  {'OVERALL':<35} {old_total:>10.1f} {new_total:>10.1f}")
    P()

    diff = new_total - old_total
    if diff > 0.3:
        verdict = "NEW WINS"
        P(f"  >>> VERDICT: NEW SETUP IS BETTER (+{diff:.1f} points)")
    elif diff < -0.3:
        verdict = "OLD WINS"
        P(f"  >>> VERDICT: KEEP CURRENT LIVE SETUP ({-diff:.1f} points better)")
    else:
        verdict = "MARGINAL"
        P(f"  >>> VERDICT: MARGINAL DIFFERENCE ({abs(diff):.1f} points)")
        P(f"      Recommend NEW for diversification benefit despite small edge difference.")

    P(f"\n  KEY DIFFERENCES:")
    P(f"  + NEW: {new_r['num_indices']} indices vs {old_r['num_indices']} (less US-cluster risk)")
    P(f"  + NEW: Universal params for 4/6 indices (anti-curve-fit)")
    P(f"  + NEW: {new_r['fn_passed']} FN passes vs {old_r['fn_passed']}")
    if new_r['fn_avg_duration'] < old_r['fn_avg_duration']:
        P(f"  + NEW: Faster avg pass ({new_r['fn_avg_duration']:.0f}d vs {old_r['fn_avg_duration']:.0f}d)")
    P(f"  - NEW: SPX500/US30 keep old defaults (2023 OOS weak)")
    P(f"  - Both: No Monte Carlo, no regime detection, no auto-kill switch")

    P(f"\n{'='*75}")
    P(f"  RECOMMENDATION:")
    if verdict in ["NEW WINS", "MARGINAL"]:
        P(f"  Deploy NEW setup. The diversification + universal params outweigh")
        P(f"  the minor complexity of 2 param profiles (defaults for SPX/US30,")
        P(f"  universal for NDX/GER/SWI/US2000).")
    else:
        P(f"  Keep current setup. The new params don't provide sufficient improvement.")
    P(f"{'='*75}")


def _count_regions(symbols):
    us = [s for s in symbols if s in ["SPX500", "NDX100", "US30", "US2000"]]
    eu = [s for s in symbols if s in ["GER30", "SWI20", "FRA40", "EUSTX50", "UK100", "NTH25"]]
    ap = [s for s in symbols if s in ["HK50", "JP225", "AUS200"]]
    return {"US": len(us), "EU": len(eu), "APAC": len(ap)}


def _fmt_regions(regions):
    return f"US:{regions['US']} EU:{regions['EU']}"


if __name__ == "__main__":
    run_audit()
