"""
Combined Strategy Backtester — Multi-Instrument
================================================
Combines:
  1. SHORT straddle on expiry days (collect theta on 0DTE)
  2. PUT-only sweep on non-expiry days (sweep reversal edge)

Instruments:
  - NIFTY:     Tuesday weekly expiry (post ~mid-2025), Thursday before
  - BANKNIFTY: Monthly only (last Wed/Tue of month)
  - FINNIFTY:  Monthly only (last Tue of month)
  - SENSEX:    Thursday weekly expiry (BFO)

Usage:
    python -m orb_sweep.combined_backtest
"""

import sys, os, calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orb_sweep.expiry_straddle import (
    _bs_0dte, StraddleTrade, ExpiryStraddleConfig,
    ExpiryShortStraddleBacktester,
)
from orb_sweep.config import TradingConfig
from orb_sweep.backtest import ORBSweepBacktester, _synthetic_premium
from backtest.option_pricer import next_expiry
from costs.zerodha import round_trip_cost

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


@dataclass
class InstrumentSpec:
    name: str
    spot_csv: str
    vix_csv: str = "zerodha_vix_day.csv"
    lot_size: int = 50
    strike_step: int = 50
    expiry_weekday: int = 1  # 0=Mon, 1=Tue, 3=Thu
    weekly_expiry_start: date = date(2019, 10, 24)
    has_weekly: bool = True
    capital: float = 1_000_000


INSTRUMENTS = {
    "NIFTY": InstrumentSpec(
        name="NIFTY",
        spot_csv="zerodha_nifty_1m.csv",
        lot_size=65,
        strike_step=50,
        expiry_weekday=1,  # Tuesday
        weekly_expiry_start=date(2025, 4, 4),
        has_weekly=True,
    ),
    "BANKNIFTY": InstrumentSpec(
        name="BANKNIFTY",
        spot_csv="zerodha_banknifty_1m.csv",
        lot_size=30,
        strike_step=100,
        expiry_weekday=1,  # Tuesday (monthly last)
        has_weekly=False,
    ),
    "FINNIFTY": InstrumentSpec(
        name="FINNIFTY",
        spot_csv="zerodha_finnifty_1m.csv",
        lot_size=60,
        strike_step=50,
        expiry_weekday=1,  # Tuesday (monthly last)
        weekly_expiry_start=date(2022, 1, 18),
        has_weekly=False,
    ),
    "SENSEX": InstrumentSpec(
        name="SENSEX",
        spot_csv="zerodha_sensex_1m.csv",
        lot_size=20,
        strike_step=100,
        expiry_weekday=3,  # Thursday
        weekly_expiry_start=date(2023, 5, 15),
        has_weekly=True,
    ),
}

# Historical NIFTY expiry evolution:
# Pre Oct 2019: Monthly Thursday
# Oct 2019 - Nov 2024: Weekly Thursday
# Nov 2024 - Apr 2025: Transition period
# Apr 2025+: Weekly Tuesday
_NIFTY_TUESDAY_START = date(2025, 4, 4)
_NIFTY_WEEKLY_THU_START = date(2019, 10, 24)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def is_expiry_day(d: date, spec: InstrumentSpec) -> bool:
    """Check if d is an expiry day for the given instrument."""
    if spec.name == "NIFTY":
        if d >= _NIFTY_TUESDAY_START:
            return d.weekday() == 1  # Tuesday weekly
        elif d >= _NIFTY_WEEKLY_THU_START:
            return d.weekday() == 3  # Thursday weekly
        else:
            return d.weekday() == 3 and d == _last_weekday_of_month(d.year, d.month, 3)

    if not spec.has_weekly:
        # Monthly only: last occurrence of expiry_weekday
        return d == _last_weekday_of_month(d.year, d.month, spec.expiry_weekday)

    if d >= spec.weekly_expiry_start:
        return d.weekday() == spec.expiry_weekday
    else:
        return d == _last_weekday_of_month(d.year, d.month, spec.expiry_weekday)


