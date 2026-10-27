"""Expiry day audit: split metrics by expiry vs non-expiry, test CALL with quick close."""
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


def replay_with_close_at_315(signals, sl_pct, tp_pct, trail_activate, trail_pct,
                              direction_filter=None, skip_expiry=False):
    """Replay exits, always closing at 15:15. Optionally filter by direction/expiry."""
    capital = 1_000_000
    lot_size = 50
    trades = []
    current_date = None
    daily_count = 0
    daily_pnl = 0.0

    for sig in signals:
        if direction_filter and sig.direction != direction_filter:
            continue
        if skip_expiry and is_expiry_day(sig.trade_date):
            continue

        if sig.trade_date != current_date:
            current_date = sig.trade_date
            daily_count = 0
            daily_pnl = 0.0
        if daily_count >= 2 or daily_pnl <= -10000:
            continue

        is_call = sig.direction == 'CALL'
        expiry = next_expiry(sig.trade_date)
        dte = max((expiry - sig.trade_date).days, 1)
        iv = max(sig.vix / 100, 0.05)
        t_entry = max(dte / 365, 0.001)
        opt_type = 'call' if is_call else 'put'
        entry_prem = bs_price(sig.entry_spot, sig.strike, t_entry, 0.065, iv, opt_type) * 1.005

        lots = max(1, int(10000 / (lot_size * entry_prem * sl_pct)))
        qty = lots * lot_size

        sl_prem_level = entry_prem * (1 - sl_pct)
        tp_prem_level = entry_prem * (1 + tp_pct)
        peak_prem = entry_prem
        exit_prem = entry_prem
        exit_type = 'TIME'
        is_exp = is_expiry_day(sig.trade_date)
        n_bars = len(sig.remaining_highs)

        for i in range(n_bars):
            t = sig.remaining_times[i]
            if t >= pd.Timestamp('15:15').time():
                spot = sig.remaining_closes[i]
                t_now = max((dte - 0.5) / 365, 0.001)
                exit_prem = bs_price(spot, sig.strike, t_now, 0.065, iv, opt_type)
                exit_type = 'TIME'
                break

            spot_c = sig.remaining_closes[i]
            elapsed_frac = i / max(n_bars, 1) * 0.8
            t_now = max((dte - elapsed_frac) / 365, 0.001)
            cur_prem = bs_price(spot_c, sig.strike, t_now, 0.065, iv, opt_type)
            if cur_prem > peak_prem:
                peak_prem = cur_prem

            if cur_prem >= tp_prem_level:
                exit_prem = cur_prem
                exit_type = 'TP'
                break
            if peak_prem >= entry_prem * (1 + trail_activate) and cur_prem <= peak_prem * (1 - trail_pct):
                exit_prem = cur_prem
                exit_type = 'TRAIL'
                break
            if cur_prem <= sl_prem_level:
                exit_prem = cur_prem
                exit_type = 'SL'
                break
        else:
            spot = sig.remaining_closes[-1]
            t_now = max((dte - 0.5) / 365, 0.001)
            exit_prem = bs_price(spot, sig.strike, t_now, 0.065, iv, opt_type)
            exit_type = 'TIME'

        exit_prem *= 0.995
        gross = (exit_prem - entry_prem) * qty
        costs = round_trip_cost(entry_prem, max(exit_prem, 0.05), qty)
        net = gross - costs
        trades.append({
            'net_pnl': net, 'exit_type': exit_type, 'date': sig.trade_date,
            'direction': sig.direction, 'is_expiry': is_exp,
            'entry_prem': entry_prem, 'exit_prem': exit_prem, 'qty': qty,
        })
        daily_pnl += net
        daily_count += 1

    return trades


