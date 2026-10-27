"""
NR7 Trail SL — Full Per-Method Breakdown
=========================================
Reads the saved nr7_trail_comparison.csv and prints a detailed
breakdown for every trailing SL method.

Run:
    python run_nr7_trail_analysis.py
"""

import sys
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd

CSV = Path(__file__).resolve().parent / "reports" / "nr7_positional" / "nr7_trail_comparison.csv"

METHOD_LABELS = {
    'ema50':    '50 EMA  (baseline)',
    'sma20':    '20 SMA  (faster)',
    'sma50':    '50 SMA  (WINNER)',
    'atr1.5':   'Chandelier 1.5xATR',
    'atr2.0':   'Chandelier 2.0xATR',
    'atr3.0':   'Chandelier 3.0xATR',
    'be_ema50': 'Breakeven + EMA50',
    'psar':     'Parabolic SAR',
}

METHOD_ORDER = list(METHOD_LABELS.keys())

RISK_RS = 2000.0


def _hold_days(row):
    try:
        return (pd.Timestamp(row['exit_date']) - pd.Timestamp(row['entry_date'])).days
    except Exception:
        return np.nan


def _drawdown_series(trades_sorted):
    """Running max drawdown % from a time-sorted trade list."""
    capital = 100_000.0
    peak = capital
    max_dd = 0.0
    worst_start = worst_end = None
    cur_dd_start = None

    for t in trades_sorted:
        capital += t['net_rs']
        if capital > peak:
            peak = capital
            cur_dd_start = None
        else:
            if cur_dd_start is None:
                cur_dd_start = t['entry_date']
            dd = (peak - capital) / peak * 100
            if dd > max_dd:
                max_dd = dd
                worst_start = cur_dd_start
                worst_end = t['exit_date']

    return max_dd, worst_start, worst_end


def _consec_losses(trades_sorted):
    """Return max consecutive losing streak."""
    max_streak = cur = 0
    for t in trades_sorted:
        if not t['win']:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0
    return max_streak


def _r_buckets(trades):
    """Bucket trades by R-multiple."""
    buckets = {
        '>3R':      [t for t in trades if t['r_multiple'] >  3.0],
        '2-3R':     [t for t in trades if 2.0 < t['r_multiple'] <= 3.0],
        '1-2R':     [t for t in trades if 1.0 < t['r_multiple'] <= 2.0],
        '0-1R':     [t for t in trades if 0.0 < t['r_multiple'] <= 1.0],
        '0 to -1R': [t for t in trades if -1.0 <= t['r_multiple'] <= 0.0],
        '<-1R':     [t for t in trades if t['r_multiple'] < -1.0],
    }
    return buckets


def _sep(char='=', width=76):
    print(char * width)