def run_instrument(spec: InstrumentSpec, start_date: date = None, end_date: date = None):
    """
    Run combined strategy for one instrument.
    Returns (straddle_trades, sweep_trades, combined_metrics).
    """
    spot_path = str(DATA_DIR / spec.spot_csv)
    vix_path = str(DATA_DIR / spec.vix_csv)

    # Load data
    df_1m = pd.read_csv(spot_path, parse_dates=["datetime"] if "datetime" in
                         pd.read_csv(spot_path, nrows=0).columns else [0])
    if "datetime" in df_1m.columns:
        df_1m = df_1m.set_index("datetime")
    elif df_1m.index.name != "datetime":
        df_1m.index = pd.to_datetime(df_1m.index)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        c_lower = col.lower()
        if c_lower in df_1m.columns and col not in df_1m.columns:
            df_1m = df_1m.rename(columns={c_lower: col})
    df_1m.index = pd.to_datetime(df_1m.index)
    if df_1m.index.tz is not None:
        df_1m.index = df_1m.index.tz_localize(None)
    df_1m = df_1m.sort_index()

    df_vix = pd.read_csv(vix_path, parse_dates=["datetime"] if "datetime" in
                          pd.read_csv(vix_path, nrows=0).columns else [0])
    if "datetime" in df_vix.columns:
        df_vix = df_vix.set_index("datetime")
    if "close" in df_vix.columns:
        df_vix = df_vix.rename(columns={"close": "Close"})
    df_vix.index = pd.to_datetime(df_vix.index)
    if df_vix.index.tz is not None:
        df_vix.index = df_vix.index.tz_localize(None)
    df_vix = df_vix.sort_index()

    all_dates = sorted(set(df_1m.index.date))
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]

    expiry_dates = [d for d in all_dates if is_expiry_day(d, spec)]
    non_expiry_dates = [d for d in all_dates if not is_expiry_day(d, spec)]

    print(f"\n  {spec.name}: {len(all_dates)} trading days, "
          f"{len(expiry_dates)} expiry, {len(non_expiry_dates)} non-expiry")

    # === Part 1: Short straddle on expiry days ===
    straddle_trades = _run_short_straddle(
        df_1m, df_vix, expiry_dates, spec
    )

    # === Part 2: PUT-only sweep on non-expiry days ===
    sweep_trades = _run_put_sweep(
        df_1m, df_vix, non_expiry_dates, spec
    )

    # Combine
    all_trades = straddle_trades + sweep_trades
    all_trades.sort(key=lambda t: t["entry_date"])

    if not all_trades:
        return straddle_trades, sweep_trades, {"error": "No trades"}

    from backtest.metrics import compute_metrics
    straddle_metrics = compute_metrics(straddle_trades, spec.capital) if straddle_trades else {"error": "No trades"}
    sweep_metrics = compute_metrics(sweep_trades, spec.capital) if sweep_trades else {"error": "No trades"}
    combined_metrics = compute_metrics(all_trades, spec.capital)

    return straddle_trades, sweep_trades, straddle_metrics, sweep_metrics, combined_metrics


def _get_vix(df_vix, d):
    mask = df_vix.index.date <= d
    return float(df_vix.loc[mask, "Close"].iloc[-1]) if mask.any() else 15.0


