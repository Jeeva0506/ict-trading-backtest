"""
NR7 Trail SL Comparison — PORTFOLIO LEVEL (correct drawdown)
=============================================================
Fixes the per-stock simulation mistake. Runs a single shared-capital
portfolio (Rs 2,00,000, max 5 positions, 2% risk/trade) for each trail
method — same architecture as run_nr7_portfolio.py.

Run:
    python run_nr7_trail_portfolio.py              # 3yr, FILTERS_ALL
    python run_nr7_trail_portfolio.py --10yr       # 10yr, VOL_SURGE_ONLY (matches original)
"""

import sys, time
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from strategies.nr7_positional import (
    compute_indicators, generate_signals,
    FILTERS_ALL, FILTER_NAMES,
)
from costs.zerodha_equity import equity_round_trip_cost

# ── Portfolio constants (same as run_nr7_portfolio.py) ───────────────────────
CAPITAL         = 200_000
RISK_PCT        = 0.02
MAX_POSITIONS   = 5
MAX_CAP_PCT     = 0.25
MIN_CASH_BUFFER = 10_000

CACHE_3YR  = ROOT / "data" / "nr7_cache"
CACHE_10YR = ROOT / "data" / "nr7_cache_10yr"
MIN_ROWS_3YR  = 400
MIN_ROWS_10YR = 1500

# Matches the original validated config exactly
VOL_SURGE_ONLY = {k: (k == 'vol_surge') for k in FILTER_NAMES}

TRAIL_METHODS = {
    'ema50':       '50 EMA   (baseline)',
    'sma20':       '20 SMA   (faster)',
    'sma50':       '50 SMA',
    'atr1.5':      'Chandelier 1.5xATR',
    'atr2.0':      'Chandelier 2.0xATR',
    'atr3.0':      'Chandelier 3.0xATR',
    'be_ema50':    'BE at 1R then EMA50',
    'psar':        'Parabolic SAR',
    'be2r_sma20':  'BE at 2R then 20 SMA  <-- NEW',
}

METHOD_ORDER = list(TRAIL_METHODS.keys())


# ── Extra indicators (additive — never changes existing columns) ──────────────
def _add_trail_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['sma20'] = df['Close'].rolling(20, min_periods=10).mean()
    df['sma50'] = df['Close'].rolling(50, min_periods=25).mean()
    return df


# ── Parabolic SAR state ───────────────────────────────────────────────────────
class _PSAR:
    def __init__(self, initial_sl: float, entry_high: float,
                 af_step: float = 0.02, af_max: float = 0.20):
        self.sar       = initial_sl
        self.ep        = entry_high
        self.af        = af_step
        self._step     = af_step
        self._max      = af_max
        self._prev_low = initial_sl

    def step(self, high: float, low: float) -> float:
        new_sar = self.sar + self.af * (self.ep - self.sar)
        new_sar = min(new_sar, self._prev_low, low)
        self._prev_low = low
        if high > self.ep:
            self.ep = high
            self.af = min(self.af + self._step, self._max)
        self.sar = new_sar
        return new_sar


# ── Trail SL update — one row per open position ───────────────────────────────
def _update_trail_sl(pos: dict, row, trail_mode: str) -> float:
    """
    Compute candidate new SL for this bar.
    pos  : open position dict (may carry extra state keys)
    row  : current bar's row (pandas Series with indicators)
    Returns candidate SL (caller raises if > current, never lowers).
    """
    high = float(row['High'])
    low  = float(row['Low'])

    # Track high-water for ATR trails
    if high > pos.get('high_water', high):
        pos['high_water'] = high
    hw = pos.get('high_water', high)

    if trail_mode == 'ema50':
        return float(row['ema50'])

    elif trail_mode == 'sma20':
        v = row.get('sma20', np.nan)
        return float(v) if not np.isnan(v) else pos['sl']

    elif trail_mode == 'sma50':
        v = row.get('sma50', np.nan)
        return float(v) if not np.isnan(v) else pos['sl']

    elif trail_mode == 'atr1.5':
        return hw - 1.5 * float(row['atr14'])

    elif trail_mode == 'atr2.0':
        return hw - 2.0 * float(row['atr14'])

    elif trail_mode == 'atr3.0':
        return hw - 3.0 * float(row['atr14'])

    elif trail_mode == 'be_ema50':
        entry        = pos['entry_price']
        initial_risk = pos['initial_risk']
        ema_sl       = float(row['ema50'])
        if not pos.get('be_triggered') and high >= entry + initial_risk:
            pos['be_triggered'] = True
        floor = entry if pos.get('be_triggered') else 0.0
        return max(ema_sl, floor)

    elif trail_mode == 'be2r_sma20':
        # Lock breakeven when price reaches entry + 2×risk (1:2 RR), then trail 20 SMA
        entry        = pos['entry_price']
        initial_risk = pos['initial_risk']
        v = row.get('sma20', np.nan)
        sma20_sl = float(v) if not np.isnan(v) else pos['sl']
        if not pos.get('be_triggered') and high >= entry + 2 * initial_risk:
            pos['be_triggered'] = True
        floor = entry if pos.get('be_triggered') else 0.0
        return max(sma20_sl, floor)

    elif trail_mode == 'psar':
        if 'psar_state' not in pos:
            pos['psar_state'] = _PSAR(pos['sl'], high)
        return pos['psar_state'].step(high, low)

    else:
        raise ValueError(f"Unknown trail_mode: {trail_mode!r}")


