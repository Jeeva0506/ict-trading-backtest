"""
Indian Options Paper Trader
============================
Runs the adaptive-risk ORB strategy in real-time using Zerodha API data.
All trades are PAPER (simulated) — no real orders are placed.

Scheduling:
  - Weekdays only, 9:00 to 15:30 IST
  - ORB window: 9:15 – 9:30 IST (15 min opening range)
  - Entry window: 9:30 – 13:00 IST (retest entry after ORB locked)
  - Tracking: bar-by-bar 5m until EOD at 15:30
  - Daily report: sent at 15:35 IST

Setup:
  1. Authenticate Zerodha: python data/zerodha_auth.py
  2. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.indian
  3. Run: python live/paper_trader.py

State file: live/state/position_state.json  (survives restart)
Journal   : live/reports/indian_paper_journal.xlsx
Log       : live/logs/paper_trader.log
"""
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.option_pricer import (
    bs_price, atm_strike, select_strike, skew_adjusted_iv,
    next_expiry, is_expiry_day, next_expiry_fn, is_expiry_day_fn,
    dte_to_expiry,
)
from costs.zerodha import round_trip_cost
from strategies.orb_structural_strategy import generate_structural_signals

from live.indian_journal   import IndianPaperJournal
from live.telegram_alert   import IndianTelegram
from live.indian_commands  import IndianCommandHandler
from live.nr7_tracker      import run_eod as nr7_run_eod, run_305pm as nr7_run_305pm

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = ROOT / "live" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "paper_trader.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("paper_trader")

# ── IST timezone ──────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> datetime:
    return datetime.now(IST)


def _ist_today() -> date:
    return _ist_now().date()


# ── Config loader ─────────────────────────────────────────────────────────────
ENV_FILE = ROOT.parent / "backtest_engine" / ".env.zerodha"
ENV_INDIAN = ROOT / ".env.indian"


