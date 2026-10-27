"""
ORB Sweep strategy — dual-mode signal generation.

CALL direction: Momentum breakout above ORB High
  - Price closes above ORB High
  - Price above VWAP
  - Breaks previous swing high (momentum confirmation)
  - SL = ORB Low (wide, structural)

PUT direction: Sweep reversal from ORB High
  - Price sweeps above ORB High
  - Within N candles closes back below ORB High
  - Price below VWAP
  - Breaks previous swing low
  - SL = sweep high
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from orb_sweep.config import TradingConfig
from orb_sweep.indicators import (
    vwap, atr, sma_volume,
    find_swing_highs, find_swing_lows,
    resample_to_5m,
)


@dataclass
class ORBLevels:
    orb_high: float
    orb_low: float
    spot_at_open: float
    first_15m_range: float
    first_15m_range_pct: float


@dataclass
class TradeSignal:
    direction: str
    entry_spot: float
    stop_loss: float
    tp1: float
    tp2: float
    r_value: float
    strike: int
    vwap_at_entry: float
    sweep_price: float
    bar_time: pd.Timestamp
    entry_mode: str = ""


class ORBSweepStrategy:
    """
    Dual-mode ORB strategy:
      CALL = momentum breakout above ORB High
      PUT  = sweep reversal from ORB High false breakout
    """

    def __init__(self, config: TradingConfig):
        self.cfg = config
        self.orb: Optional[ORBLevels] = None
        self.regime_pass: bool = False
        self._sweep_state = {
            "above": False, "above_bar_idx": -1, "sweep_high": -np.inf,
        }
        self._breakout_state = {
            "broke_above": False, "broke_above_bar_idx": -1,
        }
        self._signal_fired_today = {"CALL": False, "PUT": False}
        self._day_vwap: Optional[pd.Series] = None
        self._day_vol_sma: Optional[pd.Series] = None
        self._day_swing_highs: Optional[pd.Series] = None
        self._day_swing_lows: Optional[pd.Series] = None
        self._day_index = None

    def reset_day(self):
        self.orb = None
        self.regime_pass = False
        self._sweep_state = {
            "above": False, "above_bar_idx": -1, "sweep_high": -np.inf,
        }
        self._breakout_state = {
            "broke_above": False, "broke_above_bar_idx": -1,
        }
        self._signal_fired_today = {"CALL": False, "PUT": False}
        self._day_vwap = None
        self._day_vol_sma = None
        self._day_swing_highs = None
        self._day_swing_lows = None
        self._day_index = None

    def compute_orb(self, df_1m: pd.DataFrame, trade_date) -> Optional[ORBLevels]:
        orb_mask = (
            (df_1m.index.date == trade_date) &
            (df_1m.index.time >= pd.Timestamp("09:15").time()) &
            (df_1m.index.time < pd.Timestamp("09:30").time())
        )
        orb_bars = df_1m[orb_mask]
        if len(orb_bars) < 10:
            return None
        orb_high = float(orb_bars["High"].max())
        orb_low = float(orb_bars["Low"].min())
        spot_open = float(orb_bars.iloc[0]["Open"])
        range_abs = orb_high - orb_low
        range_pct = range_abs / spot_open if spot_open > 0 else 0
        self.orb = ORBLevels(
            orb_high=orb_high, orb_low=orb_low, spot_at_open=spot_open,
            first_15m_range=range_abs, first_15m_range_pct=range_pct,
        )
        return self.orb

    def check_regime(self, df_1m: pd.DataFrame, trade_date) -> bool:
        if self.orb is None:
            return False
        if self.orb.first_15m_range_pct < self.cfg.min_range_pct:
            return False
        df_5m = resample_to_5m(df_1m[df_1m.index.date <= trade_date])
        atr_vals = atr(df_5m, self.cfg.atr_period)
        if atr_vals.empty or np.isnan(atr_vals.iloc[-1]):
            return False
        self.regime_pass = float(atr_vals.iloc[-1]) >= self.cfg.min_atr_5m
        return self.regime_pass

    def atm_strike(self, spot: float) -> int:
        step = self.cfg.strike_step
        return int(round(spot / step) * step)

    def precompute_day(self, df_today: pd.DataFrame):
        self._day_vwap = vwap(df_today)
        self._day_vol_sma = sma_volume(df_today, self.cfg.volume_sma_period)
        self._day_swing_highs = find_swing_highs(df_today["High"], n=self.cfg.swing_pivot_n)
        self._day_swing_lows = find_swing_lows(df_today["Low"], n=self.cfg.swing_pivot_n)
        self._day_index = df_today.index

    def _get_precomputed(self, local_pos: int):
        current_vwap = float(self._day_vwap.iloc[local_pos])

        vol_sma_val = float(self._day_vol_sma.iloc[local_pos])
        if np.isnan(vol_sma_val) or vol_sma_val == 0:
            volume_ok_fn = lambda v: True
        else:
            threshold = self.cfg.volume_multiplier * vol_sma_val
            volume_ok_fn = lambda v: v > threshold

        sh_slice = self._day_swing_highs.iloc[:local_pos]
        sl_slice = self._day_swing_lows.iloc[:local_pos]
        valid_sh = sh_slice.dropna()
        valid_sl = sl_slice.dropna()
        prev_swing_high = float(valid_sh.iloc[-1]) if len(valid_sh) > 0 else np.nan
        prev_swing_low = float(valid_sl.iloc[-1]) if len(valid_sl) > 0 else np.nan

        return current_vwap, volume_ok_fn, prev_swing_high, prev_swing_low

    def scan_bar(self, df_1m: pd.DataFrame, bar_idx: int,
                 local_pos: int = -1) -> Optional[TradeSignal]:
        if self.orb is None or not self.regime_pass:
            return None

        bar = df_1m.iloc[bar_idx]
        bar_time = df_1m.index[bar_idx]

        if bar_time.time() < pd.Timestamp("09:30").time():
            return None
        if bar_time.time() >= pd.Timestamp("15:15").time():
            return None

        high = float(bar["High"])
        low = float(bar["Low"])
        close = float(bar["Close"])
        volume = float(bar["Volume"])

        ss = self._sweep_state
        bs = self._breakout_state

        # ── Track sweep above ORB High (for PUT reversal) ─────────
        if high > self.orb.orb_high and not ss["above"]:
            ss["above"] = True
            ss["above_bar_idx"] = bar_idx
            ss["sweep_high"] = high
        elif ss["above"] and high > ss["sweep_high"]:
            ss["sweep_high"] = high

        if ss["above"] and (bar_idx - ss["above_bar_idx"]) > self.cfg.sweep_closeback_bars:
            ss["above"] = False
            ss["sweep_high"] = -np.inf

        # ── Track breakout above ORB High (for CALL momentum) ────
        if close > self.orb.orb_high and not bs["broke_above"]:
            bs["broke_above"] = True
            bs["broke_above_bar_idx"] = bar_idx

        # ── Identify candidates before computing indicators ──────
        put_candidate = (
            ss["above"]
            and (bar_idx - ss["above_bar_idx"]) <= self.cfg.sweep_closeback_bars
            and close < self.orb.orb_high
            and not self._signal_fired_today["PUT"]
        )

        call_candidate = (
            bs["broke_above"]
            and (bar_idx - bs["broke_above_bar_idx"]) <= 2
            and close > self.orb.orb_high
            and not self._signal_fired_today["CALL"]
        )

        if not put_candidate and not call_candidate:
            return None

        # ── Get indicators ────────────────────────────────────────
        if local_pos >= 0 and self._day_vwap is not None:
            current_vwap, volume_ok_fn, prev_swing_high, prev_swing_low = (
                self._get_precomputed(local_pos)
            )
        else:
            today_mask = df_1m.index.date == bar_time.date()
            today_bars = df_1m.loc[today_mask]
            vwap_series = vwap(today_bars)
            up_to = today_bars.index.get_loc(bar_time)
            if isinstance(up_to, slice):
                up_to = up_to.stop - 1
            current_vwap = float(vwap_series.iloc[up_to]) if up_to < len(vwap_series) else np.nan

            vol_sma_series = sma_volume(today_bars, self.cfg.volume_sma_period)
            vol_sma_val = float(vol_sma_series.iloc[up_to]) if up_to < len(vol_sma_series) else np.nan
            if np.isnan(vol_sma_val) or vol_sma_val == 0:
                volume_ok_fn = lambda v: True
            else:
                threshold = self.cfg.volume_multiplier * vol_sma_val
                volume_ok_fn = lambda v: v > threshold

            from orb_sweep.indicators import last_confirmed_swing_high, last_confirmed_swing_low
            prev_swing_high = last_confirmed_swing_high(
                today_bars["High"], up_to, n=self.cfg.swing_pivot_n
            )
            prev_swing_low = last_confirmed_swing_low(
                today_bars["Low"], up_to, n=self.cfg.swing_pivot_n
            )

        if np.isnan(current_vwap):
            return None

        volume_ok = volume_ok_fn(volume)

        # ── CALL: Momentum breakout above ORB High ───────────────
        if (
            call_candidate
            and close > current_vwap
            and not np.isnan(prev_swing_high)
            and high > prev_swing_high
            and volume_ok
        ):
            r_value = close - self.orb.orb_low
            if r_value <= 0:
                return None
            self._signal_fired_today["CALL"] = True
            bs["broke_above"] = False
            return TradeSignal(
                direction="CALL",
                entry_spot=close,
                stop_loss=self.orb.orb_low,
                tp1=close + self.cfg.tp1_r_multiple * r_value,
                tp2=close + self.cfg.tp2_r_multiple * r_value,
                r_value=r_value,
                strike=self.atm_strike(close),
                vwap_at_entry=current_vwap,
                sweep_price=self.orb.orb_low,
                bar_time=bar_time,
                entry_mode="breakout",
            )

        # ── PUT: Sweep above ORB High -> close back below ────────
        if (
            put_candidate
            and close < current_vwap
            and not np.isnan(prev_swing_low)
            and low < prev_swing_low
            and volume_ok
        ):
            r_value = ss["sweep_high"] - close
            if r_value <= 0:
                return None
            self._signal_fired_today["PUT"] = True
            ss["above"] = False
            return TradeSignal(
                direction="PUT",
                entry_spot=close,
                stop_loss=ss["sweep_high"],
                tp1=close - self.cfg.tp1_r_multiple * r_value,
                tp2=close - self.cfg.tp2_r_multiple * r_value,
                r_value=r_value,
                strike=self.atm_strike(close),
                vwap_at_entry=current_vwap,
                sweep_price=ss["sweep_high"],
                bar_time=bar_time,
                entry_mode="sweep_reversal",
            )

        return None