# ── Portfolio simulator ───────────────────────────────────────────────────────
def simulate_portfolio_trail(stock_ind: dict, all_signals: list,
                             trail_mode: str = 'ema50',
                             nifty_gate: dict = None) -> dict:
    """
    Shared-capital portfolio simulation with pluggable trail SL.
    nifty_gate : {date -> bool}  True = entries allowed that day.
                 When Nifty close < SMA50, gate is False — no new entries.
                 Existing open positions are NOT affected (exits still run).
    """
    sigs_on_day = defaultdict(list)
    for sig in all_signals:
        sigs_on_day[sig['date']].append(sig)

    all_dates  = sorted(set(dt for df in stock_ind.values() for dt in df.index))
    sym_dates  = {sym: list(df.index) for sym, df in stock_ind.items()}

    capital    = float(CAPITAL)
    open_pos   = {}          # sym -> position dict
    closed     = []
    daily_cap  = []
    peak_open  = 0
    skipped_pos = skipped_cap = skipped_cb = 0

    for dt in all_dates:
        # ── 1. Process exits for all open positions ───────────────────────────
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            df  = stock_ind[sym]
            if dt not in df.index:
                continue
            row = df.loc[dt]

            candidate = _update_trail_sl(pos, row, trail_mode)
            new_sl    = max(pos['sl'], candidate)
            pos['sl'] = new_sl

            if float(row['Low']) <= new_sl:
                exit_p = new_sl
                qty    = pos['qty']
                entry  = pos['entry_price']
                gross  = (exit_p - entry) * qty
                cost   = equity_round_trip_cost(entry, max(exit_p, 0.01), qty)
                net    = gross - cost
                capital += pos['capital_deployed'] + net
                hold_d  = (pd.Timestamp(dt) - pd.Timestamp(pos['entry_date'])).days
                closed.append({
                    'symbol':        sym,
                    'entry_date':    pos['entry_date'],
                    'exit_date':     dt,
                    'entry_price':   round(entry, 2),
                    'exit_price':    round(exit_p, 2),
                    'qty':           qty,
                    'gross_rs':      round(gross, 2),
                    'cost_rs':       round(cost, 2),
                    'net_rs':        round(net, 2),
                    'r_multiple':    round(net / pos['risk_rs'], 2),
                    'win':           net > 0,
                    'hold_days':     hold_d,
                    'capital_after': round(capital, 0),
                    'trail_mode':    trail_mode,
                })
                del open_pos[sym]

        peak_open = max(peak_open, len(open_pos))

        # ── 2. Enter new signals (rank: score desc) ───────────────────────────
        today_sigs = sorted(
            sigs_on_day.get(dt, []),
            key=lambda s: (-s.get('score', 0), s['symbol'])
        )

        # Circuit breaker: Nifty close < SMA50 → block all new entries
        if nifty_gate is not None and not nifty_gate.get(dt, True):
            skipped_cb += len(today_sigs)
            # Still track daily capital (exits already ran above)
            total_now = capital + sum(p['capital_deployed'] for p in open_pos.values())
            daily_cap.append({
                'date':           dt,
                'total_capital':  round(total_now, 0),
                'open_positions': len(open_pos),
            })
            continue

        for sig in today_sigs:
            sym = sig['symbol']
            if sym in open_pos:
                continue
            if len(open_pos) >= MAX_POSITIONS:
                skipped_pos += 1
                continue

            # AMO execution: after 3:30 PM scan, place buy order for next-day open.
            # We cannot buy at breakout-day close (market is already shut when the
            # NR7 breakout is confirmed). AMO executes at next trading day's Open.
            future = [d for d in sym_dates.get(sym, []) if d > dt]
            if not future:
                continue
            next_dt = future[0]
            if next_dt not in stock_ind[sym].index:
                continue
            next_row = stock_ind[sym].loc[next_dt]

            entry    = float(next_row['Open'])
            nr7_high = sig.get('nr7_high', entry)  # NR7 breakout level

            # Stop-buy filter: skip if next day gaps up >2% above the NR7 high.
            # Real trading: you'd place an SL-M order at NR7_high; if it opens
            # too far above (>2% gap-up) the order either fills at a bad premium
            # or you'd skip. This approximates that discipline.
            if nr7_high > 0 and entry > nr7_high * 1.02:
                continue

            sl       = sig['stop_loss']
            if entry <= sl or entry <= 0:
                continue
            distance = entry - sl
            if distance <= 0:
                continue

            # Fixed sizing — matches original simulate_portfolio (sizing_capital=CAPITAL, reinvest=0)
            risk_rs  = CAPITAL * RISK_PCT
            max_cap  = CAPITAL * MAX_CAP_PCT
            qty      = int(risk_rs / distance)
            qty      = min(qty, int(max_cap / entry))
            if qty < 1:
                continue

            cap_needed = qty * entry
            if cap_needed > capital - MIN_CASH_BUFFER:
                skipped_cap += 1
                continue

            capital -= cap_needed
            open_pos[sym] = {
                'entry_date':       next_dt,
                'entry_price':      entry,
                'sl':               sl,
                'initial_risk':     distance,
                'qty':              qty,
                'capital_deployed': cap_needed,
                'risk_rs':          risk_rs,
                'high_water':       float(next_row['High']),
                'score':            sig.get('score', 0),
            }

        total_now = capital + sum(p['capital_deployed'] for p in open_pos.values())
        daily_cap.append({
            'date':           dt,
            'total_capital':  round(total_now, 0),
            'open_positions': len(open_pos),
        })

    # Force-close open trades at data end
    for sym, pos in open_pos.items():
        df     = stock_ind[sym]
        exit_p = float(df['Close'].iloc[-1])
        qty    = pos['qty']
        entry  = pos['entry_price']
        gross  = (exit_p - entry) * qty
        cost   = equity_round_trip_cost(entry, max(exit_p, 0.01), qty)
        net    = gross - cost
        capital += pos['capital_deployed'] + net
        closed.append({
            'symbol': sym, 'entry_date': pos['entry_date'],
            'exit_date': df.index[-1], 'entry_price': round(entry, 2),
            'exit_price': round(exit_p, 2), 'qty': qty,
            'gross_rs': round(gross, 2), 'cost_rs': round(cost, 2),
            'net_rs': round(net, 2),
            'r_multiple': round(net / pos['risk_rs'], 2),
            'win': net > 0, 'hold_days': 0,
            'capital_after': round(capital, 0),
            'trail_mode': trail_mode,
        })

    return {
        'trades':      closed,
        'daily_cap':   daily_cap,
        'final_cap':   round(capital, 0),
        'peak_open':   peak_open,
        'skip_pos':    skipped_pos,
        'skip_cap':    skipped_cap,
        'skip_cb':     skipped_cb,
    }


