"""
NR7 Positional Strategy — Indian Equities (Buy Only)
=====================================================
Core rules (user spec):
  - Universe   : NSE equities, 500-1000 stocks across sectors
  - Capital    : Rs 1,00,000  |  Risk per trade: 2% = Rs 2,000
  - Setup      : NR7 day (narrowest 7-day range)
  - Entry      : Close of breakout day (broke above NR7 high)
  - Initial SL : NR7 candle low − ATR(14)
  - Trail SL   : 50 EMA (raised daily, never lowered)
  - TP         : No fixed TP — ride until trailing 50 EMA SL is hit
  - Filter     : 50 EMA > 100 EMA required (stock uptrend)

Configurable filters (pass via `filters` dict):
  ema_cross    [user]  : EMA50 > EMA100 on stock (stock in uptrend)
  nifty_trend  [user]  : Nifty overall trend — 3-condition score >= 2/3
                           1. Nifty close > 50 EMA
                           2. Nifty 50 EMA > 200 EMA (golden cross)
                           3. Nifty within 3% of 20-day high (momentum)
  vol_surge    [mine]  : Breakout day volume > 1.5x 20-day avg
  tight_nr7    [mine]  : NR7 range < 60% of ATR14 (very compressed)
  nr_streak    [mine]  : 2+ consecutive NR days (extra squeeze)
  near_52w     [mine]  : Stock within 15% of 52-week high (momentum)
"""

import numpy as np
import pandas as pd

# ── Constants ─────────────────────────────────────────────────────────────────
CAPITAL           = 100_000
RISK_PCT          = 0.02
RISK_RS           = CAPITAL * RISK_PCT        # Rs 2,000
MAX_POSITIONS     = 5
MAX_PCT_PER_TRADE = 0.25
MIN_PRICE         = 50.0
MIN_AVG_VOL       = 50_000
VOL_SURGE_MULT    = 1.5
NR7_QUALITY_PCT   = 0.60
NEAR_52W_PCT      = 0.15
NIFTY_TREND_MIN   = 2          # require at least 2/3 Nifty trend conditions

# Default: all filters ON
FILTERS_ALL  = dict(ema_cross=True, nifty_trend=True,
                    vol_surge=True, tight_nr7=True,
                    nr_streak=True, near_52w=True)
FILTERS_NONE = dict(ema_cross=False, nifty_trend=False,
                    vol_surge=False, tight_nr7=False,
                    nr_streak=False, near_52w=False)

FILTER_NAMES = list(FILTERS_ALL.keys())


# ── Indicator computation ─────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.sort_index(inplace=True)

    df['range'] = df['High'] - df['Low']
    df['nr7']   = df['range'] == df['range'].rolling(7, min_periods=7).min()

    streak, s = [], 0
    for v in df['nr7']:
        s = (s + 1) if v else 0
        streak.append(s)
    df['nr_streak'] = streak

    prev_c = df['Close'].shift(1)
    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - prev_c).abs(),
        (df['Low']  - prev_c).abs(),
    ], axis=1).max(axis=1)
    df['atr14']   = tr.ewm(span=14,  adjust=False).mean()
    df['ema50']   = df['Close'].ewm(span=50,  adjust=False).mean()
    df['ema100']  = df['Close'].ewm(span=100, adjust=False).mean()
    df['ema200']  = df['Close'].ewm(span=200, adjust=False).mean()
    df['high20']  = df['High'].rolling(20, min_periods=10).max()
    df['vol_sma20'] = df['Volume'].rolling(20, min_periods=10).mean()
    df['high_52w']  = df['High'].rolling(252, min_periods=50).max()

    return df