def _run_short_straddle(df_1m, df_vix, expiry_dates, spec) -> list:
    """Run short straddle on given expiry dates."""
    trades = []
    cfg = ExpiryStraddleConfig(
        lot_size=spec.lot_size,
        strike_step=spec.strike_step,
        capital=spec.capital,
        sl_pct=0.50,
        tp_pct=0.40,
        trail_activate_pct=0.25,
        trail_pct=0.15,
    )

    for trade_date in expiry_dates:
        day_1m = df_1m[df_1m.index.date == trade_date]
        if len(day_1m) < 30:
            continue

        orb_mask = (
            (day_1m.index.time >= pd.Timestamp("09:15").time()) &
            (day_1m.index.time < pd.Timestamp("09:30").time())
        )
        orb_bars = day_1m[orb_mask]
        if len(orb_bars) < 10:
            continue

        vix = _get_vix(df_vix, trade_date)
        if vix < 11 or vix > 25:
            continue

        entry_mask = day_1m.index.time >= time(9, 31)
        if not entry_mask.any():
            continue
        entry_bar = day_1m[entry_mask].iloc[0]
        entry_time = day_1m[entry_mask].index[0]
        spot_entry = float(entry_bar["Close"])

        strike = int(round(spot_entry / cfg.strike_step) * cfg.strike_step)
        mins_left = max(15 * 60 + 30 - entry_time.hour * 60 - entry_time.minute, 1)
        entry_call = _bs_0dte(spot_entry, strike, mins_left, vix, "call")
        entry_put = _bs_0dte(spot_entry, strike, mins_left, vix, "put")
        straddle_entry = (entry_call + entry_put) * (1 - cfg.slippage_pct)

        if straddle_entry < 5:
            continue

        risk = cfg.capital * cfg.risk_per_trade_pct
        max_loss = straddle_entry * cfg.sl_pct
        lots = max(1, int(risk / (max_loss * cfg.lot_size)))
        quantity = lots * cfg.lot_size

        sl_value = straddle_entry * (1 + cfg.sl_pct)
        tp_value = straddle_entry * (1 - cfg.tp_pct)
        trail_on = straddle_entry * (1 - cfg.trail_activate_pct)

        post_entry = day_1m[day_1m.index > entry_time]
        trough = straddle_entry
        trail_active = False
        exit_straddle = straddle_entry
        exit_time = entry_time
        exit_spot = spot_entry
        exit_type = "TIME"

        for bt, bar in post_entry.iterrows():
            spot = float(bar["Close"])
            ml = max(15 * 60 + 30 - bt.hour * 60 - bt.minute, 1)
            cc = _bs_0dte(spot, strike, ml, vix, "call")
            cp = _bs_0dte(spot, strike, ml, vix, "put")
            cs = cc + cp

            if cs < trough:
                trough = cs
            if trough <= trail_on:
                trail_active = True

            if bt.time() >= time(15, 10):
                exit_straddle = cs
                exit_time = bt
                exit_spot = spot
                exit_type = "TIME"
                break
            if cs <= tp_value:
                exit_straddle, exit_time, exit_spot, exit_type = cs, bt, spot, "TP"
                break
            if trail_active and cs >= trough * (1 + cfg.trail_pct):
                exit_straddle, exit_time, exit_spot, exit_type = cs, bt, spot, "TRAIL"
                break
            if cs >= sl_value:
                exit_straddle, exit_time, exit_spot, exit_type = cs, bt, spot, "SL"
                break

        exit_straddle *= (1 + cfg.slippage_pct)
        gross = (straddle_entry - exit_straddle) * quantity
        cost = round_trip_cost(straddle_entry / 2, exit_straddle / 2, quantity) * 2
        net = gross - cost

        trades.append({
            "entry_date": str(trade_date),
            "exit_date": str(exit_time.date()) if hasattr(exit_time, "date") else str(trade_date),
            "direction": "short_straddle",
            "strike": strike,
            "entry_spot": round(spot_entry, 2),
            "exit_spot": round(exit_spot, 2),
            "entry_prem": round(straddle_entry, 2),
            "exit_prem": round(exit_straddle, 2),
            "lots": lots,
            "qty": quantity,
            "exit_type": exit_type,
            "gross_pnl": round(gross, 2),
            "cost": round(cost, 2),
            "pnl_net": round(net, 2),
            "strategy": "short_straddle",
        })

    return trades