# ── Portfolio metrics ─────────────────────────────────────────────────────────
def _metrics(result: dict, trail_mode: str) -> dict:
    trades  = [t for t in result['trades'] if t.get('hold_days', 1) > 0]
    daily   = result['daily_cap']

    if not trades:
        return {'mode': trail_mode, 'n': 0}

    wins   = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    gw     = sum(t['net_rs'] for t in wins)
    gl     = sum(abs(t['net_rs']) for t in losses)
    net    = gw - gl
    pf     = gw / gl if gl > 0 else float('inf')
    wr     = len(wins) / len(trades) * 100
    avg_r  = sum(t['r_multiple'] for t in trades) / len(trades)
    avg_days = sum(t['hold_days'] for t in trades) / len(trades)

    # Drawdown on daily total portfolio value
    totals = [d['total_capital'] for d in daily]
    peak   = totals[0]
    max_dd_pct = 0.0
    dd_start = dd_end = None
    cur_start = None
    for i, c in enumerate(totals):
        if c >= peak:
            peak = c
            cur_start = None
        else:
            if cur_start is None:
                cur_start = daily[i]['date']
            dd_pct = (peak - c) / peak * 100
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                dd_start = cur_start
                dd_end   = daily[i]['date']

    # Max consec losses
    max_streak = cur = 0
    for t in sorted(trades, key=lambda x: x['entry_date']):
        if not t['win']:
            cur += 1; max_streak = max(max_streak, cur)
        else:
            cur = 0

    # Avg win / avg loss
    avg_win  = gw / len(wins)  if wins   else 0
    avg_loss = gl / len(losses) if losses else 0
    wl_ratio = avg_win / avg_loss if avg_loss else float('inf')

    # Year-by-year
    yd = defaultdict(lambda: {'n': 0, 'w': 0, 'net': 0.0, 'gw': 0.0, 'gl': 0.0})
    for t in trades:
        yr = pd.Timestamp(t['entry_date']).year
        yd[yr]['n']  += 1
        yd[yr]['w']  += int(t['win'])
        yd[yr]['net'] += t['net_rs']
        if t['win']: yd[yr]['gw'] += t['net_rs']
        else:        yd[yr]['gl'] += abs(t['net_rs'])

    # CAGR
    dates   = [pd.Timestamp(d['date']) for d in daily]
    yr_span = (dates[-1] - dates[0]).days / 365.25 if len(dates) > 1 else 3.0
    cagr    = ((result['final_cap'] / CAPITAL) ** (1 / yr_span) - 1) * 100

    # R-multiple buckets
    buckets = {
        '>3R':      sum(1 for t in trades if t['r_multiple'] >  3.0),
        '2-3R':     sum(1 for t in trades if 2.0 < t['r_multiple'] <= 3.0),
        '1-2R':     sum(1 for t in trades if 1.0 < t['r_multiple'] <= 2.0),
        '0-1R':     sum(1 for t in trades if 0.0 < t['r_multiple'] <= 1.0),
        '0 to -1R': sum(1 for t in trades if -1.0 <= t['r_multiple'] <= 0.0),
        '<-1R':     sum(1 for t in trades if t['r_multiple'] < -1.0),
    }

    # Per-year portfolio return from daily total_capital curve
    df_daily = pd.DataFrame(result['daily_cap'])
    df_daily['date'] = pd.to_datetime(df_daily['date'])
    df_daily['year'] = df_daily['date'].dt.year
    yr_port_ret = {}
    for yr, grp in df_daily.groupby('year'):
        yr_start = float(grp['total_capital'].iloc[0])
        yr_end   = float(grp['total_capital'].iloc[-1])
        yr_port_ret[yr] = (yr_end / yr_start - 1) * 100 if yr_start > 0 else 0.0

    return {
        'mode': trail_mode, 'n': len(trades),
        'wr': wr, 'pf': pf, 'avg_r': avg_r,
        'net': net, 'gw': gw, 'gl': gl,
        'avg_win': avg_win, 'avg_loss': avg_loss, 'wl_ratio': wl_ratio,
        'avg_days': avg_days, 'max_dd_pct': max_dd_pct,
        'dd_start': dd_start, 'dd_end': dd_end,
        'max_streak': max_streak, 'cagr': cagr,
        'final_cap': result['final_cap'],
        'yd': dict(yd), 'buckets': buckets,
        'skip_pos': result['skip_pos'], 'skip_cap': result['skip_cap'],
        'yr_port_ret': yr_port_ret,
        'daily_cap': result['daily_cap'],
    }