# ── Nifty trend score (0-3) ───────────────────────────────────────────────────
def _nifty_trend_score(nifty_row) -> int:
    """
    3-condition Nifty overall trend score.
    Require >= NIFTY_TREND_MIN (2) to allow long entries.

    Condition 1 : Nifty close > 50 EMA          (medium-term uptrend)
    Condition 2 : Nifty 50 EMA > 200 EMA        (golden cross — long-term bull)
    Condition 3 : Nifty within 3% of 20-day high (momentum / not breaking down)
    """
    score = 0
    close  = float(nifty_row['Close'])
    ema50  = float(nifty_row['ema50'])
    ema200 = float(nifty_row['ema200'])
    high20 = float(nifty_row['high20'])

    if close > ema50:
        score += 1
    if ema50 > ema200:
        score += 1
    if high20 > 0 and close >= high20 * 0.97:
        score += 1

    return score


# ── Signal generation ─────────────────────────────────────────────────────────
def generate_signals(df: pd.DataFrame,
                     nifty_df: pd.DataFrame = None,
                     filters: dict = None) -> list:
    """
    Generate NR7 long breakout signals for one stock.

    Parameters
    ----------
    df        : daily OHLCV + indicators
    nifty_df  : Nifty50 daily with indicators (for nifty_trend filter)
    filters   : dict of bool flags per filter name; defaults to FILTERS_ALL

    Returns list of signal dicts.
    """
    if filters is None:
        filters = FILTERS_ALL
    if len(df) < 120:
        return []

    # Build Nifty date → row lookup
    nifty_map = {}
    if nifty_df is not None and 'ema50' in nifty_df.columns:
        for dt, row in nifty_df.iterrows():
            key = dt.date() if hasattr(dt, 'date') else dt
            nifty_map[key] = row

    signals = []

    for i in range(100, len(df)):
        if not df['nr7'].iloc[i - 1]:
            continue

        prev  = df.iloc[i - 1]
        curr  = df.iloc[i]
        dt    = df.index[i]
        dt_key = dt.date() if hasattr(dt, 'date') else dt

        nr7_h = float(prev['High'])
        nr7_l = float(prev['Low'])
        atr   = float(prev['atr14'])
        close = float(curr['Close'])
        ema50  = float(curr['ema50'])
        ema100 = float(curr['ema100'])
        h52w   = float(curr['high_52w']) if not np.isnan(curr['high_52w']) else close
        vol    = float(curr['Volume'])
        vsma   = float(curr['vol_sma20']) if curr['vol_sma20'] > 0 else 1

        # ── Always-on guards ─────────────────────────────────────────────────
        if close <= nr7_h:
            continue
        if close < MIN_PRICE:
            continue
        if vsma < MIN_AVG_VOL:
            continue
        sl = nr7_l - atr
        if sl <= 0 or sl >= close:
            continue
        distance = close - sl
        if distance <= 0:
            continue

        # ── Mandatory configurable filters ───────────────────────────────────
        # 1. Stock EMA cross (user)
        if filters.get('ema_cross') and ema50 <= ema100:
            continue

        # 2. Nifty overall trend (user — 3-condition score)
        if filters.get('nifty_trend'):
            nifty_row = nifty_map.get(dt_key)
            if nifty_row is not None:
                if _nifty_trend_score(nifty_row) < NIFTY_TREND_MIN:
                    continue

        # ── Scoring extras ────────────────────────────────────────────────────
        score, detail = 0, []

        if filters.get('vol_surge') and vol >= VOL_SURGE_MULT * vsma:
            score += 1
            detail.append(f"vol({vol/vsma:.1f}x)")

        if filters.get('tight_nr7') and atr > 0 and (nr7_h - nr7_l) / atr < NR7_QUALITY_PCT:
            score += 1
            detail.append(f"tight({(nr7_h-nr7_l)/atr:.2f}x)")

        if filters.get('nr_streak') and int(prev['nr_streak']) >= 2:
            score += 1
            detail.append(f"streak({int(prev['nr_streak'])})")

        if filters.get('near_52w') and h52w > 0 and close >= h52w * (1 - NEAR_52W_PCT):
            score += 1
            detail.append(f"52wh({close/h52w*100:.0f}%)")

        qty = int(RISK_RS / distance)
        if qty < 1:
            continue

        signals.append({
            "date":          dt,
            "entry_price":   close,
            "stop_loss":     round(sl, 2),
            "nr7_high":      nr7_h,
            "nr7_low":       nr7_l,
            "atr":           round(atr, 2),
            "ema50":         round(ema50, 2),
            "ema100":        round(ema100, 2),
            "score":         score,
            "score_detail":  " | ".join(detail) if detail else "base",
            "qty_suggested": qty,
        })

    return signals


