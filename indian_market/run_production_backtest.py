"""
Production-grade 3-year backtest (2024-2026) with ALL safety features.

Accounts for:
  - Lot size change: NIFTY 50 -> 75 on 2024-11-21
  - Expiry shift: Thursday -> Tuesday on 2025-04-04
  - 2% extra ELM on expiry shorts from 2024-11-20
  - Realistic slippage: 3% entry, 5% exit (Rs 600-3000/lot)
  - Kill switch: halt if daily loss > 2% of capital (Rs 6,000)
  - VIX halt: no entries if VIX > 25
  - No new entries after 14:00
  - STT trap: exit ITM by 15:05
  - Full Zerodha costs (brokerage + STT + exchange + SEBI + GST + stamp)
  - Spread penalty: extra 2% cost when VIX > 18

Capital: Rs 3,00,000
Straddle: 3% risk | Sweep: 1% risk
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
from datetime import date, time, timedelta
from pathlib import Path

from orb_sweep.expiry_straddle import _bs_0dte
from orb_sweep.combined_backtest import _get_vix, INSTRUMENTS, DATA_DIR
from orb_sweep.config import TradingConfig
from orb_sweep.strategy import ORBSweepStrategy
from orb_sweep.backtest import _synthetic_premium
from backtest.option_pricer import next_expiry, bs_delta, bs_price
from costs.zerodha import round_trip_cost

CAPITAL = 300_000
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

# ---- Rule change dates ----
LOT_SIZE_CHANGE = date(2024, 11, 21)
EXPIRY_SHIFT = date(2025, 4, 4)
ELM_EXTRA_START = date(2024, 11, 20)

# ---- Slippage model ----
SLIP_ENTRY = 0.03
SLIP_EXIT = 0.05
SPREAD_PENALTY_VIX_THRESHOLD = 18.0
SPREAD_PENALTY_EXTRA = 0.02  # extra 2% on top of slippage when VIX > 18

# ---- Safety params ----
DAILY_DD_LIMIT = CAPITAL * 0.02  # Rs 6,000
VIX_HALT = 25.0
NO_ENTRY_AFTER = time(14, 0)
FORCE_EXIT_TIME = time(15, 10)
STT_TRAP_TIME = time(15, 5)


def get_lot_size(d):
    return 75 if d >= LOT_SIZE_CHANGE else 50


def is_expiry_day(d):
    """Correct expiry calendar with shift date."""
    if d >= EXPIRY_SHIFT:
        return d.weekday() == 1  # Tuesday
    elif d >= date(2019, 10, 24):
        return d.weekday() == 3  # Thursday
    else:
        import calendar
        last_day = calendar.monthrange(d.year, d.month)[1]
        last_thu = date(d.year, d.month, last_day)
        while last_thu.weekday() != 3:
            last_thu -= timedelta(days=1)
        return d == last_thu


def effective_slippage(vix):
    """Higher slippage when VIX is elevated."""
    extra = SPREAD_PENALTY_EXTRA if vix > SPREAD_PENALTY_VIX_THRESHOLD else 0
    return SLIP_ENTRY + extra, SLIP_EXIT + extra


def estimate_margin_needed(spot, lot_size, lots, is_exp):
    """Estimate margin for short straddle."""
    contract_val = spot * lot_size * lots
    base = contract_val * 0.11
    elm_extra = contract_val * 0.02 if is_exp and ELM_EXTRA_START else 0
    return base + elm_extra


def run_production_backtest():
    spec = INSTRUMENTS["NIFTY"]

    # Load data
    df_1m = pd.read_csv(DATA_DIR / spec.spot_csv, index_col=0, parse_dates=True)
    if df_1m.index.tz:
        df_1m.index = df_1m.index.tz_localize(None)
    df_vix = pd.read_csv(DATA_DIR / "zerodha_vix_day.csv", index_col=0, parse_dates=True)
    if "close" in df_vix.columns:
        df_vix = df_vix.rename(columns={"close": "Close"})
    if df_vix.index.tz:
        df_vix.index = df_vix.index.tz_localize(None)

    all_dates = sorted(set(df_1m.index.date))
    test_dates = [d for d in all_dates if d >= date(2024, 1, 1)]

    print("=" * 80)
    print("  PRODUCTION BACKTEST: 2024-2026 WITH ALL SAFETY FEATURES")
    print("  Capital: Rs {:,} | Straddle 3% | Sweep 1%".format(CAPITAL))
    print("  Slippage: 3%/5% + 2% extra when VIX > 18")
    print("  Kill switch: 2% daily DD | VIX halt: > 25 | No entries after 14:00")
    print("=" * 80)

    trades = []
    equity = CAPITAL
    daily_pnl = 0
    daily_trades_count = 0
    kill_switch_days = 0
    vix_halt_days = 0
    stt_trap_exits = 0
    margin_blocks = 0
    current_date = None

    for td in test_dates:
        # ---- Day reset ----
        if current_date != td:
            current_date = td
            daily_pnl = 0
            daily_trades_count = 0
            kill_switch_active = False

        lot_size = get_lot_size(td)
        is_exp = is_expiry_day(td)
        vix = _get_vix(df_vix, td)

        day_1m = df_1m[df_1m.index.date == td]
        if len(day_1m) < 30:
            continue

        # ---- Kill switch check ----
        if daily_pnl <= -DAILY_DD_LIMIT:
            if not kill_switch_active:
                kill_switch_active = True
                kill_switch_days += 1
            continue

        # ---- VIX halt check (for new entries) ----
        if vix > VIX_HALT:
            vix_halt_days += 1
            continue

        # ================================================================
        # STRATEGY 1: Short straddle on expiry days
        # ================================================================
        if is_exp:
            entry_mask = day_1m.index.time >= time(9, 31)
            if not entry_mask.any():
                continue
            spot_entry = float(day_1m[entry_mask].iloc[0]["Close"])
            etime = day_1m[entry_mask].index[0]
            strike = int(round(spot_entry / 50) * 50)

            # Margin check
            risk_amt = CAPITAL * 0.03  # 3% risk
            sl_pct = 0.50
            ml = max(930 - etime.hour * 60 - etime.minute, 1)
            entry_call = _bs_0dte(spot_entry, strike, ml, vix, "call")
            entry_put = _bs_0dte(spot_entry, strike, ml, vix, "put")

            se, sx = effective_slippage(vix)
            straddle_entry = (entry_call + entry_put) * (1 - se)

            if straddle_entry < 5:
                continue

            lots = max(1, int(risk_amt / (straddle_entry * sl_pct * lot_size)))
            qty = lots * lot_size

            margin_needed = estimate_margin_needed(spot_entry, lot_size, lots, True)
            if margin_needed > equity * 0.7:  # don't use more than 70% of equity as margin
                margin_blocks += 1
                continue

            # Compute entry greeks for journal
            t_years = ml / (365 * 375)
            iv = vix / 100
            entry_delta_c = bs_delta(spot_entry, strike, t_years, 0.065, iv, "call") if t_years > 0 else 0.5
            entry_delta_p = bs_delta(spot_entry, strike, t_years, 0.065, iv, "put") if t_years > 0 else 0.5

            sl_val = straddle_entry * (1 + sl_pct)
            tp_pct = 0.40
            tp_val = straddle_entry * (1 - tp_pct)
            ta_pct, tr_pct = 0.25, 0.15
            trail_on_val = straddle_entry * (1 - ta_pct)

            post = day_1m[day_1m.index > etime]
            trough = straddle_entry
            tact = False
            xsv = straddle_entry
            xt = "TIME"
            xtime = etime
            xspot = spot_entry
            stt_forced = False

            for bt, bar in post.iterrows():
                s = float(bar["Close"])
                m = max(930 - bt.hour * 60 - bt.minute, 1)
                cs = _bs_0dte(s, strike, m, vix, "call") + _bs_0dte(s, strike, m, vix, "put")

                if cs < trough:
                    trough = cs
                if trough <= trail_on_val:
                    tact = True

                # STT trap check: if ITM at 15:05, force exit
                if bt.time() >= STT_TRAP_TIME:
                    call_itm = s > strike
                    put_itm = s < strike
                    if call_itm or put_itm:
                        xsv = cs
                        xt = "STT_TRAP"
                        xtime = bt
                        xspot = s
                        stt_forced = True
                        stt_trap_exits += 1
                        break

                if bt.time() >= FORCE_EXIT_TIME:
                    xsv = cs
                    xtime = bt
                    xspot = s
                    break
                if cs <= tp_val:
                    xsv = cs
                    xt = "TP"
                    xtime = bt
                    xspot = s
                    break
                if tact and cs >= trough * (1 + tr_pct):
                    xsv = cs
                    xt = "TRAIL"
                    xtime = bt
                    xspot = s
                    break
                if cs >= sl_val:
                    xsv = cs
                    xt = "SL"
                    xtime = bt
                    xspot = s
                    break

            xsv *= (1 + sx)
            gross = (straddle_entry - xsv) * qty
            cost = round_trip_cost(straddle_entry / 2, xsv / 2, qty) * 2
            net = gross - cost

            daily_pnl += net
            daily_trades_count += 1
            equity += net

            trades.append({
                "entry_date": str(td),
                "exit_date": str(xtime.date()) if hasattr(xtime, "date") else str(td),
                "entry_time": str(etime),
                "exit_time": str(xtime),
                "strategy": "short_straddle",
                "signal_reason": "Expiry day 0DTE theta collection",
                "direction": "SHORT_STRADDLE",
                "strike": strike,
                "spot_entry": round(spot_entry, 2),
                "spot_exit": round(xspot, 2),
                "entry_premium": round(straddle_entry, 2),
                "exit_premium": round(xsv, 2),
                "sl_amount": round(sl_val, 2),
                "tp_amount": round(tp_val, 2),
                "india_vix": round(vix, 2),
                "bs_delta_call": round(entry_delta_c, 3),
                "bs_delta_put": round(entry_delta_p, 3),
                "is_expiry_day": True,
                "lot_size": lot_size,
                "quantity": qty,
                "lots": lots,
                "gross_pnl": round(gross, 2),
                "costs": round(cost, 2),
                "pnl_net": round(net, 2),
                "exit_type": xt,
                "equity": round(equity, 2),
                "margin_used": round(margin_needed, 0),
                "slippage_entry_pct": round(se * 100, 1),
                "slippage_exit_pct": round(sx * 100, 1),
                "kill_switch": kill_switch_active,
                "stt_trap_exit": stt_forced,
            })

        # ================================================================
        # STRATEGY 2: PUT sweep on non-expiry days
        # ================================================================
        if not is_exp:
            if daily_trades_count >= 3:
                continue

            cfg = TradingConfig(lot_size=lot_size, strike_step=50, capital=CAPITAL)
            strategy = ORBSweepStrategy(cfg)
            strategy.reset_day()

            lb = df_1m[(df_1m.index.date >= td - timedelta(days=10)) & (df_1m.index.date <= td)].copy()
            orb = strategy.compute_orb(lb, td)
            if orb is None:
                continue
            if not strategy.check_regime(lb, td):
                continue

            strategy.precompute_day(day_1m.copy())
            po = day_1m[day_1m.index.time >= pd.Timestamp("09:30").time()]
            found = False

            for i in range(len(po)):
                bt = po.index[i]
                spot = float(po.iloc[i]["Close"])

                if bt.time() >= NO_ENTRY_AFTER:
                    break

                if daily_pnl <= -DAILY_DD_LIMIT:
                    kill_switch_active = True
                    kill_switch_days += 1
                    break

                gi = lb.index.get_loc(bt)
                if isinstance(gi, slice):
                    gi = gi.start
                li = day_1m.index.get_loc(bt)
                if isinstance(li, slice):
                    li = li.start

                sig = strategy.scan_bar(lb, gi, local_pos=li)
                if sig is None or sig.direction != "PUT":
                    continue

                # Entry
                se, sx = effective_slippage(vix)
                lots_sweep = max(1, int(CAPITAL * 0.01 / (lot_size * sig.r_value)))
                qty_sweep = lots_sweep * lot_size
                expiry = next_expiry(td)
                dte = max((expiry - td).days, 1)
                ep = _synthetic_premium(sig.entry_spot, sig.strike, "PUT", vix, dte) * (1 + se)

                # Compute delta
                t_y = dte / 365
                iv_s = vix / 100
                entry_delta = bs_delta(sig.entry_spot, sig.strike, t_y, 0.065, iv_s, "put") if t_y > 0 else 0.5

                # Walk bars for exit
                peak = ep
                exit_prem = ep
                exit_type = "TIME"
                exit_spot = spot
                exit_time = bt

                for j in range(i + 1, len(po)):
                    bt2 = po.index[j]
                    s2 = float(po.iloc[j]["Close"])

                    if bt2.time() >= FORCE_EXIT_TIME:
                        el = j / max(len(po), 1) * 0.8
                        dr = max(dte - el, 0.5)
                        exit_prem = _synthetic_premium(s2, sig.strike, "PUT", vix, dr) * (1 - sx)
                        exit_spot = s2
                        exit_time = bt2
                        break

                    el = j / max(len(po), 1) * 0.8
                    dr = max(dte - el, 0.5)
                    cp = _synthetic_premium(s2, sig.strike, "PUT", vix, dr)

                    if cp > peak:
                        peak = cp

                    sl_p = ep * (1 - cfg.put_sl_pct)
                    tp_p = ep * (1 + cfg.put_tp_pct)
                    ton = peak >= ep * (1 + cfg.put_trail_activate)

                    if cp >= tp_p:
                        exit_prem = cp * (1 - sx)
                        exit_type = "TP"
                        exit_spot = s2
                        exit_time = bt2
                        break
                    elif ton and cp <= peak * (1 - cfg.put_trail_pct):
                        exit_prem = cp * (1 - sx)
                        exit_type = "TRAIL"
                        exit_spot = s2
                        exit_time = bt2
                        break
                    elif cp <= sl_p:
                        exit_prem = cp * (1 - sx)
                        exit_type = "SL"
                        exit_spot = s2
                        exit_time = bt2
                        break
                else:
                    el = len(po) / max(len(po), 1) * 0.8
                    dr = max(dte - el, 0.5)
                    exit_prem = _synthetic_premium(float(po.iloc[-1]["Close"]), sig.strike, "PUT", vix, dr) * (1 - sx)
                    exit_spot = float(po.iloc[-1]["Close"])
                    exit_time = po.index[-1]

                gross = (exit_prem - ep) * qty_sweep
                cost = round_trip_cost(ep, exit_prem, qty_sweep)
                net = gross - cost

                daily_pnl += net
                daily_trades_count += 1
                equity += net

                trades.append({
                    "entry_date": str(td),
                    "exit_date": str(exit_time.date()) if hasattr(exit_time, "date") else str(td),
                    "entry_time": str(bt),
                    "exit_time": str(exit_time),
                    "strategy": "put_sweep",
                    "signal_reason": "ORB sweep reversal: {} below VWAP".format(sig.entry_mode),
                    "direction": "PUT",
                    "strike": sig.strike,
                    "spot_entry": round(sig.entry_spot, 2),
                    "spot_exit": round(exit_spot, 2),
                    "entry_premium": round(ep, 2),
                    "exit_premium": round(exit_prem, 2),
                    "sl_amount": round(ep * cfg.put_sl_pct, 2),
                    "tp_amount": round(ep * cfg.put_tp_pct, 2),
                    "india_vix": round(vix, 2),
                    "bs_delta_call": 0,
                    "bs_delta_put": round(entry_delta, 3),
                    "is_expiry_day": False,
                    "lot_size": lot_size,
                    "quantity": qty_sweep,
                    "lots": lots_sweep,
                    "gross_pnl": round(gross, 2),
                    "costs": round(cost, 2),
                    "pnl_net": round(net, 2),
                    "exit_type": exit_type,
                    "equity": round(equity, 2),
                    "margin_used": 0,
                    "slippage_entry_pct": round(se * 100, 1),
                    "slippage_exit_pct": round(sx * 100, 1),
                    "kill_switch": kill_switch_active,
                    "stt_trap_exit": False,
                })
                found = True
                break

    # ================================================================
    # RESULTS
    # ================================================================
    df_t = pd.DataFrame(trades)
    if df_t.empty:
        print("No trades!")
        return

    pnls = df_t["pnl_net"].values
    final_equity = CAPITAL + pnls.sum()
    eq_curve = CAPITAL + np.cumsum(pnls)
    peak = np.maximum.accumulate(eq_curve)
    dd = ((eq_curve - peak) / peak * 100).min()

    first_dt = pd.to_datetime(df_t["entry_date"].min())
    last_dt = pd.to_datetime(df_t["entry_date"].max())
    years = (last_dt - first_dt).days / 365.25
    cagr = ((final_equity / CAPITAL) ** (1 / max(years, 0.1)) - 1) * 100

    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    wr = len(wins) / len(pnls) * 100
    pf = wins.sum() / abs(losses.sum()) if len(losses) else float("inf")

    dpnl = df_t.groupby("entry_date")["pnl_net"].sum()
    sharpe = dpnl.mean() / dpnl.std() * np.sqrt(252) if dpnl.std() > 0 else 0

    # Nifty B&H
    nd = pd.read_csv(DATA_DIR / "zerodha_nifty_day.csv", index_col=0, parse_dates=True)
    nbh = nd[(nd.index >= first_dt) & (nd.index <= last_dt)]
    n0 = float(nbh.iloc[0]["Close"])
    n1 = float(nbh.iloc[-1]["Close"])
    bh_ret = (n1 / n0 - 1) * 100
    bh_cagr = ((n1 / n0) ** (1 / max(years, 0.1)) - 1) * 100

    sep = "=" * 80
    line = "-" * 80

    print("\n" + sep)
    print("  PRODUCTION BACKTEST RESULTS")
    print(sep)

    print("\n  HEADLINE:")
    print("  " + line)
    print("  {:28s} {:>18s} {:>18s}".format("Metric", "Strategy", "Nifty B&H"))
    print("  " + line)
    print("  {:28s} {:>18,d} {:>18s}".format("Trades", len(pnls), "1"))
    print("  {:28s} {:>17.1f}% {:>18s}".format("Win Rate", wr, "N/A"))
    print("  {:28s} {:>18.3f} {:>18s}".format("Profit Factor", pf, "N/A"))
    print("  {:28s} {:>17.1f}% {:>17.1f}%".format("CAGR", cagr, bh_cagr))
    print("  {:28s} {:>18.3f} {:>18s}".format("Sharpe", sharpe, "N/A"))
    print("  {:28s} {:>17.1f}% {:>18s}".format("Max Drawdown", dd, "N/A"))
    print("  {:28s} {:>14s} {:>14s}".format("Final Equity",
          "Rs {:,}".format(int(final_equity)), "Rs {:,}".format(int(CAPITAL * n1 / n0))))
    print("  {:28s} {:>17.1f}% {:>17.1f}%".format("Total Return",
          (final_equity / CAPITAL - 1) * 100, bh_ret))
    print("  " + line)

    # Safety stats
    straddle_t = df_t[df_t["strategy"] == "short_straddle"]
    sweep_t = df_t[df_t["strategy"] == "put_sweep"]

    print("\n  COMPONENTS:")
    print("  " + line)
    for label, sub in [("Short Straddle", straddle_t), ("PUT Sweep", sweep_t)]:
        if sub.empty:
            print("  {:25s} No trades".format(label))
            continue
        sp = sub["pnl_net"]
        sw = (sp > 0).sum()
        sgp = sp[sp > 0].sum()
        sgl = abs(sp[sp <= 0].sum()) if (sp <= 0).any() else 1e-9
        print("  {:25s} N={:>4}  WR={:>5.1f}%  PF={:>6.3f}  Net=Rs{:>10,}".format(
            label, len(sp), sw / len(sp) * 100, sgp / sgl, int(sp.sum())))
    print("  " + line)

    # Safety feature activation
    print("\n  SAFETY FEATURES ACTIVATED:")
    print("  " + line)
    print("    Kill switch days:        {}".format(kill_switch_days))
    print("    VIX halt days:           {}".format(vix_halt_days))
    print("    STT trap exits:          {}".format(stt_trap_exits))
    print("    Margin blocks:           {}".format(margin_blocks))
    print("  " + line)

    # Lot size periods
    pre_lot = df_t[pd.to_datetime(df_t["entry_date"]) < str(LOT_SIZE_CHANGE)]
    post_lot = df_t[pd.to_datetime(df_t["entry_date"]) >= str(LOT_SIZE_CHANGE)]
    print("\n  LOT SIZE IMPACT:")
    print("    Before Nov 2024 (lot=50): {} trades, Net=Rs{:,}".format(
        len(pre_lot), int(pre_lot["pnl_net"].sum()) if not pre_lot.empty else 0))
    print("    After Nov 2024 (lot=75):  {} trades, Net=Rs{:,}".format(
        len(post_lot), int(post_lot["pnl_net"].sum()) if not post_lot.empty else 0))

    # Expiry calendar periods
    pre_shift = df_t[(df_t["is_expiry_day"]) & (pd.to_datetime(df_t["entry_date"]) < str(EXPIRY_SHIFT))]
    post_shift = df_t[(df_t["is_expiry_day"]) & (pd.to_datetime(df_t["entry_date"]) >= str(EXPIRY_SHIFT))]
    print("\n  EXPIRY CALENDAR:")
    print("    Thursday expiry (pre Apr-2025): {} straddle trades".format(len(pre_shift)))
    print("    Tuesday expiry (post Apr-2025): {} straddle trades".format(len(post_shift)))

    # Year-by-year
    df_t["year"] = pd.to_datetime(df_t["entry_date"]).dt.year
    print("\n  YEARLY BREAKDOWN:")
    print("  " + line)
    print("  {:>6s} {:>7s} {:>7s} {:>8s} {:>12s} {:>8s} {:>8s} {:>5s}".format(
        "Year", "N", "WR", "PF", "Net P&L", "Strat%", "Nifty%", "Beat"))
    print("  " + line)
    for yr in sorted(df_t["year"].unique()):
        yd = df_t[df_t["year"] == yr]
        p = yd["pnl_net"]
        n = len(yd)
        w = (p > 0).sum()
        gp = p[p > 0].sum()
        gl = abs(p[p <= 0].sum()) if (p <= 0).any() else 1e-9
        s_pct = p.sum() / CAPITAL * 100
        yn = nd[nd.index.year == yr]
        n_pct = (float(yn.iloc[-1]["Close"]) / float(yn.iloc[0]["Close"]) - 1) * 100 if len(yn) > 1 else 0
        beat = "YES" if s_pct > n_pct else "no"
        print("  {:>6d} {:>5d} {:>6.1f}% {:>7.2f} Rs{:>9,} {:>+7.1f}% {:>+7.1f}% {:>5s}".format(
            yr, n, w / n * 100, gp / gl, int(p.sum()), s_pct, n_pct, beat))
    print("  " + line)

    # Monthly (last 12)
    df_t["month"] = pd.to_datetime(df_t["entry_date"]).dt.to_period("M")
    mo = df_t.groupby("month").agg(
        n=("pnl_net", "count"), net=("pnl_net", "sum"),
        wins=("pnl_net", lambda x: (x > 0).sum()))
    mo["wr"] = mo["wins"] / mo["n"] * 100
    print("\n  MONTHLY (last 12):")
    print("  " + line)
    for idx, r in mo.tail(12).iterrows():
        bar = "+" * min(int(max(r["net"] / 200, 0)), 30) + "-" * min(int(max(-r["net"] / 200, 0)), 30)
        print("    {:>7s}  N={:>3d}  WR={:>5.1f}%  Rs{:>7,}  {}".format(
            str(idx), int(r["n"]), r["wr"], int(r["net"]), bar))
    print("  " + line)
    print("  Profitable months: {}/{} ({:.1f}%)".format(
        (mo["net"] > 0).sum(), len(mo), (mo["net"] > 0).mean() * 100))

    # Exit type breakdown
    print("\n  EXIT TYPES:")
    for strategy in ["short_straddle", "put_sweep"]:
        sub = df_t[df_t["strategy"] == strategy]
        if sub.empty:
            continue
        exits = sub["exit_type"].value_counts().to_dict()
        print("    {}: {}".format(strategy, exits))

    # Avg slippage cost
    avg_slip_entry = df_t["slippage_entry_pct"].mean()
    avg_slip_exit = df_t["slippage_exit_pct"].mean()
    total_costs = df_t["costs"].sum()
    total_gross = df_t["gross_pnl"].sum()
    print("\n  COST ANALYSIS:")
    print("    Total gross P&L:    Rs {:>10,}".format(int(total_gross)))
    print("    Total costs:        Rs {:>10,}".format(int(total_costs)))
    print("    Costs as % of gross: {:.1f}%".format(total_costs / max(abs(total_gross), 1) * 100))
    print("    Avg entry slippage:  {:.1f}%".format(avg_slip_entry))
    print("    Avg exit slippage:   {:.1f}%".format(avg_slip_exit))

    # Monte Carlo
    print("\n  MONTE CARLO (10K shuffles):")
    print("  " + line)
    np.random.seed(42)
    mc_dd = []
    mc_f = []
    for _ in range(10000):
        sh = np.random.permutation(pnls)
        eq = CAPITAL + np.cumsum(sh)
        pk = np.maximum.accumulate(eq)
        mc_dd.append(((eq - pk) / pk * 100).min())
        mc_f.append(eq[-1])
    mc_dd = np.array(mc_dd)
    mc_f = np.array(mc_f)
    print("    DD:    median={:.1f}%  95th={:.1f}%  worst={:.1f}%".format(
        np.median(mc_dd), np.percentile(mc_dd, 5), mc_dd.min()))
    print("    Final: median=Rs{:>8,}  5th=Rs{:>8,}  worst=Rs{:>8,}".format(
        int(np.median(mc_f)), int(np.percentile(mc_f, 5)), int(mc_f.min())))
    print("    P(ruin < Rs 1.5L): {:.2f}%".format((mc_f < 150000).mean() * 100))
    print("    P(profit):         {:.1f}%".format((mc_f > CAPITAL).mean() * 100))
    print("  " + line)

    # Save
    df_t.to_csv(REPORTS_DIR / "production_backtest_3yr.csv", index=False)
    print("\n  Saved: reports/production_backtest_3yr.csv ({} trades)".format(len(df_t)))
    print(sep)


if __name__ == "__main__":
    run_production_backtest()
