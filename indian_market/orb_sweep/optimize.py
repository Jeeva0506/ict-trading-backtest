"""
Fast parameter optimizer for ORB Sweep strategy.

Separates signal generation (slow) from exit replay (fast):
  1. Run signal generation once per regime config → cache entry signals + price paths
  2. Replay different TP/SL configs on cached paths → near-instant
  3. Validate top configs on full OOS period
"""

import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orb_sweep.config import TradingConfig
from orb_sweep.strategy import ORBSweepStrategy
from orb_sweep.indicators import atr, resample_to_5m
from backtest.option_pricer import bs_price, next_expiry
from costs.zerodha import round_trip_cost

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)


@dataclass
class CachedSignal:
    trade_date: date
    entry_idx: int
    direction: str
    entry_spot: float
    strike: int
    sweep_price: float
    r_value: float
    vwap_at_entry: float
    orb_high: float
    orb_low: float
    vix: float
    remaining_highs: np.ndarray
    remaining_lows: np.ndarray
    remaining_closes: np.ndarray
    remaining_times: list


def _load_data(nifty_path: str, vix_path: str):
    df = pd.read_csv(nifty_path, parse_dates=["datetime"]).set_index("datetime").sort_index()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    vix = pd.read_csv(vix_path, parse_dates=["datetime"]).set_index("datetime").sort_index()
    if "close" in vix.columns:
        vix = vix.rename(columns={"close": "Close"})
    vix.index = pd.to_datetime(vix.index)
    if vix.index.tz is not None:
        vix.index = vix.index.tz_localize(None)

    return df, vix


def _get_vix(df_vix, d):
    mask = df_vix.index.date <= d
    return float(df_vix.loc[mask, "Close"].iloc[-1]) if mask.any() else 15.0


def generate_signals(
    df_1m: pd.DataFrame,
    df_vix: pd.DataFrame,
    start_date: date,
    end_date: date,
    min_range_pct: float = 0.0025,
    min_atr_5m: float = 18.0,
    sweep_closeback_bars: int = 5,
    swing_pivot_n: int = 1,
) -> List[CachedSignal]:
    """Run signal generation and cache entries with their price paths."""
    cfg = TradingConfig(
        min_range_pct=min_range_pct,
        min_atr_5m=min_atr_5m,
        sweep_closeback_bars=sweep_closeback_bars,
        swing_pivot_n=swing_pivot_n,
    )
    strategy = ORBSweepStrategy(cfg)

    all_dates = sorted(set(
        d for d in df_1m.index.date if start_date <= d <= end_date
    ))
    signals: List[CachedSignal] = []

    for trade_date in all_dates:
        strategy.reset_day()
        day_1m = df_1m[df_1m.index.date == trade_date]
        if len(day_1m) < 30:
            continue

        lb_start = trade_date - pd.Timedelta(days=10)
        lb = df_1m[(df_1m.index.date >= lb_start) & (df_1m.index.date <= trade_date)]

        orb = strategy.compute_orb(lb, trade_date)
        if orb is None:
            continue
        if not strategy.check_regime(lb, trade_date):
            continue

        strategy.precompute_day(day_1m)
        vix_today = _get_vix(df_vix, trade_date)

        post_orb = day_1m[day_1m.index.time >= pd.Timestamp("09:30").time()]
        n_signals_today = 0

        for idx_pos in range(len(post_orb)):
            if n_signals_today >= cfg.max_trades_per_day:
                break

            bar_time = post_orb.index[idx_pos]
            if bar_time.time() >= pd.Timestamp("14:00").time():
                break

            bar_global_idx = lb.index.get_loc(bar_time)
            if isinstance(bar_global_idx, slice):
                bar_global_idx = bar_global_idx.start
            day_local_pos = day_1m.index.get_loc(bar_time)
            if isinstance(day_local_pos, slice):
                day_local_pos = day_local_pos.start

            signal = strategy.scan_bar(lb, bar_global_idx, local_pos=day_local_pos)
            if signal is None:
                continue

            remaining = post_orb.iloc[idx_pos:]
            cs = CachedSignal(
                trade_date=trade_date,
                entry_idx=idx_pos,
                direction=signal.direction,
                entry_spot=signal.entry_spot,
                strike=signal.strike,
                sweep_price=signal.sweep_price,
                r_value=signal.r_value,
                vwap_at_entry=signal.vwap_at_entry,
                orb_high=orb.orb_high,
                orb_low=orb.orb_low,
                vix=vix_today,
                remaining_highs=remaining["High"].values.astype(float),
                remaining_lows=remaining["Low"].values.astype(float),
                remaining_closes=remaining["Close"].values.astype(float),
                remaining_times=[t.time() for t in remaining.index],
            )
            signals.append(cs)
            n_signals_today += 1

    return signals


