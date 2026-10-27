"""
PO3 (Power of 3) Strategy — Indian Index Options (ICT Framework)
================================================================
Timeframes:
  - 15-min: HTF bias via SMA50 + Order Block identification
  - 3-min:  AMD cycle detection (Accumulation → Manipulation → Distribution)

Entry logic:
  1. HTF SMA50 on 15-min determines direction (price > SMA50 = bull bias only)
  2. Identify fresh 15-min Order Block (OB) near current price
  3. Detect accumulation range on 3-min (tight consolidation near OB)
  4. Detect manipulation: sweep beyond accum boundary with volume spike
  5. Find FVG created by manipulation displacement move
  6. Distribution trigger: accumulation boundary broken in trade direction
  7. iFVG entry: price pulls back into FVG zone after distribution confirmed
  8. SL at manipulation candle extreme; option SL at 30% premium

Original ICT filters:
  - OB freshness (≤ 2 retests invalidates the OB)
  - Accumulation range < 0.40 × ATR14 (genuine consolidation)
  - iFVG body close required (wick pierce does not qualify)

Added filters (my design):
  - India VIX < 20 (options not overpriced)
  - Session window 9:30–13:00 IST (manipulation quality degrades midday)
  - Volume spike on manipulation candle (≥ 1.5× 20-bar average = institutional)
  - HTF SMA50 directional lock (eliminates counter-trend trades)
  - Accumulation minimum 5 bars (< 5 bars = noise, not institutional accumulation)

No lookahead bias: all conditions use strictly prior/completed bar data.
"""

import numpy as np
import pandas as pd
from typing import Optional, List, Dict, Tuple


# ── Default parameters ─────────────────────────────────────────────────────────

PO3_PARAMS: Dict = {
    # HTF (15-min)
    "htf_sma_period"       : 50,    # SMA50 on 15-min for bias
    "ob_impulse_bars"      : 2,     # consecutive displacement bars after OB candle
    "ob_max_retests"       : 2,     # OB invalidated after N touches
    "ob_proximity_pct"     : 1.5,   # accum must be within N% of OB high/low

    # Accumulation (3-min)
    "accum_min_bars"       : 5,     # min candles in consolidation
    "accum_max_bars"       : 25,    # max candles
    "accum_atr_mult"       : 1.2,   # total range < N × ATR14 (Nifty 3m ATR~20pts → ~24pt range)

    # Manipulation (3-min)
    "manip_sweep_min_pts"  : 3,     # sweep at least N pts beyond accum boundary
    "manip_volume_mult"    : 1.5,   # manip candle volume ≥ N × 20-bar vol avg
    "manip_max_bars"       : 8,     # look for manipulation within N bars of accum end

    # iFVG / entry mode
    "entry_mode"           : "ifvg",  # "ifvg" = wait for retracement; "breakout" = enter on distribution break
    "ifvg_body_close"      : True,    # candle body must enter iFVG (not just wick)
    "ifvg_max_bars"        : 5,       # bars to look for FVG after manipulation

    # Session filter (extended to 14:30 to allow afternoon moves)
    "session_start_h"      : 9,
    "session_start_m"      : 30,
    "session_end_h"        : 14,
    "session_end_m"        : 30,
    "hard_exit_h"          : 15,
    "hard_exit_m"          : 5,

    # VIX gate
    "vix_max"              : 20.0,

    # Options exit management (used by backtest engine)
    "sl_pct"               : 0.30,    # hard SL at 30% of entry premium
    "breakeven_pct"        : 0.15,    # move SL to entry premium once up 15%
    "trail_activate_pct"   : 0.25,    # trail activates at +25% premium gain
    "trail_pct"            : 0.15,    # trail 15% below peak (tighter than before)
    "tp_pct"               : 0.60,    # TP at +60% premium gain (was 5.00 = impossible)
    "lot_size"             : 50,      # Nifty lot size
    "strike_step"          : 50,
    "risk_per_trade"       : 10_000,
    "max_lots"             : 5,
    "rfr"                  : 0.065,
}


# ── Indicator helpers ──────────────────────────────────────────────────────────

