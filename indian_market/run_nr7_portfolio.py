"""
NR7 Positional Backtest — Portfolio Simulation (10yr, Rs 2L capital)
=====================================================================
Key design choices:
  - Rs 2,00,000 starting capital
  - Dynamic fixed-fractional sizing: risk = 2% of CURRENT total portfolio value
    (compounding — position sizes grow as account grows, shrink after losses)
  - Max 25% of current portfolio per position
  - Rs 10,000 minimum cash buffer
  - Separate 10yr cache (data/nr7_cache_10yr/) — chunked Kite fetch
  - Compares: No Filters  vs  Volume Surge priority

Run:
    python run_nr7_portfolio.py          # use cached data (fast)
    python run_nr7_portfolio.py --dl     # force re-download
"""
import sys, time
from pathlib import Path
from collections import defaultdict

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from strategies.nr7_positional import (
    compute_indicators, generate_signals,
    FILTER_NAMES, FILTERS_NONE,
)
from costs.zerodha_equity import equity_round_trip_cost
from run_nr7_backtest import (
    _universe, _get_kite,
    _fetch_daily_chunked,
    download_and_cache_dir, load_from_cache_dir,
    NIFTY50_TOKEN,
)

# ── Portfolio constants ───────────────────────────────────────────────────────
CAPITAL         = 200_000      # Rs 2,00,000
RISK_PCT        = 0.02         # 2% of total portfolio value per trade
MAX_POSITIONS   = 5
MAX_CAP_PCT     = 0.25         # max 25% of portfolio in one stock
MIN_CASH_BUFFER = 10_000       # always keep Rs 10k free
LOOKBACK_DAYS   = 10 * 365 + 60

CACHE_10YR = ROOT / "data" / "nr7_cache_10yr"

VOL_SURGE_ONLY = {k: (k == 'vol_surge') for k in FILTER_NAMES}

# CONFIGS = (label, filters, reinvest_pct, apply_tax)
CONFIGS = [
    ("Fixed  Pre-Tax",          FILTERS_NONE,    0.0, False),
    ("Fixed  After-Tax",        FILTERS_NONE,    0.0, True),
    ("50% Reinvest  Pre-Tax",   FILTERS_NONE,    0.5, False),
    ("50% Reinvest  After-Tax", FILTERS_NONE,    0.5, True),
]


def _stcg_rate(year: int) -> float:
    """Indian STCG rate on listed equity. Budget 2024 raised to 20% from Jul 23, 2024."""
    return 0.15 if year <= 2023 else 0.20