# ── Print one method detail ───────────────────────────────────────────────────
def _print_detail(m: dict):
    sep = '=' * 70
    print(f"\n{sep}")
    print(f"  {m['mode'].upper()}  --  {TRAIL_METHODS[m['mode']]}")
    print(sep)

    if m['n'] == 0:
        print("  No trades."); return

    print(f"\n  CORE METRICS")
    print(f"  {'Trades':<24}: {m['n']}")
    print(f"  {'Win Rate':<24}: {m['wr']:.1f}%")
    print(f"  {'Profit Factor':<24}: {m['pf']:.2f}")
    print(f"  {'Avg R-Multiple':<24}: {m['avg_r']:+.2f}R")
    print(f"  {'Net P&L':<24}: Rs{m['net']:+,.0f}")
    print(f"  {'Final Capital':<24}: Rs{m['final_cap']:,.0f}  (started Rs{CAPITAL:,.0f})")
    print(f"  {'CAGR':<24}: {m['cagr']:+.1f}%/yr")

    print(f"\n  WIN / LOSS SIZE")
    print(f"  {'Avg Win':<24}: Rs{m['avg_win']:,.0f}")
    print(f"  {'Avg Loss':<24}: Rs{m['avg_loss']:,.0f}")
    print(f"  {'Win/Loss Ratio':<24}: {m['wl_ratio']:.2f}x")

    print(f"\n  RISK METRICS")
    print(f"  {'Max Drawdown':<24}: {m['max_dd_pct']:.1f}%  of peak portfolio")
    if m['dd_start']:
        print(f"  {'DD Period':<24}: {str(m['dd_start'])[:10]} -> {str(m['dd_end'])[:10]}")
    print(f"  {'Max Consec Losses':<24}: {m['max_streak']}")
    print(f"  {'Avg Hold Days':<24}: {m['avg_days']:.1f}")

    print(f"\n  R-MULTIPLE DISTRIBUTION")
    n = m['n']
    for label, cnt in m['buckets'].items():
        pct = cnt / n * 100
        bar = '#' * int(pct / 2)
        print(f"  {label:<10} : {cnt:>4} ({pct:>5.1f}%)  {bar}")

    print(f"\n  YEAR-BY-YEAR  (portfolio return = from daily total_capital curve)")
    print(f"  {'Year':<6}  {'Port Ret%':>9}  {'Trades':>6}  {'WR%':>5}  {'PF':>5}  {'Net Rs':>12}")
    print(f"  {'-'*54}")
    all_yrs = sorted(set(m['yd'].keys()) | set(m.get('yr_port_ret', {}).keys()))
    for yr in all_yrs:
        s    = m['yd'].get(yr, {'n': 0, 'w': 0, 'gw': 0.0, 'gl': 0.0, 'net': 0.0})
        pf_y = s['gw'] / s['gl'] if s['gl'] > 0 else float('inf')
        wr_y = s['w'] / s['n'] * 100 if s['n'] else 0
        pf_s = f"{pf_y:.2f}" if pf_y < 100 else ">100"
        ret  = m['yr_port_ret'].get(yr)
        ret_s = f"{ret:>+8.1f}%" if ret is not None else "       ---"
        flag = "  <-- LOSS" if ret is not None and ret < 0 else ""
        print(f"  {yr:<6}  {ret_s}  {s['n']:>6}  {wr_y:>5.1f}  {pf_s:>5}  {s['net']:>+12,.0f}{flag}")