def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _atr(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _vol_ma(vol: pd.Series, n: int = 20) -> pd.Series:
    return vol.rolling(n, min_periods=1).mean()


# ── Order Block detection (15-min) ─────────────────────────────────────────────

def find_ob_zones(df_15m: pd.DataFrame,
                  ob_impulse_bars: int = 2,
                  ob_max_retests: int = 2) -> List[Dict]:
    """
    Identify Order Block zones on the 15-min chart.

    Bullish OB: last bearish candle before bullish displacement (N+ green bars)
    Bearish OB: last bullish candle before bearish displacement (N+ red bars)
    OB invalidated if retested more than ob_max_retests times.

    Returns list of dicts sorted by timestamp descending (newest first).
    Each dict: {high, low, mid, direction, ts, retests}
    """
    zones: List[Dict] = []
    n = len(df_15m)
    if n < ob_impulse_bars + 4:
        return zones

    opens  = df_15m["Open"].values
    highs  = df_15m["High"].values
    lows   = df_15m["Low"].values
    closes = df_15m["Close"].values
    idx    = df_15m.index

    for i in range(1, n - ob_impulse_bars - 1):
        bear = closes[i] < opens[i]
        bull = closes[i] > opens[i]

        if not (bear or bull):
            continue

        direction = "bull" if bear else "bear"

        # Check impulse: next N candles all in trade direction
        valid_impulse = True
        for j in range(1, ob_impulse_bars + 1):
            if i + j >= n:
                valid_impulse = False
                break
            if direction == "bull" and closes[i + j] <= opens[i + j]:
                valid_impulse = False
                break
            if direction == "bear" and closes[i + j] >= opens[i + j]:
                valid_impulse = False
                break
        if not valid_impulse:
            continue

        ob_high = highs[i]
        ob_low  = lows[i]

        # Count retests (price returns into OB zone after initial impulse)
        retests = 0
        for k in range(i + ob_impulse_bars + 1, n):
            if lows[k] <= ob_high and highs[k] >= ob_low:
                retests += 1
            if retests > ob_max_retests:
                break

        if retests <= ob_max_retests:
            zones.append({
                "high"     : float(ob_high),
                "low"      : float(ob_low),
                "mid"      : float((ob_high + ob_low) / 2),
                "direction": direction,
                "ts"       : idx[i],
                "retests"  : retests,
            })

    # Newest OBs first
    zones.sort(key=lambda z: z["ts"], reverse=True)
    return zones


def nearest_ob(current_price: float, zones: List[Dict],
               direction: str, proximity_pct: float) -> Optional[Dict]:
    """
    Return the nearest valid OB matching direction that is within proximity_pct
    of current_price.
    """
    for z in zones:
        if z["direction"] != direction:
            continue
        # OB must be below price for bull, above for bear
        if direction == "bull" and z["high"] >= current_price:
            continue
        if direction == "bear" and z["low"] <= current_price:
            continue
        dist_pct = abs(current_price - z["mid"]) / current_price * 100
        if dist_pct <= proximity_pct:
            return z
    return None


# ── Accumulation detection (3-min) ────────────────────────────────────────────

def detect_accumulation(df_3m: pd.DataFrame, end_idx: int,
                        min_bars: int, max_bars: int,
                        atr_mult: float,
                        precomp_atr: Optional[np.ndarray] = None,
                        precomp_high: Optional[np.ndarray] = None,
                        precomp_low:  Optional[np.ndarray] = None) -> Optional[Dict]:
    """
    Scan backward from end_idx for a consolidation range.
    Returns dict {high, low, mid, start_idx, n_bars} or None.
    The range must be < atr_mult × ATR14 of the current bar.
    precomp_* arrays avoid recomputing per bar (pass full-day arrays).
    """
    if end_idx < min_bars + 14:
        return None

    if precomp_atr is not None:
        current_atr = float(precomp_atr[end_idx])
    else:
        atr_series = _atr(df_3m["High"], df_3m["Low"], df_3m["Close"], 14)
        current_atr = float(atr_series.iloc[end_idx])
    if np.isnan(current_atr) or current_atr <= 0:
        return None

    max_range = atr_mult * current_atr

    highs = precomp_high if precomp_high is not None else df_3m["High"].values
    lows  = precomp_low  if precomp_low  is not None else df_3m["Low"].values

    # Try decreasing window sizes from max_bars down to min_bars
    for n in range(max_bars, min_bars - 1, -1):
        start = end_idx - n + 1
        if start < 0:
            continue
        a_high = highs[start : end_idx + 1].max()
        a_low  = lows [start : end_idx + 1].min()
        if (a_high - a_low) <= max_range:
            return {
                "high"     : float(a_high),
                "low"      : float(a_low),
                "mid"      : float((a_high + a_low) / 2),
                "start_idx": start,
                "n_bars"   : n,
            }
    return None


# ── iFVG zone (OB2) detection ──────────────────────────────────────────────────
# Reference: ICT OB 2.0 PineScript (project archive).
# The iFVG zone = OB2 = last consecutive opposite candles BEFORE the manipulation
# displacement. Not a 3-candle gap pattern. When price retraces into this zone
# after distribution, that is the iFVG entry.
#
# Example (bull): manipulation sweep is bearish then snaps up.
#   - The candles just BEFORE that bullish snap = last bearish candles = OB2 zone.
#   - On distribution pullback, price returns to OB2 = iFVG support → enter long.

def find_ifvg_zone(df_3m: pd.DataFrame, manip_idx: int,
                   direction: str, lookback: int = 4) -> Optional[Dict]:
    """
    Find iFVG zone = last 1–N consecutive opposite-direction candles
    immediately before the manipulation displacement candle.

    Bull: last bearish (close < open) candles before manip_idx → zone = (low, high) of group
    Bear: last bullish (close > open) candles before manip_idx → zone = (low, high) of group

    Returns dict {low, high, mid} or None.
    """
    opens  = df_3m["Open"].values
    highs  = df_3m["High"].values
    lows   = df_3m["Low"].values
    closes = df_3m["Close"].values

    max_h: Optional[float] = None
    min_l: Optional[float] = None

    for i in range(1, min(lookback + 1, manip_idx + 1)):
        idx = manip_idx - i
        if idx < 0:
            break
        is_opposite = (
            (direction == "bull" and closes[idx] < opens[idx]) or
            (direction == "bear" and closes[idx] > opens[idx])
        )
        if is_opposite:
            if max_h is None or highs[idx] > max_h:
                max_h = highs[idx]
            if min_l is None or lows[idx] < min_l:
                min_l = lows[idx]
        else:
            break  # stop at first non-opposite candle

    if max_h is not None and min_l is not None and max_h > min_l:
        return {
            "low" : float(min_l),
            "high": float(max_h),
            "mid" : float((min_l + max_h) / 2),
        }
    return None


# ── Manipulation detection ─────────────────────────────────────────────────────

def detect_manipulation(df_3m: pd.DataFrame,
                        search_start: int,
                        accum_high: float,
                        accum_low: float,
                        direction: str,
                        sweep_min_pts: float,
                        vol_mult: float,
                        max_bars: int,
                        ifvg_max_bars: int,
                        precomp_vol_ma: Optional[np.ndarray] = None) -> Optional[Dict]:
    """
    Search for a manipulation sweep after search_start.

    Bull setup: bar sweeps BELOW accum_low by sweep_min_pts, then closes above.
    Bear setup: bar sweeps ABOVE accum_high by sweep_min_pts, then closes below.
    Volume on sweep bar must be >= vol_mult × 20-bar avg vol.

    Returns dict {manip_idx, extreme, direction, ifvg} or None.
    ifvg is the FVG created by the displacement that follows the sweep.
    precomp_vol_ma: optional precomputed vol MA array (avoids recomputing per call).
    """
    if precomp_vol_ma is None:
        precomp_vol_ma = _vol_ma(df_3m["Volume"], 20).values
    n = len(df_3m)

    for i in range(search_start, min(search_start + max_bars, n)):
        bar = df_3m.iloc[i]
        avg_vol = float(precomp_vol_ma[i])

        # Volume check
        if avg_vol > 0 and bar["Volume"] < vol_mult * avg_vol:
            continue

        if direction == "bull":
            # Sweep below accum_low, close back above
            if (bar["Low"] < accum_low - sweep_min_pts
                    and bar["Close"] > accum_low):
                extreme = float(bar["Low"])
                # Find FVG in the displacement that follows
                # iFVG zone = OB2 = last bearish candles BEFORE this displacement bar
                ifvg = find_ifvg_zone(df_3m, i, "bull", ifvg_max_bars)
                return {
                    "manip_idx": i,
                    "extreme"  : extreme,
                    "direction": "bull",
                    "ifvg"     : ifvg,
                }
        else:  # bear
            # Sweep above accum_high, close back below
            if (bar["High"] > accum_high + sweep_min_pts
                    and bar["Close"] < accum_high):
                extreme = float(bar["High"])
                # iFVG zone = OB2 = last bullish candles BEFORE the displacement bar
                ifvg = find_ifvg_zone(df_3m, i, "bear", ifvg_max_bars)
                return {
                    "manip_idx": i,
                    "extreme"  : extreme,
                    "direction": "bear",
                    "ifvg"     : ifvg,
                }
    return None


# ── iFVG entry check ───────────────────────────────────────────────────────────

def check_ifvg_entry(bar: pd.Series,
                     ifvg: Dict,
                     direction: str,
                     body_close_required: bool) -> bool:
    """
    Check if current bar enters the iFVG zone.
    If body_close_required=True, the candle body must close inside the zone.
    Otherwise a wick touch is sufficient.
    """
    fvg_lo = ifvg["low"]
    fvg_hi = ifvg["high"]
    o, h, l, c = bar["Open"], bar["High"], bar["Low"], bar["Close"]

    if direction == "bull":
        # Price must retrace down into iFVG zone
        body_lo = min(o, c)
        if body_close_required:
            return body_lo <= fvg_hi and c >= fvg_lo
        else:
            return l <= fvg_hi
    else:
        # Price must retrace up into iFVG zone
        body_hi = max(o, c)
        if body_close_required:
            return body_hi >= fvg_lo and c <= fvg_hi
        else:
            return h >= fvg_lo


# ── Main stateful signal generator ────────────────────────────────────────────

class PO3SignalGenerator:
    """
    Stateful bar-by-bar PO3 signal generator.

    Usage (live / backtesting bar-by-bar):
        gen = PO3SignalGenerator(params)
        gen.new_day()                    # call at start of each session
        for each 3-min bar:
            signal = gen.update(df_3m_so_far, df_15m_so_far, vix)
            if signal:
                enter_trade(signal)

    signal dict keys:
        direction   : 'bull' | 'bear'
        entry_price : float (spot price at iFVG midpoint)
        sl_spot     : float (spot SL at manipulation extreme)
        accum_high  : float
        accum_low   : float
        manip_extreme: float
        ifvg        : dict {low, high, mid}
        ob_zone     : dict {high, low, mid} or None
        bar_ts      : Timestamp
    """

    IDLE       = "idle"
    ACCUMULATE = "accumulate"
    MANIPULATE = "manipulate"
    DISTRIBUTE = "distribute"

    def __init__(self, params: Optional[Dict] = None):
        self.p = {**PO3_PARAMS, **(params or {})}
        self._reset()

    def _reset(self):
        self.state           = self.IDLE
        self.accum           = None   # detect_accumulation result
        self.manip           = None   # detect_manipulation result
        self.direction       = None
        self.active_ob       = None
        self._dist_confirmed = False  # True once accum boundary is closed
        self.signal_fired    = False  # one signal per day max
        self._day_atr        = None   # precomputed ATR array for current day
        self._day_vol_ma     = None   # precomputed vol MA array for current day
        self._day_high       = None   # precomputed High array for current day
        self._day_low        = None   # precomputed Low array for current day

    def new_day(self):
        """Call at the start of each trading session."""
        self._reset()

    def prepare_bars(self, df_3m_day: pd.DataFrame):
        """
        Precompute per-bar indicators for a full trading day.
        Call once per day before the bar loop (after new_day + prepare_day).
        """
        self._day_atr    = _atr(df_3m_day["High"], df_3m_day["Low"],
                                df_3m_day["Close"], 14).values
        self._day_vol_ma = _vol_ma(df_3m_day["Volume"], 20).values
        self._day_high   = df_3m_day["High"].values
        self._day_low    = df_3m_day["Low"].values

    def prepare_day(self, df_15m: pd.DataFrame,
                    cached: Optional[Dict] = None):
        """
        Precompute expensive per-day values (HTF bias, OB zones) once.
        Must be called after new_day() and before the per-bar update() loop.

        cached: optional precomputed dict with keys 'bias' and 'ob_zones'
                (pass from an outer loop to avoid recomputing across grid combos)
        """
        if cached is not None:
            self._bias     = cached.get("bias")
            self._ob_zones = cached.get("ob_zones", [])
            self._active_ob_cache = {}
            return

        if len(df_15m) < self.p["htf_sma_period"] + 5:
            self._bias     = None
            self._ob_zones = []
            self._active_ob_cache = {}
            return

        sma50     = _sma(df_15m["Close"], self.p["htf_sma_period"])
        htf_close = df_15m["Close"].iloc[-1]
        if pd.isna(sma50.iloc[-1]):
            self._bias = None
        else:
            self._bias = "bull" if htf_close > sma50.iloc[-1] else "bear"

        self._ob_zones = find_ob_zones(
            df_15m, self.p["ob_impulse_bars"], self.p["ob_max_retests"]
        )
        self._active_ob_cache = {}

    def _in_session(self, ts: pd.Timestamp) -> bool:
        h, m = ts.hour, ts.minute
        sh, sm = self.p["session_start_h"], self.p["session_start_m"]
        eh, em = self.p["session_end_h"],   self.p["session_end_m"]
        return (h * 60 + m) >= (sh * 60 + sm) and (h * 60 + m) <= (eh * 60 + em)

    def update(self,
               df_3m:  pd.DataFrame,
               df_15m: pd.DataFrame,
               vix:    float) -> Optional[Dict]:
        """
        Process the latest completed 3-min bar.
        df_3m / df_15m must contain only bars up to (and including) current bar.
        Returns signal dict on entry trigger, None otherwise.
        """
        if self.signal_fired:
            return None

        if len(df_3m) < 20:
            return None

        bar    = df_3m.iloc[-1]
        bar_ts = bar.name
        bar_i  = len(df_3m) - 1

        # Session filter
        if not self._in_session(bar_ts):
            return None

        # VIX filter
        if vix > self.p["vix_max"]:
            return None

        # HTF bias and OB zones — precomputed once per day by prepare_day()
        bias = getattr(self, "_bias", None)
        if bias is None:
            return None

        spot_price = float(bar["Close"])
        ob_zones   = getattr(self, "_ob_zones", [])
        active_ob  = nearest_ob(spot_price, ob_zones, bias, self.p["ob_proximity_pct"])

        # ── State: IDLE → look for accumulation ───────────────────────────────
        if self.state == self.IDLE:
            accum = detect_accumulation(
                df_3m, bar_i,
                self.p["accum_min_bars"],
                self.p["accum_max_bars"],
                self.p["accum_atr_mult"],
                precomp_atr=self._day_atr,
                precomp_high=self._day_high,
                precomp_low=self._day_low,
            )
            if accum is not None:
                # For bull: accum must be above/near OB (OB acts as support below)
                # For bear: accum must be below/near OB (OB acts as resistance above)
                near_ob = True
                if active_ob is not None:
                    if bias == "bull":
                        # OB should be below accum
                        near_ob = active_ob["high"] < accum["high"]
                    else:
                        # OB should be above accum
                        near_ob = active_ob["low"] > accum["low"]

                if near_ob:
                    self.state     = self.MANIPULATE
                    self.accum     = accum
                    self.direction = bias
                    self.active_ob = active_ob
            return None

        # ── State: MANIPULATE → look for sweep ────────────────────────────────
        if self.state == self.MANIPULATE:
            search_start = self.accum["start_idx"] + self.accum["n_bars"]
            search_start = max(search_start, bar_i - self.p["manip_max_bars"])

            manip = detect_manipulation(
                df_3m,
                search_start,
                self.accum["high"],
                self.accum["low"],
                self.direction,
                self.p["manip_sweep_min_pts"],
                self.p["manip_volume_mult"],
                self.p["manip_max_bars"],
                self.p["ifvg_max_bars"],
                precomp_vol_ma=self._day_vol_ma,
            )
            if manip is not None:
                self.manip = manip
                self.state = self.DISTRIBUTE
            else:
                # If too many bars passed with no manipulation, reset
                bars_since_accum = bar_i - (self.accum["start_idx"] + self.accum["n_bars"])
                if bars_since_accum > self.p["manip_max_bars"] + 5:
                    self.state = self.IDLE
            return None

        # ── State: DISTRIBUTE ─────────────────────────────────────────────────
        if self.state == self.DISTRIBUTE:
            direction  = self.direction
            accum_high = self.accum["high"]
            accum_low  = self.accum["low"]
            ifvg       = self.manip.get("ifvg")

            if ifvg is None:
                q1 = accum_low  + (accum_high - accum_low) * 0.25
                q3 = accum_low  + (accum_high - accum_low) * 0.75
                ifvg = {"low": q1, "high": q3, "mid": self.accum["mid"]}

            entry_mode = self.p.get("entry_mode", "ifvg")

            # ── BREAKOUT mode: enter the bar that CLOSES beyond accum boundary ──
            if entry_mode == "breakout":
                if self._dist_confirmed:
                    return None  # breakout bar already passed, missed entry
                bull_break = direction == "bull" and float(bar["Close"]) > accum_high
                bear_break = direction == "bear" and float(bar["Close"]) < accum_low
                if bull_break or bear_break:
                    self.signal_fired = True
                    # SL reference: just inside the broken boundary
                    sl_spot = accum_high if direction == "bull" else accum_low
                    return {
                        "direction"    : direction,
                        "entry_price"  : float(bar["Close"]),
                        "sl_spot"      : float(sl_spot),
                        "accum_high"   : accum_high,
                        "accum_low"    : accum_low,
                        "manip_extreme": self.manip["extreme"],
                        "ifvg"         : ifvg,
                        "ob_zone"      : self.active_ob,
                        "bar_ts"       : bar_ts,
                    }
                return None

            # ── iFVG mode (default): wait for retracement into iFVG zone ────────
            # Phase A: distribution not yet confirmed
            if not self._dist_confirmed:
                if direction == "bull" and float(bar["Close"]) > accum_high:
                    self._dist_confirmed = True
                elif direction == "bear" and float(bar["Close"]) < accum_low:
                    self._dist_confirmed = True
                else:
                    return None

            # Phase B: wait for retracement into iFVG zone
            in_ifvg = check_ifvg_entry(
                bar, ifvg, direction, self.p["ifvg_body_close"]
            )
            if not in_ifvg:
                return None

            self.signal_fired = True
            return {
                "direction"    : direction,
                "entry_price"  : float(bar["Close"]),
                "sl_spot"      : float(self.manip["extreme"]),
                "accum_high"   : accum_high,
                "accum_low"    : accum_low,
                "manip_extreme": self.manip["extreme"],
                "ifvg"         : ifvg,
                "ob_zone"      : self.active_ob,
                "bar_ts"       : bar_ts,
            }

        return None


# ── Batch signal generator for backtesting ────────────────────────────────────

def generate_signals(df_3m:  pd.DataFrame,
                     df_15m: pd.DataFrame,
                     vix_series: pd.Series,
                     params: Optional[Dict] = None) -> List[Dict]:
    """
    Run PO3 signal generator across a full dataset.
    One signal per trading day at most.

    df_3m / df_15m must have DatetimeIndex (IST-aware or naive).
    vix_series must be daily, indexed by date.

    Returns list of signal dicts (one per triggered trade).
    """
    gen  = PO3SignalGenerator(params)
    sigs = []

    # Group 3-min bars by trading day
    days = df_3m.groupby(df_3m.index.date)

    for day, day_3m in days:
        gen.new_day()
        vix = float(vix_series.get(day, np.nan))
        if np.isnan(vix):
            continue

        # 15-min data up to end of day
        df_15m_day = df_15m[df_15m.index.date <= day]
        if len(df_15m_day) < gen.p["htf_sma_period"] + 5:
            continue

        for i in range(20, len(day_3m)):
            df_3m_slice = day_3m.iloc[: i + 1]
            sig = gen.update(df_3m_slice, df_15m_day, vix)
            if sig:
                sigs.append(sig)
                break  # one trade per day

    return sigs