# ── Portfolio simulator ───────────────────────────────────────────────────────
def simulate_portfolio(stock_data: dict, all_signals: list,
                       reinvest_pct: float = 0.0,
                       apply_tax: bool = False) -> dict:
    """
    Single shared-capital portfolio simulation across all stocks.

    reinvest_pct : fraction of each year's POST-TAX profit added to sizing base.
    apply_tax    : if True, deduct Indian STCG at year-end before reinvesting
                   (15% up to 2023, 20% from 2024 — Budget 2024 change).
    """
    sigs_on_day = defaultdict(list)
    for sig in all_signals:
        sigs_on_day[sig['date']].append(sig)

    all_dates  = sorted(set(dt for df in stock_data.values() for dt in df.index))
    sym_dates  = {sym: list(df.index) for sym, df in stock_data.items()}

    capital        = float(CAPITAL)
    sizing_capital = float(CAPITAL)
    open_pos       = {}
    closed         = []
    daily_cap      = []
    peak_open      = 0
    skipped_no_cap  = 0
    skipped_max_pos = 0
    reinvest_log    = []
    total_tax_paid  = 0.0

    cur_year       = None
    year_start_tot = float(CAPITAL)

    for dt in all_dates:
        yr = dt.year

        # ── Year-end: tax deduction then reinvestment ─────────────────────────
        if cur_year is not None and yr != cur_year:
            total_now   = capital + sum(p['capital_deployed'] for p in open_pos.values())
            year_profit = total_now - year_start_tot

            if year_profit > 0:
                # Deduct STCG before reinvesting
                tax = year_profit * _stcg_rate(cur_year) if apply_tax else 0.0
                capital         -= tax
                total_tax_paid  += tax
                post_tax_profit  = year_profit - tax

                if reinvest_pct > 0:
                    added           = post_tax_profit * reinvest_pct
                    sizing_capital += added
                    reinvest_log.append((cur_year,
                                         round(year_profit), round(tax),
                                         round(post_tax_profit), round(added),
                                         round(sizing_capital)))
            year_start_tot = total_now - (year_profit - (year_profit if year_profit > 0 else 0))
            year_start_tot = capital + sum(p['capital_deployed'] for p in open_pos.values())

        if cur_year != yr:
            cur_year = yr
            if yr == all_dates[0].year:
                year_start_tot = float(CAPITAL)

        # ── 1. Process exits ─────────────────────────────────────────────────
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            df  = stock_data[sym]
            if dt not in df.index:
                continue
            row = df.loc[dt]

            new_sl = float(row['ema50'])
            if new_sl > pos['sl']:
                pos['sl'] = new_sl

            if float(row['Low']) <= pos['sl']:
                exit_p = pos['sl']
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
                    'exit_reason':   'SL',
                    'qty':           qty,
                    'gross_rs':      round(gross, 2),
                    'cost_rs':       round(cost, 2),
                    'net_rs':        round(net, 2),
                    'r_multiple':    round(net / pos['risk_rs'], 2),
                    'win':           net > 0,
                    'score':         pos['score'],
                    'hold_days':     hold_d,
                    'capital_after': round(capital, 0),
                })
                del open_pos[sym]

        peak_open = max(peak_open, len(open_pos))

        # ── 2. New signals — rank: score desc, then symbol asc ───────────────
        today_sigs = sorted(
            sigs_on_day.get(dt, []),
            key=lambda s: (-s.get('score', 0), s['symbol'])
        )

        for sig in today_sigs:
            sym = sig['symbol']
            if sym in open_pos:
                continue
            if len(open_pos) >= MAX_POSITIONS:
                skipped_max_pos += 1
                continue

            future = [d for d in sym_dates.get(sym, []) if d > dt]
            if not future:
                continue
            next_dt  = future[0]
            if next_dt not in stock_data[sym].index:
                continue
            next_row = stock_data[sym].loc[next_dt]

            entry = float(next_row['Open'])
            sl    = sig['stop_loss']
            if entry <= sl or entry <= 0:
                continue
            distance = entry - sl
            if distance <= 0:
                continue

            # Sizing based on sizing_capital (grows by 50% of each year's profit)
            risk_rs = sizing_capital * RISK_PCT
            max_cap = sizing_capital * MAX_CAP_PCT

            qty = int(risk_rs / distance)
            qty = min(qty, int(max_cap / entry))
            if qty < 1:
                continue

            capital_needed = qty * entry
            if capital_needed > capital - MIN_CASH_BUFFER:
                skipped_no_cap += 1
                continue

            capital -= capital_needed
            open_pos[sym] = {
                'entry_date':       next_dt,
                'entry_price':      entry,
                'sl':               sl,
                'qty':              qty,
                'capital_deployed': capital_needed,
                'risk_rs':          risk_rs,
                'score':            sig.get('score', 0),
            }

        total_now = capital + sum(p['capital_deployed'] for p in open_pos.values())
        daily_cap.append({
            'date':           dt,
            'free_cash':      round(capital, 0),
            'total_capital':  round(total_now, 0),
            'sizing_capital': round(sizing_capital, 0),
            'open_positions': len(open_pos),
        })

    # Force-close anything still open
    for sym, pos in open_pos.items():
        df     = stock_data[sym]
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
            'exit_price': round(exit_p, 2), 'exit_reason': 'DATA_END',
            'qty': qty, 'gross_rs': round(gross, 2), 'cost_rs': round(cost, 2),
            'net_rs': round(net, 2),
            'r_multiple': round(net / pos['risk_rs'], 2),
            'win': net > 0, 'score': pos['score'], 'hold_days': 0,
            'capital_after': round(capital, 0),
        })

    return {
        'trades':                closed,
        'daily_capital':         daily_cap,
        'peak_open_positions':   peak_open,
        'skipped_no_capital':    skipped_no_cap,
        'skipped_max_positions': skipped_max_pos,
        'final_capital':         round(capital, 0),
        'reinvest_log':          reinvest_log,
        'final_sizing_capital':  round(sizing_capital, 0),
        'total_tax_paid':        round(total_tax_paid, 0),
    }


