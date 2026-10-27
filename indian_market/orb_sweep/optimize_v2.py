"""
V2 Optimizer: Dual-mode strategy with asymmetric exits.
IS: 2020-2022 (3yr, high volatility) | OOS: 2018-2019 + 2023-2026
"""
import sys, io, logging, time
from datetime import date
from pathlib import Path
from itertools import product
import numpy as np
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.WARNING)

from orb_sweep.config import TradingConfig
from orb_sweep.backtest import ORBSweepBacktester

IS_START = date(2020, 1, 1)
IS_END   = date(2022, 12, 31)

OOS_RANGES = [
    (date(2018, 1, 1), date(2019, 12, 31)),
    (date(2023, 1, 1), date(2026, 5, 22)),
]

CALL_EXIT_GRID = [
    (0.25, 0.40, 0.15, 0.10),
    (0.25, 0.50, 0.20, 0.15),
    (0.30, 0.40, 0.15, 0.10),
    (0.30, 0.50, 0.20, 0.15),
    (0.30, 0.50, 0.25, 0.15),
    (0.30, 0.75, 0.25, 0.20),
    (0.35, 0.50, 0.20, 0.15),
    (0.35, 0.75, 0.25, 0.20),
    (0.40, 0.50, 0.25, 0.15),
    (0.40, 0.75, 0.25, 0.20),
]

PUT_EXIT_GRID = [
    (0.25, 0.50, 0.20, 0.15),
    (0.25, 0.75, 0.25, 0.20),
    (0.30, 0.50, 0.25, 0.15),
    (0.30, 0.75, 0.25, 0.20),
    (0.30, 1.00, 0.25, 0.20),
    (0.35, 0.75, 0.25, 0.20),
    (0.35, 1.00, 0.30, 0.20),
    (0.40, 0.75, 0.30, 0.20),
]


def run_combo(nifty_path, vix_path, call_exit, put_exit, start, end):
    csl, ctp, cta, ctr = call_exit
    psl, ptp, pta, ptr = put_exit
    cfg = TradingConfig(
        call_sl_pct=csl, call_tp_pct=ctp, call_trail_activate=cta, call_trail_pct=ctr,
        put_sl_pct=psl, put_tp_pct=ptp, put_trail_activate=pta, put_trail_pct=ptr,
    )
    bt = ORBSweepBacktester(cfg)
    bt.load_data(nifty_path, vix_path)
    metrics = bt.run(start_date=start, end_date=end)

    call_trades = [t for t in bt.trades if t.direction == "CALL"]
    put_trades  = [t for t in bt.trades if t.direction == "PUT"]
    call_pnl = sum(t.net_pnl for t in call_trades)
    put_pnl  = sum(t.net_pnl for t in put_trades)
    call_wr = (sum(1 for t in call_trades if t.net_pnl > 0) / len(call_trades) * 100) if call_trades else 0
    put_wr  = (sum(1 for t in put_trades if t.net_pnl > 0) / len(put_trades) * 100) if put_trades else 0

    pnls = np.array([t.net_pnl for t in bt.trades]) if bt.trades else np.array([0])
    equity = cfg.capital + np.cumsum(pnls)
    pk = np.maximum.accumulate(equity)
    dd = float(((equity - pk) / pk * 100).min()) if len(pnls) > 0 else 0

    years_span = max((end - start).days / 365.25, 0.5)
    annual_ret = float(pnls.sum()) / cfg.capital / years_span * 100

    return {
        "n": metrics.get("n_trades", 0),
        "wr": metrics.get("win_rate_pct", 0),
        "pf": metrics.get("profit_factor", 0),
        "net": metrics.get("net_pnl", 0),
        "dd": dd,
        "sharpe": metrics.get("sharpe", 0),
        "annual_ret": round(annual_ret, 1),
        "call_n": len(call_trades), "call_pnl": call_pnl, "call_wr": call_wr,
        "put_n": len(put_trades), "put_pnl": put_pnl, "put_wr": put_wr,
        "trades": bt.trades,
    }


