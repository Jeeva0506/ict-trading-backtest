"""
PO3 Paper Trader — Indian Index Options (Intraday)
===================================================
Runs the PO3 strategy live using Zerodha 3-min + 15-min data.
All trades are PAPER (simulated). No real orders placed.

Session schedule (IST, weekdays only):
  08:50  Zerodha auth check / reload kite
  09:00  Start main loop
  09:30  Signal window opens
  13:00  Signal window closes (no new entries after 1 PM)
  15:05  Hard close all positions
  15:10  EOD summary to Telegram

Features:
  - PO3 state machine (accumulation → manipulation → iFVG entry)
  - Options entry: ATM CE/PE, nearest weekly expiry, Black-Scholes premium
  - SL: 30% premium | Trail: activates at +10%, trails 20% below peak
  - Chart auto-updated every bar (live/charts/po3_chart.html)
  - Telegram alerts on entry, exit, error

State persisted to: live/state/po3_state.json  (survives restart)
Log: live/logs/po3_paper.log
"""
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.option_pricer import bs_price, atm_strike, next_expiry, dte_to_expiry
from costs.zerodha          import round_trip_cost
from live.paper_trader      import _load_env, _get_kite
from live.telegram_alert    import IndianTelegram
from strategies.po3_strategy import (
    PO3SignalGenerator, PO3_PARAMS, find_ob_zones,
)

try:
    from live.po3_chart import PO3Chart
    CHART_ENABLED = True
except ImportError:
    CHART_ENABLED = False

IST = pytz.timezone("Asia/Kolkata")