def calc_metrics(trades):
    if not trades:
        return {'n': 0, 'wr': 0, 'pf': 0, 'net': 0, 'dd': 0, 'avg_w': 0, 'avg_l': 0, 'exp': 0}
    pnls = np.array([t['net_pnl'] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    n = len(pnls)
    wr = len(wins) / n * 100
    gp = float(wins.sum()) if len(wins) else 0
    gl = abs(float(losses.sum())) if len(losses) else 1e-9
    pf = gp / gl
    avg_w = float(wins.mean()) if len(wins) else 0
    avg_l = float(losses.mean()) if len(losses) else 0
    exp = (len(wins)/n * avg_w + len(losses)/n * avg_l) if n else 0
    eq = 1_000_000 + np.cumsum(pnls)
    pk = np.maximum.accumulate(eq)
    dd = float(((eq - pk) / pk * 100).min())
    dates = [t['date'] for t in trades]
    years = max((max(dates) - min(dates)).days / 365.25, 0.5)
    ann = float(pnls.sum()) / 1_000_000 / years * 100
    return {'n': n, 'wr': round(wr, 1), 'pf': round(pf, 3), 'net': round(float(pnls.sum()), 0),
            'dd': round(dd, 1), 'avg_w': round(avg_w, 0), 'avg_l': round(avg_l, 0),
            'exp': round(exp, 0), 'ann': round(ann, 1)}


def print_metrics(label, m):
    print(f'  {label}:')
    print(f'    N={m["n"]} | WR={m["wr"]}% | PF={m["pf"]:.3f} | Net=Rs{m["net"]:+,.0f} | Ann={m["ann"]}%')
    print(f'    AvgWin=Rs{m["avg_w"]:+,.0f} | AvgLoss=Rs{m["avg_l"]:+,.0f} | Exp=Rs{m["exp"]:+,.0f} | DD={m["dd"]}%')


def main():
    data_dir = ROOT / 'data'
    print('Loading data...')
    df_1m, df_vix = _load_data(str(data_dir / 'zerodha_nifty_1m.csv'),
                                str(data_dir / 'zerodha_vix_day.csv'))
    t0 = time.time()

    print('Generating signals (full period 2018-2026)...')
    sigs = generate_signals(df_1m, df_vix, date(2018,1,1), date(2026,5,22),
                            min_range_pct=0.0025, min_atr_5m=18.0,
                            sweep_closeback_bars=5, swing_pivot_n=1)
    print(f'  {len(sigs)} signals ({time.time()-t0:.0f}s)')

    call_sigs = sum(1 for s in sigs if s.direction == 'CALL')
    put_sigs = sum(1 for s in sigs if s.direction == 'PUT')
    exp_sigs = sum(1 for s in sigs if is_expiry_day(s.trade_date))
    print(f'  CALL: {call_sigs} | PUT: {put_sigs} | Expiry day: {exp_sigs}')

    sep = '=' * 80

    # ── 1. PUT with best params: expiry vs non-expiry ──────────────────
    print(f'\n{sep}')
    print('  1. PUT ANALYSIS: EXPIRY vs NON-EXPIRY DAYS')
    print(f'{sep}')

    put_trades = replay_with_close_at_315(sigs, 0.30, 0.75, 0.25, 0.20, direction_filter='PUT')
    put_expiry = [t for t in put_trades if t['is_expiry']]
    put_non_exp = [t for t in put_trades if not t['is_expiry']]

    print_metrics('PUT ALL DAYS', calc_metrics(put_trades))
    print_metrics('PUT EXPIRY DAYS ONLY', calc_metrics(put_expiry))
    print_metrics('PUT NON-EXPIRY DAYS', calc_metrics(put_non_exp))

    # Exit type breakdown
    for label, subset in [('Expiry', put_expiry), ('Non-Expiry', put_non_exp)]:
        exits = defaultdict(int)
        for t in subset:
            exits[t['exit_type']] += 1
        print(f'    {label} exits: {dict(exits)}')

    # ── 2. CALL with best params: expiry vs non-expiry ─────────────────
    print(f'\n{sep}')
    print('  2. CALL ANALYSIS: EXPIRY vs NON-EXPIRY DAYS')
    print(f'{sep}')

    for sl, tp, ta, tr in [(0.30, 0.50, 0.25, 0.15), (0.25, 0.40, 0.15, 0.10),
                            (0.30, 0.30, 0.25, 0.10), (0.20, 0.30, 0.25, 0.20)]:
        call_trades = replay_with_close_at_315(sigs, sl, tp, ta, tr, direction_filter='CALL')
        call_expiry = [t for t in call_trades if t['is_expiry']]
        call_non_exp = [t for t in call_trades if not t['is_expiry']]

        label = f'SL{sl*100:.0f}/TP{tp*100:.0f}/T{ta*100:.0f}/{tr*100:.0f}'
        print(f'\n  CALL {label}:')
        m_all = calc_metrics(call_trades)
        m_exp = calc_metrics(call_expiry)
        m_non = calc_metrics(call_non_exp)
        print(f'    ALL:     N={m_all["n"]:<5} WR={m_all["wr"]:<6}% PF={m_all["pf"]:<7.3f} Net=Rs{m_all["net"]:>+12,.0f} Ann={m_all["ann"]}%')
        print(f'    EXPIRY:  N={m_exp["n"]:<5} WR={m_exp["wr"]:<6}% PF={m_exp["pf"]:<7.3f} Net=Rs{m_exp["net"]:>+12,.0f}')
        print(f'    NON-EXP: N={m_non["n"]:<5} WR={m_non["wr"]:<6}% PF={m_non["pf"]:<7.3f} Net=Rs{m_non["net"]:>+12,.0f}')

    # ── 3. SKIP EXPIRY DAYS: combined both directions ──────────────────
    print(f'\n{sep}')
    print('  3. COMBINED (BOTH DIRECTIONS) - SKIP EXPIRY DAYS')
    print(f'{sep}')

    configs = [
        ('SL30/TP75/T25/20', 0.30, 0.75, 0.25, 0.20),
        ('SL30/TP50/T25/15', 0.30, 0.50, 0.25, 0.15),
        ('SL25/TP50/T20/15', 0.25, 0.50, 0.20, 0.15),
        ('SL30/TP30/T25/10', 0.30, 0.30, 0.25, 0.10),
        ('SL20/TP30/T25/20', 0.20, 0.30, 0.25, 0.20),
    ]

    for label, sl, tp, ta, tr in configs:
        # All days
        all_trades = replay_with_close_at_315(sigs, sl, tp, ta, tr)
        m_all = calc_metrics(all_trades)
        # Skip expiry
        noexp_trades = replay_with_close_at_315(sigs, sl, tp, ta, tr, skip_expiry=True)
        m_noexp = calc_metrics(noexp_trades)
        # Split by direction (skip expiry)
        call_ne = [t for t in noexp_trades if t['direction'] == 'CALL']
        put_ne = [t for t in noexp_trades if t['direction'] == 'PUT']
        mc = calc_metrics(call_ne)
        mp = calc_metrics(put_ne)

        print(f'\n  {label}:')
        print(f'    ALL DAYS:  N={m_all["n"]:<5} PF={m_all["pf"]:<7.3f} Net=Rs{m_all["net"]:>+12,.0f} Ann={m_all["ann"]}%')
        print(f'    NO EXPIRY: N={m_noexp["n"]:<5} PF={m_noexp["pf"]:<7.3f} Net=Rs{m_noexp["net"]:>+12,.0f} Ann={m_noexp["ann"]}%')
        print(f'      CALL:    N={mc["n"]:<5} PF={mc["pf"]:<7.3f} WR={mc["wr"]}% Net=Rs{mc["net"]:>+12,.0f}')
        print(f'      PUT:     N={mp["n"]:<5} PF={mp["pf"]:<7.3f} WR={mp["wr"]}% Net=Rs{mp["net"]:>+12,.0f}')

    # ── 4. ASYMMETRIC: different exits per direction, skip expiry ──────
    print(f'\n{sep}')
    print('  4. ASYMMETRIC EXITS + SKIP EXPIRY')
    print(f'{sep}')

    call_cfgs = [
        (0.25, 0.30, 0.15, 0.10),
        (0.25, 0.40, 0.15, 0.10),
        (0.30, 0.30, 0.25, 0.10),
        (0.30, 0.50, 0.20, 0.15),
        (0.20, 0.30, 0.25, 0.20),
    ]
    put_cfgs = [
        (0.30, 0.75, 0.25, 0.20),
        (0.25, 0.75, 0.25, 0.20),
        (0.30, 1.00, 0.25, 0.20),
    ]

    best_combo = None
    best_pf = 0

    for cc in call_cfgs:
        for pc in put_cfgs:
            call_t = replay_with_close_at_315(sigs, *cc, direction_filter='CALL', skip_expiry=True)
            put_t = replay_with_close_at_315(sigs, *pc, direction_filter='PUT', skip_expiry=True)
            all_t = call_t + put_t
            all_t.sort(key=lambda x: x['date'])
            m = calc_metrics(all_t)
            mc = calc_metrics(call_t)
            mp = calc_metrics(put_t)

            both_pos = mc['net'] > 0 and mp['net'] > 0
            if m['pf'] > best_pf:
                best_pf = m['pf']
                best_combo = (cc, pc, m, mc, mp)

            if both_pos or m['pf'] > 1.1:
                print(f'  C:SL{cc[0]*100:.0f}/TP{cc[1]*100:.0f}/T{cc[2]*100:.0f}/{cc[3]*100:.0f} '
                      f'P:SL{pc[0]*100:.0f}/TP{pc[1]*100:.0f}/T{pc[2]*100:.0f}/{pc[3]*100:.0f}')
                flag = ' ** BOTH +ve **' if both_pos else ''
                print(f'    Combined: N={m["n"]:<5} PF={m["pf"]:.3f} WR={m["wr"]}% Net=Rs{m["net"]:+,.0f} Ann={m["ann"]}% DD={m["dd"]}%{flag}')
                print(f'    CALL:     N={mc["n"]:<5} PF={mc["pf"]:.3f} WR={mc["wr"]}% Net=Rs{mc["net"]:+,.0f}')
                print(f'    PUT:      N={mp["n"]:<5} PF={mp["pf"]:.3f} WR={mp["wr"]}% Net=Rs{mp["net"]:+,.0f}')

    if best_combo:
        cc, pc, m, mc, mp = best_combo
        print(f'\n  BEST OVERALL:')
        print(f'    CALL: SL={cc[0]*100:.0f}% TP={cc[1]*100:.0f}% Trail@{cc[2]*100:.0f}%/{cc[3]*100:.0f}%')
        print(f'    PUT:  SL={pc[0]*100:.0f}% TP={pc[1]*100:.0f}% Trail@{pc[2]*100:.0f}%/{pc[3]*100:.0f}%')
        print(f'    Skip expiry days: YES')
        print(f'    N={m["n"]} PF={m["pf"]:.3f} WR={m["wr"]}% Net=Rs{m["net"]:+,.0f} Ann={m["ann"]}% DD={m["dd"]}%')
        print(f'    CALL: N={mc["n"]} PF={mc["pf"]:.3f} Net=Rs{mc["net"]:+,.0f}')
        print(f'    PUT:  N={mp["n"]} PF={mp["pf"]:.3f} Net=Rs{mp["net"]:+,.0f}')

    # ── 5. Year-by-year for best config ────────────────────────────────
    if best_combo:
        cc, pc, _, _, _ = best_combo
        print(f'\n{sep}')
        print('  5. YEAR-BY-YEAR (best config, skip expiry)')
        print(f'{sep}')

        call_t = replay_with_close_at_315(sigs, *cc, direction_filter='CALL', skip_expiry=True)
        put_t = replay_with_close_at_315(sigs, *pc, direction_filter='PUT', skip_expiry=True)
        all_t = sorted(call_t + put_t, key=lambda x: x['date'])

        years = sorted(set(t['date'].year for t in all_t))
        cum = 0
        print(f'  {"Year":<6} {"N":<5} {"WR%":<6} {"PF":<7} {"Net PnL":<13} {"Cum PnL":<13} {"Ret%":<7} {"DD%":<7} {"C_PnL":<12} {"P_PnL":<12}')
        print(f'  {"-"*95}')
        for y in years:
            yt = [t for t in all_t if t['date'].year == y]
            yp = np.array([t['net_pnl'] for t in yt])
            wins = len(yp[yp > 0])
            wr = wins / len(yp) * 100
            gp = float(yp[yp > 0].sum()) if wins else 0
            gl = abs(float(yp[yp <= 0].sum())) if len(yp) - wins else 1e-9
            pf = gp / gl
            net = float(yp.sum())
            cum += net
            ret = net / 1_000_000 * 100
            yeq = 1_000_000 + np.cumsum(yp)
            ypk = np.maximum.accumulate(yeq)
            ydd = float(((yeq - ypk) / ypk * 100).min())
            cp = sum(t['net_pnl'] for t in yt if t['direction'] == 'CALL')
            pp = sum(t['net_pnl'] for t in yt if t['direction'] == 'PUT')
            print(f'  {y:<6} {len(yt):<5} {wr:<6.1f} {pf:<7.3f} Rs{net:>+11,.0f} Rs{cum:>+11,.0f} {ret:>+6.1f}% {ydd:>+6.1f}% Rs{cp:>+10,.0f} Rs{pp:>+10,.0f}')

    print(f'\nTotal time: {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
