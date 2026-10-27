"""Fast V2 optimizer using signal cache + instant exit replay."""
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

from orb_sweep.optimize import _load_data, generate_signals, CachedSignal
from backtest.option_pricer import bs_price, next_expiry
from costs.zerodha import round_trip_cost

def replay_asymmetric(signals, call_exit, put_exit):
    csl, ctp, cta, ctr = call_exit
    psl, ptp, pta, ptr = put_exit
    capital = 1_000_000
    lot_size = 50
    trades = []
    current_date = None
    daily_count = 0
    daily_pnl = 0.0

    for sig in signals:
        if sig.trade_date != current_date:
            current_date = sig.trade_date
            daily_count = 0
            daily_pnl = 0.0
        if daily_count >= 2 or daily_pnl <= -10000:
            continue

        is_call = sig.direction == 'CALL'
        sl_p = csl if is_call else psl
        tp_p = ctp if is_call else ptp
        ta_p = cta if is_call else pta
        tr_p = ctr if is_call else ptr

        expiry = next_expiry(sig.trade_date)
        dte = max((expiry - sig.trade_date).days, 1)
        iv = max(sig.vix / 100, 0.05)
        t_entry = max(dte / 365, 0.001)
        opt_type = 'call' if is_call else 'put'
        entry_prem = bs_price(sig.entry_spot, sig.strike, t_entry, 0.065, iv, opt_type) * 1.005

        lots = max(1, int(10000 / (lot_size * entry_prem * sl_p)))
        qty = lots * lot_size

        sl_prem = entry_prem * (1 - sl_p)
        tp_prem = entry_prem * (1 + tp_p)
        peak_prem = entry_prem
        exit_prem = entry_prem
        exit_type = 'TIME'
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

            if cur_prem >= tp_prem:
                exit_prem = cur_prem
                exit_type = 'TP'
                break
            if peak_prem >= entry_prem * (1 + ta_p) and cur_prem <= peak_prem * (1 - tr_p):
                exit_prem = cur_prem
                exit_type = 'TRAIL'
                break
            if cur_prem <= sl_prem:
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
        trades.append({'net_pnl': net, 'exit_type': exit_type, 'date': sig.trade_date, 'direction': sig.direction})
        daily_pnl += net
        daily_count += 1

    if not trades:
        return {'n': 0, 'pf': 0, 'wr': 0, 'net': 0, 'dd': 0, 'ann': 0,
                'call_pnl': 0, 'put_pnl': 0, 'call_n': 0, 'put_n': 0,
                'call_wr': 0, 'put_wr': 0}

    pnls = np.array([t['net_pnl'] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    n = len(pnls)
    wr = len(wins) / n * 100
    gp = float(wins.sum()) if len(wins) else 0
    gl = abs(float(losses.sum())) if len(losses) else 1e-9
    pf = gp / gl
    eq = 1_000_000 + np.cumsum(pnls)
    pk = np.maximum.accumulate(eq)
    dd = float(((eq - pk) / pk * 100).min())

    call_t = [t for t in trades if t['direction'] == 'CALL']
    put_t = [t for t in trades if t['direction'] == 'PUT']
    call_pnl = sum(t['net_pnl'] for t in call_t)
    put_pnl = sum(t['net_pnl'] for t in put_t)
    call_wr = sum(1 for t in call_t if t['net_pnl'] > 0) / len(call_t) * 100 if call_t else 0
    put_wr = sum(1 for t in put_t if t['net_pnl'] > 0) / len(put_t) * 100 if put_t else 0

    dates_span = (max(t['date'] for t in trades) - min(t['date'] for t in trades)).days
    years = max(dates_span / 365.25, 0.5)
    ann = float(pnls.sum()) / 1_000_000 / years * 100

    return {'n': n, 'pf': round(pf, 3), 'wr': round(wr, 1), 'net': round(float(pnls.sum()), 0),
            'dd': round(dd, 1), 'ann': round(ann, 1),
            'call_pnl': round(call_pnl, 0), 'put_pnl': round(put_pnl, 0),
            'call_n': len(call_t), 'put_n': len(put_t),
            'call_wr': round(call_wr, 1), 'put_wr': round(put_wr, 1)}


def main():
    data_dir = ROOT / 'data'
    nifty_path = str(data_dir / 'zerodha_nifty_1m.csv')
    vix_path = str(data_dir / 'zerodha_vix_day.csv')
    print('Loading data...')
    df_1m, df_vix = _load_data(nifty_path, vix_path)
    t0 = time.time()

    print('Generating IS signals (2020-2022)...')
    is_sigs = generate_signals(df_1m, df_vix, date(2020,1,1), date(2022,12,31),
                               min_range_pct=0.0025, min_atr_5m=18.0,
                               sweep_closeback_bars=5, swing_pivot_n=1)
    print(f'  IS: {len(is_sigs)} signals ({time.time()-t0:.0f}s)')

    print('Generating OOS signals (2018-2019 + 2023-2026)...')
    oos1 = generate_signals(df_1m, df_vix, date(2018,1,1), date(2019,12,31),
                            min_range_pct=0.0025, min_atr_5m=18.0,
                            sweep_closeback_bars=5, swing_pivot_n=1)
    oos2 = generate_signals(df_1m, df_vix, date(2023,1,1), date(2026,5,22),
                            min_range_pct=0.0025, min_atr_5m=18.0,
                            sweep_closeback_bars=5, swing_pivot_n=1)
    oos_sigs = oos1 + oos2
    print(f'  OOS: {len(oos_sigs)} signals ({time.time()-t0:.0f}s)')

    CALL_EXITS = [
        (0.25, 0.40, 0.15, 0.10), (0.25, 0.50, 0.20, 0.15),
        (0.30, 0.40, 0.15, 0.10), (0.30, 0.50, 0.20, 0.15),
        (0.30, 0.50, 0.25, 0.15), (0.30, 0.75, 0.25, 0.20),
        (0.35, 0.50, 0.20, 0.15), (0.35, 0.75, 0.25, 0.20),
        (0.40, 0.50, 0.25, 0.15), (0.40, 0.75, 0.25, 0.20),
    ]
    PUT_EXITS = [
        (0.25, 0.50, 0.20, 0.15), (0.25, 0.75, 0.25, 0.20),
        (0.30, 0.50, 0.25, 0.15), (0.30, 0.75, 0.25, 0.20),
        (0.30, 1.00, 0.25, 0.20), (0.35, 0.75, 0.25, 0.20),
        (0.35, 1.00, 0.30, 0.20), (0.40, 0.75, 0.30, 0.20),
    ]

    combos = list(product(CALL_EXITS, PUT_EXITS))
    print(f'\nSweeping {len(combos)} exit combos on IS...')

    is_results = []
    for ce, pe in combos:
        m = replay_asymmetric(is_sigs, ce, pe)
        score = 0
        if m['n'] >= 20 and m['pf'] > 1.0 and m['call_pnl'] > 0 and m['put_pnl'] > 0:
            score = m['pf'] * (m['wr'] / 100) * min(m['ann'] / 40, 2.0)
        is_results.append({'ce': ce, 'pe': pe, 'm': m, 'score': score})

    is_results.sort(key=lambda x: x['score'], reverse=True)

    # Fallback: if no config has both directions profitable, use overall PF
    top_both = [r for r in is_results if r['score'] > 0][:15]
    if not top_both:
        is_results.sort(key=lambda x: x['m']['pf'] * x['m']['n'] if x['m']['n'] >= 15 else 0, reverse=True)
        top_both = is_results[:15]

    print(f'\n{"="*110}')
    print('  IS RESULTS (sorted by score)')
    print(f'{"="*110}')
    top = top_both
    print(f'  {"#":<4} {"N":<5} {"WR":<6} {"PF":<7} {"Ann%":<7} {"DD%":<7} '
          f'{"C_N":<5} {"C_WR":<6} {"C_PnL":<11} {"P_N":<5} {"P_WR":<6} {"P_PnL":<11}')
    print(f'  {"-"*90}')
    for i, r in enumerate(top, 1):
        m = r['m']
        print(f'  {i:<4} {m["n"]:<5} {m["wr"]:<6.1f} {m["pf"]:<7.3f} {m["ann"]:<7.1f} {m["dd"]:<7.1f} '
              f'{m["call_n"]:<5} {m["call_wr"]:<6.1f} Rs{m["call_pnl"]:>+9,.0f} '
              f'{m["put_n"]:<5} {m["put_wr"]:<6.1f} Rs{m["put_pnl"]:>+9,.0f}')

    print(f'\n{"="*110}')
    print('  OOS VALIDATION (top 10)')
    print(f'{"="*110}')
    validate = top[:10]
    oos_results = []
    for i, r in enumerate(validate, 1):
        oos_m = replay_asymmetric(oos_sigs, r['ce'], r['pe'])
        decay = oos_m['pf'] / r['m']['pf'] if r['m']['pf'] > 0 else 0
        oos_results.append({'rank': i, 'ce': r['ce'], 'pe': r['pe'],
                           'is': r['m'], 'oos': oos_m, 'decay': decay})

    print(f'  {"#":<4} {"IS_PF":<7} {"OOS_PF":<7} {"Decay":<7} {"OOS_N":<6} {"OOS_WR":<7} {"OOS_DD":<7} '
          f'{"OOS_Ann%":<9} {"C_PnL":<11} {"P_PnL":<11}')
    print(f'  {"-"*90}')
    for r in oos_results:
        o = r['oos']
        flag = ' <--' if o['pf'] > 1.0 and o['call_pnl'] > 0 else ''
        print(f'  {r["rank"]:<4} {r["is"]["pf"]:<7.3f} {o["pf"]:<7.3f} {r["decay"]:<7.2f} '
              f'{o["n"]:<6} {o["wr"]:<7.1f} {o["dd"]:<7.1f} {o["ann"]:<9.1f} '
              f'Rs{o["call_pnl"]:>+9,.0f} Rs{o["put_pnl"]:>+9,.0f}{flag}')

    # Full period
    best = None
    for r in oos_results:
        if r['oos']['pf'] > 1.0 and r['oos']['call_pnl'] > 0 and r['oos']['put_pnl'] > 0:
            if best is None or r['oos']['pf'] > best['oos']['pf']:
                best = r
    if best is None:
        for r in oos_results:
            if r['oos']['pf'] > 1.0:
                if best is None or r['oos']['pf'] > best['oos']['pf']:
                    best = r
    if best is None:
        best = max(oos_results, key=lambda r: r['oos']['pf'])

    print(f'\n{"="*110}')
    print('  FULL PERIOD (2018-2026)')
    print(f'{"="*110}')

    print('Generating full signals...')
    full_sigs = generate_signals(df_1m, df_vix, date(2018,1,1), date(2026,5,22),
                                 min_range_pct=0.0025, min_atr_5m=18.0,
                                 sweep_closeback_bars=5, swing_pivot_n=1)
    full_m = replay_asymmetric(full_sigs, best['ce'], best['pe'])
    ce, pe = best['ce'], best['pe']

    print(f'  CALL exit: SL={ce[0]*100:.0f}% TP={ce[1]*100:.0f}% Trail@{ce[2]*100:.0f}%/{ce[3]*100:.0f}%')
    print(f'  PUT exit:  SL={pe[0]*100:.0f}% TP={pe[1]*100:.0f}% Trail@{pe[2]*100:.0f}%/{pe[3]*100:.0f}%')
    print(f'  N={full_m["n"]} WR={full_m["wr"]}% PF={full_m["pf"]} Ann={full_m["ann"]}% DD={full_m["dd"]}%')
    print(f'  Net=Rs{full_m["net"]:+,.0f}')
    print(f'  CALL: {full_m["call_n"]} trades, WR={full_m["call_wr"]}%, PnL=Rs{full_m["call_pnl"]:+,.0f}')
    print(f'  PUT:  {full_m["put_n"]} trades, WR={full_m["put_wr"]}%, PnL=Rs{full_m["put_pnl"]:+,.0f}')

    print(f'\n  CONFIG UPDATE:')
    print(f'    call_sl_pct={ce[0]}, call_tp_pct={ce[1]}, call_trail_activate={ce[2]}, call_trail_pct={ce[3]}')
    print(f'    put_sl_pct={pe[0]}, put_tp_pct={pe[1]}, put_trail_activate={pe[2]}, put_trail_pct={pe[3]}')

    print(f'\nTotal time: {time.time()-t0:.0f}s')

if __name__ == '__main__':
    main()
