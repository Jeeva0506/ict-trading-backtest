"""PUT-only detailed report: year-by-year, monthly DD, yearly DD."""
import sys, io, logging, time
from datetime import date, timedelta
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.WARNING)

from orb_sweep.optimize import _load_data, generate_signals
from backtest.option_pricer import bs_price, next_expiry, is_expiry_day
from costs.zerodha import round_trip_cost


def replay_put_only(signals, sl_pct=0.30, tp_pct=0.75, trail_activate=0.25, trail_pct=0.20):
    capital = 1_000_000
    lot_size = 50
    trades = []
    current_date = None
    daily_count = 0
    daily_pnl = 0.0

    for sig in signals:
        if sig.direction != 'PUT':
            continue
        if is_expiry_day(sig.trade_date):
            continue
        if sig.trade_date != current_date:
            current_date = sig.trade_date
            daily_count = 0
            daily_pnl = 0.0
        if daily_count >= 2 or daily_pnl <= -10000:
            continue

        expiry = next_expiry(sig.trade_date)
        dte = max((expiry - sig.trade_date).days, 1)
        iv = max(sig.vix / 100, 0.05)
        t_entry = max(dte / 365, 0.001)
        entry_prem = bs_price(sig.entry_spot, sig.strike, t_entry, 0.065, iv, 'put') * 1.005
        lots = max(1, int(10000 / (lot_size * entry_prem * sl_pct)))
        qty = lots * lot_size

        sl_prem = entry_prem * (1 - sl_pct)
        tp_prem = entry_prem * (1 + tp_pct)
        peak_prem = entry_prem
        exit_prem = entry_prem
        exit_type = 'TIME'
        n_bars = len(sig.remaining_highs)

        for i in range(n_bars):
            t = sig.remaining_times[i]
            if t >= pd.Timestamp('15:15').time():
                spot = sig.remaining_closes[i]
                t_now = max((dte - 0.5) / 365, 0.001)
                exit_prem = bs_price(spot, sig.strike, t_now, 0.065, iv, 'put')
                exit_type = 'TIME'
                break
            spot_c = sig.remaining_closes[i]
            elapsed_frac = i / max(n_bars, 1) * 0.8
            t_now = max((dte - elapsed_frac) / 365, 0.001)
            cur_prem = bs_price(spot_c, sig.strike, t_now, 0.065, iv, 'put')
            if cur_prem > peak_prem:
                peak_prem = cur_prem
            if cur_prem >= tp_prem:
                exit_prem = cur_prem; exit_type = 'TP'; break
            if peak_prem >= entry_prem * (1 + trail_activate) and cur_prem <= peak_prem * (1 - trail_pct):
                exit_prem = cur_prem; exit_type = 'TRAIL'; break
            if cur_prem <= sl_prem:
                exit_prem = cur_prem; exit_type = 'SL'; break
        else:
            spot = sig.remaining_closes[-1]
            t_now = max((dte - 0.5) / 365, 0.001)
            exit_prem = bs_price(spot, sig.strike, t_now, 0.065, iv, 'put')
            exit_type = 'TIME'

        exit_prem *= 0.995
        gross = (exit_prem - entry_prem) * qty
        costs = round_trip_cost(entry_prem, max(exit_prem, 0.05), qty)
        net = gross - costs
        trades.append({'net_pnl': net, 'exit_type': exit_type, 'date': sig.trade_date,
                       'entry_prem': entry_prem, 'exit_prem': exit_prem, 'qty': qty,
                       'gross': gross, 'costs': costs})
        daily_pnl += net
        daily_count += 1

    return trades