def _run_put_sweep(df_1m, df_vix, dates, spec) -> list:
    """Run PUT-only sweep on given dates."""
    from orb_sweep.strategy import ORBSweepStrategy
    from orb_sweep.indicators import atr, resample_to_5m, vwap

    cfg = TradingConfig(
        lot_size=spec.lot_size,
        strike_step=spec.strike_step,
        capital=spec.capital,
    )
    strategy = ORBSweepStrategy(cfg)
    trades = []

    for trade_date in dates:
        strategy.reset_day()
        day_1m = df_1m[df_1m.index.date == trade_date].copy()
        if len(day_1m) < 30:
            continue

        lookback_start = trade_date - timedelta(days=10)
        lookback_1m = df_1m[
            (df_1m.index.date >= lookback_start) &
            (df_1m.index.date <= trade_date)
        ].copy()

        orb = strategy.compute_orb(lookback_1m, trade_date)
        if orb is None:
            continue
        if not strategy.check_regime(lookback_1m, trade_date):
            continue

        vix = _get_vix(df_vix, trade_date)
        strategy.precompute_day(day_1m)

        post_orb = day_1m[day_1m.index.time >= pd.Timestamp("09:30").time()]
        active = None
        peak_prem = 0

        for idx_pos in range(len(post_orb)):
            bar = post_orb.iloc[idx_pos]
            bar_time = post_orb.index[idx_pos]
            spot = float(bar["Close"])

            if bar_time.time() >= pd.Timestamp("15:15").time():
                if active:
                    trades.append(_exit_put(active, bar_time, spot, vix, "TIME", spec))
                    active = None
                break

            if active:
                # Premium-based exit for PUT
                mins_total = 375
                elapsed = idx_pos / max(len(post_orb), 1) * 0.8
                dte_rem = max(active["_dte"] - elapsed, 0.5)
                cur_prem = _synthetic_premium(spot, active["strike"], "PUT", vix, dte_rem)

                if cur_prem > peak_prem:
                    peak_prem = cur_prem

                sl_prem = active["entry_prem"] * (1 - cfg.put_sl_pct)
                tp_prem = active["entry_prem"] * (1 + cfg.put_tp_pct)
                trail_on = peak_prem >= active["entry_prem"] * (1 + cfg.put_trail_activate)

                if cur_prem >= tp_prem:
                    active = _exit_put(active, bar_time, spot, vix, "TP", spec, cur_prem)
                    trades.append(active)
                    active = None
                elif trail_on and cur_prem <= peak_prem * (1 - cfg.put_trail_pct):
                    active = _exit_put(active, bar_time, spot, vix, "TRAIL", spec, cur_prem)
                    trades.append(active)
                    active = None
                elif cur_prem <= sl_prem:
                    active = _exit_put(active, bar_time, spot, vix, "SL", spec, cur_prem)
                    trades.append(active)
                    active = None
                continue

            if bar_time.time() >= pd.Timestamp("14:00").time():
                continue

            bar_global_idx = lookback_1m.index.get_loc(bar_time)
            if isinstance(bar_global_idx, slice):
                bar_global_idx = bar_global_idx.start

            day_local_pos = day_1m.index.get_loc(bar_time)
            if isinstance(day_local_pos, slice):
                day_local_pos = day_local_pos.start

            signal = strategy.scan_bar(lookback_1m, bar_global_idx, local_pos=day_local_pos)
            if signal is None or signal.direction != "PUT":
                continue

            # Enter PUT
            lots = max(1, int(cfg.risk_per_trade / (cfg.lot_size * signal.r_value)))
            quantity = lots * cfg.lot_size
            expiry = next_expiry(trade_date)
            dte = max((expiry - trade_date).days, 1)
            entry_prem = _synthetic_premium(signal.entry_spot, signal.strike, "PUT", vix, dte)
            entry_prem *= (1 + cfg.slippage_pct)

            active = {
                "entry_date": str(trade_date),
                "entry_time": bar_time,
                "strike": signal.strike,
                "entry_spot": signal.entry_spot,
                "entry_prem": entry_prem,
                "quantity": quantity,
                "lots": lots,
                "_dte": dte,
                "_r_value": signal.r_value,
            }
            peak_prem = entry_prem

        if active:
            last_bar = post_orb.iloc[-1]
            last_time = post_orb.index[-1]
            active = _exit_put(active, last_time, float(last_bar["Close"]), vix, "TIME", spec)
            trades.append(active)

    return trades


