"""
Full portfolio audit with realistic assumptions.
Capital: Rs 3,00,000
Slippage: 3% entry / 5% exit (realistic for 0DTE)
Costs: Full Zerodha model
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np
from datetime import date, time, timedelta
from pathlib import Path

from orb_sweep.expiry_straddle import _bs_0dte
from orb_sweep.combined_backtest import is_expiry_day, INSTRUMENTS, DATA_DIR, _get_vix
from orb_sweep.config import TradingConfig
from orb_sweep.strategy import ORBSweepStrategy
from orb_sweep.backtest import _synthetic_premium
from backtest.option_pricer import next_expiry
from costs.zerodha import round_trip_cost

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
CAPITAL = 300_000
SLIP_ENTRY = 0.03
SLIP_EXIT = 0.05


def load_data(spec):
    df = pd.read_csv(DATA_DIR / spec.spot_csv, index_col=0, parse_dates=True)
    if df.index.tz: df.index = df.index.tz_localize(None)
    for c in ["Open","High","Low","Close","Volume"]:
        if c.lower() in df.columns and c not in df.columns:
            df = df.rename(columns={c.lower(): c})
    vx = pd.read_csv(DATA_DIR / "zerodha_vix_day.csv", index_col=0, parse_dates=True)
    if "close" in vx.columns: vx = vx.rename(columns={"close": "Close"})
    if vx.index.tz: vx.index = vx.index.tz_localize(None)
    return df.sort_index(), vx.sort_index()


def run_straddle(df_1m, df_vix, dates, spec, sl, tp, ta, tr, vlo, vhi, risk_pct=0.03):
    trades = []
    for td in dates:
        day = df_1m[df_1m.index.date == td]
        if len(day) < 30: continue
        vix = _get_vix(df_vix, td)
        if vix < vlo or vix > vhi: continue
        em = day.index.time >= time(9,31)
        if not em.any(): continue
        spot = float(day[em].iloc[0]["Close"])
        etime = day[em].index[0]
        strike = int(round(spot / spec.strike_step) * spec.strike_step)
        ml = max(930 - etime.hour*60 - etime.minute, 1)
        se = (_bs_0dte(spot, strike, ml, vix, "call") + _bs_0dte(spot, strike, ml, vix, "put")) * (1 - SLIP_ENTRY)
        if se < 5: continue
        risk = CAPITAL * risk_pct
        lots = max(1, int(risk / (se * sl * spec.lot_size)))
        qty = lots * spec.lot_size
        sl_v, tp_v, to_v = se*(1+sl), se*(1-tp), se*(1-ta)
        post = day[day.index > etime]; trough = se; tact = False
        xsv = se; xt = "TIME"; xtime = etime; xspot = spot
        for bt, bar in post.iterrows():
            s = float(bar["Close"]); m = max(930 - bt.hour*60 - bt.minute, 1)
            cs = _bs_0dte(s, strike, m, vix, "call") + _bs_0dte(s, strike, m, vix, "put")
            if cs < trough: trough = cs
            if trough <= to_v: tact = True
            if bt.time() >= time(15,10): xsv=cs; xtime=bt; xspot=s; break
            if cs <= tp_v: xsv=cs; xt="TP"; xtime=bt; xspot=s; break
            if tact and cs >= trough*(1+tr): xsv=cs; xt="TRAIL"; xtime=bt; xspot=s; break
            if cs >= sl_v: xsv=cs; xt="SL"; xtime=bt; xspot=s; break
        xsv *= (1 + SLIP_EXIT)
        g = (se - xsv) * qty
        c = round_trip_cost(se/2, xsv/2, qty) * 2
        trades.append({"entry_date":str(td), "exit_date":str(xtime.date()) if hasattr(xtime,"date") else str(td),
            "direction":"short_straddle", "component":f"{spec.name.lower()}_straddle",
            "strike":strike, "lots":lots, "qty":qty, "exit_type":xt,
            "gross_pnl":round(g,2), "cost":round(c,2), "pnl_net":round(g-c,2)})
    return trades


def run_sweep(df_1m, df_vix, dates, spec, risk_pct=0.01, min_atr=18, min_range=0.0025, vol_mult=1.5):
    cfg = TradingConfig(lot_size=spec.lot_size, strike_step=spec.strike_step,
        capital=CAPITAL, min_atr_5m=min_atr, min_range_pct=min_range, volume_multiplier=vol_mult)
    strategy = ORBSweepStrategy(cfg)
    trades = []
    for td in dates:
        strategy.reset_day()
        day = df_1m[df_1m.index.date == td].copy()
        if len(day) < 30: continue
        lb = df_1m[(df_1m.index.date >= td - timedelta(days=10)) & (df_1m.index.date <= td)].copy()
        orb = strategy.compute_orb(lb, td)
        if orb is None: continue
        if not strategy.check_regime(lb, td): continue
        vix = _get_vix(df_vix, td)
        strategy.precompute_day(day)
        po = day[day.index.time >= pd.Timestamp("09:30").time()]
        active = None; peak = 0
        for i in range(len(po)):
            bt = po.index[i]; spot = float(po.iloc[i]["Close"])
            if bt.time() >= pd.Timestamp("15:15").time():
                if active:
                    dr = max(active["_dte"]-0.5, 0.5)
                    xp = _synthetic_premium(spot, active["strike"], "PUT", vix, dr)*(1-SLIP_EXIT)
                    g = (xp - active["ep"])*active["qty"]
                    c = round_trip_cost(active["ep"], xp, active["qty"])
                    trades.append({"entry_date":active["ed"],"exit_date":str(bt.date()),
                        "direction":"put_sweep","component":f"{spec.name.lower()}_sweep",
                        "strike":active["strike"],"lots":active["lots"],"qty":active["qty"],
                        "exit_type":"TIME","gross_pnl":round(g,2),"cost":round(c,2),"pnl_net":round(g-c,2)})
                    active = None
                break
            if active:
                el = i/max(len(po),1)*0.8; dr = max(active["_dte"]-el, 0.5)
                cp = _synthetic_premium(spot, active["strike"], "PUT", vix, dr)
                if cp > peak: peak = cp
                sl_p = active["ep"]*(1-cfg.put_sl_pct); tp_p = active["ep"]*(1+cfg.put_tp_pct)
                ton = peak >= active["ep"]*(1+cfg.put_trail_activate)
                xt = None
                if cp >= tp_p: xt="TP"
                elif ton and cp <= peak*(1-cfg.put_trail_pct): xt="TRAIL"
                elif cp <= sl_p: xt="SL"
                if xt:
                    xp = cp*(1-SLIP_EXIT); g=(xp-active["ep"])*active["qty"]
                    c = round_trip_cost(active["ep"], xp, active["qty"])
                    trades.append({"entry_date":active["ed"],"exit_date":str(bt.date()),
                        "direction":"put_sweep","component":f"{spec.name.lower()}_sweep",
                        "strike":active["strike"],"lots":active["lots"],"qty":active["qty"],
                        "exit_type":xt,"gross_pnl":round(g,2),"cost":round(c,2),"pnl_net":round(g-c,2)})
                    active = None
                continue
            if bt.time() >= pd.Timestamp("14:00").time(): continue
            gi = lb.index.get_loc(bt)
            if isinstance(gi, slice): gi = gi.start
            li = day.index.get_loc(bt)
            if isinstance(li, slice): li = li.start
            sig = strategy.scan_bar(lb, gi, local_pos=li)
            if sig is None or sig.direction != "PUT": continue
            lots = max(1, int(CAPITAL*risk_pct/(spec.lot_size*sig.r_value)))
            qty = lots*spec.lot_size; expiry = next_expiry(td); dte = max((expiry-td).days, 1)
            ep = _synthetic_premium(sig.entry_spot, sig.strike, "PUT", vix, dte)*(1+SLIP_ENTRY)
            active = {"ed":str(td),"strike":sig.strike,"ep":ep,"qty":qty,"lots":lots,"_dte":dte}
            peak = ep
        if active:
            last = po.iloc[-1]; lt = po.index[-1]
            dr = max(active["_dte"]-0.5, 0.5)
            xp = _synthetic_premium(float(last["Close"]), active["strike"], "PUT", vix, dr)*(1-SLIP_EXIT)
            g = (xp-active["ep"])*active["qty"]; c = round_trip_cost(active["ep"], xp, active["qty"])
            trades.append({"entry_date":active["ed"],"exit_date":str(lt.date()),
                "direction":"put_sweep","component":f"{spec.name.lower()}_sweep",
                "strike":active["strike"],"lots":active["lots"],"qty":active["qty"],
                "exit_type":"TIME","gross_pnl":round(g,2),"cost":round(c,2),"pnl_net":round(g-c,2)})
    return trades