def _load_env() -> dict:
    env = {}
    for fpath in (ENV_FILE, ENV_INDIAN):
        if fpath.exists():
            for line in fpath.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def _write_env_token(access_token: str):
    """Replace ZERODHA_ACCESS_TOKEN line in ENV_FILE with new token."""
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"{ENV_FILE} not found")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    updated = []
    found = False
    for line in lines:
        if line.strip().startswith("ZERODHA_ACCESS_TOKEN"):
            updated.append(f"ZERODHA_ACCESS_TOKEN={access_token}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"ZERODHA_ACCESS_TOKEN={access_token}")
    ENV_FILE.write_text("\n".join(updated) + "\n", encoding="utf-8")


# ── Strategy constants (must match backtest) ──────────────────────────────────
CAPITAL           = 1_000_000
BASELINE_FILTERS  = {"fvg", "sweep", "bar_quality", "ema_slope"}
APLUS_FILTERS     = BASELINE_FILTERS | {"body_strength", "vix_percentile", "atr"}
RISK_APLUS        = 20_000
RISK_REGULAR      = 10_000
LOTS_MAX_APLUS    = 15
LOTS_MAX_REGULAR  = 8
VIX_BUY_MIN       = 12.0
BEP_ACTIVATE      = 0.25   # lock breakeven once up 25% — extra protection
BEP_LOCK          = 0.01
TRAIL_ACTIVATE    = 0.10   # matches backtest orb_structural trail_activate_pct=0.10
TRAIL_PCT         = 0.20   # matches backtest trail_pct=0.20
MAX_RETEST_BARS   = 12
RETEST_TOLERANCE  = 10
MONTHLY_DD_LIMIT  = -0.05   # -5% of capital

# Market schedule (IST)
MARKET_OPEN      = (9,  0)
ORB_START        = (9, 15)
ORB_END          = (9, 30)
ENTRY_CUTOFF     = (13, 0)
MARKET_CLOSE     = (15, 30)
EOD_REPORT_TIME  = (15, 35)
POLL_INTERVAL_S  = 60        # poll Zerodha API every 60 s
BAR_SIZE         = "5minute" # 5m bars for signal generation

# Instruments
INSTRUMENTS = {
    "NIFTY": {
        "token":      256265,
        "lot_size":   50,
        "strike_step": 50,
        "rfr":        0.065,
        "itm_strikes": 0,
        "next_exp":   next_expiry,
        "is_exp":     is_expiry_day,
    },
    "FINNIFTY": {
        "token":      257801,
        "lot_size":   60,
        "strike_step": 50,
        "rfr":        0.065,
        "itm_strikes": 1,
        "next_exp":   next_expiry_fn,
        "is_exp":     is_expiry_day_fn,
    },
}
VIX_TOKEN = 264969   # INDIA VIX

# State file
STATE_FILE = ROOT / "live" / "state" / "position_state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


# ── Kite connector ────────────────────────────────────────────────────────────
def _get_kite():
    from kiteconnect import KiteConnect
    env = _load_env()
    key   = env.get("ZERODHA_API_KEY", "")
    token = env.get("ZERODHA_ACCESS_TOKEN", "")
    if not key or not token:
        raise ValueError("ZERODHA_API_KEY / ZERODHA_ACCESS_TOKEN missing. Run zerodha_auth.py")
    kite = KiteConnect(api_key=key)
    kite.set_access_token(token)
    return kite


# ── Historical data fetcher ───────────────────────────────────────────────────
def _fetch_bars(kite, token: int, interval: str, days_back: int = 10) -> pd.DataFrame:
    """Fetch recent bars from Zerodha (last N days)."""
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days_back)
    try:
        rows = kite.historical_data(token, start_dt, end_dt, interval,
                                    continuous=False, oi=False)
    except Exception as e:
        logger.error(f"historical_data({token}, {interval}) failed: {e}")
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).rename(columns={
        "date": "datetime", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime")[["Open", "High", "Low", "Close", "Volume"]]
    df.index = df.index.tz_localize(None)
    return df.sort_index()


def _fetch_today_bars(kite, token: int, interval: str) -> pd.DataFrame:
    """Fetch today's intraday bars only."""
    today = datetime.now().date()
    return _fetch_bars(kite, token, interval, days_back=2)


def _latest_vix(kite) -> float:
    """Get today's India VIX close (daily bar)."""
    try:
        rows = kite.historical_data(
            VIX_TOKEN,
            datetime.now() - timedelta(days=5),
            datetime.now(),
            "day", continuous=False, oi=False,
        )
        if rows:
            return float(rows[-1]["close"])
    except Exception as e:
        logger.warning(f"VIX fetch failed: {e}")
    return 15.0   # fallback


# ── ORB calculator ────────────────────────────────────────────────────────────
def _compute_orb(df_5m: pd.DataFrame) -> tuple:
    """
    Compute today's ORB high/low from the 5m bars that fall
    in the 9:15 – 9:30 window (inclusive).
    Returns (orb_h, orb_l) or (None, None) if insufficient data.
    """
    today = df_5m.index[-1].date()
    # Select bars with open-time in [9:15, 9:30)
    mask = (
        (df_5m.index.date == today) &
        (df_5m.index.time >= pd.Timestamp("09:15").time()) &
        (df_5m.index.time <= pd.Timestamp("09:25").time())
    )
    orb_bars = df_5m[mask]
    if orb_bars.empty:
        return None, None
    return float(orb_bars["High"].max()), float(orb_bars["Low"].min())


# ── Signal generator (intraday wrapper) ──────────────────────────────────────
def _classify_signal(df_5m: pd.DataFrame, df_daily: pd.DataFrame,
                     df_vix: pd.DataFrame) -> tuple:
    """
    Run both baseline and A+ signal generators on today's data.
    Returns (signal, quality) for the current bar or (None, None).
    """
    if len(df_5m) < 20:
        return None, None

    base  = generate_structural_signals(df_5m, df_daily, df_vix,
                                         active_filters=BASELINE_FILTERS)
    aplus = generate_structural_signals(df_5m, df_daily, df_vix,
                                         active_filters=APLUS_FILTERS)

    today = df_5m.index[-1].date()
    today_base  = base[ base.index.date  == today]
    today_aplus = aplus[aplus.index.date == today]

    today_base = today_base[today_base["signal"].notna()]
    if today_base.empty:
        return None, None

    sig   = today_base.iloc[0]["signal"]
    aplus_signal_dates = set(
        today_aplus[today_aplus["signal"].notna()].index.normalize()
    )
    entry_ts = today_base.index[0].normalize()
    quality  = "A+" if entry_ts in aplus_signal_dates else "regular"
    return sig, quality


# ── State persistence ─────────────────────────────────────────────────────────
def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _clear_state():
    _save_state({})


# ── Position tracker (one position max across both instruments) ───────────────
class ActivePosition:
    """Holds state for one open paper trade."""

    def __init__(self, trade_id: int, instrument: str, direction: str,
                 quality: str, inst_cfg: dict, entry_spot: float,
                 entry_prem: float, K: float, dte: int, iv: float,
                 orb_h: float, orb_l: float, vix: float,
                 signal_time: str, entry_time: str, bar_date: date):
        self.trade_id   = trade_id
        self.instrument = instrument
        self.direction  = direction
        self.quality    = quality
        self.cfg        = inst_cfg
        self.entry_spot = entry_spot
        self.entry_prem = entry_prem
        self.K          = K
        self.dte        = dte
        self.T          = dte / 365.0
        self.iv         = iv
        self.rfr        = inst_cfg["rfr"]
        self.lot_size   = inst_cfg["lot_size"]

        # Risk sizing
        risk_amt         = RISK_APLUS    if quality == "A+" else RISK_REGULAR
        max_lots         = LOTS_MAX_APLUS if quality == "A+" else LOTS_MAX_REGULAR
        option_sl_pts    = entry_prem * 0.25   # matches backtest sl_pct=0.25
        self.lots        = max(1, min(int(risk_amt / (option_sl_pts * self.lot_size)), max_lots))
        self.qty         = self.lots * self.lot_size
        self.option_sl   = option_sl_pts
        self.isl         = entry_prem - option_sl_pts

        # Exit thresholds
        self.bep_trig    = entry_prem * (1 + BEP_ACTIVATE)
        self.bep_sl      = entry_prem * (1 + BEP_LOCK)
        self.trail_trig  = entry_prem * (1 + TRAIL_ACTIVATE)

        # Running state
        self.peak        = entry_prem
        self.bep_active  = False
        self.trail_active = False
        self.peak_spot   = entry_spot

        # MFE / MAE tracking
        self.mfe_rs      = 0.0
        self.mae_rs      = 0.0

        # Metadata
        self.orb_h       = orb_h
        self.orb_l       = orb_l
        self.vix         = vix
        self.signal_time = signal_time
        self.entry_time  = entry_time
        self.bar_date    = bar_date

    def current_premium(self, spot: float) -> float:
        """Calculate current option premium using BS."""
        return bs_price(spot, self.K, self.T, self.rfr, self.iv, self.direction)

    def update_bar(self, bar_high: float, bar_low: float, bar_close: float) -> tuple:
        """
        Process one 5m bar. Returns (exit_reason, exit_prem) or (None, None).
        Also updates peak, MFE, MAE.
        """
        if self.direction == "call":
            opt_h = bs_price(bar_high,  self.K, self.T, self.rfr, self.iv, "call")
            opt_l = bs_price(bar_low,   self.K, self.T, self.rfr, self.iv, "call")
        else:
            opt_h = bs_price(bar_low,   self.K, self.T, self.rfr, self.iv, "put")
            opt_l = bs_price(bar_high,  self.K, self.T, self.rfr, self.iv, "put")

        if opt_h > self.peak:
            self.peak = opt_h
        if self.peak >= self.bep_trig:
            self.bep_active = True
        if self.peak >= self.trail_trig:
            self.trail_active = True

        # MFE / MAE
        upside = (opt_h - self.entry_prem) * self.qty
        downside = (opt_l - self.entry_prem) * self.qty
        if upside > self.mfe_rs:
            self.mfe_rs = upside
        if downside < self.mae_rs:
            self.mae_rs = downside

        # Effective SL
        if self.trail_active:
            eff_sl = max(self.bep_sl if self.bep_active else self.isl,
                         self.peak * (1 - TRAIL_PCT))
        elif self.bep_active:
            eff_sl = self.bep_sl
        else:
            eff_sl = self.isl

        if opt_l <= eff_sl:
            if self.trail_active and eff_sl > self.isl:
                return "Trail SL", eff_sl
            elif self.bep_active:
                return "BEP", eff_sl
            else:
                return "SL", eff_sl

        return None, None

    def to_dict(self) -> dict:
        """Serialise to JSON-safe dict for state persistence."""
        return {
            "trade_id":    self.trade_id,
            "instrument":  self.instrument,
            "direction":   self.direction,
            "quality":     self.quality,
            "entry_spot":  self.entry_spot,
            "entry_prem":  self.entry_prem,
            "K":           self.K,
            "dte":         self.dte,
            "iv":          self.iv,
            "orb_h":       self.orb_h,
            "orb_l":       self.orb_l,
            "vix":         self.vix,
            "peak":        self.peak,
            "bep_active":  self.bep_active,
            "trail_active": self.trail_active,
            "mfe_rs":      self.mfe_rs,
            "mae_rs":      self.mae_rs,
            "lots":        self.lots,
            "qty":         self.qty,
            "option_sl":   self.option_sl,
            "isl":         self.isl,
            "bep_trig":    self.bep_trig,
            "bep_sl":      self.bep_sl,
            "trail_trig":  self.trail_trig,
            "signal_time": self.signal_time,
            "entry_time":  self.entry_time,
            "bar_date":    str(self.bar_date),
        }


# ── Main bot class ─────────────────────────────────────────────────────────────
class IndianPaperBot:

    def __init__(self):
        env = _load_env()
        self.kite = _get_kite()

        tg_token   = env.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat_id = env.get("TELEGRAM_CHAT_ID", "")
        self.telegram = IndianTelegram(tg_token, tg_chat_id, label="NIFTY-PAPER")
        self.journal  = IndianPaperJournal(capital=CAPITAL)

        # Command handler (long-polling daemon)
        self.cmd = IndianCommandHandler(tg_token, tg_chat_id, label="NIFTY-PAPER")
        self.cmd.get_status    = self._cb_status
        self.cmd.get_positions = self._cb_positions
        self.cmd.get_capital   = self._cb_capital
        self.cmd.get_trades    = self._cb_trades
        self.cmd.get_risk      = self._cb_risk
        self.cmd.get_orb       = self._cb_orb
        self.cmd.do_pause      = self._cb_pause
        self.cmd.do_resume     = self._cb_resume
        self.cmd.do_report     = self._cb_report
        self.cmd.do_token_auth = self._cb_token_auth
        self.cmd.start_polling()

        # Running state
        self.position: dict = {}            # instrument -> ActivePosition
        self.orb_levels: dict = {}          # instrument -> {"h": float, "l": float}
        self.signal_seen_today: set = set() # instruments already traded today
        self.orb_locked_today = False
        self.last_poll_minute  = -1
        self.running_capital   = float(CAPITAL)
        self.monthly_pnl: dict = {}         # (year, month) -> float
        self.month_paused: set = set()

        # Load persisted position from prior session
        saved = _load_state()
        if saved.get("position"):
            logger.info("Restoring position from state file...")
            self._restore_position(saved["position"])
        if saved.get("running_capital"):
            self.running_capital = float(saved["running_capital"])

        logger.info(f"Paper bot initialised. Capital=Rs{CAPITAL:,}  "
                    f"Running=Rs{self.running_capital:,.0f}")

    # ── State persistence ──────────────────────────────────────────────────────
    def _persist(self):
        state = {
            "running_capital": self.running_capital,
            "position": {
                inst: pos.to_dict()
                for inst, pos in self.position.items()
            },
        }
        _save_state(state)

    def _restore_position(self, pos_dict: dict):
        """Restore ActivePosition objects from saved state dict."""
        for inst, pd_ in pos_dict.items():
            cfg = INSTRUMENTS.get(inst)
            if not cfg:
                continue
            pos = ActivePosition.__new__(ActivePosition)
            pos.__dict__.update(pd_)
            pos.cfg      = cfg
            pos.lot_size = cfg["lot_size"]
            pos.rfr      = cfg["rfr"]
            pos.bar_date = date.fromisoformat(pd_["bar_date"])
            pos.T        = pos.dte / 365.0
            self.position[inst]         = pos
            self.signal_seen_today.add(inst)
        logger.info(f"Restored {len(self.position)} open positions: {list(self.position.keys())}")

    # ── Command handler callbacks ──────────────────────────────────────────────
    def _cb_status(self) -> dict:
        today = _ist_today()
        month_key = (today.year, today.month)
        return {
            "running_capital": self.running_capital,
            "return_pct":      (self.running_capital - CAPITAL) / CAPITAL * 100,
            "open_positions":  len(self.position),
            "monthly_pnl":     self.monthly_pnl.get(month_key, 0.0),
            "month_paused":    month_key in self.month_paused,
        }

    def _cb_positions(self) -> list:
        result = []
        for inst, pos in self.position.items():
            try:
                df_5m = _fetch_bars(self.kite, INSTRUMENTS[inst]["token"],
                                    "5minute", days_back=1)
                spot = float(df_5m["Close"].iloc[-1]) if not df_5m.empty else pos.entry_spot
            except Exception:
                spot = pos.entry_spot
            cur_prem = pos.current_premium(spot)
            live_pnl = (cur_prem - pos.entry_prem) * pos.qty
            result.append({
                "instrument":   inst,
                "direction":    pos.direction,
                "quality":      pos.quality,
                "strike":       pos.K,
                "dte":          pos.dte,
                "entry_prem":   pos.entry_prem,
                "current_prem": round(cur_prem, 2),
                "peak":         pos.peak,
                "lots":         pos.lots,
                "qty":          pos.qty,
                "live_pnl":     round(live_pnl, 2),
                "bep_active":   pos.bep_active,
                "trail_active": pos.trail_active,
            })
        return result

    def _cb_capital(self) -> dict:
        today = _ist_today()
        month_key = (today.year, today.month)
        net_pnl = self.running_capital - CAPITAL
        return {
            "initial":     CAPITAL,
            "running":     self.running_capital,
            "net_pnl":     net_pnl,
            "return_pct":  net_pnl / CAPITAL * 100,
            "monthly_pnl": self.monthly_pnl.get(month_key, 0.0),
        }

    def _cb_trades(self) -> dict:
        return self.journal.get_today_stats(str(_ist_today()))

    def _cb_risk(self) -> dict:
        today = _ist_today()
        month_key = (today.year, today.month)
        return {
            "monthly_pnl":       self.monthly_pnl.get(month_key, 0.0),
            "monthly_dd_limit_rs": CAPITAL * MONTHLY_DD_LIMIT,
            "monthly_dd_pct":    MONTHLY_DD_LIMIT * 100,
            "month_paused":      month_key in self.month_paused,
            "vix_buy_min":       VIX_BUY_MIN,
            "capital":           self.running_capital,
        }

    def _cb_orb(self) -> dict:
        return self.orb_levels if self.orb_locked_today else {}

    def _cb_pause(self):
        today = _ist_today()
        month_key = (today.year, today.month)
        self.month_paused.add(month_key)
        logger.info("Trading PAUSED via Telegram command.")

    def _cb_resume(self):
        today = _ist_today()
        month_key = (today.year, today.month)
        self.month_paused.discard(month_key)
        logger.info("Trading RESUMED via Telegram command.")

    def _cb_report(self):
        today = _ist_today()
        stats   = self.journal.get_today_stats(str(today))
        cum_dd  = (self.running_capital - CAPITAL) / CAPITAL * 100
        self.telegram.send_daily_report(stats, self.running_capital, cum_dd)

    def _cb_token_auth(self, request_token: str) -> str:
        """
        Called when user sends /token REQUEST_TOKEN via Telegram.
        Exchanges request_token for access_token, writes to .env.zerodha,
        and reloads the live kite connection.
        """
        from kiteconnect import KiteConnect
        env = _load_env()
        api_key    = env.get("ZERODHA_API_KEY", "")
        api_secret = env.get("ZERODHA_API_SECRET", "")
        if not api_key or not api_secret:
            return "[NIFTY-PAPER] Auth FAILED: ZERODHA_API_KEY or ZERODHA_API_SECRET missing in .env.zerodha"

        kite_tmp = KiteConnect(api_key=api_key)
        try:
            data         = kite_tmp.generate_session(request_token, api_secret=api_secret)
            access_token = data["access_token"]
        except Exception as e:
            logger.error(f"generate_session failed: {e}")
            return f"[NIFTY-PAPER] Auth FAILED: {e}\n\nCheck request_token is correct and not expired."

        # Write new token to .env.zerodha
        try:
            _write_env_token(access_token)
        except Exception as e:
            logger.error(f"Failed to write token to .env: {e}")
            return f"[NIFTY-PAPER] Token generated but FAILED to save: {e}"

        # Reload live kite connection
        try:
            self.kite = _get_kite()
            profile = self.kite.profile()
            user = profile.get("user_name", "")
            logger.info(f"Zerodha token refreshed via Telegram. User: {user}")
            return (
                f"[NIFTY-PAPER] Zerodha auth SUCCESS\n"
                f"User    : {user}\n"
                f"Token   : ...{access_token[-6:]}\n"
                f"Saved to: .env.zerodha\n"
                f"Kite    : reloaded and verified"
            )
        except Exception as e:
            logger.error(f"Kite reload after token auth failed: {e}")
            return f"[NIFTY-PAPER] Token saved but kite reload FAILED: {e}"

    # ── Market hours helpers ───────────────────────────────────────────────────
    def _is_trading_day(self) -> bool:
        return _ist_today().weekday() < 5   # Mon–Fri

    def _time_in(self, start: tuple, end: tuple) -> bool:
        now = _ist_now()
        sh, sm = start; eh, em = end
        after  = now.hour > sh or (now.hour == sh and now.minute >= sm)
        before = now.hour < eh or (now.hour == eh and now.minute <= em)
        return after and before

    # ── Data helpers ───────────────────────────────────────────────────────────
    def _get_instrument_data(self, inst: str) -> tuple:
        """Return (df_5m_today, df_daily, df_vix_daily, current_spot, current_vix)."""
        cfg     = INSTRUMENTS[inst]
        token   = cfg["token"]

        df_5m   = _fetch_bars(self.kite, token, "5minute", days_back=30)
        df_daily = _fetch_bars(self.kite, token, "day",    days_back=400)
        df_vix   = _fetch_bars(self.kite, VIX_TOKEN, "day", days_back=400)

        current_spot = float(df_5m["Close"].iloc[-1]) if not df_5m.empty else 0
        current_vix  = float(df_vix["Close"].iloc[-1]) if not df_vix.empty else 15.0

        return df_5m, df_daily, df_vix, current_spot, current_vix

    # ── ORB lock ───────────────────────────────────────────────────────────────
    def _try_lock_orb(self):
        """Lock ORB levels at 9:30 for all instruments."""
        if self.orb_locked_today:
            return
        now = _ist_now()
        # Lock at 9:30 or catch up if bot started late (any time past 9:30)
        past_orb_time = (now.hour > 9) or (now.hour == 9 and now.minute >= 30)
        if not past_orb_time:
            return

        for inst, cfg in INSTRUMENTS.items():
            try:
                df_5m = _fetch_bars(self.kite, cfg["token"], "5minute", days_back=2)
                if df_5m.empty:
                    continue
                orb_h, orb_l = _compute_orb(df_5m)
                if orb_h and orb_l:
                    self.orb_levels[inst] = {"h": orb_h, "l": orb_l}
                    logger.info(f"ORB locked {inst}: H={orb_h:.0f}  L={orb_l:.0f}  "
                                f"Range={orb_h - orb_l:.0f}pts")
            except Exception as e:
                logger.error(f"ORB lock failed for {inst}: {e}")

        if self.orb_levels:
            self.orb_locked_today = True
            self.telegram.send_info(
                "\n".join([
                    f"ORB locked  {inst}: H={v['h']:.0f}  L={v['l']:.0f}  "
                    f"Rng={v['h']-v['l']:.0f}pts"
                    for inst, v in self.orb_levels.items()
                ])
            )

    # ── Signal check ───────────────────────────────────────────────────────────
    def _check_signals(self):
        """Look for ORB breakout + retest entry on each instrument."""
        if not self._time_in(ORB_END, ENTRY_CUTOFF):
            return
        if not self.orb_locked_today:
            return

        today = _ist_today()
        month_key = (today.year, today.month)
        if month_key in self.month_paused or self.cmd.is_paused:
            return

        for inst, cfg in INSTRUMENTS.items():
            if inst in self.signal_seen_today:
                continue
            if inst in self.position:
                continue

            orb = self.orb_levels.get(inst)
            if not orb:
                continue

            try:
                df_5m, df_daily, df_vix, spot, vix = self._get_instrument_data(inst)
                if df_5m.empty:
                    continue
            except Exception as e:
                logger.error(f"Data fetch failed for {inst}: {e}")
                continue

            # VIX gate
            if vix < VIX_BUY_MIN:
                continue

            # Get signal from strategy
            try:
                sig, quality = _classify_signal(df_5m, df_daily, df_vix)
            except Exception as e:
                logger.error(f"Signal classify failed for {inst}: {e}")
                continue

            if sig is None:
                continue

            # Retest check on the latest 5m bar
            last_bar  = df_5m.iloc[-1]
            last_time = df_5m.index[-1]
            orb_h, orb_l = orb["h"], orb["l"]

            if sig == "call":
                # Looking for bar that touched ORB_H and closed above it
                touched = last_bar["Low"]  <= orb_h + RETEST_TOLERANCE
                bounced = last_bar["Close"] > orb_h
                if not (touched and bounced):
                    continue
                entry_spot = float(last_bar["Close"])
            else:
                touched = last_bar["High"] >= orb_l - RETEST_TOLERANCE
                bounced = last_bar["Close"] < orb_l
                if not (touched and bounced):
                    continue
                entry_spot = float(last_bar["Close"])

            # Price the option
            step = cfg["strike_step"]
            itm  = cfg["itm_strikes"]
            K    = select_strike(entry_spot, sig, step, itm)
            dte  = (cfg["next_exp"](today) - today).days
            if dte < 2:
                logger.info(f"{inst}: DTE={dte} too close, skipping")
                continue
            T    = dte / 365.0
            iv   = skew_adjusted_iv(vix, sig, itm) / 100.0
            ep   = bs_price(entry_spot, K, T, cfg["rfr"], iv, sig)

            if ep < 1.0:
                continue

            now_str = _ist_now().strftime("%Y-%m-%d %H:%M:%S")
            sig_str = last_time.strftime("%Y-%m-%d %H:%M:%S")

            pos = ActivePosition(
                trade_id=0,   # filled after journal entry
                instrument=inst,
                direction=sig,
                quality=quality,
                inst_cfg=cfg,
                entry_spot=entry_spot,
                entry_prem=ep,
                K=K, dte=dte, iv=iv,
                orb_h=orb_h, orb_l=orb_l,
                vix=vix,
                signal_time=sig_str,
                entry_time=now_str,
                bar_date=today,
            )

            # Log to journal
            trade_meta = {
                "date":         str(today),
                "signal_time":  sig_str,
                "entry_time":   now_str,
                "instrument":   inst,
                "direction":    sig,
                "quality":      quality,
                "orb_h":        orb_h,
                "orb_l":        orb_l,
                "entry_spot":   entry_spot,
                "strike":       K,
                "dte":          dte,
                "iv_used":      round(iv * 100, 2),
                "entry_prem":   round(ep, 2),
                "option_sl":    round(pos.option_sl, 2),
                "lots":         pos.lots,
                "qty":          pos.qty,
                "vix":          round(vix, 2),
                "bep_activate": BEP_ACTIVATE,
                "trail_activate": TRAIL_ACTIVATE,
            }
            tid = self.journal.add_trade(trade_meta)
            pos.trade_id = tid

            self.position[inst] = pos
            self.signal_seen_today.add(inst)
            self._persist()

            logger.info(f"ENTRY  {inst} {sig.upper()} {quality}  "
                        f"K={K:.0f}  DTE={dte}  ep=Rs{ep:.2f}  "
                        f"lots={pos.lots}  risk=Rs{pos.option_sl*pos.qty:.0f}")
            self.telegram.send_entry(trade_meta)

    # ── Position tracker ───────────────────────────────────────────────────────
    def _track_positions(self):
        """Evaluate open positions against latest bar."""
        if not self.position:
            return

        today = _ist_today()
        eod   = self._time_in((15, 5), (15, 35))

        for inst in list(self.position.keys()):
            pos = self.position[inst]

            # Force EOD close
            if eod:
                try:
                    df_5m = _fetch_bars(self.kite, INSTRUMENTS[inst]["token"],
                                        "5minute", days_back=1)
                    spot  = float(df_5m["Close"].iloc[-1]) if not df_5m.empty else pos.entry_spot
                except Exception:
                    spot = pos.entry_spot
                exit_prem = bs_price(spot, pos.K, pos.T, pos.rfr, pos.iv, pos.direction)
                self._close_position(inst, pos, exit_prem, spot, "EOD")
                continue

            # Normal bar tracking
            try:
                df_5m = _fetch_bars(self.kite, INSTRUMENTS[inst]["token"],
                                    "5minute", days_back=1)
                if df_5m.empty:
                    continue
                last = df_5m.iloc[-1]
                bar_h = float(last["High"])
                bar_l = float(last["Low"])
                bar_c = float(last["Close"])
            except Exception as e:
                logger.error(f"Tracking data fetch failed {inst}: {e}")
                continue

            reason, exit_prem = pos.update_bar(bar_h, bar_l, bar_c)
            self._persist()   # write peak/bep_active/trail_active to disk every bar
            if reason:
                self._close_position(inst, pos, exit_prem, bar_c, reason)

    def _close_position(self, inst: str, pos: ActivePosition,
                        exit_prem: float, exit_spot: float, reason: str):
        """Close position, update journal and run capital."""
        gross   = (exit_prem - pos.entry_prem) * pos.qty
        cost    = round_trip_cost(pos.entry_prem, max(exit_prem, 0.05), pos.qty)
        net     = gross - cost

        today      = _ist_today()
        month_key  = (today.year, today.month)
        self.monthly_pnl[month_key] = self.monthly_pnl.get(month_key, 0.0) + net
        if self.monthly_pnl[month_key] < CAPITAL * MONTHLY_DD_LIMIT:
            self.month_paused.add(month_key)
            logger.warning(f"Monthly DD limit hit — pausing {today.year}-{today.month:02d}")
            self.telegram.send_info(
                f"Monthly DD limit hit (Rs{self.monthly_pnl[month_key]:,.0f}). "
                f"No new entries for rest of {today.strftime('%B %Y')}."
            )

        self.running_capital += net
        cum_dd_pct = (self.running_capital - CAPITAL) / CAPITAL * 100

        now_str = _ist_now().strftime("%Y-%m-%d %H:%M:%S")
        exit_data = {
            "exit_time":   now_str,
            "exit_spot":   exit_spot,
            "exit_prem":   round(exit_prem, 2),
            "exit_reason": reason,
            "peak_prem":   round(pos.peak, 2),
            "gross_rs":    round(gross, 2),
            "cost_rs":     round(cost, 2),
            "net_rs":      round(net, 2),
            "mfe_rs":      round(pos.mfe_rs, 2),
            "mae_rs":      round(pos.mae_rs, 2),
            "be_hit":      pos.bep_active,
            "trail_hit":   pos.trail_active,
        }

        self.journal.close_trade(
            pos.trade_id, exit_data,
            self.running_capital,
            self.monthly_pnl.get(month_key, 0.0),
        )
        self.telegram.send_exit(pos.trade_id, exit_data, inst, pos.direction)

        logger.info(f"EXIT  {inst} {pos.direction.upper()}  reason={reason}  "
                    f"net=Rs{net:+,.0f}  capital=Rs{self.running_capital:,.0f}")

        del self.position[inst]
        self._persist()

    # ── End-of-day ─────────────────────────────────────────────────────────────
    def _end_of_day(self):
        """Send daily report and refresh journal at 15:35."""
        today   = _ist_today()
        stats   = self.journal.get_today_stats(str(today))
        cum_dd  = (self.running_capital - CAPITAL) / CAPITAL * 100
        self.journal.update_daily_summary(str(today), self.running_capital, cum_dd)
        self.journal.update_dashboard(self.running_capital)
        self.telegram.send_daily_report(stats, self.running_capital, cum_dd)

        # Weekly report on Friday
        if today.weekday() == 4:   # Friday
            week_start = today - timedelta(days=4)
            week_stats = {
                "week_start": str(week_start),
                "week_end":   str(today),
                "trades":     stats["trades"],
                "win_rate":   stats["win_rate"],
                "net_rs":     stats["net_rs"],
            }
            self.telegram.send_weekly_report(week_stats, self.running_capital)

        logger.info(f"EOD report sent. Capital=Rs{self.running_capital:,.0f}  "
                    f"CumDD={cum_dd:.2f}%")

    # ── Main loop ──────────────────────────────────────────────────────────────
    def run(self):
        logger.info("=" * 60)
        logger.info("  Indian Options Paper Trader  STARTED")
        logger.info(f"  Capital: Rs{CAPITAL:,}  |  Running: Rs{self.running_capital:,.0f}")
        logger.info("=" * 60)
        self.telegram.send_info("Paper trader STARTED. Watching for ORB signals.")

        eod_sent_today      = None
        daily_reset_done    = None
        token_reminder_sent = None
        token_reload_done   = None
        nr7_run_done        = None
        nr7_305pm_done      = None

        while True:
            try:
                now   = _ist_now()
                today = _ist_today()

                # Skip weekends
                if not self._is_trading_day():
                    time.sleep(300)
                    continue

                # 8:30 AM — send Zerodha login URL and /token instructions
                if (now.hour == 8 and now.minute >= 30 and now.minute < 40
                        and token_reminder_sent != today):
                    token_reminder_sent = today
                    env = _load_env()
                    api_key = env.get("ZERODHA_API_KEY", "")
                    # & must be &amp; in HTML parse_mode or Telegram rejects the message
                    login_url = (
                        f"https://kite.zerodha.com/connect/login"
                        f"?api_key={api_key}&amp;v=3"
                    )
                    self.telegram.send_info(
                        f"Good morning! Zerodha auth required.\n\n"
                        f"1. Open this link and log in:\n{login_url}\n\n"
                        f"2. After login, copy the <b>request_token</b> from the redirect URL\n"
                        f"   (value after ?request_token=)\n\n"
                        f"3. Reply here: /token YOUR_REQUEST_TOKEN\n\n"
                        f"Bot reloads automatically at 8:50 AM."
                    )

                # 8:50 AM — reload .env.zerodha and reinitialise Kite connection
                if (now.hour == 8 and now.minute == 50
                        and token_reload_done != today):
                    token_reload_done = today
                    try:
                        self.kite = _get_kite()
                        self.kite.profile()   # verify token works
                        logger.info("Zerodha token reloaded OK")
                        self.telegram.send_info(
                            "Zerodha token reloaded. Ready for 9:00 AM."
                        )
                    except Exception as e:
                        logger.error(f"Token reload failed: {e}")
                        self.telegram.send_error(
                            f"Zerodha token reload FAILED: {e}\n"
                            "Update ZERODHA_ACCESS_TOKEN in .env.zerodha."
                        )

                # Daily reset at 9:00 AM
                if daily_reset_done != today and now.hour == 9 and now.minute < 5:
                    self.orb_locked_today   = False
                    self.orb_levels         = {}
                    self.signal_seen_today  = set()
                    daily_reset_done        = today
                    logger.info(f"--- New trading day: {today} ---")
                    self.telegram.send_info(f"New day: {today}. Waiting for ORB (9:15-9:30)...")

                # Before 8:40 AM — sleep in chunks (wake at 8:40 for token reminder)
                if now.hour < 8 or (now.hour == 8 and now.minute < 40):
                    wake_at_840 = now.replace(hour=8, minute=40, second=0, microsecond=0)
                    sleep_s = max(60, int((wake_at_840 - now).total_seconds()))
                    logger.info(f"Pre-market. Sleeping {sleep_s//60}m until 8:40...")
                    time.sleep(min(sleep_s, 1800))
                    continue

                # After market close: EOD report then sleep till next day
                if now.hour > MARKET_CLOSE[0] or (
                        now.hour == MARKET_CLOSE[0] and now.minute > 35):
                    if eod_sent_today != today and not self.position:
                        self._end_of_day()
                        eod_sent_today = today

                    # 4:15 PM — run NR7 positional screener
                    if (nr7_run_done != today
                            and now.hour == 16 and now.minute >= 15):
                        nr7_run_done = today
                        try:
                            nr7_run_eod(self.kite, self.telegram)
                        except Exception as e:
                            logger.error(f"NR7 EOD run failed: {e}")
                            self.telegram.send_error(f"NR7 screener error: {e}")

                    time.sleep(600)
                    continue

                # During market hours: poll every POLL_INTERVAL_S seconds
                current_minute = now.hour * 60 + now.minute
                if current_minute == self.last_poll_minute:
                    time.sleep(10)
                    continue
                self.last_poll_minute = current_minute

                # 3:05 PM — NR7 live-LTP breakout scan (true market-hours entry)
                if (nr7_305pm_done != today
                        and now.hour == 15 and now.minute >= 5 and now.minute < 10):
                    nr7_305pm_done = today
                    try:
                        nr7_run_305pm(self.kite, self.telegram)
                    except Exception as e:
                        logger.error(f"NR7 3:05 PM scan failed: {e}")
                        self.telegram.send_error(f"NR7 3:05 PM error: {e}")

                # ORB lock at 9:30
                self._try_lock_orb()

                # Signal check (entry window 9:30 – 13:00)
                self._check_signals()

                # Track open positions
                self._track_positions()

            except KeyboardInterrupt:
                logger.info("Shutting down paper trader (KeyboardInterrupt).")
                self.telegram.send_info("Paper trader STOPPED (manual).")
                self.cmd.stop_polling()
                break
            except Exception as e:
                logger.exception(f"Unhandled error in main loop: {e}")
                self.telegram.send_error(str(e)[:200])
                time.sleep(60)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        bot = IndianPaperBot()
        bot.run()
    except Exception as e:
        logger.exception(f"Fatal startup error: {e}")
        sys.exit(1)