# ── Trade simulation ──────────────────────────────────────────────────────────
def simulate_trades(df: pd.DataFrame, signals: list,
                    starting_capital: float = CAPITAL) -> list:
    """
    Simulate positional trades. Entry at next day's open.
    Trail SL: each day SL = max(current_SL, 50_EMA). Exit when Low <= SL.
    """
    from costs.zerodha_equity import equity_round_trip_cost

    trades, open_trade = [], None
    capital = starting_capital

    sig_map = {s['date']: s for s in signals}

    for i in range(len(df)):
        row   = df.iloc[i]
        dt    = df.index[i]
        prev_dt = df.index[i - 1] if i > 0 else None

        # ── Trail SL + check exit ─────────────────────────────────────────────
        if open_trade is not None:
            new_sl = float(row['ema50'])
            if new_sl > open_trade['sl']:
                open_trade['sl'] = new_sl

            if float(row['Low']) <= open_trade['sl']:
                exit_p = open_trade['sl']
                qty    = open_trade['qty']
                entry  = open_trade['entry_price']
                gross  = (exit_p - entry) * qty
                cost   = equity_round_trip_cost(entry, max(exit_p, 0.01), qty)
                net    = gross - cost
                open_trade.update({
                    'exit_date':   dt,
                    'exit_price':  round(exit_p, 2),
                    'exit_reason': 'SL',
                    'gross_rs':    round(gross, 2),
                    'cost_rs':     round(cost, 2),
                    'net_rs':      round(net, 2),
                    'r_multiple':  round(net / RISK_RS, 2),
                    'win':         net > 0,
                })
                trades.append(open_trade)
                capital += net
                open_trade = None

        # ── New entry (signal fired yesterday, enter at today's open) ─────────
        if open_trade is None and prev_dt is not None and prev_dt in sig_map:
            sig   = sig_map[prev_dt]
            entry = float(row['Open'])
            sl    = sig['stop_loss']
            if entry <= sl:
                continue
            distance = entry - sl
            qty = min(
                int(RISK_RS / distance),
                int(capital * MAX_PCT_PER_TRADE / entry),
            )
            if qty < 1 or capital < 5_000:
                continue
            open_trade = {
                'entry_date':   dt,
                'entry_price':  entry,
                'sl':           sl,
                'qty':          qty,
                'symbol':       sig.get('symbol', ''),
                'score':        sig['score'],
                'score_detail': sig['score_detail'],
                'atr_at_entry': sig['atr'],
                'nr7_high':     sig['nr7_high'],
                'nr7_low':      sig['nr7_low'],
            }

    # Force-close at data end
    if open_trade is not None:
        exit_p = float(df['Close'].iloc[-1])
        qty    = open_trade['qty']
        entry  = open_trade['entry_price']
        gross  = (exit_p - entry) * qty
        cost   = equity_round_trip_cost(entry, max(exit_p, 0.01), qty)
        net    = gross - cost
        open_trade.update({
            'exit_date': df.index[-1], 'exit_price': round(exit_p, 2),
            'exit_reason': 'DATA_END', 'gross_rs': round(gross, 2),
            'cost_rs': round(cost, 2), 'net_rs': round(net, 2),
            'r_multiple': round(net / RISK_RS, 2), 'win': net > 0,
        })
        trades.append(open_trade)

    return trades