# ── Reporting ─────────────────────────────────────────────────────────────────
def _report(result: dict, label: str = "") -> dict:
    """Print full report; return summary dict for comparison."""
    trades   = [t for t in result['trades'] if t['exit_reason'] != 'DATA_END']
    all_t    = result['trades']
    daily    = result['daily_capital']

    if not trades:
        print("  No closed trades.")
        return {}

    wins  = [t for t in trades if t['win']]
    loss  = [t for t in trades if not t['win']]
    gw    = sum(t['net_rs'] for t in wins)
    gl    = sum(abs(t['net_rs']) for t in loss)
    net   = gw - gl
    pf    = gw / gl if gl else float('inf')
    wr    = len(wins) / len(trades) * 100
    avg_r = sum(t['r_multiple'] for t in trades) / len(trades)

    final = result['final_capital']

    # Date range from daily_cap
    dates = [pd.Timestamp(d['date']) for d in daily]
    yr_span = (dates[-1] - dates[0]).days / 365.25 if len(dates) > 1 else 3.0

    cagr = ((final / CAPITAL) ** (1 / yr_span) - 1) * 100

    # Drawdown on total_capital (not just free cash)
    totals = [d['total_capital'] for d in daily]
    peak   = totals[0]
    max_dd, max_dd_pct = 0.0, 0.0
    for c in totals:
        if c > peak: peak = c
        dd = peak - c
        dd_pct = dd / peak * 100
        if dd > max_dd: max_dd = dd; max_dd_pct = dd_pct

    print(f"\n{'='*60}")
    print(f"  CONFIG: {label}")
    print(f"{'='*60}")
    print(f"  Starting capital  : Rs{CAPITAL:,.0f}")
    print(f"  Final capital     : Rs{final:,.0f}")
    print(f"  Net P&L           : Rs{final-CAPITAL:+,.0f}  ({(final-CAPITAL)/CAPITAL*100:+.1f}%)")
    print(f"  Period            : {dates[0].date()} to {dates[-1].date()} ({yr_span:.1f}yr)")
    print(f"  CAGR              : {cagr:+.1f}%/yr")
    print(f"  Max drawdown      : Rs{max_dd:,.0f}  ({max_dd_pct:.1f}% of peak)")
    print(f"  Sizing capital    : Rs{CAPITAL:,.0f} -> Rs{result['final_sizing_capital']:,.0f}")
    print(f"  Peak open pos     : {result['peak_open_positions']} / {MAX_POSITIONS}")
    print(f"  Skipped (max pos) : {result['skipped_max_positions']}")
    print(f"  Skipped (no cap)  : {result['skipped_no_capital']}")

    # Reinvestment log
    if result.get('reinvest_log'):
        rlog = result['reinvest_log']
        has_tax = len(rlog[0]) == 6  # (yr, profit, tax, post_tax, added, base)
        if has_tax:
            print(f"\n  ANNUAL REINVESTMENT (after STCG tax, 50% reinvested):")
            print(f"  {'Year':>4}  {'Pre-Tax':>11}  {'STCG Tax':>10}  "
                  f"{'Post-Tax':>10}  {'Added 50%':>10}  {'Sizing Base':>12}")
            print(f"  {'-'*65}")
            for yr, profit, tax, post_tax, added, base in rlog:
                print(f"  {yr:>4}  Rs{profit:>9,.0f}  Rs{tax:>8,.0f}  "
                      f"Rs{post_tax:>8,.0f}  Rs{added:>8,.0f}  Rs{base:>10,.0f}")
        else:
            print(f"\n  ANNUAL REINVESTMENT (50% of profit -> sizing base):")
            print(f"  {'Year':>4}  {'Yr Profit':>12}  {'Added 50%':>12}  {'New Base':>12}")
            print(f"  {'-'*46}")
            for yr, profit, added, base in rlog:
                print(f"  {yr:>4}  Rs{profit:>10,.0f}  Rs{added:>10,.0f}  Rs{base:>10,.0f}")

    if result.get('total_tax_paid', 0) > 0:
        print(f"\n  Total STCG tax paid : Rs{result['total_tax_paid']:,.0f}"
              f"  ({result['total_tax_paid']/(result['final_capital']-CAPITAL+result['total_tax_paid'])*100:.1f}% of gross profit)")

    print()
    print(f"  TRADE STATS ({len(trades)} closed trades | {len(all_t)-len(trades)} still open at end)")
    print(f"  {'-'*50}")
    print(f"  Win rate          : {wr:.1f}%")
    print(f"  Profit factor     : {pf:.2f}")
    print(f"  Avg R-multiple    : {avg_r:+.2f}R")
    print(f"  Gross wins        : Rs{gw:,.0f}")
    print(f"  Gross losses      : Rs{gl:,.0f}")
    print(f"  Avg hold days     : {sum(t['hold_days'] for t in trades)/len(trades):.0f}d")

    # ── Open position distribution ────────────────────────────────────────────
    pos_cnt = defaultdict(int)
    for d in daily:
        pos_cnt[d['open_positions']] += 1
    print(f"\n  POSITION UTILISATION (days at each level):")
    for n in sorted(pos_cnt):
        bar = '|' * (pos_cnt[n] // 10)
        pct = pos_cnt[n] / len(daily) * 100
        print(f"    {n} pos: {pos_cnt[n]:>4}d ({pct:4.0f}%)  {bar}")

    # ── Year-by-year using total portfolio value ──────────────────────────────
    df_daily = pd.DataFrame(daily)
    df_daily['date'] = pd.to_datetime(df_daily['date'])
    df_daily['year'] = df_daily['date'].dt.year

    year_start_cap = {}
    for yr, grp in df_daily.groupby('year'):
        year_start_cap[yr] = grp['total_capital'].iloc[0]

    yd = defaultdict(lambda: {'n':0,'w':0,'gw':0.0,'gl':0.0})
    for t in trades:
        yr = pd.Timestamp(t['entry_date']).year
        yd[yr]['n'] += 1
        if t['win']:
            yd[yr]['w']  += 1
            yd[yr]['gw'] += t['net_rs']
        else:
            yd[yr]['gl'] += abs(t['net_rs'])

    print(f"\n  YEAR-BY-YEAR:")
    print(f"  {'Year':>4}  {'N':>4}  {'WR':>5}  {'PF':>5}  {'Net P&L':>11}  {'Ret%':>6}  {'Avg Pos':>7}")
    print(f"  {'-'*60}")
    for yr in sorted(yd.keys()):
        s    = yd[yr]
        pf_y = s['gw'] / s['gl'] if s['gl'] else float('inf')
        wr_y = s['w'] / s['n'] * 100 if s['n'] else 0
        net_y = s['gw'] - s['gl']
        cap_y = year_start_cap.get(yr, CAPITAL)
        ret_y = net_y / cap_y * 100
        pf_s  = f"{pf_y:.2f}" if pf_y < 100 else ">100"
        avg_op = df_daily[df_daily['year'] == yr]['open_positions'].mean()
        print(f"  {yr:>4}  {s['n']:>4}  {wr_y:>4.0f}%  {pf_s:>5}  "
              f"Rs{net_y:>+9,.0f}  {ret_y:>+5.1f}%  avg={avg_op:.1f}")

    # ── Top 15 stocks ─────────────────────────────────────────────────────────
    sym_stats = defaultdict(lambda: {'n':0,'w':0,'gw':0.0,'gl':0.0})
    for t in trades:
        s = sym_stats[t['symbol']]
        s['n'] += 1
        if t['win']: s['w'] += 1; s['gw'] += t['net_rs']
        else: s['gl'] += abs(t['net_rs'])
    ranked = sorted(sym_stats.items(),
                    key=lambda x: x[1]['gw'] - x[1]['gl'], reverse=True)
    print(f"\n  TOP 15 STOCKS (by net P&L):")
    print(f"  {'Symbol':<14}  {'N':>3}  {'WR':>5}  {'PF':>5}  {'Net':>12}")
    print(f"  {'-'*48}")
    for sym, s in ranked[:15]:
        pf_s  = f"{s['gw']/s['gl']:.2f}" if s['gl'] else "inf"
        net_s = s['gw'] - s['gl']
        print(f"  {sym:<14}  {s['n']:>3}  {s['w']/s['n']*100:>4.0f}%  "
              f"{pf_s:>5}  Rs{net_s:>+10,.0f}")

    return {
        'label':      label, 'trades': len(trades), 'wr': wr, 'pf': pf,
        'avg_r':      avg_r, 'net': net, 'final': final, 'cagr': cagr,
        'max_dd':     max_dd, 'max_dd_pct': max_dd_pct,
        'skipped_pos': result['skipped_max_positions'],
        'skipped_cap': result['skipped_no_capital'],
        'sizing_cap':  result.get('final_sizing_capital', CAPITAL),
        'tax_paid':    result.get('total_tax_paid', 0),
    }


# ── Comparison table ──────────────────────────────────────────────────────────
def _compare(summaries: list):
    print(f"\n{'='*75}")
    print(f"  SIDE-BY-SIDE COMPARISON")
    print(f"{'='*75}")
    metrics = [
        ("Trades",            lambda s: f"{s['trades']}"),
        ("Win rate",          lambda s: f"{s['wr']:.1f}%"),
        ("Profit factor",     lambda s: f"{s['pf']:.2f}"),
        ("Avg R",             lambda s: f"{s['avg_r']:+.2f}R"),
        ("Net P&L",           lambda s: f"Rs{s['net']:+,.0f}"),
        ("STCG Tax paid",     lambda s: f"Rs{s['tax_paid']:,.0f}" if s['tax_paid'] else "-"),
        ("Final capital",     lambda s: f"Rs{s['final']:,.0f}"),
        ("CAGR",              lambda s: f"{s['cagr']:+.1f}%/yr"),
        ("Max drawdown",      lambda s: f"{s['max_dd_pct']:.1f}% of peak"),
        ("Sizing base end",   lambda s: f"Rs{s.get('sizing_cap',200000):,.0f}"),
    ]
    col_w = 20
    hdr = f"  {'Metric':<22}" + "".join(f"{s['label'][:col_w]:>{col_w}}" for s in summaries)
    print(hdr)
    print(f"  {'-'*(22 + col_w * len(summaries))}")
    for name, fn in metrics:
        row = f"  {name:<22}" + "".join(f"{fn(s):>{col_w}}" for s in summaries)
        print(row)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    force_dl = "--dl" in sys.argv

    print("=" * 65)
    print("  NR7 PORTFOLIO BACKTEST — Rs 2,00,000 CAPITAL  (10yr)")
    print(f"  Risk/trade: {RISK_PCT*100:.0f}% of portfolio  |  Max pos: {MAX_POSITIONS}")
    print(f"  Max cap/trade: {MAX_CAP_PCT*100:.0f}%  |  Buffer: Rs{MIN_CASH_BUFFER:,.0f}")
    print("=" * 65)

    kite     = _get_kite()
    universe = _universe(kite)

    # Nifty50 — chunked fetch
    print("Fetching Nifty50 history...")
    nifty_raw = _fetch_daily_chunked(kite, NIFTY50_TOKEN, days=LOOKBACK_DAYS)
    nifty_df  = compute_indicators(nifty_raw) if not nifty_raw.empty else pd.DataFrame()
    print(f"Nifty: {len(nifty_df)} trading days\n")

    # Stock data — 10yr cache
    if force_dl:
        download_and_cache_dir(kite, universe, LOOKBACK_DAYS,
                               cache_dir=CACHE_10YR, min_rows=1500)

    stock_data = load_from_cache_dir(universe, cache_dir=CACHE_10YR)

    if not stock_data:
        print("No cached data found. Run with --dl to download.")
        print("  python run_nr7_portfolio.py --dl")
        return

    print(f"Loaded {len(stock_data)} stocks from cache")
    # Show data span of a sample stock
    sample = next(iter(stock_data.values()))
    print(f"Data range: {sample.index[0].date()} to {sample.index[-1].date()}"
          f" ({len(sample)} days)\n")

    # Pre-compute indicators once for all stocks
    print("Computing indicators...")
    stock_ind = {sym: compute_indicators(df) for sym, df in stock_data.items()}

    # Run all configs — cache signals per filter set to avoid regenerating
    all_summaries = []
    all_results   = {}
    cached_sigs   = {}   # filters_label -> all_sigs list

    for label, filters, reinvest, with_tax in CONFIGS:
        filters_key = str(sorted(filters.items()))

        if filters_key not in cached_sigs:
            print(f"\nGenerating signals ({label.split()[0]})...")
            all_sigs = []
            for sym, df_ind in stock_ind.items():
                sigs = generate_signals(df_ind, nifty_df=nifty_df, filters=filters)
                for s in sigs:
                    s['symbol'] = sym
                all_sigs.extend(sigs)
            cached_sigs[filters_key] = all_sigs
            print(f"  Raw signals: {len(all_sigs)}")

        sigs_for_run = cached_sigs[filters_key]
        tax_str = "after-tax" if with_tax else "pre-tax"
        print(f"\n  Running: {label}  (reinvest={reinvest*100:.0f}%  {tax_str})...")
        result  = simulate_portfolio(stock_ind, sigs_for_run,
                                     reinvest_pct=reinvest, apply_tax=with_tax)
        summary = _report(result, label=label)
        all_summaries.append(summary)
        all_results[label] = result

    # Side-by-side comparison of all 4 configs
    _compare(all_summaries)

    # Save
    out = ROOT / "reports" / "nr7_positional"
    out.mkdir(parents=True, exist_ok=True)
    for label, result in all_results.items():
        label = label  # already a string key
        safe = label.lower().replace(" ", "_")
        pd.DataFrame(result['trades']).to_csv(
            out / f"nr7_portfolio_{safe}.csv", index=False)
        pd.DataFrame(result['daily_capital']).to_csv(
            out / f"nr7_portfolio_{safe}_capital.csv", index=False)
    print(f"\n  Results saved to {out}/")


if __name__ == "__main__":
    main()