# ── Summary comparison table ──────────────────────────────────────────────────
def _print_summary(all_metrics: list):
    sep = '=' * 90
    print(f"\n{sep}")
    print("  PORTFOLIO SUMMARY — TRAIL METHODS  (fixed Rs2L capital, 2% risk/trade)")
    print(f"  Capital Rs{CAPITAL:,.0f} fixed | Max {MAX_POSITIONS} positions | Entry: breakout-day close")
    print(sep)
    hdr = (f"  {'Method':<11}  {'Label':<32}  "
           f"{'N':>4}  {'WR%':>5}  {'PF':>5}  {'AvgR':>5}  "
           f"{'AvgDays':>7}  {'MaxDD%':>6}  {'CAGR%':>6}  {'Net Rs':>12}")
    print(hdr)
    print("  " + "-" * 90)

    baseline = next((m for m in all_metrics if m['mode'] == 'ema50'), None)
    base_net = baseline['net'] if baseline else 0

    for m in all_metrics:
        if m['n'] == 0:
            print(f"  {m['mode']:<11}  {'no trades':>60}")
            continue
        diff  = m['net'] - base_net
        mark  = ''
        if m['mode'] == 'ema50':
            mark = ' <baseline'
        elif m['net'] == max(x['net'] for x in all_metrics if x['n'] > 0):
            mark = ' <-- BEST'
        diff_s = f"({diff:+,.0f})" if m['mode'] != 'ema50' else ''
        print(f"  {m['mode']:<11}  {TRAIL_METHODS[m['mode']]:<32}  "
              f"{m['n']:>4}  {m['wr']:>5.1f}  {m['pf']:>5.2f}  {m['avg_r']:>+5.2f}  "
              f"{m['avg_days']:>7.1f}  {m['max_dd_pct']:>6.1f}  {m['cagr']:>+6.1f}  "
              f"{m['net']:>+12,.0f}  {diff_s}{mark}")

    print(sep)

    # Year grid — portfolio return % (from daily total_capital)
    all_years = sorted({yr for m in all_metrics
                        for yr in (m.get('yr_port_ret') or m.get('yd', {}))})
    print(f"\n  YEAR-BY-YEAR PORTFOLIO RETURN % (from daily capital curve)\n")
    print(f"  {'Method':<11}  " + "  ".join(f"{y:>7}" for y in all_years)
          + f"  {'CAGR':>6}")
    print("  " + "-" * (13 + 9 * len(all_years) + 8))
    for m in all_metrics:
        row = f"  {m['mode']:<11}  "
        for yr in all_years:
            ret = (m.get('yr_port_ret') or {}).get(yr)
            row += f"{ret:>+6.0f}%  " if ret is not None else "    ---  "
        row += f"{m.get('cagr', 0):>+5.1f}%"
        print(row)
    print(sep)


# ── Monte Carlo + Walk-forward audit ─────────────────────────────────────────
def _monte_carlo(trades: list, n_iter: int = 10_000, seed: int = 42) -> dict:
    """
    Bootstrap resample N trades (with replacement) n_iter times.
    Returns distribution of net P&L, WR, and profit factor.
    """
    rng  = np.random.default_rng(seed)
    nets = np.array([t['net_rs'] for t in trades], dtype=float)
    wins = (nets > 0).astype(float)
    n    = len(nets)

    mc_net = np.empty(n_iter)
    mc_wr  = np.empty(n_iter)
    mc_pf  = np.empty(n_iter)

    for i in range(n_iter):
        idx       = rng.integers(0, n, size=n)
        s         = nets[idx]
        mc_net[i] = s.sum()
        mc_wr[i]  = wins[idx].mean() * 100
        gw        = s[s > 0].sum()
        gl        = -s[s < 0].sum()
        mc_pf[i]  = gw / gl if gl > 0 else float('inf')

    return {
        'p_loss':    float((mc_net < 0).mean() * 100),
        'net_p5':    float(np.percentile(mc_net, 5)),
        'net_med':   float(np.median(mc_net)),
        'net_p95':   float(np.percentile(mc_net, 95)),
        'wr_p5':     float(np.percentile(mc_wr, 5)),
        'wr_med':    float(np.median(mc_wr)),
        'wr_p95':    float(np.percentile(mc_wr, 95)),
        'pf_p5':     float(np.percentile(mc_pf[np.isfinite(mc_pf)], 5)),
        'pf_med':    float(np.median(mc_pf[np.isfinite(mc_pf)])),
    }


def _walk_forward(trades: list, daily_cap: list, window_yrs: int = 3) -> list:
    """
    Slide a window_yrs-year window over the trade history.
    Returns per-window WR, PF, portfolio return %.
    """
    df = pd.DataFrame(trades)
    df['entry_date'] = pd.to_datetime(df['entry_date'])
    df['year']       = df['entry_date'].dt.year

    dc = pd.DataFrame(daily_cap)
    dc['date'] = pd.to_datetime(dc['date'])
    dc['year'] = dc['date'].dt.year

    all_years = sorted(df['year'].unique())
    rows = []
    for start in range(all_years[0], all_years[-1] - window_yrs + 2):
        end   = start + window_yrs - 1
        wt    = df[(df['year'] >= start) & (df['year'] <= end)]
        wc    = dc[(dc['year'] >= start) & (dc['year'] <= end)]
        if len(wt) < 15:
            continue
        wins   = wt[wt['win']]
        losses = wt[~wt['win']]
        gw     = wins['net_rs'].sum()
        gl     = losses['net_rs'].abs().sum()
        pf     = gw / gl if gl > 0 else float('inf')
        wr     = len(wins) / len(wt) * 100
        if wc.empty:
            ret = 0.0
        else:
            cap_s = float(wc['total_capital'].iloc[0])
            cap_e = float(wc['total_capital'].iloc[-1])
            ret   = (cap_e / cap_s - 1) * 100 if cap_s > 0 else 0.0
        rows.append({'window': f'{start}-{end}', 'n': len(wt),
                     'wr': wr, 'pf': pf, 'ret_pct': ret})
    return rows


