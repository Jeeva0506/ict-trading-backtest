from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List, Tuple, Any, Dict
import pandas as pd
from .utils import find_swing_highs_lows, aggregate_1m_to_3m


@dataclass
class TradeSignalICT:
    direction: str
    entry_time: Any
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_points: float

    exit_time: Any = None
    exit_price: float = None
    exit_reason: str = None
    pnl_points: float = 0.0


class StrategyICTClean:
    def __init__(
        self,
        sl_buffer=1.0,
        bos_required=2,
        swing_lookback=5,
        pullback_min_opposite_candles=3,
        ob2_retracement=0.65,
        min_sl_points=2.0,
        max_sl_points=25.0,
        debug=False,
    ):
        self.sl_buffer = sl_buffer
        self.bos_required = bos_required
        self.swing_lookback = swing_lookback
        self.pullback_min = pullback_min_opposite_candles
        self.ob2_retracement = ob2_retracement
        self.min_sl = min_sl_points
        self.max_sl = max_sl_points
        self.debug = debug

        self.debug_rows = []
        self._reset()

    # -----------------------------------------------------

    def _reset(self):
        self.trend = None
        self.bull_bos = 0
        self.bear_bos = 0

        self.sweep_active = False
        self.pullback_start = None
        self.pullback_end = None

        self.ob1 = None
        self.ob1_touch_idx = None
        self.ob1_touch_price = None

        self.choch = False
        self.choch_idx = None

        self.waiting_entry = False
        self.trade_taken = False
        self.trade = None

        self.tp1 = False
        self.tp2 = False
        self.position = 1.0

    # -----------------------------------------------------

    def _log(self, **k):
        if self.debug:
            self.debug_rows.append(k)

    # -----------------------------------------------------

    def _bull(self, r):
        return r.close > r.open

    def _bear(self, r):
        return r.close < r.open

    # -----------------------------------------------------

    def _bos(self, df, i, highs, lows):
        if i < 2:
            return None

        c = df.iloc[i]
        prev = df.iloc[i-1]

        sh = next((x for x in reversed(highs) if x <= i - self.swing_lookback), None)
        sl = next((x for x in reversed(lows) if x <= i - self.swing_lookback), None)
        if sh is None or sl is None:
            return None

        shv = df.iloc[sh].high
        slv = df.iloc[sl].low

        if c.close > shv and prev.close <= shv:
            return ("bullish", shv, sh, sl)

        if c.close < slv and prev.close >= slv:
            return ("bearish", slv, sh, sl)

        return None

    # -----------------------------------------------------

    def _opp_count(self, df, a, b, trend):
        if a is None or b is None:
            return 0
        n = 0
        for i in range(a, b+1):
            r = df.iloc[i]
            if trend == "bullish":
                if self._bear(r):
                    n += 1
            else:
                if self._bull(r):
                    n += 1
        return n

    # -----------------------------------------------------

    def _ob1(self, df, start, end, trend):
        part = df.loc[start:end]
        if len(part) < 6:
            return None

        m3 = aggregate_1m_to_3m(part)
        if len(m3) == 0:
            return None

        for _, r in m3.iloc[::-1].iterrows():
            if trend == "bullish" and r.close < r.open:
                low = min(r.low, part.low.min())
                return (r.high, low)
            if trend == "bearish" and r.close > r.open:
                high = max(r.high, part.high.max())
                return (high, r.low)

        return None

    # -----------------------------------------------------

    def _touch(self, c, z):
        return c.low <= z[0] and c.high >= z[1]

    # -----------------------------------------------------

    def _choch_level(self, df, start, i, trend):
        seg = df.iloc[start:i]
        if len(seg) == 0:
            return None
        return seg.high.max() if trend=="bullish" else seg.low.min()

    # -----------------------------------------------------

    def _ob2(self, trend, low, high):
        rng = high - low
        return high - rng*self.ob2_retracement if trend=="bullish" else low + rng*self.ob2_retracement

    # -----------------------------------------------------

    def _manage(self, df, i):
        t = self.trade
        c = df.iloc[i]

        # SL
        if t.direction=="bullish":
            if c.low <= t.stop_loss:
                t.exit_time=c.name
                t.exit_price=t.stop_loss
                t.exit_reason=("SL_BE_TP2" if self.tp2 else "SL_BE") if t.stop_loss==t.entry_price else "SL_HIT"
                t.pnl_points += (t.stop_loss-t.entry_price)*self.position
                self.position=0
                return True
        else:
            if c.high >= t.stop_loss:
                t.exit_time=c.name
                t.exit_price=t.stop_loss
                t.exit_reason=("SL_BE_TP2" if self.tp2 else "SL_BE") if t.stop_loss==t.entry_price else "SL_HIT"
                t.pnl_points += (t.entry_price-t.stop_loss)*self.position
                self.position=0
                return True

        # TP1 BE
        if not self.tp1:
            if t.direction=="bullish" and c.high>=t.tp1:
                self.tp1=True
                t.stop_loss=t.entry_price
            if t.direction=="bearish" and c.low<=t.tp1:
                self.tp1=True
                t.stop_loss=t.entry_price

        # TP2 50%
        if self.tp1 and not self.tp2:
            if t.direction=="bullish" and c.high>=t.tp2:
                self.tp2=True
                t.pnl_points+=(t.tp2-t.entry_price)*0.5
                self.position=0.5
            if t.direction=="bearish" and c.low<=t.tp2:
                self.tp2=True
                t.pnl_points+=(t.entry_price-t.tp2)*0.5
                self.position=0.5

        # TP3 close all
        if t.direction=="bullish" and c.high>=t.tp3:
            t.pnl_points+=(t.tp3-t.entry_price)*self.position
            t.exit_reason="TP3"
            t.exit_time=c.name
            t.exit_price=t.tp3
            return True

        if t.direction=="bearish" and c.low<=t.tp3:
            t.pnl_points+=(t.entry_price-t.tp3)*self.position
            t.exit_reason="TP3"
            t.exit_time=c.name
            t.exit_price=t.tp3
            return True

        return False

    # -----------------------------------------------------

    def generate_signals(self, df: pd.DataFrame):
        self._reset()
        out=[]

        highs,lows=find_swing_highs_lows(df,self.swing_lookback)
        cutoff = df.index[-1] - pd.Timedelta(minutes=15)

        for i in range(self.swing_lookback,len(df)):
            c=df.iloc[i]

            # manage trade
            if self.trade:
                if self._manage(df,i):
                    out.append(self.trade)
                    self.trade=None
                    self.trade_taken=True
                elif c.name >= cutoff:
                    # 15-min early close: force close at market price
                    t = self.trade
                    t.exit_time = c.name
                    t.exit_price = float(c.close)
                    if t.direction == "bullish":
                        t.pnl_points += (c.close - t.entry_price) * self.position
                    else:
                        t.pnl_points += (t.entry_price - c.close) * self.position
                    t.exit_reason = "SESSION_CLOSE"
                    out.append(t)
                    self.trade = None
                    self.trade_taken = True
                continue

            if self.trade_taken:
                continue

            # no new setups in last 15 min
            if c.name >= cutoff:
                continue

            bos=self._bos(df,i,highs,lows)
            if bos:
                d,level,sh,sl=bos
                self._log(event="BOS",time=c.name,dir=d,level=level)

                if d=="bullish":
                    self.bull_bos+=1
                    self.bear_bos=0
                    if self.bull_bos>=self.bos_required:
                        self.trend="bullish"
                else:
                    self.bear_bos+=1
                    self.bull_bos=0
                    if self.bear_bos>=self.bos_required:
                        self.trend="bearish"

                if self.trend and d!=self.trend:
                    self.sweep_active=True
                    self.pullback_start=i
                    self.ob1=None
                    self.ob1_touch_idx=None
                    self.ob1_touch_price=None
                    self.choch=False
                    self.choch_idx=None
                    self._log(event="SWEEP",time=c.name,trend=self.trend)

                continue

            if not self.trend or not self.sweep_active:
                continue

            # extend pullback
            self.pullback_end=i

            if not self.ob1:
                cnt=self._opp_count(df,self.pullback_start,self.pullback_end,self.trend)
                if cnt>=self.pullback_min:
                    s=df.index[self.pullback_start]
                    e=df.index[self.pullback_end]
                    self.ob1=self._ob1(df,s,e,self.trend)
                    if self.ob1:
                        self._log(event="OB1",time=c.name,zone=self.ob1)

            if not self.ob1:
                continue

            if self.ob1 and self.ob1_touch_idx is None:
                if self._touch(c,self.ob1):
                    self.ob1_touch_idx=i
                    self.ob1_touch_price=c.low if self.trend=="bullish" else c.high
                    self._log(event="OB1_TOUCH",time=c.name)
                continue

            if self.ob1_touch_idx and not self.choch:
                lvl=self._choch_level(df,self.ob1_touch_idx,i,self.trend)
                if lvl is None:
                    continue

                if self.trend=="bullish" and c.close>lvl:
                    self.choch=True
                    self.choch_idx=i
                    self._log(event="CHOCH",time=c.name)

                if self.trend=="bearish" and c.close<lvl:
                    self.choch=True
                    self.choch_idx=i
                    self._log(event="CHOCH",time=c.name)
                continue

            if self.choch and not self.trade_taken:
                # OB2 = last opposite-color 3m candle from touch → CHoCH (same logic as OB1)
                touch_ts = df.index[self.ob1_touch_idx]
                choch_ts = df.index[self.choch_idx]
                ob2_zone = self._ob1(df, touch_ts, choch_ts, self.trend)

                if ob2_zone is None:
                    continue

                if not self._touch(c, ob2_zone):
                    continue

                # market order entry: aim for zone midpoint (realistic fill for manual entry)
                entry = (ob2_zone[0] + ob2_zone[1]) / 2.0

                # SL = OB2 zone low - buffer (bullish) or OB2 zone high + buffer (bearish)
                if self.trend == "bullish":
                    sl = ob2_zone[1] - self.sl_buffer
                    risk = entry - sl
                else:
                    sl = ob2_zone[0] + self.sl_buffer
                    risk = sl - entry

                if risk<self.min_sl or risk>self.max_sl:
                    continue

                tp1=entry+risk if self.trend=="bullish" else entry-risk
                tp2=entry+risk*2 if self.trend=="bullish" else entry-risk*2
                tp3=entry+risk*3 if self.trend=="bullish" else entry-risk*3

                self.trade=TradeSignalICT(self.trend,c.name,entry,sl,tp1,tp2,tp3,risk)
                self.tp1=False
                self.tp2=False
                self.position=1
                self._log(event="ENTRY",time=c.name)

        # safety fallback: close any surviving trade at market
        if self.trade and self.trade.exit_reason is None:
            t = self.trade
            t.exit_time = df.index[-1]
            t.exit_price = float(df.iloc[-1].close)
            if t.direction == "bullish":
                t.pnl_points += (t.exit_price - t.entry_price) * self.position
            else:
                t.pnl_points += (t.entry_price - t.exit_price) * self.position
            t.exit_reason = "SESSION_CLOSE"
            out.append(t)
            self.trade = None

        return out