def analyze(df: pd.DataFrame):
    df['hold_days'] = df.apply(_hold_days, axis=1)
    df['win'] = df['win'].astype(bool)

    for mode in METHOD_ORDER:
        sub = df[df['trail_mode'] == mode]
        if sub.empty:
            continue

        trades = sub.to_dict('records')
        trades_sorted = sorted(trades, key=lambda x: x['entry_date'])

        wins   = [t for t in trades if t['win']]
        losses = [t for t in trades if not t['win']]

        n  = len(trades)
        nw = len(wins)
        nl = len(losses)
        wr = nw / n * 100

        gw = sum(t['net_rs'] for t in wins)
        gl = sum(abs(t['net_rs']) for t in losses)
        net = gw - gl
        pf  = gw / gl if gl > 0 else float('inf')

        avg_win  = gw / nw if nw else 0
        avg_loss = gl / nl if nl else 0
        wl_ratio = avg_win / avg_loss if avg_loss else float('inf')

        avg_r    = sum(t['r_multiple'] for t in trades) / n
        avg_days = sub['hold_days'].mean()
        win_days = sub[sub['win']]['hold_days'].mean()
        los_days = sub[~sub['win']]['hold_days'].mean()

        best_trade  = max(trades, key=lambda x: x['net_rs'])
        worst_trade = min(trades, key=lambda x: x['net_rs'])

        max_dd, dd_start, dd_end = _drawdown_series(trades_sorted)
        max_streak = _consec_losses(trades_sorted)

        buckets = _r_buckets(trades)

        # Per-year
        yd = defaultdict(lambda: {'n': 0, 'w': 0, 'net': 0.0, 'gw': 0.0, 'gl': 0.0})
        for t in trades:
            yr = pd.Timestamp(t['entry_date']).year
            yd[yr]['n']   += 1
            yd[yr]['w']   += int(t['win'])
            yd[yr]['net'] += t['net_rs']
            if t['win']:
                yd[yr]['gw'] += t['net_rs']
            else:
                yd[yr]['gl'] += abs(t['net_rs'])

        # Top / worst stocks by net P&L
        sym_net = defaultdict(float)
        for t in trades:
            sym_net[t.get('symbol', '?')] += t['net_rs']
        top5  = sorted(sym_net.items(), key=lambda x: x[1], reverse=True)[:5]
        bot5  = sorted(sym_net.items(), key=lambda x: x[1])[:5]

        # ── Print ─────────────────────────────────────────────────────────────
        _sep('=')
        print(f"  {mode.upper()}  —  {METHOD_LABELS[mode]}")
        _sep('=')

        # Core metrics
        print(f"\n  CORE METRICS")
        print(f"  {'Trades':<22}: {n}")
        print(f"  {'Wins / Losses':<22}: {nw} / {nl}")
        print(f"  {'Win Rate':<22}: {wr:.1f}%")
        print(f"  {'Profit Factor':<22}: {pf:.2f}")
        print(f"  {'Avg R-Multiple':<22}: {avg_r:+.2f}R")
        print(f"  {'Net P&L':<22}: Rs{net:+,.0f}")
        print(f"  {'Gross Wins':<22}: Rs{gw:+,.0f}")
        print(f"  {'Gross Losses':<22}: Rs{-gl:,.0f}")

        print(f"\n  WIN / LOSS SIZE")
        print(f"  {'Avg Win':<22}: Rs{avg_win:,.0f}")
        print(f"  {'Avg Loss':<22}: Rs{avg_loss:,.0f}")
        print(f"  {'Win/Loss Ratio':<22}: {wl_ratio:.2f}x")
        print(f"  {'Largest Win':<22}: Rs{best_trade['net_rs']:+,.0f}  "
              f"({best_trade.get('symbol','?')} on {str(best_trade['entry_date'])[:10]})")
        print(f"  {'Largest Loss':<22}: Rs{worst_trade['net_rs']:+,.0f}  "
              f"({worst_trade.get('symbol','?')} on {str(worst_trade['entry_date'])[:10]})")

        print(f"\n  HOLDING PERIOD")
        print(f"  {'Avg (all trades)':<22}: {avg_days:.1f} days")
        print(f"  {'Avg (winners)':<22}: {win_days:.1f} days")
        print(f"  {'Avg (losers)':<22}: {los_days:.1f} days")

        print(f"\n  RISK METRICS")
        print(f"  {'Max Drawdown':<22}: {max_dd:.1f}%")
        if dd_start:
            print(f"  {'DD Period':<22}: {str(dd_start)[:10]} -> {str(dd_end)[:10]}")
        print(f"  {'Max Consec Losses':<22}: {max_streak}")

        print(f"\n  R-MULTIPLE DISTRIBUTION")
        total_n = n
        for label, bucket in buckets.items():
            pct = len(bucket) / total_n * 100
            bar = '#' * int(pct / 2)
            print(f"  {label:<10} : {len(bucket):>4} trades ({pct:>5.1f}%)  {bar}")

        print(f"\n  YEAR-BY-YEAR")
        print(f"  {'Year':<6}  {'Trades':>6}  {'WR%':>5}  {'PF':>5}  {'Net Rs':>12}")
        print("  " + "-" * 40)
        for yr in sorted(yd):
            s  = yd[yr]
            pf_yr = s['gw'] / s['gl'] if s['gl'] > 0 else float('inf')
            wr_yr = s['w'] / s['n'] * 100 if s['n'] else 0
            print(f"  {yr:<6}  {s['n']:>6}  {wr_yr:>5.1f}  {pf_yr:>5.2f}  {s['net']:>+12,.0f}")

        print(f"\n  TOP 5 STOCKS (net P&L)")
        for sym, val in top5:
            print(f"    {sym:<16} Rs{val:>+10,.0f}")

        print(f"\n  WORST 5 STOCKS (net P&L)")
        for sym, val in bot5:
            print(f"    {sym:<16} Rs{val:>+10,.0f}")

        print()

    _sep('=')
    print("  QUICK COMPARISON SNAPSHOT")
    _sep('=')
    hdr = (f"  {'Method':<11}  {'Trades':>6}  {'WR%':>5}  {'PF':>5}  "
           f"{'AvgR':>5}  {'W:L':>5}  {'AvgDays':>7}  {'MaxDD%':>6}  {'Net Rs':>12}")
    print(hdr)
    print("  " + "-" * 76)
    for mode in METHOD_ORDER:
        sub = df[df['trail_mode'] == mode]
        if sub.empty:
            continue
        trades = sub.to_dict('records')
        wins   = [t for t in trades if t['win']]
        losses = [t for t in trades if not t['win']]
        n  = len(trades)
        nw = len(wins)
        nl = len(losses)
        wr = nw / n * 100
        gw = sum(t['net_rs'] for t in wins)
        gl = sum(abs(t['net_rs']) for t in losses)
        net = gw - gl
        pf  = gw / gl if gl > 0 else float('inf')
        avg_r = sum(t['r_multiple'] for t in trades) / n
        avg_win  = gw / nw if nw else 0
        avg_loss = gl / nl if nl else 0
        wl = avg_win / avg_loss if avg_loss else float('inf')
        avg_days = sub['hold_days'].mean()
        max_dd, _, _ = _drawdown_series(sorted(trades, key=lambda x: x['entry_date']))
        mark = ' <-- BEST' if mode == 'sma50' else ''
        print(f"  {mode:<11}  {n:>6}  {wr:>5.1f}  {pf:>5.2f}  "
              f"{avg_r:>+5.2f}  {wl:>5.2f}  {avg_days:>7.1f}  "
              f"{max_dd:>6.1f}  {net:>+12,.0f}{mark}")
    _sep('=')


def main():
    if not CSV.exists():
        print(f"ERROR: {CSV} not found.")
        print("Run: python run_nr7_trail_comparison.py --cached")
        sys.exit(1)

    df = pd.read_csv(CSV)
    print(f"Loaded {len(df):,} trades across {df['trail_mode'].nunique()} methods\n")
    analyze(df)


if __name__ == "__main__":
    main()