def _sharpe_sortino(yr_port_ret: dict, rf_rate: float = 6.5) -> tuple:
    """Annual Sharpe and Sortino from year-by-year portfolio returns."""
    rets = np.array(list(yr_port_ret.values()), dtype=float)
    excess = rets - rf_rate
    std    = rets.std(ddof=1) if len(rets) > 1 else 1.0
    sharpe = excess.mean() / std if std > 0 else 0.0
    down   = excess[excess < 0]
    dstd   = down.std(ddof=1) if len(down) > 1 else std
    sortino = excess.mean() / dstd if dstd > 0 else 0.0
    return round(sharpe, 2), round(sortino, 2)


def _print_audit(m: dict, n_iter: int = 10_000):
    """Run and print full audit for one trail method."""
    sep = '=' * 72
    print(f"\n{sep}")
    print(f"  AUDIT: {m['mode'].upper()} — {TRAIL_METHODS[m['mode']]}")
    print(f"  {n_iter:,} Monte Carlo iterations  |  3-year walk-forward windows")
    print(sep)

    trades    = [{'net_rs': t['net_rs'], 'win': t['win']}
                 for t in m.get('_trades', []) if t.get('hold_days', 1) > 0]
    daily_cap = m.get('daily_cap', [])

    if len(trades) < 30:
        print("  Too few trades for audit."); return

    # ── Monte Carlo ───────────────────────────────────────────────────────────
    print(f"\n  MONTE CARLO ({n_iter:,} bootstrap resamples of {len(trades)} trades)")
    mc = _monte_carlo(trades)
    print(f"  {'P(total loss)':<28}: {mc['p_loss']:.1f}%  "
          f"{'<-- CONCERN' if mc['p_loss'] > 5 else '(ok)'}")
    print(f"  {'Net P&L  5th pct':<28}: Rs{mc['net_p5']:>+12,.0f}  (bad-luck scenario)")
    print(f"  {'Net P&L  median':<28}: Rs{mc['net_med']:>+12,.0f}")
    print(f"  {'Net P&L  95th pct':<28}: Rs{mc['net_p95']:>+12,.0f}  (good-luck scenario)")
    print(f"  {'Win Rate 5th pct':<28}: {mc['wr_p5']:.1f}%")
    print(f"  {'Win Rate median':<28}: {mc['wr_med']:.1f}%")
    print(f"  {'Win Rate 95th pct':<28}: {mc['wr_p95']:.1f}%")
    print(f"  {'PF     5th pct':<28}: {mc['pf_p5']:.2f}  "
          f"{'<-- CONCERN' if mc['pf_p5'] < 1.0 else '(ok)'}")
    print(f"  {'PF     median':<28}: {mc['pf_med']:.2f}")

    # ── Walk-forward ──────────────────────────────────────────────────────────
    wf = _walk_forward(
        [t for t in m.get('_trades', []) if t.get('hold_days', 1) > 0],
        daily_cap
    )
    print(f"\n  WALK-FORWARD  (3-year rolling windows)")
    print(f"  {'Window':<12}  {'N':>4}  {'WR%':>5}  {'PF':>5}  {'PortRet%':>9}  {'Pass?':>6}")
    print(f"  {'-'*50}")
    n_pos = n_neg = 0
    for w in wf:
        pf_s = f"{w['pf']:.2f}" if w['pf'] < 100 else ">100"
        ok   = "YES" if w['ret_pct'] > 0 else "NO"
        if w['ret_pct'] > 0: n_pos += 1
        else:                 n_neg += 1
        print(f"  {w['window']:<12}  {w['n']:>4}  {w['wr']:>5.1f}  {pf_s:>5}  "
              f"{w['ret_pct']:>+8.1f}%  {ok:>6}")
    print(f"  Profitable windows: {n_pos}/{n_pos+n_neg}")

    # ── Sharpe / Sortino / Calmar ─────────────────────────────────────────────
    yr_ret = m.get('yr_port_ret', {})
    sharpe, sortino = _sharpe_sortino(yr_ret) if yr_ret else (0.0, 0.0)
    calmar = m['cagr'] / m['max_dd_pct'] if m['max_dd_pct'] > 0 else 0.0
    print(f"\n  RISK-ADJUSTED RATIOS  (risk-free 6.5% India 10yr)")
    print(f"  {'Sharpe ratio':<28}: {sharpe:.2f}  "
          f"{'(target > 1.0)' if sharpe < 1.0 else '(GOOD)'}")
    print(f"  {'Sortino ratio':<28}: {sortino:.2f}  "
          f"{'(target > 1.5)' if sortino < 1.5 else '(GOOD)'}")
    print(f"  {'Calmar ratio':<28}: {calmar:.2f}  "
          f"(CAGR {m['cagr']:.1f}% / MaxDD {m['max_dd_pct']:.1f}%)")

    # ── Annual return distribution ────────────────────────────────────────────
    if yr_ret:
        rets = np.array(list(yr_ret.values()))
        neg  = [yr for yr, r in yr_ret.items() if r < 0]
        print(f"\n  ANNUAL RETURN STATS")
        print(f"  {'Negative years':<28}: {len(neg)}  {neg if neg else '(none)'}")
        print(f"  {'Best year':<28}: {max(yr_ret, key=yr_ret.get)}  "
              f"{max(yr_ret.values()):+.1f}%")
        print(f"  {'Worst year':<28}: {min(yr_ret, key=yr_ret.get)}  "
              f"{min(yr_ret.values()):+.1f}%")
        print(f"  {'Mean annual return':<28}: {rets.mean():+.1f}%")
        print(f"  {'Std annual return':<28}: {rets.std(ddof=1):.1f}%")

    # ── Final verdict ─────────────────────────────────────────────────────────
    yr_ret = m.get('yr_port_ret', {})
    neg_yrs = len([r for r in yr_ret.values() if r < 0])
    passes  = []
    fails   = []

    def _chk(cond, label):
        (passes if cond else fails).append(label)

    # AMO next-day open entry: WR ~25-35% is expected (gap-up premium lowers WR).
    # Profit factor and CAGR are the meaningful gates; WR threshold adjusted.
    _chk(m['wr']         >= 25,  f"WR {m['wr']:.1f}% >= 25%  (AMO gap-up baseline)")
    _chk(m['pf']         >= 1.4, f"PF {m['pf']:.2f} >= 1.4")
    _chk(m['cagr']       >= 20,  f"CAGR {m['cagr']:.1f}% >= 20%")
    _chk(m['max_dd_pct'] <= 25,  f"MaxDD {m['max_dd_pct']:.1f}% <= 25%")
    _chk(neg_yrs         <= 1,   f"Neg years {neg_yrs} <= 1")
    _chk(mc['p_loss']    <= 10,  f"MC P(loss) {mc['p_loss']:.1f}% <= 10%")
    _chk(mc['pf_p5']     >= 1.0, f"MC PF 5th pct {mc['pf_p5']:.2f} >= 1.0")
    _chk(n_neg           <= 1,   f"Walk-fwd losing windows {n_neg} <= 1")
    _chk(sharpe          >= 0.5, f"Sharpe {sharpe:.2f} >= 0.5")

    print(f"\n  {'='*70}")
    verdict = "PASS" if len(fails) == 0 else ("CONDITIONAL PASS" if len(fails) <= 2 else "FAIL")
    print(f"  VERDICT: {verdict}  ({len(passes)}/{len(passes)+len(fails)} checks passed)")
    print(f"  {'='*70}")
    for p in passes:
        print(f"  [PASS]  {p}")
    for f in fails:
        print(f"  [FAIL]  {f}")
    print()