# ── Paths ──────────────────────────────────────────────────────────────────────
LOG_DIR   = ROOT / "live" / "logs"
STATE_DIR = ROOT / "live" / "state"
LOG_DIR.mkdir(parents=True,   exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "po3_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "po3_paper.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("po3_paper")

# ── Instruments ────────────────────────────────────────────────────────────────
NIFTY_TOKEN  = 256265
NIFTY_SYMBOL = "NSE:NIFTY 50"


def _ist_now() -> datetime:
    return datetime.now(IST)


def _ist_today() -> date:
    return _ist_now().date()


class PO3PaperTrader:
    """
    Live paper trader for the PO3 strategy.

    Instantiate once; call run() to start the trading loop.
    """

    def __init__(self, params: Optional[Dict] = None):
        self.p      = {**PO3_PARAMS, **(params or {})}
        self.kite   = None
        self.tg     = None
        self.chart  = PO3Chart() if CHART_ENABLED else None
        self.gen    = PO3SignalGenerator(self.p)

        # In-session state
        self.position: Optional[Dict] = None
        self.df_3m_today  = pd.DataFrame()
        self.df_15m_hist  = pd.DataFrame()
        self.last_bar_ts  = None
        self.vix_today    = 15.0
        self.eod_done_day = None

        self._load_state()

    # ── State persistence ──────────────────────────────────────────────────────

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self.position = data.get("position")
                logger.info(f"State loaded. Position: {self.position is not None}")
            except Exception:
                self.position = None

    def _save_state(self):
        STATE_FILE.write_text(
            json.dumps({"position": self.position}, indent=2, default=str),
            encoding="utf-8",
        )

    # ── Zerodha data ──────────────────────────────────────────────────────────

    def _fetch_today_3m(self) -> pd.DataFrame:
        today = _ist_today()
        try:
            data = self.kite.historical_data(
                NIFTY_TOKEN,
                from_date=today,
                to_date=today,
                interval="3minute",
            )
            if not data:
                return pd.DataFrame()
            df = pd.DataFrame(data).set_index("date")
            df.index = pd.to_datetime(df.index)
            df.columns = [c.capitalize() for c in df.columns]
            return df.between_time("09:00", "15:35")
        except Exception as e:
            logger.error(f"3-min fetch error: {e}")
            return pd.DataFrame()

    def _fetch_15m_hist(self, days: int = 60) -> pd.DataFrame:
        today    = _ist_today()
        from_dt  = today - timedelta(days=days)
        try:
            data = self.kite.historical_data(
                NIFTY_TOKEN,
                from_date=from_dt,
                to_date=today,
                interval="15minute",
            )
            if not data:
                return pd.DataFrame()
            df = pd.DataFrame(data).set_index("date")
            df.index = pd.to_datetime(df.index)
            df.columns = [c.capitalize() for c in df.columns]
            return df.between_time("09:00", "15:35")
        except Exception as e:
            logger.error(f"15-min fetch error: {e}")
            return pd.DataFrame()

    def _fetch_vix(self) -> float:
        try:
            VIX_TOKEN = 264969
            quote = self.kite.quote([f"NSE:INDIA VIX"])
            return float(quote.get("NSE:INDIA VIX", {}).get("last_price", 15.0))
        except Exception:
            return 15.0

    # ── Trade execution (paper) ────────────────────────────────────────────────

    def _enter(self, sig: Dict):
        today      = _ist_today()
        expiry     = next_expiry(today)
        dte        = dte_to_expiry(today, expiry)
        if dte < 1:
            logger.warning("DTE < 1 — skipping entry (expiry too close)")
            return

        direction  = sig["direction"]
        spot       = sig["entry_price"]
        opt_type   = "call" if direction == "bull" else "put"
        strike     = atm_strike(spot, self.p["strike_step"])
        iv         = self.vix_today / 100.0

        prem = bs_price(spot, strike, self.p["rfr"], iv, dte, opt_type)
        if prem < 5:
            logger.warning(f"Premium too low (Rs{prem:.1f}) — skipping")
            return

        sl_prem = prem * (1 - self.p["sl_pct"])
        lots    = max(1, min(
            self.p["max_lots"],
            int(self.p["risk_per_trade"] / (prem * self.p["sl_pct"] * self.p["lot_size"]))
        ))
        qty  = lots * self.p["lot_size"]
        cost = round_trip_cost(prem, qty, opt_type)

        self.position = {
            "direction"  : direction,
            "opt_type"   : opt_type,
            "strike"     : strike,
            "expiry"     : str(expiry),
            "dte"        : dte,
            "entry_prem" : prem,
            "sl_prem"    : sl_prem,
            "tp_prem"    : prem * (1 + self.p["tp_pct"]),
            "peak_prem"  : prem,
            "trail_sl"   : None,
            "trail_active": False,
            "lots"       : lots,
            "qty"        : qty,
            "cost"       : cost,
            "entry_spot" : spot,
            "sl_spot"    : sig["sl_spot"],
            "ifvg"       : sig.get("ifvg"),
            "entry_ts"   : str(_ist_now()),
        }
        self._save_state()

        msg = (
            f"[PO3] {'▲ LONG CALL' if direction=='bull' else '▼ SHORT PUT'}\n"
            f"Strike : {strike} {opt_type.upper()}\n"
            f"Expiry : {expiry}\n"
            f"Premium: Rs{prem:.1f} × {lots} lots ({qty} qty)\n"
            f"SL     : Rs{sl_prem:.1f}  (−30%)\n"
            f"Spot SL: {sig['sl_spot']:.0f}\n"
            f"iFVG   : {sig['ifvg']['low']:.0f}–{sig['ifvg']['high']:.0f}\n"
            f"Capital@Risk: Rs{prem*self.p['sl_pct']*qty:,.0f}"
        )
        logger.info(msg.replace("\n", " | "))
        self.tg.send_info(msg)

    def _update_position(self, bar: pd.Series) -> Optional[str]:
        """
        Check SL / trail / TP against current bar. Return exit reason or None.
        Uses bar close as current premium proxy.
        """
        pos  = self.position
        if pos is None:
            return None

        spot   = float(bar["Close"])
        dte_r  = max(0.5 / 252, pos["dte"] - 0.5 / 252)
        iv_r   = (self.vix_today / 100.0) * 1.05
        cur_prem = bs_price(spot, pos["strike"], self.p["rfr"],
                            iv_r, dte_r, pos["opt_type"])
        cur_prem = max(cur_prem, 1.0)

        # SL
        if cur_prem <= pos["sl_prem"]:
            return "SL"

        # Update peak and trail
        if cur_prem > pos["peak_prem"]:
            pos["peak_prem"] = cur_prem

        gain_pct = (pos["peak_prem"] - pos["entry_prem"]) / pos["entry_prem"]
        if gain_pct >= self.p["trail_activate_pct"]:
            pos["trail_active"] = True

        if pos["trail_active"]:
            trail_sl = pos["peak_prem"] * (1 - self.p["trail_pct"])
            if pos["trail_sl"] is None or trail_sl > pos["trail_sl"]:
                pos["trail_sl"] = trail_sl
            if cur_prem <= pos["trail_sl"]:
                return "TRAIL"

        # TP (safety net)
        if cur_prem >= pos["tp_prem"]:
            return "TP"

        self._save_state()
        return None

    def _close_position(self, reason: str, bar: pd.Series):
        pos  = self.position
        if pos is None:
            return

        spot     = float(bar["Close"])
        dte_r    = max(0.5 / 252, pos["dte"] - 0.5 / 252)
        iv_r     = (self.vix_today / 100.0) * 1.05
        exit_prem = bs_price(spot, pos["strike"], self.p["rfr"],
                             iv_r, dte_r, pos["opt_type"])
        exit_prem = max(exit_prem, 1.0)

        gross = (exit_prem - pos["entry_prem"]) * pos["qty"]
        net   = gross - pos["cost"]
        emoji = "✅" if net > 0 else "❌"

        msg = (
            f"[PO3] {emoji} {reason} EXIT\n"
            f"Entry  : Rs{pos['entry_prem']:.1f}  Exit: Rs{exit_prem:.1f}\n"
            f"Peak   : Rs{pos['peak_prem']:.1f}   Trail active: {pos['trail_active']}\n"
            f"Net P&L: Rs{net:+,.0f}  (lots: {pos['lots']})\n"
            f"Spot   : {spot:.0f}"
        )
        logger.info(msg.replace("\n", " | "))
        self.tg.send_info(msg)
        self.position = None
        self._save_state()

    # ── Chart refresh ─────────────────────────────────────────────────────────

    def _refresh_chart(self, state_extra: Dict = None):
        if self.chart is None or self.df_3m_today.empty:
            return
        try:
            ob_zones = find_ob_zones(
                self.df_15m_hist,
                self.p["ob_impulse_bars"],
                self.p["ob_max_retests"],
            ) if not self.df_15m_hist.empty else []

            state = {
                "ob_zones" : ob_zones[:6],
                "direction": self.gen.direction,
            }
            if self.gen.accum:
                state["accum_high"] = self.gen.accum["high"]
                state["accum_low"]  = self.gen.accum["low"]
            if self.gen.manip:
                state["manip_idx"] = self.gen.manip.get("manip_idx")
                state["ifvg"]      = self.gen.manip.get("ifvg")
            if self.position:
                state["trade"] = {
                    "entry_price": self.position["entry_spot"],
                    "sl_spot"    : self.position["sl_spot"],
                    "entry_ts"   : self.position["entry_ts"],
                    "direction"  : self.position["direction"],
                }
            if state_extra:
                state.update(state_extra)

            self.chart.update(self.df_3m_today, self.df_15m_hist, state)
        except Exception as e:
            logger.debug(f"Chart update error: {e}")

    # ── EOD ───────────────────────────────────────────────────────────────────

    def _eod(self):
        if self.position:
            last_bar = self.df_3m_today.iloc[-1] if not self.df_3m_today.empty else None
            if last_bar is not None:
                self._close_position("EOD 15:05", last_bar)

        self.tg.send_info(
            "[PO3] EOD\n"
            "All positions closed. Chart saved.\n"
            f"Chart: live/charts/po3_chart.html"
        )
        self.eod_done_day = _ist_today()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        env = _load_env()
        self.kite = _get_kite()
        self.tg   = IndianTelegram(
            bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=env.get("TELEGRAM_CHAT_ID", ""),
            label="PO3-PAPER",
        )

        logger.info("PO3 Paper Trader started")
        self.tg.send_info("[PO3] Paper trader started. Waiting for market open...")

        # Preload 15-min history
        self.df_15m_hist = self._fetch_15m_hist(days=80)
        logger.info(f"15-min history loaded: {len(self.df_15m_hist)} bars")

        self.gen.new_day()
        last_poll_minute = -1

        while True:
            try:
                now   = _ist_now()
                today = now.date()

                # Skip weekends
                if today.weekday() >= 5:
                    time.sleep(3600)
                    continue

                # Reset at new day
                if today != getattr(self, "_last_day", None):
                    self._last_day = today
                    self.gen.new_day()
                    self.df_3m_today = pd.DataFrame()
                    self.last_bar_ts = None
                    self.vix_today   = self._fetch_vix()
                    self.df_15m_hist = self._fetch_15m_hist(days=80)
                    self.eod_done_day = None
                    logger.info(f"New day: {today}  VIX={self.vix_today:.1f}")

                # Pre-market sleep
                if now.hour < 9:
                    time.sleep(600)
                    continue

                # EOD
                h, m = now.hour, now.minute
                is_eod = h > 15 or (h == 15 and m >= 5)
                if is_eod:
                    if self.eod_done_day != today:
                        self.df_3m_today = self._fetch_today_3m()
                        self._eod()
                    time.sleep(600)
                    continue

                # Throttle: one poll per 3-min bar
                cur_min = h * 60 + m
                if cur_min == last_poll_minute:
                    time.sleep(15)
                    continue
                last_poll_minute = cur_min

                # Fetch latest 3-min bars
                self.df_3m_today = self._fetch_today_3m()
                if self.df_3m_today.empty:
                    time.sleep(30)
                    continue

                # Skip if no new bar
                latest_ts = self.df_3m_today.index[-1]
                if latest_ts == self.last_bar_ts:
                    time.sleep(15)
                    continue
                self.last_bar_ts = latest_ts
                bar = self.df_3m_today.iloc[-1]

                # Manage open position
                if self.position:
                    reason = self._update_position(bar)
                    if reason:
                        self._close_position(reason, bar)

                # Look for new entry (only if no position and within session window)
                if (not self.position
                        and 9 <= h < 13
                        and len(self.df_3m_today) >= 20
                        and len(self.df_15m_hist) >= self.p["htf_sma_period"] + 5):

                    sig = self.gen.update(
                        self.df_3m_today,
                        self.df_15m_hist,
                        self.vix_today,
                    )
                    if sig:
                        logger.info(
                            f"PO3 signal: {sig['direction']} | "
                            f"entry={sig['entry_price']:.0f} | "
                            f"sl={sig['sl_spot']:.0f}"
                        )
                        self._enter(sig)

                # Refresh chart every bar
                self._refresh_chart()

            except KeyboardInterrupt:
                logger.info("Stopped by user.")
                break
            except Exception as e:
                logger.exception(f"Loop error: {e}")
                self.tg.send_error(f"PO3 loop error: {e}")
                time.sleep(60)


if __name__ == "__main__":
    PO3PaperTrader().run()