def _exit_put(active, exit_time, spot, vix, exit_type, spec, override_prem=0):
    if override_prem > 0:
        exit_prem = override_prem * (1 - 0.005)
    else:
        dte_rem = max(active["_dte"] - 0.5, 0.5)
        exit_prem = _synthetic_premium(spot, active["strike"], "PUT", vix, dte_rem)
        exit_prem *= (1 - 0.005)

    gross = (exit_prem - active["entry_prem"]) * active["quantity"]
    cost = round_trip_cost(active["entry_prem"], exit_prem, active["quantity"])
    net = gross - cost

    return {
        "entry_date": active["entry_date"],
        "exit_date": str(exit_time.date()) if hasattr(exit_time, "date") else active["entry_date"],
        "direction": "put_sweep",
        "strike": active["strike"],
        "entry_spot": round(active["entry_spot"], 2),
        "exit_spot": round(spot, 2),
        "entry_prem": round(active["entry_prem"], 2),
        "exit_prem": round(exit_prem, 2),
        "lots": active["lots"],
        "qty": active["quantity"],
        "exit_type": exit_type,
        "gross_pnl": round(gross, 2),
        "cost": round(cost, 2),
        "pnl_net": round(net, 2),
        "strategy": "put_sweep",
    }


def print_combined_report(name, straddle_m, sweep_m, combined_m):
    sep = "-" * 65
    print(f"\n{'=' * 65}")
    print(f"  {name} — COMBINED STRATEGY REPORT")
    print(f"{'=' * 65}")

    for label, m in [("SHORT STRADDLE (expiry)", straddle_m),
                     ("PUT SWEEP (non-expiry)", sweep_m),
                     ("COMBINED", combined_m)]:
        print(f"\n  [{label}]")
        if "error" in m:
            print(f"    No trades")
            continue
        print(f"    Trades: {m['n_trades']:>5}  |  WR: {m['win_rate_pct']:>5.1f}%  |  "
              f"PF: {m['profit_factor']:>6.3f}  |  Net: Rs{m['net_pnl']:>10,.0f}")
        print(f"    CAGR: {m['cagr_pct']:>5.1f}%  |  Sharpe: {m['sharpe']:>6.3f}  |  "
              f"MaxDD: {m['max_drawdown_pct']:>5.1f}%")

    print(f"\n{'=' * 65}")


def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for inst_name, spec in INSTRUMENTS.items():
        spot_path = DATA_DIR / spec.spot_csv
        if not spot_path.exists():
            print(f"\n  SKIP {inst_name}: {spec.spot_csv} not found")
            continue

        print(f"\n{'#' * 65}")
        print(f"  BACKTESTING: {inst_name}")
        print(f"{'#' * 65}")

        result = run_instrument(spec)
        straddle_trades, sweep_trades, straddle_m, sweep_m, combined_m = result

        print_combined_report(inst_name, straddle_m, sweep_m, combined_m)

        # Save trades
        all_trades = straddle_trades + sweep_trades
        if all_trades:
            df = pd.DataFrame(all_trades)
            df = df.sort_values("entry_date")
            out_path = REPORTS_DIR / f"combined_{inst_name.lower()}_trades.csv"
            df.to_csv(out_path, index=False)
            print(f"  Saved: {out_path.name} ({len(df)} trades)")


if __name__ == "__main__":
    main()