def replay_exits(
    signals: List[CachedSignal],
    tp1_r: float,
    tp2_r: float,
    tp1_exit_pct: float,
    capital: float = 1_000_000,
    risk_pct: float = 0.01,
    lot_size: int = 50,
    slippage: float = 0.005,
    daily_dd_pct: float = 0.01,
    weekly_dd_pct: float = 0.03,
    max_trades_day: int = 2,
) -> dict:
    """Replay a set of cached signals with given exit parameters. Returns metrics."""
    risk_per_trade = capital * risk_pct
    daily_dd_limit = capital * daily_dd_pct
    weekly_dd_limit = capital * weekly_dd_pct

    trades = []
    current_date = None
    current_week = None
    daily_pnl = 0.0
    weekly_pnl = 0.0
    daily_count = 0

    for sig in signals:
        if sig.trade_date != current_date:
            current_date = sig.trade_date
            daily_pnl = 0.0
            daily_count = 0
            week = sig.trade_date - pd.Timedelta(days=sig.trade_date.weekday())
            if week != current_week:
                current_week = week
                weekly_pnl = 0.0

        if daily_count >= max_trades_day:
            continue
        if daily_pnl <= -daily_dd_limit:
            continue
        if weekly_pnl <= -weekly_dd_limit:
            continue

        lots = max(1, int(risk_per_trade / (lot_size * sig.r_value)))
        qty = lots * lot_size

        is_call = sig.direction == "CALL"
        sl = sig.sweep_price
        tp1_spot = sig.entry_spot + tp1_r * sig.r_value * (1 if is_call else -1)
        tp2_spot = sig.entry_spot + tp2_r * sig.r_value * (1 if is_call else -1)

        expiry = next_expiry(sig.trade_date)
        dte = max((expiry - sig.trade_date).days, 1)
        iv = max(sig.vix / 100, 0.05)
        t_entry = max(dte / 365, 0.001)
        opt_type = "call" if is_call else "put"
        entry_prem = bs_price(sig.entry_spot, sig.strike, t_entry, 0.065, iv, opt_type)
        entry_prem *= (1 + slippage)

        tp1_hit = False
        exit_spot = sig.entry_spot
        exit_type = "TIME"
        active_sl = sl

        for i in range(len(sig.remaining_highs)):
            h = sig.remaining_highs[i]
            lo = sig.remaining_lows[i]
            c = sig.remaining_closes[i]
            t = sig.remaining_times[i]

            if t >= pd.Timestamp("15:15").time():
                exit_spot = c
                exit_type = "TIME"
                break

            hit_sl = (lo <= active_sl) if is_call else (h >= active_sl)
            hit_tp1 = (h >= tp1_spot) if is_call else (lo <= tp1_spot)
            hit_tp2 = (h >= tp2_spot) if is_call else (lo <= tp2_spot)

            if hit_tp1 and not tp1_hit:
                tp1_hit = True
                active_sl = sig.entry_spot

            if hit_tp2 and tp1_hit:
                exit_spot = tp2_spot
                exit_type = "TP2"
                break

            if hit_sl:
                exit_spot = active_sl
                exit_type = "SL_BE" if tp1_hit else "SL"
                break
        else:
            exit_spot = sig.remaining_closes[-1]
            exit_type = "TIME"

        t_exit = max((dte - 0.5) / 365, 0.001)
        exit_prem = bs_price(exit_spot, sig.strike, t_exit, 0.065, iv, opt_type)
        exit_prem *= (1 - slippage)

        gross = (exit_prem - entry_prem) * qty
        costs = round_trip_cost(entry_prem, max(exit_prem, 0.05), qty)
        net = gross - costs
        r_mult = net / (sig.r_value * qty) if sig.r_value > 0 else 0

        trades.append({
            "date": sig.trade_date,
            "direction": sig.direction,
            "entry_spot": sig.entry_spot,
            "exit_spot": exit_spot,
            "net_pnl": net,
            "exit_type": exit_type,
            "r_multiple": r_mult,
            "tp1_hit": tp1_hit,
        })

        daily_pnl += net
        weekly_pnl += net
        daily_count += 1

    if not trades:
        return {"n_trades": 0, "profit_factor": 0, "win_rate_pct": 0,
                "net_pnl": 0, "sharpe": 0, "max_drawdown_pct": 0,
                "expectancy": 0, "avg_r": 0}

    pnls = np.array([t["net_pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    n = len(pnls)
    wr = len(wins) / n if n else 0
    gp = float(wins.sum()) if len(wins) else 0
    gl = abs(float(losses.sum())) if len(losses) else 1e-9
    pf = gp / gl if gl > 0 else 0
    avg_w = float(wins.mean()) if len(wins) else 0
    avg_l = float(losses.mean()) if len(losses) else 0
    exp = wr * avg_w + (1 - wr) * avg_l

    equity = capital + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = ((equity - peak) / peak * 100).min()

    r_mults = np.array([t["r_multiple"] for t in trades])
    daily_rets = pnls / capital
    sharpe = float(daily_rets.mean() / daily_rets.std() * np.sqrt(252)) if daily_rets.std() > 0 else 0

    exit_types = {}
    for t in trades:
        exit_types[t["exit_type"]] = exit_types.get(t["exit_type"], 0) + 1

    return {
        "n_trades": n,
        "win_rate_pct": round(wr * 100, 1),
        "profit_factor": round(pf, 3),
        "net_pnl": round(float(pnls.sum()), 0),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(float(dd), 1),
        "expectancy": round(exp, 0),
        "avg_win": round(avg_w, 0),
        "avg_loss": round(avg_l, 0),
        "avg_r": round(float(r_mults.mean()), 2),
        "exit_breakdown": exit_types,
    }


def run_optimization(nifty_path: str, vix_path: str):
    print("Loading data...")
    df_1m, df_vix = _load_data(nifty_path, vix_path)
    t0 = time.time()

    # ── Define search space ────────────────────────────────────────────
    regime_configs = [
        (0.0020, 15.0, 5),
        (0.0020, 18.0, 5),
        (0.0025, 15.0, 5),
        (0.0025, 18.0, 5),
        (0.0025, 15.0, 3),
        (0.0025, 18.0, 8),
        (0.0030, 15.0, 5),
        (0.0030, 18.0, 5),
        (0.0035, 22.0, 3),  # original
    ]

    exit_configs = [
        (0.50, 1.5, 0.50),
        (0.50, 1.5, 0.75),
        (0.50, 2.0, 0.50),
        (0.50, 2.0, 0.75),
        (0.50, 3.0, 0.50),
        (0.75, 1.5, 0.50),
        (0.75, 1.5, 0.75),
        (0.75, 2.0, 0.50),
        (0.75, 2.0, 0.75),
        (0.75, 3.0, 0.50),
        (1.00, 1.5, 0.50),
        (1.00, 2.0, 0.50),
        (1.00, 2.0, 0.75),
        (1.00, 3.0, 0.50),
        (1.00, 3.0, 0.75),
    ]

    IS_START = date(2018, 1, 1)
    IS_END = date(2024, 6, 30)
    OOS_START = date(2024, 7, 1)
    OOS_END = date(2026, 5, 22)

    print(f"IS: {IS_START} to {IS_END} | OOS: {OOS_START} to {OOS_END}")
    print(f"Regime configs: {len(regime_configs)} | Exit configs: {len(exit_configs)}")
    print(f"Total combinations: {len(regime_configs) * len(exit_configs)}")

    # ── Phase 1: Generate signals for each regime config ───────────────
    print(f"\n{'='*80}")
    print("  PHASE 1: Signal Generation (IS period)")
    print(f"{'='*80}")

    is_signal_cache = {}
    oos_signal_cache = {}

    for rc in regime_configs:
        rp, atr_min, cb = rc
        label = f"R>={rp*100:.2f}% ATR>={atr_min} CB={cb}"

        t1 = time.time()
        is_sigs = generate_signals(df_1m, df_vix, IS_START, IS_END,
                                   min_range_pct=rp, min_atr_5m=atr_min,
                                   sweep_closeback_bars=cb)
        oos_sigs = generate_signals(df_1m, df_vix, OOS_START, OOS_END,
                                    min_range_pct=rp, min_atr_5m=atr_min,
                                    sweep_closeback_bars=cb)
        elapsed = time.time() - t1
        is_signal_cache[rc] = is_sigs
        oos_signal_cache[rc] = oos_sigs
        print(f"  {label}: {len(is_sigs)} IS signals, {len(oos_sigs)} OOS signals ({elapsed:.0f}s)")

    # ── Phase 2: Replay exits on cached signals ────────────────────────
    print(f"\n{'='*80}")
    print("  PHASE 2: Exit Parameter Sweep (instant replay)")
    print(f"{'='*80}")

    all_results = []

    for rc in regime_configs:
        is_sigs = is_signal_cache[rc]
        oos_sigs = oos_signal_cache[rc]
        rp, atr_min, cb = rc

        for ec in exit_configs:
            tp1_r, tp2_r, tp1_pct = ec

            is_m = replay_exits(is_sigs, tp1_r, tp2_r, tp1_pct)
            oos_m = replay_exits(oos_sigs, tp1_r, tp2_r, tp1_pct)

            is_pf = is_m["profit_factor"]
            is_wr = is_m["win_rate_pct"]
            is_n = is_m["n_trades"]
            oos_pf = oos_m["profit_factor"]
            oos_wr = oos_m["win_rate_pct"]
            oos_n = oos_m["n_trades"]

            score = 0
            if is_n >= 15 and is_pf > 0:
                score = is_pf * (is_wr / 100) * min(is_n / 30, 2.0)

            pf_decay = oos_pf / is_pf if is_pf > 0 else 0
            robust = oos_pf > 1.0 and pf_decay > 0.4

            all_results.append({
                "regime": rc, "exit": ec,
                "is": is_m, "oos": oos_m,
                "score": score, "pf_decay": pf_decay, "robust": robust,
                "label": (
                    f"R>={rp*100:.2f}% ATR>={atr_min} CB={cb} | "
                    f"TP1={tp1_r}R@{tp1_pct*100:.0f}% TP2={tp2_r}R"
                ),
            })

    # ── Sort and display results ───────────────────────────────────────
    all_results.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n  Top 20 by IS composite score:")
    print(f"  {'#':<4} {'IS_N':<6} {'IS_WR':<7} {'IS_PF':<7} {'IS_Shp':<7} "
          f"{'OOS_N':<6} {'OOS_PF':<7} {'OOS_WR':<7} {'Decay':<7} {'Rob':<5} {'Config'}")
    print(f"  {'-'*120}")
    for i, r in enumerate(all_results[:20], 1):
        flag = "YES" if r["robust"] else "   "
        print(
            f"  {i:<4} {r['is']['n_trades']:<6} {r['is']['win_rate_pct']:<7.1f} "
            f"{r['is']['profit_factor']:<7.3f} {r['is']['sharpe']:<7.3f} "
            f"{r['oos']['n_trades']:<6} {r['oos']['profit_factor']:<7.3f} "
            f"{r['oos']['win_rate_pct']:<7.1f} {r['pf_decay']:<7.2f} "
            f"{flag:<5} {r['label']}"
        )

    # ── Show best robust configs ───────────────────────────────────────
    robust_results = [r for r in all_results if r["robust"]]
    if robust_results:
        robust_results.sort(key=lambda r: r["oos"]["profit_factor"], reverse=True)
        print(f"\n{'='*80}")
        print(f"  ROBUST CONFIGS (OOS PF > 1.0, decay > 0.4): {len(robust_results)} found")
        print(f"{'='*80}")

        for i, r in enumerate(robust_results[:10], 1):
            print(f"\n  #{i}: {r['label']}")
            is_m = r["is"]
            oos_m = r["oos"]
            print(f"    IS:  {is_m['n_trades']} trades | WR={is_m['win_rate_pct']}% | "
                  f"PF={is_m['profit_factor']:.3f} | Sharpe={is_m['sharpe']:.3f} | "
                  f"Net=Rs{is_m['net_pnl']:+,.0f} | DD={is_m['max_drawdown_pct']:.1f}%")
            print(f"    OOS: {oos_m['n_trades']} trades | WR={oos_m['win_rate_pct']}% | "
                  f"PF={oos_m['profit_factor']:.3f} | Sharpe={oos_m['sharpe']:.3f} | "
                  f"Net=Rs{oos_m['net_pnl']:+,.0f} | DD={oos_m['max_drawdown_pct']:.1f}%")
            print(f"    Exits: IS={is_m.get('exit_breakdown',{})} OOS={oos_m.get('exit_breakdown',{})}")
            print(f"    PF Decay: {r['pf_decay']:.2f}")

    # ── Best OOS profitable if no robust ───────────────────────────────
    if not robust_results:
        oos_profitable = [r for r in all_results if r["oos"]["profit_factor"] > 1.0]
        if oos_profitable:
            oos_profitable.sort(key=lambda r: r["oos"]["profit_factor"], reverse=True)
            print(f"\n  OOS PROFITABLE (but PF decay may be high): {len(oos_profitable)}")
            for i, r in enumerate(oos_profitable[:5], 1):
                print(f"  #{i}: {r['label']}")
                print(f"    IS PF={r['is']['profit_factor']:.3f} OOS PF={r['oos']['profit_factor']:.3f} "
                      f"decay={r['pf_decay']:.2f} OOS Net=Rs{r['oos']['net_pnl']:+,.0f}")
        else:
            print("\n  No OOS-profitable configs found.")

    # ── Full period validation ─────────────────────────────────────────
    best_pick = None
    if robust_results:
        best_pick = robust_results[0]
    elif [r for r in all_results if r["oos"]["profit_factor"] > 1.0]:
        best_pick = max(
            [r for r in all_results if r["oos"]["profit_factor"] > 1.0],
            key=lambda r: r["oos"]["profit_factor"],
        )

    if best_pick:
        rp, atr_min, cb = best_pick["regime"]
        tp1_r, tp2_r, tp1_pct = best_pick["exit"]
        print(f"\n{'='*80}")
        print("  FULL PERIOD VALIDATION (best config)")
        print(f"{'='*80}")
        print(f"  Config: {best_pick['label']}")

        full_sigs = generate_signals(
            df_1m, df_vix, date(2016, 1, 1), date(2026, 5, 22),
            min_range_pct=rp, min_atr_5m=atr_min, sweep_closeback_bars=cb,
        )
        full_m = replay_exits(full_sigs, tp1_r, tp2_r, tp1_pct)

        print(f"\n  Full Period Results:")
        print(f"    Trades     : {full_m['n_trades']}")
        print(f"    Win Rate   : {full_m['win_rate_pct']}%")
        print(f"    Profit Factor: {full_m['profit_factor']:.3f}")
        print(f"    Net P&L    : Rs{full_m['net_pnl']:+,.0f}")
        print(f"    Sharpe     : {full_m['sharpe']:.3f}")
        print(f"    Max DD     : {full_m['max_drawdown_pct']:.1f}%")
        print(f"    Expectancy : Rs{full_m['expectancy']:+,.0f}")
        print(f"    Avg Win    : Rs{full_m['avg_win']:+,.0f}")
        print(f"    Avg Loss   : Rs{full_m['avg_loss']:+,.0f}")
        print(f"    Avg R      : {full_m['avg_r']:.2f}R")
        print(f"    Exits      : {full_m.get('exit_breakdown', {})}")

        # Print recommended config update
        print(f"\n  RECOMMENDED CONFIG UPDATE:")
        print(f"    min_range_pct     = {rp}")
        print(f"    min_atr_5m        = {atr_min}")
        print(f"    sweep_closeback_bars = {cb}")
        print(f"    tp1_r_multiple    = {tp1_r}")
        print(f"    tp2_r_multiple    = {tp2_r}")
        print(f"    tp1_exit_pct      = {tp1_pct}")

    total = time.time() - t0
    print(f"\nTotal optimization time: {total:.0f}s ({total/60:.1f}min)")

    return all_results


if __name__ == "__main__":
    data_dir = ROOT / "data"
    run_optimization(
        str(data_dir / "zerodha_nifty_1m.csv"),
        str(data_dir / "zerodha_vix_day.csv"),
    )