def main():
    data_dir = ROOT / 'data'
    print('Loading data & generating signals...')
    df_1m, df_vix = _load_data(str(data_dir / 'zerodha_nifty_1m.csv'),
                                str(data_dir / 'zerodha_vix_day.csv'))
    t0 = time.time()
    sigs = generate_signals(df_1m, df_vix, date(2018,1,1), date(2026,5,22),
                            min_range_pct=0.0025, min_atr_5m=18.0,
                            sweep_closeback_bars=5, swing_pivot_n=1)
    print(f'  {len(sigs)} signals ({time.time()-t0:.0f}s)')

    trades = replay_put_only(sigs)
    capital = 1_000_000
    sep = '=' * 90

    all_pnls = np.array([t['net_pnl'] for t in trades])
    equity = capital + np.cumsum(all_pnls)
    peak_eq = np.maximum.accumulate(equity)

    # ── HEADER ─────────────────────────────────────────────────────────
    print(f'\n{sep}')
    print('  PUT-ONLY STRATEGY | Skip Expiry | Close 15:15 | After All Costs')
    print(f'  SL=30% | TP=75% | Trail@25%/20% | Slippage 0.5%/leg')
    print(f'{sep}')

    total_gross = sum(t['gross'] for t in trades)
    total_costs = sum(t['costs'] for t in trades)
    total_net = float(all_pnls.sum())
    wins = all_pnls[all_pnls > 0]
    losses = all_pnls[all_pnls <= 0]
    wr = len(wins) / len(all_pnls) * 100
    gp = float(wins.sum()) if len(wins) else 0
    gl = abs(float(losses.sum())) if len(losses) else 1e-9
    pf = gp / gl
    max_dd = float(((equity - peak_eq) / peak_eq * 100).min())
    years_span = (trades[-1]['date'] - trades[0]['date']).days / 365.25
    cagr = ((1 + total_net / capital) ** (1 / years_span) - 1) * 100

    print(f'\n  OVERALL: {len(trades)} trades over {years_span:.1f} years')
    print(f'  WR={wr:.1f}% | PF={pf:.3f} | CAGR={cagr:.1f}% | MaxDD={max_dd:.1f}%')
    print(f'  Net=Rs{total_net:+,.0f} | Gross=Rs{total_gross:+,.0f} | Costs=Rs{total_costs:,.0f}')
    print(f'  AvgWin=Rs{float(wins.mean()):+,.0f} | AvgLoss=Rs{float(losses.mean()):+,.0f} | Exp=Rs{total_net/len(trades):+,.0f}')

    # ── YEAR-BY-YEAR ───────────────────────────────────────────────────
    print(f'\n{sep}')
    print('  YEAR-BY-YEAR')
    print(f'{sep}')
    print(f'  {"Year":<6} {"N":<5} {"W":<4} {"L":<4} {"WR%":<7} {"PF":<7} {"Net PnL":<13} {"Ret%":<8} {"YearDD%":<9} {"AvgWin":<10} {"AvgLoss":<10} {"Exits"}')
    print(f'  {"-"*105}')

    years = sorted(set(t['date'].year for t in trades))
    cum = 0
    for y in years:
        yt = [t for t in trades if t['date'].year == y]
        yp = np.array([t['net_pnl'] for t in yt])
        nw = len(yp[yp > 0])
        nl = len(yp[yp <= 0])
        wr_y = nw / len(yp) * 100
        gp_y = float(yp[yp > 0].sum()) if nw else 0
        gl_y = abs(float(yp[yp <= 0].sum())) if nl else 1e-9
        pf_y = gp_y / gl_y
        net_y = float(yp.sum())
        cum += net_y
        ret_y = net_y / capital * 100
        yeq = capital + np.cumsum(yp)
        ypk = np.maximum.accumulate(yeq)
        ydd = float(((yeq - ypk) / ypk * 100).min())
        aw = float(yp[yp > 0].mean()) if nw else 0
        al = float(yp[yp <= 0].mean()) if nl else 0
        exits = defaultdict(int)
        for t in yt:
            exits[t['exit_type']] += 1
        ex_str = ' '.join(f'{k}:{v}' for k, v in sorted(exits.items()))
        print(f'  {y:<6} {len(yt):<5} {nw:<4} {nl:<4} {wr_y:<7.1f} {pf_y:<7.3f} Rs{net_y:>+11,.0f} {ret_y:>+7.1f}% {ydd:>+8.1f}% Rs{aw:>+8,.0f} Rs{al:>+8,.0f} {ex_str}')

    # ── MONTHLY P&L + MONTHLY DRAWDOWN ─────────────────────────────────
    print(f'\n{sep}')
    print('  MONTHLY P&L & DRAWDOWN')
    print(f'{sep}')

    monthly = defaultdict(list)
    for t in trades:
        key = f'{t["date"].year}-{t["date"].month:02d}'
        monthly[key].append(t['net_pnl'])

    months_sorted = sorted(monthly.keys())
    prof_m = 0
    loss_m = 0

    # For monthly DD: track equity month by month
    print(f'  {"Month":<10} {"N":<5} {"W":<4} {"L":<4} {"WR%":<7} {"Net PnL":<13} {"MonthDD%":<10} {"Status"}')
    print(f'  {"-"*65}')

    running_equity = capital
    for m in months_sorted:
        pnls_m = monthly[m]
        n_m = len(pnls_m)
        arr_m = np.array(pnls_m)
        nw_m = len(arr_m[arr_m > 0])
        nl_m = n_m - nw_m
        wr_m = nw_m / n_m * 100 if n_m else 0
        net_m = float(arr_m.sum())

        # Month drawdown from start of month
        meq = running_equity + np.cumsum(arr_m)
        mpk = np.maximum.accumulate(np.concatenate([[running_equity], meq]))
        mdd_arr = (np.concatenate([[running_equity], meq]) - mpk) / mpk * 100
        mdd = float(mdd_arr.min())
        running_equity = float(meq[-1])

        status = ' WIN' if net_m > 0 else 'LOSS'
        if net_m > 0:
            prof_m += 1
        else:
            loss_m += 1
        print(f'  {m:<10} {n_m:<5} {nw_m:<4} {nl_m:<4} {wr_m:<7.1f} Rs{net_m:>+11,.0f} {mdd:>+9.1f}% {status}')

    total_m = prof_m + loss_m
    print(f'\n  Profitable months: {prof_m}/{total_m} ({prof_m/total_m*100:.0f}%)')
    print(f'  Loss months:       {loss_m}/{total_m} ({loss_m/total_m*100:.0f}%)')

    # ── YEARLY DRAWDOWN TABLE ──────────────────────────────────────────
    print(f'\n{sep}')
    print('  YEARLY DRAWDOWN SUMMARY')
    print(f'{sep}')
    print(f'  {"Year":<6} {"MaxDD%":<9} {"MaxDD Rs":<13} {"Worst Month":<12} {"Worst Mo DD%":<13}')
    print(f'  {"-"*55}')

    for y in years:
        yt = [t for t in trades if t['date'].year == y]
        yp = np.array([t['net_pnl'] for t in yt])
        yeq = capital + np.cumsum(yp)
        ypk = np.maximum.accumulate(yeq)
        ydd = float(((yeq - ypk) / ypk * 100).min())
        ydd_rs = float((yeq - ypk).min())

        # Worst month
        ym = defaultdict(list)
        for t in yt:
            ym[t['date'].month].append(t['net_pnl'])
        worst_month = None
        worst_mdd = 0
        for mo, pnls_mo in ym.items():
            arr_mo = np.array(pnls_mo)
            meq = capital + np.cumsum(arr_mo)
            mpk = np.maximum.accumulate(np.concatenate([[capital], meq]))
            mdd_v = float((np.concatenate([[capital], meq]) - mpk) / mpk * 100).min() if len(arr_mo) else 0
            # Actually just use net P&L as worst indicator
            net_mo = float(arr_mo.sum())
            if net_mo < worst_mdd:
                worst_mdd = net_mo
                worst_month = f'{y}-{mo:02d}'

        if worst_month is None:
            worst_month = 'N/A'
            wm_dd = 0
        else:
            wm_pnls = monthly.get(worst_month, [])
            wm_arr = np.array(wm_pnls) if wm_pnls else np.array([0])
            wm_dd = float(wm_arr.sum()) / capital * 100

        print(f'  {y:<6} {ydd:>+8.1f}% Rs{ydd_rs:>+11,.0f} {worst_month:<12} {wm_dd:>+12.1f}%')

    # ── EXIT TYPE SUMMARY ──────────────────────────────────────────────
    print(f'\n{sep}')
    print('  EXIT TYPE BREAKDOWN')
    print(f'{sep}')
    exit_stats = defaultdict(lambda: {'n': 0, 'pnl': 0, 'wins': 0})
    for t in trades:
        et = t['exit_type']
        exit_stats[et]['n'] += 1
        exit_stats[et]['pnl'] += t['net_pnl']
        if t['net_pnl'] > 0:
            exit_stats[et]['wins'] += 1

    print(f'  {"Type":<8} {"N":<6} {"Pct%":<7} {"WinRate":<8} {"Net PnL":<13} {"Avg PnL":<10}')
    print(f'  {"-"*55}')
    for et in ['TP', 'TRAIL', 'SL', 'TIME']:
        s = exit_stats.get(et)
        if not s or s['n'] == 0:
            continue
        pct = s['n'] / len(trades) * 100
        wr_e = s['wins'] / s['n'] * 100
        avg_e = s['pnl'] / s['n']
        print(f'  {et:<8} {s["n"]:<6} {pct:<7.1f} {wr_e:<8.1f} Rs{s["pnl"]:>+11,.0f} Rs{avg_e:>+8,.0f}')

    # ── FINAL ──────────────────────────────────────────────────────────
    print(f'\n{sep}')
    print(f'  FINAL: Rs{capital:,.0f} -> Rs{float(equity[-1]):,.0f} | Net +Rs{total_net:,.0f} | {years_span:.1f}yr | CAGR {cagr:.1f}%')
    print(f'  All numbers AFTER brokerage + STT + exchange + SEBI + GST + stamp + 0.5% slippage')
    print(f'{sep}')
    print(f'\nTotal time: {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