def main():
    data_dir = ROOT / "data"
    nifty_path = str(data_dir / "zerodha_nifty_1m.csv")
    vix_path = str(data_dir / "zerodha_vix_day.csv")

    combos = list(product(CALL_EXIT_GRID, PUT_EXIT_GRID))
    print(f"Testing {len(combos)} combinations")
    print(f"IS: {IS_START} to {IS_END} | OOS: 2018-2019 + 2023-2026")
    t0 = time.time()

    # ── Stage 1: IS optimization ───────────────────────────────────────
    print(f"\n{'='*100}")
    print("  STAGE 1: In-Sample (2020-2022)")
    print(f"{'='*100}")

    is_results = []
    for i, (ce, pe) in enumerate(combos):
        m = run_combo(nifty_path, vix_path, ce, pe, IS_START, IS_END)
        score = 0
        if m["n"] >= 20 and m["pf"] > 1.0 and m["call_pnl"] > 0 and m["put_pnl"] > 0:
            score = m["pf"] * (m["wr"] / 100) * (1 + m["sharpe"]) * min(m["annual_ret"] / 40, 2.0)
        is_results.append({"ce": ce, "pe": pe, "m": m, "score": score})
        if (i + 1) % 20 == 0:
            el = time.time() - t0
            eta = el / (i + 1) * (len(combos) - i - 1)
            print(f"  [{i+1}/{len(combos)}] {el:.0f}s elapsed, ETA {eta:.0f}s")

    is_results.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n  Top 15 IS results (both directions profitable):")
    print(f'  {"#":<4} {"N":<5} {"WR%":<6} {"PF":<7} {"Ann%":<7} {"DD%":<7} {"C_N":<5} {"C_WR":<6} {"C_PnL":<12} {"P_N":<5} {"P_WR":<6} {"P_PnL":<12} {"Call Exit":<24} {"Put Exit"}')
    print(f"  {'-'*130}")

    top_is = [r for r in is_results if r["score"] > 0][:15]
    for rank, r in enumerate(top_is, 1):
        m = r["m"]
        ce, pe = r["ce"], r["pe"]
        print(
            f'  {rank:<4} {m["n"]:<5} {m["wr"]:<6.1f} {m["pf"]:<7.3f} {m["annual_ret"]:<7.1f} '
            f'{m["dd"]:<7.1f} {m["call_n"]:<5} {m["call_wr"]:<6.1f} Rs{m["call_pnl"]:>+10,.0f} '
            f'{m["put_n"]:<5} {m["put_wr"]:<6.1f} Rs{m["put_pnl"]:>+10,.0f} '
            f'SL{ce[0]*100:.0f}/TP{ce[1]*100:.0f}/T{ce[2]*100:.0f}/{ce[3]*100:.0f}  '
            f'SL{pe[0]*100:.0f}/TP{pe[1]*100:.0f}/T{pe[2]*100:.0f}/{pe[3]*100:.0f}'
        )

    if not top_is:
        print("  No configs with both directions profitable in IS.")
        # Fall back to best overall
        top_is = sorted(is_results, key=lambda x: x["m"].get("pf", 0) * x["m"].get("n", 0), reverse=True)[:10]
        print("  Showing top 10 by PF*N instead:")
        for rank, r in enumerate(top_is, 1):
            m = r["m"]
            print(f'  {rank:<4} N={m["n"]:<5} PF={m["pf"]:<7.3f} Ann={m["annual_ret"]:<6.1f}% C_PnL=Rs{m["call_pnl"]:>+10,.0f} P_PnL=Rs{m["put_pnl"]:>+10,.0f}')

    # ── Stage 2: OOS validation of top 10 ──────────────────────────────
    print(f"\n{'='*100}")
    print("  STAGE 2: Out-of-Sample Validation (2018-2019 + 2023-2026)")
    print(f"{'='*100}")

    validate = top_is[:10]
    oos_results = []

    for rank, r in enumerate(validate, 1):
        ce, pe = r["ce"], r["pe"]
        all_oos_trades = []

        for oos_start, oos_end in OOS_RANGES:
            oos_m = run_combo(nifty_path, vix_path, ce, pe, oos_start, oos_end)
            all_oos_trades.extend(oos_m["trades"])

        oos_pnls = np.array([t.net_pnl for t in all_oos_trades]) if all_oos_trades else np.array([0])
        oos_n = len(all_oos_trades)
        oos_net = float(oos_pnls.sum())
        oos_wins = len(oos_pnls[oos_pnls > 0])
        oos_wr = oos_wins / oos_n * 100 if oos_n else 0
        oos_gp = float(oos_pnls[oos_pnls > 0].sum()) if oos_wins else 0
        oos_gl = abs(float(oos_pnls[oos_pnls <= 0].sum())) if oos_n - oos_wins else 1e-9
        oos_pf = oos_gp / oos_gl if oos_gl > 0 else 0

        oos_eq = 1_000_000 + np.cumsum(oos_pnls)
        oos_pk = np.maximum.accumulate(oos_eq)
        oos_dd = float(((oos_eq - oos_pk) / oos_pk * 100).min()) if oos_n > 0 else 0

        oos_call = [t for t in all_oos_trades if t.direction == "CALL"]
        oos_put  = [t for t in all_oos_trades if t.direction == "PUT"]
        oos_call_pnl = sum(t.net_pnl for t in oos_call)
        oos_put_pnl  = sum(t.net_pnl for t in oos_put)

        oos_years = 5.4  # 2yr + 3.4yr
        oos_annual = oos_net / 1_000_000 / oos_years * 100

        is_pf = r["m"]["pf"]
        decay = oos_pf / is_pf if is_pf > 0 else 0

        oos_results.append({
            "rank": rank, "ce": ce, "pe": pe,
            "is_m": r["m"], "is_pf": is_pf,
            "oos_n": oos_n, "oos_wr": oos_wr, "oos_pf": oos_pf,
            "oos_net": oos_net, "oos_dd": oos_dd, "oos_annual": oos_annual,
            "oos_call_pnl": oos_call_pnl, "oos_put_pnl": oos_put_pnl,
            "decay": decay,
            "oos_trades": all_oos_trades,
        })

    print(f'\n  {"#":<4} {"IS_PF":<7} {"OOS_PF":<7} {"Decay":<7} {"OOS_N":<6} {"OOS_WR":<7} {"OOS_DD":<7} {"OOS_Ann%":<9} {"C_PnL":<12} {"P_PnL":<12} {"Call":<24} {"Put"}')
    print(f"  {'-'*130}")

    for r in oos_results:
        ce, pe = r["ce"], r["pe"]
        flag = " <--" if r["oos_pf"] > 1.0 and r["oos_annual"] > 10 else ""
        print(
            f'  {r["rank"]:<4} {r["is_pf"]:<7.3f} {r["oos_pf"]:<7.3f} {r["decay"]:<7.2f} '
            f'{r["oos_n"]:<6} {r["oos_wr"]:<7.1f} {r["oos_dd"]:<7.1f} {r["oos_annual"]:<9.1f} '
            f'Rs{r["oos_call_pnl"]:>+10,.0f} Rs{r["oos_put_pnl"]:>+10,.0f} '
            f'SL{ce[0]*100:.0f}/TP{ce[1]*100:.0f}/T{ce[2]*100:.0f}/{ce[3]*100:.0f}  '
            f'SL{pe[0]*100:.0f}/TP{pe[1]*100:.0f}/T{pe[2]*100:.0f}/{pe[3]*100:.0f}{flag}'
        )

    # ── Stage 3: Full period validation of best ────────────────────────
    best = None
    for r in oos_results:
        if r["oos_pf"] > 1.0 and r["oos_call_pnl"] > 0 and r["oos_put_pnl"] > 0:
            if best is None or r["oos_pf"] * r["decay"] > best["oos_pf"] * best["decay"]:
                best = r
    if best is None:
        profitable = [r for r in oos_results if r["oos_pf"] > 1.0]
        if profitable:
            best = max(profitable, key=lambda r: r["oos_pf"])
    if best is None:
        best = max(oos_results, key=lambda r: r["oos_pf"])

    ce, pe = best["ce"], best["pe"]
    print(f"\n{'='*100}")
    print(f"  STAGE 3: Full Period Validation (best config)")
    print(f"{'='*100}")
    print(f"  CALL exits: SL={ce[0]*100:.0f}% TP={ce[1]*100:.0f}% Trail@{ce[2]*100:.0f}%/{ce[3]*100:.0f}%")
    print(f"  PUT exits:  SL={pe[0]*100:.0f}% TP={pe[1]*100:.0f}% Trail@{pe[2]*100:.0f}%/{pe[3]*100:.0f}%")

    full = run_combo(nifty_path, vix_path, ce, pe, date(2018, 1, 1), date(2026, 5, 22))
    fm = full

    print(f"\n  Full Period: {fm['n']} trades | WR={fm['wr']:.1f}% | PF={fm['pf']:.3f}")
    print(f"  Net P&L: Rs{fm['net']:+,.0f} | Annual: {fm['annual_ret']:.1f}% | DD: {fm['dd']:.1f}%")
    print(f"  CALL: {fm['call_n']} trades, WR={fm['call_wr']:.1f}%, PnL=Rs{fm['call_pnl']:+,.0f}")
    print(f"  PUT:  {fm['put_n']} trades, WR={fm['put_wr']:.1f}%, PnL=Rs{fm['put_pnl']:+,.0f}")

    # Year-by-year
    print(f"\n  Year-by-Year:")
    all_trades = full["trades"]
    years = sorted(set(t.trade_date.year for t in all_trades))
    cum = 0
    for y in years:
        yt = [t for t in all_trades if t.trade_date.year == y]
        yp = np.array([t.net_pnl for t in yt])
        wins = len(yp[yp > 0])
        wr = wins / len(yp) * 100 if len(yp) else 0
        gp = float(yp[yp > 0].sum()) if wins else 0
        gl = abs(float(yp[yp <= 0].sum())) if len(yp) - wins else 1e-9
        pf = gp / gl
        net = float(yp.sum())
        cum += net
        ret = net / 1_000_000 * 100
        yeq = 1_000_000 + np.cumsum(yp)
        ypk = np.maximum.accumulate(yeq)
        ydd = float(((yeq - ypk) / ypk * 100).min())

        call_t = [t for t in yt if t.direction == "CALL"]
        put_t = [t for t in yt if t.direction == "PUT"]
        call_p = sum(t.net_pnl for t in call_t)
        put_p = sum(t.net_pnl for t in put_t)

        print(f"    {y}: {len(yt):>3} trades | WR={wr:>5.1f}% | PF={pf:>6.3f} | "
              f"Ret={ret:>+7.1f}% | DD={ydd:>+6.1f}% | C=Rs{call_p:>+10,.0f} P=Rs{put_p:>+10,.0f}")

    print(f"\n  RECOMMENDED CONFIG UPDATE:")
    print(f"    call_sl_pct        = {ce[0]}")
    print(f"    call_tp_pct        = {ce[1]}")
    print(f"    call_trail_activate = {ce[2]}")
    print(f"    call_trail_pct     = {ce[3]}")
    print(f"    put_sl_pct         = {pe[0]}")
    print(f"    put_tp_pct         = {pe[1]}")
    print(f"    put_trail_activate = {pe[2]}")
    print(f"    put_trail_pct      = {pe[3]}")

    total = time.time() - t0
    print(f"\nTotal time: {total:.0f}s ({total/60:.1f}min)")


if __name__ == "__main__":
    main()