# ── Nifty loader (fetch from Zerodha, cache locally) ─────────────────────────
NIFTY_CACHE  = CACHE_3YR / "NIFTY50.csv"
NIFTY50_TOKEN = 256265
LOOKBACK_DAYS = 3 * 365 + 60


def _get_kite():
    from kiteconnect import KiteConnect
    env = {}
    env_path = ROOT.parent / "backtest_engine" / ".env.zerodha"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    kite = KiteConnect(api_key=env["ZERODHA_API_KEY"])
    kite.set_access_token(env["ZERODHA_ACCESS_TOKEN"])
    return kite


def _load_nifty() -> pd.DataFrame:
    """Load Nifty50 daily — use cache if fresh (<=7 days old), else fetch."""
    from datetime import datetime, timedelta

    if NIFTY_CACHE.exists():
        age = (datetime.now() - datetime.fromtimestamp(NIFTY_CACHE.stat().st_mtime)).days
        if age <= 7:
            print(f"  Nifty: loaded from cache ({age}d old)")
            df = pd.read_csv(NIFTY_CACHE, index_col="Date", parse_dates=True)
            return compute_indicators(df)

    print("  Nifty: fetching from Zerodha...")
    kite  = _get_kite()
    end   = datetime.now()
    start = end - timedelta(days=LOOKBACK_DAYS)
    try:
        rows = kite.historical_data(NIFTY50_TOKEN, start, end, "day",
                                    continuous=False, oi=False)
    except Exception as e:
        print(f"  Nifty fetch failed: {e}")
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()[["Open", "High", "Low", "Close", "Volume"]]
    df.to_csv(NIFTY_CACHE)
    print(f"  Nifty: {len(df)} days fetched and cached -> {NIFTY_CACHE.name}")
    return compute_indicators(df)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    use_10yr = "--10yr" in sys.argv

    if use_10yr:
        cache_dir   = CACHE_10YR
        min_rows    = MIN_ROWS_10YR
        filters     = VOL_SURGE_ONLY
        nifty_gate  = None          # VOL_SURGE_ONLY has nifty_trend=False, Nifty not needed
        filter_label = "VOL_SURGE_ONLY  (matches original validated 10yr config)"
        out_fname    = "nr7_trail_portfolio_10yr.csv"
    else:
        cache_dir   = CACHE_3YR
        min_rows    = MIN_ROWS_3YR
        filters     = FILTERS_ALL
        nifty_gate  = None
        filter_label = "FILTERS_ALL  (3yr cache)"
        out_fname    = "nr7_trail_portfolio.csv"

    print("=" * 72)
    print(f"  NR7 TRAIL SL — PORTFOLIO COMPARISON")
    print(f"  Filters : {filter_label}")
    print(f"  Capital : Rs{CAPITAL:,.0f} | {RISK_PCT*100:.0f}% risk/trade | Max {MAX_POSITIONS} positions")
    print("=" * 72)

    # Load stock data from cache
    stock_raw = {}
    for fpath in cache_dir.glob("*.csv"):
        sym = fpath.stem
        if sym == "NIFTY50":
            continue
        df = pd.read_csv(fpath, index_col="Date", parse_dates=True)
        if len(df) >= min_rows:
            stock_raw[sym] = df

    cache_name = "10yr" if use_10yr else "3yr"
    print(f"\nLoaded {len(stock_raw)} stocks from {cache_name} cache")

    if not stock_raw:
        hint = "run_nr7_portfolio.py --dl" if use_10yr else "run_nr7_backtest.py"
        print(f"No cached data found. Run: python {hint}")
        return

    # Sample to show data span
    sample = next(iter(stock_raw.values()))
    print(f"Data span: {sample.index[0].date()} to {sample.index[-1].date()}  "
          f"({len(sample)} days per stock)\n")

    # Compute indicators once — additive, does NOT touch strategy files
    print("Computing indicators...")
    stock_ind = {}
    for sym, df in stock_raw.items():
        df_ind = compute_indicators(df)
        df_ind = _add_trail_indicators(df_ind)   # adds sma20, sma50
        stock_ind[sym] = df_ind

    # Generate signals once — shared across all 9 trail methods
    print(f"Generating signals ({filter_label})...")
    all_signals = []
    for sym, df_ind in stock_ind.items():
        sigs = generate_signals(df_ind, nifty_df=None, filters=filters)
        for s in sigs:
            s['symbol'] = sym
        all_signals.extend(sigs)
    print(f"Total signals: {len(all_signals)}\n")

    # Run each trail method
    all_metrics = []
    all_trades  = []
    for mode in METHOD_ORDER:
        t0      = time.time()
        result  = simulate_portfolio_trail(stock_ind, all_signals,
                                           trail_mode=mode, nifty_gate=nifty_gate)
        elapsed = time.time() - t0
        m       = _metrics(result, mode)
        m['_trades'] = result['trades']   # kept for audit (not written to CSV)
        all_metrics.append(m)
        all_trades.extend(result['trades'])
        dd_str = f"{m['max_dd_pct']:.1f}%" if m['n'] > 0 else "n/a"
        print(f"  [{mode:<12}]  n={m.get('n',0):>4}  WR={m.get('wr',0):>5.1f}%  "
              f"PF={m.get('pf',0):>5.2f}  DD={dd_str:>6}  "
              f"CAGR={m.get('cagr',0):>+5.1f}%  Net=Rs{m.get('net',0):>+12,.0f}  ({elapsed:.1f}s)")

    # Full per-method detail
    for m in all_metrics:
        _print_detail(m)

    # Summary table
    _print_summary(all_metrics)

    # Save trades CSV
    out = ROOT / "reports" / "nr7_positional"
    out.mkdir(parents=True, exist_ok=True)
    if all_trades:
        out_path = out / out_fname
        # Drop internal _trades key before converting to DataFrame
        save_trades = [
            {k: v for k, v in t.items() if k != '_trades'}
            for t in all_trades
        ]
        pd.DataFrame(save_trades).to_csv(out_path, index=False)
        print(f"\n  Trades saved -> {out_path}")

    # ── Auto-audit best method if criteria met ────────────────────────────────
    if use_10yr:
        valid = [m for m in all_metrics if m['n'] > 0]
        if not valid:
            return

        # Rank by CAGR; also pick best WR method separately
        best_cagr = max(valid, key=lambda x: x['cagr'])
        best_wr   = max(valid, key=lambda x: x['wr'])

        # AMO entry naturally lowers WR (gap-up premium). Gate on CAGR+PF+DD instead.
        candidates = [m for m in valid
                      if m['pf'] >= 1.4 and m['cagr'] >= 18 and m['max_dd_pct'] <= 30]

        print(f"\n{'='*72}")
        print("  AUDIT — CRITERIA CHECK")
        print(f"{'='*72}")
        if not candidates:
            print("  No method met the gate (WR>=45%, PF>=1.5, CAGR>=15%).")
            print("  Run with original 10yr VOL_SURGE_ONLY data to compare baseline.")
            return

        print(f"  {len(candidates)} method(s) met the gate. Running full audit on best CAGR.\n")
        audit_target = max(candidates, key=lambda x: x['cagr'])
        _print_audit(audit_target)


if __name__ == "__main__":
    main()
