"""
MT5 Live Trading Connector — ORB Clean Breakout Strategy
=========================================================
Runs 24/7 and trades profitable indices during their market hours
With FundedNext Stellar rules built in.

Architecture:
  MT5Connector       → all MT5 operations (connect/place/modify/close)
  DrawdownProtection → enforce DD limits (overall + daily from start balance)
  LiveTrader         → main orchestrator: signal generation, position management,
                       news filter, state persistence, edge-case handling
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import time
import random
import threading
import logging
import json
import requests
import socket
import traceback
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List, Tuple
import sys
import signal
import atexit
import io

# ── local imports ───────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))           # live/
sys.path.insert(0, str(Path(__file__).parent.parent))     # backtest_engine/

from config_fundednext import (
    FUNDEDNEXT_RULES,
    ADAPTIVE_RISK,
    PROFITABLE_INDICES,
    get_index_config,
    calculate_position_size,
    calculate_position_size_with_broker_data,
    get_broker_symbol_specs,
    MT5_SERVER,
    MT5_LOGIN,
    MT5_PASSWORD,
    ACCOUNT_SIZE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    ACCOUNT_LABEL,
    MT5_PATH,
)
from strategy.strategy_ict_clean import StrategyICTClean
from telegram_notifier import TelegramNotifier
from telegram_commands import TelegramCommandHandler, ist_time
from trade_journal import TradeJournal, ReportScheduler
from sl_protection import (
    set_telegram_config as sl_set_telegram,
    start_monitoring as sl_start_monitoring,
    stop_monitoring as sl_stop_monitoring,
    verify_sl_after_entry,
    is_trading_paused as sl_is_trading_paused,
    close_all_positions as sl_close_all,
    track_original_sl as sl_track_original_sl,
    update_risk_percent as sl_update_risk,
)
from news_filter import EconomicCalendar
from edge_guard import EdgeGuard

# ── logging (namespaced per account profile) ───────────────────────────
logger = logging.getLogger(f"LiveTrader.{ACCOUNT_LABEL}")
logger.setLevel(logging.INFO)

# Namespace log directory: live/logs/<account_label>/
_log_dir = Path(__file__).parent / "logs" / ACCOUNT_LABEL.lower().replace(' ', '_')
_log_dir.mkdir(parents=True, exist_ok=True)

_fh = logging.FileHandler(_log_dir / "trading.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_fh)

# Separate detection logger — one file per account for ORB diagnostics
_det_logger = logging.getLogger(f"Detection.{ACCOUNT_LABEL}")
_det_logger.setLevel(logging.INFO)
_det_fh = logging.FileHandler(_log_dir / "detection.log", encoding="utf-8")
_det_fh.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
_det_logger.addHandler(_det_fh)

if sys.platform == "win32":
    _cs = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
else:
    _cs = sys.stdout
_ch = logging.StreamHandler(_cs)
_ch.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_ch)

# ── magic number for all bot orders (unique per account) ────────────────
# Derive from login to guarantee different accounts get different magic numbers
BOT_MAGIC = 20260000 + (MT5_LOGIN % 10000)


# ============================================================================
#  Helpers
# ============================================================================
def _get_fn_day() -> str:
    """Return the FundedNext trading day string (yyyy-mm-dd).
    FN day flips at 5 PM US/Eastern.  Before 5 PM it's still 'yesterday'.
    This MUST be used everywhere we compare/set last_reset_date so that
    initialize(), _load_state(), and _check_daily_reset() all agree."""
    import pytz
    try:
        est = pytz.timezone('US/Eastern')
        est_now = datetime.now(est)
        if est_now.hour < 17:
            return (est_now - timedelta(days=1)).strftime("%Y-%m-%d")
        return est_now.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


# ============================================================================
#  TradingState
# ============================================================================
@dataclass
class TradingState:
    daily_start_balance: float = 0.0
    current_balance: float = 0.0
    daily_pnl: float = 0.0
    daily_trades: int = 0
    is_daily_limit_hit: bool = False
    last_reset_date: str = ""
    open_positions: Dict = field(default_factory=dict)


# ============================================================================
#  DrawdownProtection  (fixed: daily DD from day-start balance, not peak)
# ============================================================================
class DrawdownProtection:
    """Enforces FundedNext Stellar DD rules.

    Overall DD: from INITIAL balance (FundedNext Stellar rule)
    Daily DD:   from equity at START of trading day (not from daily peak)
    """

    def __init__(self, initial_balance: float):
        self.initial_balance = initial_balance
        self.peak_balance = initial_balance
        self.daily_start_balance = initial_balance

    # ── core check ──────────────────────────────────────────────────────
    def update(self, current_balance: float) -> dict:
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance

        # Overall DD from INITIAL balance (FundedNext Stellar)
        overall_dd = ((self.initial_balance - current_balance) / self.initial_balance) * 100

        # Daily DD from day-start equity (FundedNext rule)
        if self.daily_start_balance > 0:
            daily_dd = ((self.daily_start_balance - current_balance) / self.daily_start_balance) * 100
        else:
            daily_dd = 0.0

        max_dd_limit = FUNDEDNEXT_RULES["max_drawdown_percent"]
        daily_dd_stop = FUNDEDNEXT_RULES["daily_drawdown_stop"]

        return {
            "overall_dd": max(0.0, overall_dd),
            "daily_dd": max(0.0, daily_dd),
            "is_max_dd_breach": overall_dd >= max_dd_limit,
            "is_daily_dd_breach": daily_dd >= daily_dd_stop,
            "can_trade": overall_dd < max_dd_limit and daily_dd < daily_dd_stop,
        }

    # ── adaptive risk (2-tier step-down) ─────────────────────────────────
    def get_adaptive_risk(self, current_balance: float) -> float:
        if not ADAPTIVE_RISK.get("enabled", False):
            return FUNDEDNEXT_RULES["risk_per_trade_percent"]

        if current_balance > self.peak_balance:
            self.peak_balance = current_balance

        # DD from initial balance (FundedNext Stellar measures from initial)
        dd_pct = ((self.initial_balance - current_balance) / self.initial_balance) * 100
        dd_pct = max(0.0, dd_pct)  # can't be negative

        default_r = ADAPTIVE_RISK.get("default_risk_percent", 1.0)
        tier2_r   = ADAPTIVE_RISK.get("tier2_risk_percent", 0.5)
        tier2_dd  = ADAPTIVE_RISK.get("tier2_threshold", 4.0)

        if dd_pct >= tier2_dd:
            logger.info(f"Adaptive Risk: DD={dd_pct:.2f}% >= {tier2_dd}% -> {tier2_r}% risk (TIER 2)")
            return tier2_r
        return default_r

    # ── daily reset ─────────────────────────────────────────────────────
    def reset_daily(self, current_balance: float):
        self.daily_start_balance = current_balance


# ============================================================================
#  MT5Connector
# ============================================================================
class MT5Connector:
    """All MetaTrader 5 operations."""

    def __init__(self):
        self.connected = False

    # ── connect ─────────────────────────────────────────────────────────
    def connect(self) -> bool:
        # STEP 1: initialize() with path ONLY — binds to the correct terminal
        # WITHOUT switching accounts (avoids "algo trading disabled because
        # account has been changed" in multi-instance setups).
        init_kwargs = {'timeout': 30000}  # 30s timeout for IPC
        if MT5_PATH:
            init_kwargs['path'] = MT5_PATH

        if not mt5.initialize(**init_kwargs):
            logger.error(f"MT5 init failed: {mt5.last_error()}")
            return False

        # STEP 2: check if already logged into the correct account
        info = mt5.account_info()
        if info and info.login == MT5_LOGIN:
            self.connected = True
            logger.info(f"Connected to MT5 - Account: {info.login}, Balance: ${info.balance:,.2f}")
            return True

        # STEP 3: not logged in (or wrong account) — do login ONCE
        if MT5_LOGIN and MT5_PASSWORD:
            ok = mt5.login(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
            if not ok:
                logger.error(f"MT5 login failed: {mt5.last_error()}")
                mt5.shutdown()
                return False
            logger.info(f"Logged in to {MT5_SERVER} with account {MT5_LOGIN}")

        self.connected = True
        info = mt5.account_info()
        if not info:
            logger.error("MT5 account_info() returned None after login")
            return False
        logger.info(f"Connected to MT5 - Account: {info.login}, Balance: ${info.balance:,.2f}")
        return True

    def disconnect(self):
        mt5.shutdown()
        self.connected = False
        logger.info("Disconnected from MT5")

    # ── account ─────────────────────────────────────────────────────────
    def get_account_info(self) -> dict:
        if not self.connected:
            return {}
        info = mt5.account_info()
        if not info:
            return {}
        return {
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "free_margin": info.margin_free,
            "profit": info.profit,
            "leverage": info.leverage,
        }

    # ── price ───────────────────────────────────────────────────────────
    def get_current_price(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            return tick.bid, tick.ask
        return None, None

    # ── OHLC ────────────────────────────────────────────────────────────
    def get_ohlc_data(self, symbol: str, timeframe=mt5.TIMEFRAME_M1,
                      count: int = 500) -> pd.DataFrame:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            logger.error(f"Failed to get rates for {symbol}")
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        df.columns = ["open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
        return df

    # ── place order ─────────────────────────────────────────────────────
    def place_order(self, symbol: str, order_type: str, volume: float,
                    sl: float, tp: float, comment: str = "") -> dict:
        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            logger.error(f"Symbol {symbol} not found")
            return {"success": False, "error": "Symbol not found"}
        if not sym_info.visible:
            mt5.symbol_select(symbol, True)

        bid, ask = self.get_current_price(symbol)
        if bid is None:
            return {"success": False, "error": "No price available"}

        if order_type.lower() == "buy":
            price, trade_type = ask, mt5.ORDER_TYPE_BUY
        else:
            price, trade_type = bid, mt5.ORDER_TYPE_SELL

        # ── PRE-VALIDATE stop distance (avoid "Invalid stops" rejection) ──
        _sym = mt5.symbol_info(symbol)
        if _sym:
            _min_stops = _sym.trade_stops_level * _sym.point  # pts
            if _min_stops > 0:
                sl_dist = abs(price - sl)
                tp_dist = abs(price - tp) if tp else 999999
                if sl_dist < _min_stops or tp_dist < _min_stops:
                    logger.warning(
                        f"{symbol}: Stops too close — SL dist={sl_dist:.2f}, "
                        f"TP dist={tp_dist:.2f}, min={_min_stops:.2f} pts. "
                        f"Adjusting to minimum distance.")
                    # Adjust SL/TP to minimum distance from price
                    if sl_dist < _min_stops:
                        if trade_type == mt5.ORDER_TYPE_BUY:
                            sl = round(price - _min_stops, _sym.digits)
                        else:
                            sl = round(price + _min_stops, _sym.digits)
                    if tp_dist < _min_stops:
                        if trade_type == mt5.ORDER_TYPE_BUY:
                            tp = round(price + _min_stops, _sym.digits)
                        else:
                            tp = round(price - _min_stops, _sym.digits)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": round(volume, 2),
            "type": trade_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,
            "magic": BOT_MAGIC,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return {"success": False, "error": "order_send returned None"}
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Order failed: {result.comment}")
            return {"success": False, "error": result.comment, "retcode": result.retcode}

        logger.info(f"Order placed: {order_type.upper()} {volume} {symbol} @ {price}, SL: {sl}, TP: {tp}")

        # IOC fills on some brokers (e.g. FundedNext-Server 2) return result.price=0.0
        # Recover actual fill price from deal history with progressive retry
        fill_price = result.price
        position_id = result.order  # default: assume position_id = order_id
        if fill_price == 0.0 and result.deal:
            from datetime import timezone as _tz
            for _retry in range(4):  # 0.1s, 0.2s, 0.3s, 0.5s
                time.sleep([0.1, 0.2, 0.3, 0.5][_retry])
                try:
                    _now = datetime.now(_tz.utc)
                    _start = _now - timedelta(seconds=60)
                    deals = mt5.history_deals_get(_start, _now, ticket=result.deal)
                    if deals and deals[0].price > 0:
                        fill_price = deals[0].price
                        logger.info(f"Fill price recovered from deal history: {fill_price} "
                                    f"(attempt {_retry + 1})")
                        if hasattr(deals[0], 'position_id') and deals[0].position_id:
                            position_id = deals[0].position_id
                            if position_id != result.order:
                                logger.info(f"Position ID {position_id} differs from order ID {result.order}")
                        break
                except Exception as e:
                    logger.warning(f"Fill price recovery attempt {_retry + 1} failed: {e}")
        if fill_price == 0.0:
            fill_price = price  # ultimate fallback: use requested price
            logger.warning(f"Fill price still 0.0, using requested price {price} as fallback")

        return {
            "success": True,
            "ticket": result.order,
            "position_id": position_id,
            "volume": volume,
            "price": price,
            "sl": sl,
            "tp": tp,
            "fill_price": fill_price,
        }

    # ── modify SL/TP ────────────────────────────────────────────────────
    def modify_position(self, ticket: int, sl: float = None, tp: float = None,
                        max_retries: int = 3) -> bool:
        for attempt in range(max_retries):
            pos = mt5.positions_get(ticket=ticket)
            if not pos:
                logger.error(f"Position {ticket} not found")
                return False
            p = pos[0]
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": p.symbol,
                "position": ticket,
                "sl": sl if sl is not None else p.sl,
                "tp": tp if tp is not None else p.tp,
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"Position {ticket} modified - SL: {sl}, TP: {tp}")
                return True
            logger.warning(f"Modify attempt {attempt+1}/{max_retries} failed: "
                           f"{result.comment if result else 'None'}")
            if attempt < max_retries - 1:
                time.sleep(0.5)
        logger.error(f"Failed to modify position {ticket} after {max_retries} attempts")
        return False

    # ── close position ──────────────────────────────────────────────────
    def close_position(self, ticket: int, volume: float = None) -> bool:
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            logger.error(f"Position {ticket} not found")
            return False
        p = pos[0]
        close_volume = volume if volume else p.volume

        tick = mt5.symbol_info_tick(p.symbol)
        if not tick:
            logger.error(f"No tick data for {p.symbol} during close — retrying")
            time.sleep(1)
            tick = mt5.symbol_info_tick(p.symbol)
            if not tick:
                logger.error(f"No tick data for {p.symbol} after retry — cannot close")
                return False
        if p.type == mt5.POSITION_TYPE_BUY:
            trade_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            trade_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": close_volume,
            "type": trade_type,
            "position": ticket,
            "price": price,
            "deviation": 10,
            "magic": BOT_MAGIC,
            "comment": "Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Close failed: {result.comment if result else 'None'}")
            return False
        logger.info(f"Closed {close_volume} lots of position {ticket}")
        return True

    # ── list open positions ─────────────────────────────────────────────
    def get_open_positions(self, symbol: str = None) -> List[dict]:
        if symbol:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                "volume": p.volume,
                "price": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "time": datetime.fromtimestamp(p.time),
                "magic": p.magic,
            }
            for p in positions
        ]


# ============================================================================
#  Market-hours helpers
# ============================================================================
def is_market_open(symbol: str) -> bool:
    config = get_index_config(symbol)
    if not config:
        return False

    import pytz
    try:
        utc_now = datetime.now(pytz.UTC)
        # Convert to the index's LOCAL timezone for accurate DST handling
        local_tz = pytz.timezone(config.get('timezone', 'America/New_York'))
        local_now = utc_now.astimezone(local_tz).replace(tzinfo=None)
    except Exception:
        # Fallback: use config mt5_* hours as-is (already in server time approx)
        try:
            local_now_fallback = datetime.now()
            if local_now_fallback.weekday() >= 5:
                return False
            # Approximate: use IST → broker offset (~+2 or +3 from UTC)
            import time as _fallback_time
            for _fb_sym in ['EURUSD', 'SPX500', 'GER30']:
                _fb_tick = mt5.symbol_info_tick(_fb_sym)
                if _fb_tick and _fb_tick.time > 0:
                    _fb_offset = round((_fb_tick.time - int(_fallback_time.time())) / 3600)
                    if -12 <= _fb_offset <= 14:
                        from datetime import timezone as _fbtz, timedelta as _fbtd
                        _fb_broker_tz = _fbtz(_fbtd(hours=_fb_offset))
                        _fb_utc = datetime.now(pytz.UTC)
                        local_now = _fb_utc.astimezone(_fb_broker_tz).replace(tzinfo=None)
                        break
            else:
                # Last resort: use mt5 hours with IST-based approximation
                local_tz_fb = pytz.timezone("Asia/Kolkata")
                ist_now = datetime.now(local_tz_fb)
                # Broker is roughly IST - 2:30 or IST - 3:30
                ny = pytz.timezone('America/New_York')
                _fb_offset2 = 3 if bool(datetime.now(ny).dst()) else 2
                _fb_broker_tz2 = _fbtz(_fbtd(hours=_fb_offset2))
                local_now = ist_now.astimezone(_fb_broker_tz2).replace(tzinfo=None)

            cur = local_now.hour * 60 + local_now.minute
            o = config["mt5_open_hour"] * 60 + config["mt5_open_minute"]
            c = config["mt5_close_hour"] * 60 + config["mt5_close_minute"]
            if c < o:
                return cur >= o or cur < c
            return o <= cur < c
        except Exception:
            return False

    if local_now.weekday() >= 5:
        return False

    cur = local_now.hour * 60 + local_now.minute
    o = config["market_open_hour"] * 60 + config["market_open_minute"]
    c = config["market_close_hour"] * 60 + config["market_close_minute"]

    if c < o:
        return cur >= o or cur < c
    return o <= cur < c


def get_active_indices() -> List[str]:
    return [s for s in PROFITABLE_INDICES if is_market_open(s)]


# ============================================================================
#  Internet-connectivity check
# ============================================================================
def _internet_ok(timeout: float = 3.0) -> bool:
    for host in ("8.8.8.8", "1.1.1.1"):
        try:
            socket.create_connection((host, 53), timeout=timeout)
            return True
        except OSError:
            continue
    return False


# ============================================================================
#  LiveTrader — ORB Clean Breakout
# ============================================================================
class LiveTrader:
    """
    Main orchestrator for live ORB breakout trading.

    Edge cases handled:
      - Network loss        -> reconnect loop with exponential back-off
      - SL slippage         -> sl_protection emergency close at 130% risk
      - Power off           -> state persisted every cycle + graceful shutdown
      - MT5 crash           -> watchdog restarts whole bot; state reloaded
      - Broker disconnect   -> reconnect + position reconciliation
      - Stale data          -> skip signal if last candle > 5 bars old
      - SL stripped/missing -> re-apply or emergency close
      - News event          -> block new trades within buffer window
      - Max DD breach       -> close everything + stop trading
      - Daily DD breach     -> stop new trades, keep managing existing
      - Multiple restarts   -> watchdog caps at 5/hour
      - Weekend/holiday     -> is_market_open() blocks all activity
      - Slippage logging    -> compare expected vs fill price on every order
    """

    STATE_FILE = _log_dir / "position_state.json"
    HEARTBEAT_FILE = _log_dir / "heartbeat.txt"

    def __init__(self):
        self.mt5 = MT5Connector()
        self.news_filter = EconomicCalendar(
            buffer_before_minutes=5,   # block 5 min before high-impact news
            buffer_after_minutes=15,   # block 15 min after high-impact news
            cache_dir=_log_dir,        # FIX B8: per-account news cache
        )
        self.dd_protection: Optional[DrawdownProtection] = None
        self.state = TradingState()
        self.strategies: Dict[str, StrategyICTClean] = {}
        self.telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                                          account_label=ACCOUNT_LABEL)
        self.journal = TradeJournal(
            journal_path=str(_log_dir.parent.parent / "reports" / f"trade_journal_{ACCOUNT_LABEL.lower().replace(' ', '_')}.xlsx")
        )
        self.edge_guard = EdgeGuard(data_dir=_log_dir, initial_balance=ACCOUNT_SIZE)
        self.report_scheduler = ReportScheduler(self.journal, self.telegram)

        # position tracking:  ticket -> {symbol, direction, entry, sl_init, tp, R,
        #                                hit_be, max_favorable, volume, entry_time}
        self.tracked_positions: Dict[int, dict] = {}
        # per-symbol daily trade taken flag (reset at daily_reset)
        self.trade_taken_today: Dict[str, bool] = {}
        # Telegram block notification throttle: {symbol: last_notify_time}
        self._block_notify_sent: Dict[str, datetime] = {}

        # No-trade streak tracker (alert after 7 consecutive trading days)
        self._no_trade_streak: int = 0          # consecutive trading days without a trade
        self._last_trade_date: str = ""         # date string of last trade

        # Detection log: track which symbols have been logged this session day
        # Resets in _check_daily_reset(). Prevents spamming the same info every 10s.
        self._detection_logged_today: Dict[str, bool] = {}
        # Drift/stale signal log throttle: {symbol: count} — log first + every 10th
        self._drift_log_count: Dict[str, int] = {}

        # heartbeat
        self._heartbeat_file = self.HEARTBEAT_FILE

        # shutdown
        self._shutdown_requested = False
        self._trading_enabled = True
        self._trading_paused = False

        # Telegram command handler
        self.cmd_handler = TelegramCommandHandler(
            TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
            state_dir=_log_dir, account_label=ACCOUNT_LABEL,
        )
        self._setup_command_callbacks()

        # market-open notification tracking
        self._prev_active: List[str] = []
        # Per-symbol session recap: {symbol: {signal_found, signal_dir, reason, entry_price}}
        self._session_recap: Dict[str, dict] = {}
        # Track which sessions already sent a recap today
        self._recap_sent_today: Dict[str, bool] = {}

        # error tracking
        self._consecutive_errors = 0
        self._error_backoff = 30

        # signals
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        atexit.register(self._cleanup)

    # ────────────────────────────────────────────────────────────────────
    #  Signal handlers
    # ────────────────────────────────────────────────────────────────────
    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self._shutdown_requested = True
        try:
            sl_stop_monitoring()
            self._save_state()
            if hasattr(self, "cmd_handler"):
                self.cmd_handler.bot_state['is_running'] = False
                self.cmd_handler._save_state()
                self.cmd_handler.stop_polling()
            account = self.mt5.get_account_info()
            if account:
                self.telegram.bot_stopped("Manual shutdown (Ctrl+C)",
                                          account.get("balance", 0), self.state.daily_pnl)
                time.sleep(1)
            self.mt5.disconnect()
            logger.info("Shutdown complete.")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

    def _cleanup(self):
        if not self._shutdown_requested:
            try:
                sl_stop_monitoring()
                self._save_state()
                self.mt5.disconnect()
            except Exception:
                pass

    # ────────────────────────────────────────────────────────────────────
    #  State persistence (ORB-shaped)
    # ────────────────────────────────────────────────────────────────────
    def _save_state(self):
        """Persist tracked positions + daily state to disk."""
        try:
            data = {
                "save_time": str(datetime.now()),
                "strategy": "ORB_Clean_Breakout",
                "daily_pnl": self.state.daily_pnl,
                "daily_trades": self.state.daily_trades,
                "last_reset_date": self.state.last_reset_date,
                "trade_taken_today": self.trade_taken_today,
                "no_trade_streak": self._no_trade_streak,
                "last_trade_date": self._last_trade_date,
                "positions": {},
            }
            for ticket, info in self.tracked_positions.items():
                safe = {}
                for k, v in info.items():
                    if isinstance(v, datetime):
                        safe[k] = str(v)
                    else:
                        safe[k] = v
                data["positions"][str(ticket)] = safe

            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Write to temp file first, then rename (atomic on Windows NTFS)
            tmp = self.STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            tmp.replace(self.STATE_FILE)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_state(self):
        """Reload state after restart / power recovery."""
        if not self.STATE_FILE.exists():
            return
        try:
            with open(self.STATE_FILE) as f:
                data = json.load(f)

            today = _get_fn_day()
            saved_date = data.get("last_reset_date", "")

            if saved_date == today:
                self.state.daily_pnl = data.get("daily_pnl", 0.0)
                self.state.daily_trades = data.get("daily_trades", 0)
                self.state.last_reset_date = saved_date
                self.trade_taken_today = data.get("trade_taken_today", {})
                logger.info(f"Restored daily state: PnL=${self.state.daily_pnl:.2f}, "
                            f"trades={self.state.daily_trades}")

            # Always restore streak tracker (not date-dependent)
            self._no_trade_streak = data.get("no_trade_streak", 0)
            self._last_trade_date = data.get("last_trade_date", "")
            logger.info(f"No-trade streak: {self._no_trade_streak} days (last trade: {self._last_trade_date or 'never'})")

            # Reconcile tracked positions with MT5
            saved_pos = data.get("positions", {})
            mt5_positions = self.mt5.get_open_positions()
            mt5_tickets = {p["ticket"] for p in mt5_positions}

            for ticket_str, info in saved_pos.items():
                ticket = int(ticket_str)
                if ticket in mt5_tickets:
                    self.tracked_positions[ticket] = info
                    logger.info(f"Restored position {ticket} ({info.get('symbol')}) tracking")
                else:
                    logger.warning(f"Saved position {ticket} no longer open in MT5 - skipping")

            # Check for MT5 positions we don't track (opened manually or before crash)
            for p in mt5_positions:
                if p["ticket"] not in self.tracked_positions and p.get("magic") == BOT_MAGIC:
                    logger.warning(f"Untracked bot position found: {p['ticket']} {p['symbol']} - "
                                   f"adding with conservative tracking")
                    self.tracked_positions[p["ticket"]] = {
                        "symbol": p["symbol"],
                        "direction": p["type"],
                        "entry": p["price"],
                        "sl_init": p["sl"],
                        "tp": p["tp"],
                        "R": abs(p["price"] - p["sl"]) if p["sl"] else 10.0,
                        "hit_be": False,
                        "max_favorable": 0.0,
                        "volume": p["volume"],
                        "entry_time": str(p["time"]),
                    }

        except Exception as e:
            logger.error(f"Failed to load state: {e}")

    # ────────────────────────────────────────────────────────────────────
    #  Telegram command callbacks
    # ────────────────────────────────────────────────────────────────────
    def _setup_command_callbacks(self):
        self.cmd_handler.on_start = self._on_start
        self.cmd_handler.on_stop = self._on_stop
        self.cmd_handler.on_pause = self._on_pause
        self.cmd_handler.on_resume = self._on_resume
        self.cmd_handler.on_status = self._on_status
        self.cmd_handler.on_positions = self._on_positions
        self.cmd_handler.on_balance = self._on_balance
        self.cmd_handler.on_trades = self._on_trades
        self.cmd_handler.on_risk = self._on_risk
        self.cmd_handler.on_performance = self._on_performance
        self.cmd_handler.on_emergency = self._on_emergency

    def _on_start(self, *a, **kw):
        self._trading_enabled = True
        self._trading_paused = False
        self.edge_guard.force_override()
        logger.info("Trading STARTED via Telegram (EdgeGuard override applied)")

    def _on_stop(self, *a, **kw):
        self._trading_enabled = False
        self._shutdown_requested = True
        logger.info("Trading STOPPED via Telegram - shutting down")
        self._save_state()

    def _on_pause(self, *a, **kw):
        self._trading_paused = True
        logger.info("Trading PAUSED via Telegram")

    def _on_resume(self, *a, **kw):
        self._trading_paused = False
        logger.info("Trading RESUMED via Telegram")

    def _on_status(self, *a, **kw) -> dict:
        """Return status dict for Telegram /status command."""
        account = self.mt5.get_account_info()
        if not account:
            return {'balance': 0, 'daily_pnl': 0, 'open_positions': 0,
                    'daily_dd': 0, 'overall_dd': 0, 'active_markets': [],
                    'error': 'Not connected to MT5'}
        dd = self.dd_protection.update(account["balance"]) if self.dd_protection else {}
        active = get_active_indices()
        positions = self.mt5.get_open_positions()
        return {
            'balance': account.get('balance', 0),
            'equity': account.get('equity', 0),
            'daily_pnl': self.state.daily_pnl,
            'open_positions': len(positions) if positions else 0,
            'daily_dd': dd.get('daily_dd', 0),
            'overall_dd': dd.get('overall_dd', 0),
            'active_markets': active,
            'trades_today': self.state.daily_trades,
            'tracked': len(self.tracked_positions),
            'paused': self._trading_paused,
            'edge_guard': self.edge_guard.get_rolling_metrics(),
            'edge_degraded': self.edge_guard.is_edge_degraded,
        }

    def _on_positions(self, *a, **kw) -> list:
        """Return positions list for Telegram /positions command."""
        positions = self.mt5.get_open_positions()
        if not positions:
            return []
        result = []
        for p in positions:
            # Enrich with tracked data if available
            tracked = self.tracked_positions.get(p["ticket"], {})
            result.append({
                'symbol': p['symbol'],
                'type': p['type'].upper(),
                'volume': p['volume'],
                'profit': p['profit'],
                'price_open': p['price'],
                'price_current': p.get('price_current', p['price']),
                'sl': p.get('sl', 0),
                'tp': p.get('tp', 0),
                'ticket': p['ticket'],
                'entry_time': str(p.get('time', '')),
                'hit_be': tracked.get('hit_be', False),
            })
        return result

    def _on_balance(self, *a, **kw) -> dict:
        """Return balance dict for Telegram /balance command."""
        account = self.mt5.get_account_info()
        if not account:
            return {'balance': 0, 'equity': 0, 'margin': 0,
                    'free_margin': 0, 'profit': 0, 'error': 'Not connected'}
        return {
            'balance': account.get('balance', 0),
            'equity': account.get('equity', 0),
            'margin': account.get('margin', 0),
            'free_margin': account.get('free_margin', 0),
            'profit': account.get('profit', 0),
        }

    def _on_trades(self, *a, **kw) -> dict:
        """Return today's trades for Telegram /trades command."""
        try:
            stats = self.journal.get_daily_stats()
            return stats
        except Exception as e:
            logger.error(f"Failed to get trades: {e}")
            return {'trades': 0, 'error': str(e)}

    def _on_risk(self, *a, **kw) -> dict:
        """Return risk/DD info for Telegram /risk command."""
        account = self.mt5.get_account_info()
        if not account:
            return {'error': 'Not connected'}
        balance = account.get('balance', 0)
        dd = self.dd_protection.update(balance) if self.dd_protection else {}
        risk_pct = self.dd_protection.get_adaptive_risk(balance) if self.dd_protection else 0
        return {
            'balance': balance,
            'initial_balance': self.dd_protection.initial_balance if self.dd_protection else ACCOUNT_SIZE,
            'daily_dd': dd.get('daily_dd', 0),
            'overall_dd': dd.get('overall_dd', 0),
            'daily_dd_limit': FUNDEDNEXT_RULES.get('daily_drawdown_stop', 3.0),
            'overall_dd_limit': FUNDEDNEXT_RULES.get('max_drawdown_percent', 8.0),
            'can_trade': dd.get('can_trade', True),
            'risk_percent': risk_pct,
            'risk_dollars': balance * risk_pct / 100,
            'daily_pnl': self.state.daily_pnl,
            'daily_trades': self.state.daily_trades,
            'paused': self._trading_paused,
        }

    def _on_performance(self, *a, **kw) -> dict:
        """Return weekly performance for Telegram /performance command."""
        try:
            daily = self.journal.get_daily_stats()
            weekly = self.journal.get_weekly_stats()
            account = self.mt5.get_account_info()
            balance = account.get('balance', 0) if account else 0
            initial = self.dd_protection.initial_balance if self.dd_protection else ACCOUNT_SIZE
            return {
                'daily': daily,
                'weekly': weekly,
                'balance': balance,
                'initial_balance': initial,
                'total_return_pct': ((balance - initial) / initial * 100) if initial else 0,
            }
        except Exception as e:
            logger.error(f"Failed to get performance: {e}")
            return {'error': str(e)}

    def _on_emergency(self, *a, **kw) -> dict:
        """Emergency close all positions via Telegram /emergency command."""
        try:
            positions = self.mt5.get_open_positions()
            if not positions:
                return {'closed': 0, 'message': 'No open positions to close'}
            closed = 0
            failed = 0
            total_pnl = 0.0
            for p in positions:
                ticket = p['ticket']
                ok = self.mt5.close_position(ticket)
                if ok:
                    closed += 1
                    logger.warning(f"EMERGENCY CLOSE: {p['symbol']} ticket {ticket}")
                    # Record PnL and exit details in journal + telegram
                    try:
                        info = self.tracked_positions.get(ticket, {})
                        symbol = info.get('symbol', p.get('symbol', ''))
                        pos_id = info.get('position_id', ticket)
                        details = self._get_closed_trade_details(pos_id, expected_symbol=symbol)
                        pnl = details['pnl']
                        total_pnl += pnl
                        self.state.daily_pnl += pnl
                        trade_id = info.get('journal_trade_id')
                        if trade_id:
                            pos_R = info.get('R', 0)
                            mfe_pts = round(info.get('max_favorable', 0) * pos_R, 2) if pos_R else 0
                            mae_pts = round(info.get('max_adverse', 0) * pos_R, 2) if pos_R else 0
                            pnl_pts = pnl / (info.get('volume', 1) * self._get_point_value(symbol)) if info.get('volume') else 0
                            self.journal.close_trade(trade_id, {
                                'exit_price': details['exit_price'],
                                'exit_reason': 'EMERGENCY_CMD',
                                'pnl': pnl,
                                'pnl_percent': round(pnl / self.state.current_balance * 100, 3) if self.state.current_balance else 0,
                                'pnl_points': round(pnl_pts, 2),
                                'mfe_points': mfe_pts,
                                'mae_points': mae_pts,
                                'be_hit': 'Yes' if info.get('hit_be') else 'No',
                                'balance_after': self.state.current_balance + pnl,
                                'drawdown': 0,
                            })
                        self.telegram.trade_closed(symbol, 'EMERGENCY_CMD', pnl)
                        self.edge_guard.record_trade({
                            'date': datetime.now().strftime('%Y-%m-%d'),
                            'symbol': symbol,
                            'direction': info.get('direction', p.get('type', '')),
                            'pnl': pnl,
                            'balance_after': self.state.current_balance + pnl,
                            'session': 'emergency',
                        })
                    except Exception as e:
                        logger.error(f"Emergency journal/telegram failed for {ticket}: {e}")
                else:
                    failed += 1
            self.tracked_positions.clear()
            self._trading_paused = True
            self.telegram.critical_alert("EMERGENCY CLOSE",
                f"All positions closed via Telegram command.\n"
                f"Closed: {closed}, Failed: {failed}\n"
                f"Total PnL: ${total_pnl:+.2f}\nTrading PAUSED.")
            return {'closed': closed, 'failed': failed, 'total': len(positions)}
        except Exception as e:
            logger.error(f"Emergency close failed: {e}")
            return {'error': str(e)}

    # ────────────────────────────────────────────────────────────────────
    #  Heartbeat (for watchdog)
    # ────────────────────────────────────────────────────────────────────
    def _write_heartbeat(self):
        try:
            self._heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._heartbeat_file, "w") as f:
                f.write(datetime.now().isoformat())
        except Exception:
            pass

    # ────────────────────────────────────────────────────────────────────
    #  Session name helper (edge diagnostics)
    # ────────────────────────────────────────────────────────────────────
    def _get_session_name(self, symbol: str) -> str:
        """Map symbol to trading session name for edge diagnostics."""
        us_symbols = {'SPX500', 'NDX100', 'US30', 'US2000'}
        eu_symbols = {'GER30', 'FRA40'}
        if symbol in us_symbols:
            return "US"
        elif symbol in eu_symbols:
            return "EU"
        return "Other"

    # ────────────────────────────────────────────────────────────────────
    #  Initialize
    # ────────────────────────────────────────────────────────────────────
    def initialize(self) -> bool:
        if not self.mt5.connect():
            logger.error("Cannot connect to MT5")
            return False

        account = self.mt5.get_account_info()
        balance = account.get("balance", ACCOUNT_SIZE)
        # C1 FIX: Use ACCOUNT_SIZE for overall DD floor (FundedNext measures from initial balance)
        self.dd_protection = DrawdownProtection(ACCOUNT_SIZE)
        # FIX: Sync daily_start_balance to actual MT5 balance on init.
        # Without this, DrawdownProtection.__init__ sets daily_start_balance
        # to ACCOUNT_SIZE (initial), causing a phantom daily DD breach every
        # restart when the account has any drawdown from initial size.
        self.dd_protection.reset_daily(balance)
        self.state.current_balance = balance
        self.state.daily_start_balance = balance
        self.state.last_reset_date = _get_fn_day()

        logger.info(f"LiveTrader initialized - Account Size: ${balance:,.2f}")
        risk_pct = self.dd_protection.get_adaptive_risk(balance)
        logger.info(f"Risk per trade: {risk_pct}% = ${balance * risk_pct / 100:,.2f}")

        # Create strategy instances for each symbol (with backtest-locked params)
        for symbol, cfg in PROFITABLE_INDICES.items():
            self.strategies[symbol] = StrategyICTClean(
                sl_buffer=cfg.get("sl_buffer", 1.0),
                orb_minutes=cfg.get("orb_minutes", 15),
                entry_hours=cfg.get("entry_hours", 3),
                breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
                disp_mult=cfg.get("disp_mult", 1.0),       # Universal (aligned with backtest)
                body_min=cfg.get("body_min", 0.70),         # Universal (aligned with backtest)
                close_strength=cfg.get("close_strength", 0.25),
                breakout_margin=cfg.get("breakout_margin", 0.15),  # Universal (aligned with backtest)
                orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
                min_sl_points=cfg.get("min_sl_points", 2.0),
                max_sl_points=cfg.get("max_sl_points", 80.0),  # Aligned with backtest
                tp3_r=2.5,
                tp1_r=1.0,
                orb_bias_filter=False,  # v3.14.0: HTF-only mode (15yr A/B validated, +62% total R)
                debug=True,   # Enable detection logging for live diagnostics
            )
            self.trade_taken_today[symbol] = False

        # Load saved state (handles power-off recovery)
        self._load_state()

        # Start subsystems
        sl_set_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ACCOUNT_LABEL)
        sl_start_monitoring(log_dir=_log_dir)
        logger.info("SL Protection monitoring started")

        self.cmd_handler.start_polling()
        self.cmd_handler.bot_state['is_running'] = True
        self.cmd_handler.bot_state['started_at'] = ist_time()
        self.cmd_handler._save_state()
        logger.info("Telegram command handler started - use /help for commands")

        self.telegram.bot_started(list(PROFITABLE_INDICES.keys()), balance)
        logger.info("Starting 24/7 trading loop...")
        return True

    # ────────────────────────────────────────────────────────────────────
    #  Daily reset
    # ────────────────────────────────────────────────────────────────────
    def _check_daily_reset(self):
        # C5 FIX: FundedNext resets daily DD at 5:00 PM EST = 00:00 EET (MT5 server time)
        today = _get_fn_day()
        if self.state.last_reset_date != today:
            logger.info(f"New day: {today} - resetting daily counters "
                        f"(prev PnL: ${self.state.daily_pnl:.2f})")
            account = self.mt5.get_account_info()
            if account:
                balance = account["balance"]
                self.dd_protection.reset_daily(balance)
                self.state.daily_start_balance = balance
            # ── No-trade streak check (before resetting daily_trades) ──
            prev_day = self.state.last_reset_date
            if prev_day:  # not first boot
                from datetime import date as _date
                try:
                    prev = _date.fromisoformat(prev_day)
                    # Only count weekdays (Mon-Fri) as trading days
                    if prev.weekday() < 5:
                        if self.state.daily_trades == 0:
                            self._no_trade_streak += 1
                            logger.info(f"No trades on {prev_day} — streak: {self._no_trade_streak} days")
                        else:
                            self._no_trade_streak = 0
                            self._last_trade_date = prev_day

                        if self._no_trade_streak >= 7:
                            self.telegram.send_message(
                                f"\U0001f6a8 <b>NO TRADES FOR {self._no_trade_streak} TRADING DAYS</b>\n\n"
                                f"Last trade: {self._last_trade_date or 'never'}\n"
                                f"Strategy filters may be too strict.\n"
                                f"Please review missed signals.")
                            logger.warning(f"NO-TRADE STREAK: {self._no_trade_streak} consecutive trading days")
                except Exception:
                    pass

            self.state.daily_pnl = 0.0
            self.state.daily_trades = 0
            self.state.is_daily_limit_hit = False
            self.state.last_reset_date = today
            for sym in self.trade_taken_today:
                self.trade_taken_today[sym] = False
            self._detection_logged_today = {}  # reset detection log for new day
            self._drift_log_count = {}           # reset drift log throttle
            self._session_recap = {}             # reset session recaps
            self._recap_sent_today = {}          # reset recap sent flags

    # ────────────────────────────────────────────────────────────────────
    #  Connection check + reconnect  (handles network loss, MT5 crash)
    # ────────────────────────────────────────────────────────────────────
    def _check_connection(self) -> bool:
        """Returns True if connected, attempts reconnection if not."""
        if self.mt5.connected:
            info = mt5.account_info()
            if info:
                return True

        logger.warning("MT5 connection lost - attempting reconnect...")

        # Check internet first
        if not _internet_ok():
            logger.warning("No internet - waiting...")
            self.telegram.send_message("<b>No Internet</b>\nWaiting for connection...")
            for wait in (10, 20, 30, 60, 60):
                time.sleep(wait)
                if _internet_ok():
                    logger.info("Internet restored")
                    break
            else:
                return False

        # Reconnect MT5 with retries
        for attempt in range(5):
            self.mt5.connected = False  # reset flag
            if self.mt5.connect():
                logger.info("MT5 reconnected")
                self.telegram.send_message("MT5 reconnected after dropout")
                try:
                    self._verify_positions_after_reconnect()
                except Exception as e:
                    logger.error(f"Position verification after reconnect failed: {e}")
                return True
            time.sleep(5 * (attempt + 1))

        self.telegram.critical_alert("MT5 RECONNECT FAILED",
                                     "Could not reconnect after 5 attempts")
        return False

    def _verify_positions_after_reconnect(self):
        """Reconcile tracked positions with MT5 reality after disconnect/power loss."""
        mt5_positions = self.mt5.get_open_positions()
        mt5_tickets = {p["ticket"] for p in mt5_positions}

        # Detect closed positions
        closed = [t for t in self.tracked_positions if t not in mt5_tickets]
        for ticket in closed:
            info = self.tracked_positions.pop(ticket)
            logger.warning(f"Position {ticket} ({info['symbol']}) closed during disconnect")
            self.telegram.send_message(
                f"<b>Position Closed During Disconnect</b>\n"
                f"Ticket: {ticket}\nSymbol: {info['symbol']}")

        # Verify SL exists on surviving positions (SL stripped = broker issue)
        for p in mt5_positions:
            if p["ticket"] in self.tracked_positions:
                if p["sl"] == 0.0:
                    original = self.tracked_positions[p["ticket"]]
                    sl_price = original.get("sl_init", 0)
                    logger.critical(f"Position {p['ticket']} ({p['symbol']}) has NO SL! "
                                    f"Re-applying {sl_price}")
                    if sl_price:
                        ok = self.mt5.modify_position(p["ticket"], sl=sl_price)
                        if not ok:
                            logger.critical(f"FAILED to re-apply SL on {p['ticket']}. Emergency close!")
                            self.mt5.close_position(p["ticket"])
                            self.tracked_positions.pop(p["ticket"], None)
                            self.telegram.critical_alert(
                                "SL MISSING AFTER RECONNECT",
                                f"Could not re-apply SL on {p['symbol']} #{p['ticket']}. "
                                f"Position closed.")

    # ────────────────────────────────────────────────────────────────────
    #  Broker UTC offset detection (DST-safe)
    # ────────────────────────────────────────────────────────────────────
    def _get_broker_utc_offset(self) -> int:
        """Auto-detect broker server's UTC offset from tick timestamps.

        MT5's tick.time returns server time encoded as a pseudo-UTC epoch:
        the epoch value represents the *server-local* wall-clock, NOT real UTC.
        Compare with actual UTC epoch to derive the offset.

        Returns offset in hours (e.g. 3 for UTC+3 / EEST).
        """
        import time as _time

        for sym in ['EURUSD', 'SPX500', 'GER30', 'XAUUSD']:
            tick = mt5.symbol_info_tick(sym)
            if tick and tick.time > 0:
                real_utc = int(_time.time())
                offset_hours = round((tick.time - real_utc) / 3600)
                if -12 <= offset_hours <= 14:
                    return offset_hours

        # Fallback: FundedNext follows US DST (UTC+3 when US DST active, else UTC+2)
        import pytz
        ny = pytz.timezone('America/New_York')
        return 3 if bool(datetime.now(ny).dst()) else 2

    # ────────────────────────────────────────────────────────────────────
    #  HTF EMA computation (live: from MT5 H1 bars)
    # ────────────────────────────────────────────────────────────────────
    def _get_htf_trend(self, symbol: str) -> Optional[str]:
        """Fetch 300 H1 bars from MT5, compute 20/50 EMA crossover, return trend."""
        try:
            h1 = self.mt5.get_ohlc_data(symbol, mt5.TIMEFRAME_H1, count=300)
            if h1.empty or len(h1) < 60:
                return None
            h1["ema20"] = h1["close"].ewm(span=20, adjust=False).mean()
            h1["ema50"] = h1["close"].ewm(span=50, adjust=False).mean()
            last = h1.iloc[-1]
            return "bullish" if last["ema20"] > last["ema50"] else "bearish"
        except Exception as e:
            logger.error(f"HTF trend error for {symbol}: {e}")
            return None

    # ────────────────────────────────────────────────────────────────────
    #  Signal generation (live ORB detection)
    # ────────────────────────────────────────────────────────────────────
    def _generate_signal(self, symbol: str) -> Optional[dict]:
        """
        Fetch today's M1 data, slice to session window, run ORB strategy.
        Returns dict with entry info or None.
        """
        config = get_index_config(symbol)
        if not config:
            return None

        # Get 500 M1 bars (covers today's session)
        m1 = self.mt5.get_ohlc_data(symbol, mt5.TIMEFRAME_M1, count=500)
        if m1.empty:
            return None

        # Get MT5 server time and compute session boundaries.
        # PRIMARY: use mt5_open_hour / mt5_close_hour (fixed server times).
        # These match the backtest boundaries and are immune to the 3-week
        # US-DST / EU-non-DST mismatch window (Mar 9–29, Oct–Nov) that
        # caused the old timezone-conversion code to shift sessions by 1 hr.
        #
        # VALIDATION: snap session_start to the first actual M1 bar of the
        # day if a gap > 30 min is detected (handles any residual DST drift
        # or broker schedule changes automatically).
        try:
            broker_offset = self._get_broker_utc_offset()
            from datetime import timezone as _tz, timedelta as _td
            broker_tz = _tz(_td(hours=broker_offset))
            import pytz
            utc_now = datetime.now(pytz.UTC)
            mt5_now = utc_now.astimezone(broker_tz).replace(tzinfo=None)
            today = mt5_now.date()
        except Exception:
            today = datetime.now().date()

        session_start = pd.Timestamp(
            year=today.year, month=today.month, day=today.day,
            hour=config["mt5_open_hour"], minute=config["mt5_open_minute"])
        session_end = pd.Timestamp(
            year=today.year, month=today.month, day=today.day,
            hour=config["mt5_close_hour"], minute=config["mt5_close_minute"])

        # ── Data-driven validation: snap to first bar if config is stale ──
        # During DST mismatch windows the broker may start the session
        # up to 1 h earlier/later than mt5_open_hour suggests.  Detect
        # this by comparing to the actual first bar of today's data.
        # CAP at 90 min — a real DST shift is exactly 60 min; with some
        # tolerance for late first bars this covers it.  Gaps > 90 min
        # are pre-market / extended-hours data (e.g. US indices serving
        # bars from ~14:33 when cash open is 16:30) and must NOT snap.
        today_bars = m1[m1.index.date == today]
        if len(today_bars) > 0:
            first_bar = today_bars.index[0]
            gap_seconds = (session_start - first_bar).total_seconds()
            gap_minutes = gap_seconds / 60
            # Only snap for DST-sized gaps: 30–90 min
            if 30 * 60 < gap_seconds <= 90 * 60:
                logger.info(f"{symbol}: Session start snapped from {session_start} "
                            f"to first data bar {first_bar} "
                            f"(gap={gap_minutes:.0f}min — likely DST offset)")
                # Preserve session duration — shift end by same amount
                duration = session_end - session_start
                session_start = pd.Timestamp(first_bar)
                session_end = session_start + duration
            elif gap_seconds > 90 * 60:
                logger.debug(f"{symbol}: Pre-market/extended-hours data "
                             f"(first bar {first_bar}, gap={gap_minutes:.0f}min) "
                             f"— keeping configured session_start {session_start}")

        session_data = m1[(m1.index >= session_start) & (m1.index <= session_end)]
        if len(session_data) < 20:
            return None

        # HTF trend
        htf_trend = self._get_htf_trend(symbol)

        # Run strategy
        strategy = self.strategies.get(symbol)
        if not strategy:
            return None

        signals = strategy.generate_signals(session_data, htf_trend=htf_trend)

        # ── Detection log: log ORB diagnostics ONCE per symbol per day ──
        if not self._detection_logged_today.get(symbol, False):
            bars_in_session = len(session_data)
            orb_minutes = strategy.orb_minutes if hasattr(strategy, 'orb_minutes') else 15
            orb_slice = session_data.iloc[:orb_minutes]
            orb_hi = float(orb_slice['high'].max()) if len(orb_slice) > 0 else 0
            orb_lo = float(orb_slice['low'].min()) if len(orb_slice) > 0 else 0
            orb_rng = round(orb_hi - orb_lo, 2)

            # Only log once we have enough bars (past ORB formation)
            if bars_in_session >= orb_minutes + 5:
                debug_events = strategy.debug_rows if hasattr(strategy, 'debug_rows') else []
                event_types = [d.get('event', '') for d in debug_events]
                reject_reason = ''
                for d in debug_events:
                    if d.get('event') == 'ORB_REJECT':
                        reject_reason = d.get('reason', 'unknown')
                        break
                    if d.get('event') == 'TREND_CONFLICT':
                        reject_reason = f"HTF={d.get('htf_trend')} vs ORB={d.get('orb_bias')} (bias filter ON)"
                        break

                has_signal = '>>> SIGNAL' if signals else 'no signal'
                entry_sig = ''
                for d in debug_events:
                    if d.get('event') == 'ENTRY_SIGNAL':
                        entry_sig = (f" | Entry={d.get('dir')} @{d.get('entry')}"
                                     f" SL={d.get('sl')} R={d.get('R')}"
                                     f" body={d.get('body_pct')} disp={d.get('disp_ratio')}x")
                        break

                log_parts = [
                    f"{symbol}: ORB Detection",
                    f"bars={bars_in_session}",
                    f"ORB={orb_lo:.2f}-{orb_hi:.2f} rng={orb_rng}",
                    f"HTF={htf_trend or 'N/A'}",
                ]
                if reject_reason:
                    log_parts.append(f"REJECT={reject_reason}")
                log_parts.append(has_signal)
                if entry_sig:
                    log_parts.append(entry_sig)

                logger.info(" | ".join(log_parts))
                _det_logger.info(" | ".join(log_parts))
                self._detection_logged_today[symbol] = True

        if not signals:
            return None

        sig = signals[0]  # 1 trade per day per symbol

        # The signal's entry_time must be reasonably recent (within last 15 bars)
        # to avoid re-entering old signals on restart. Previously 5 bars — too
        # tight; a 15-min news block would make the signal stale before the
        # block lifts. 15 bars gives a safe window for news + reconnect delays.
        entry_ts = pd.Timestamp(sig.entry_time)
        bars_since_entry = len(session_data[session_data.index > entry_ts])
        if bars_since_entry > 15:
            # Record once per symbol (not every 10s)
            if not self._session_recap.get(symbol, {}).get('signal_found'):
                self.edge_guard.record_missed_trade(symbol, "stale_signal", sig.entry_price)
            self._session_recap.setdefault(symbol, {}).update({
                'signal_found': True, 'signal_dir': sig.direction,
                'reason': f'stale_signal ({bars_since_entry} bars old)',
                'entry_price': sig.entry_price,
            })
            return None  # signal is old

        # Price drift guard — BACKTEST-MATCHED
        # Compare signal_price (breakout/retest candle close) vs entry_price
        # (next bar open). This is the same check the backtest uses.
        # Previously this compared live market price vs entry_price, which
        # blocked ~74% of valid signals due to execution latency.
        signal_price = getattr(sig, 'signal_price', None)
        if signal_price is not None and sig.risk_points > 0:
            drift = abs(float(sig.entry_price) - float(signal_price))
            if drift > 0.5 * sig.risk_points:
                cnt = self._drift_log_count.get(symbol, 0) + 1
                self._drift_log_count[symbol] = cnt
                if cnt == 1 or cnt % 10 == 0:
                    logger.info(f"{symbol}: Signal drift - entry vs signal {drift:.2f} pts "
                                f"(> 0.5R = {0.5 * sig.risk_points:.2f}) [x{cnt}]")
                if cnt == 1:
                    self.edge_guard.record_missed_trade(
                        symbol, f"price_drift_{drift:.1f}pts", sig.entry_price)
                self._session_recap.setdefault(symbol, {}).update({
                    'signal_found': True, 'signal_dir': sig.direction,
                    'reason': f'price_drift ({drift:.1f} pts > 0.5R)',
                    'entry_price': sig.entry_price,
                })
                return None

        # Compute ORB range from session data (first orb_minutes)
        orb_minutes = strategy.orb_minutes if hasattr(strategy, 'orb_minutes') else 15
        orb_data = session_data.iloc[:orb_minutes]
        orb_range = round(float(orb_data['high'].max()) - float(orb_data['low'].min()), 2) if len(orb_data) > 0 else 0

        return {
            "direction": sig.direction,
            "entry_price": sig.entry_price,
            "stop_loss": sig.stop_loss,
            "tp": sig.tp3,                     # 2.5R single target
            "risk_points": sig.risk_points,
            "entry_time": sig.entry_time,
            "htf_trend": htf_trend or "",
            "orb_range": orb_range,
            "signal_time": str(sig.entry_time),
            "signal_price": getattr(sig, 'signal_price', sig.entry_price),
            "breakout_time": str(sig.breakout_time) if getattr(sig, 'breakout_time', None) else "",
        }

    # ────────────────────────────────────────────────────────────────────
    #  Pre-trade checks
    # ────────────────────────────────────────────────────────────────────
    def _can_trade(self, symbol: str) -> Tuple[bool, str]:
        if not self._trading_enabled:
            return False, "trading disabled"
        if self._trading_paused:
            return False, "trading paused"
        if sl_is_trading_paused():
            return False, "SL protection pause"
        # Market open check FIRST (avoid noisy news/DD logs for closed markets)
        if not is_market_open(symbol):
            return False, "market closed"
        if self.state.is_daily_limit_hit:
            return False, "daily limit hit"
        if self.trade_taken_today.get(symbol, False):
            return False, "already traded today"
        # Max trades per day across all symbols
        if self.state.daily_trades >= FUNDEDNEXT_RULES.get("max_trades_per_day", 2):
            return False, "max daily trades reached"
        # DD check
        if self.dd_protection:
            account = self.mt5.get_account_info()
            if account:
                dd = self.dd_protection.update(
                    account.get("equity", account.get("balance", 0))
                )
                if not dd["can_trade"]:
                    return False, (f"DD limit (overall={dd['overall_dd']:.1f}%, "
                                   f"daily={dd['daily_dd']:.1f}%)")
        # Edge guard check (rolling metrics + hard rules)
        eg_can, eg_reason = self.edge_guard.can_trade()
        if not eg_can:
            return False, eg_reason
        # Max open positions across ALL symbols (aligned with backtest MAX_OPEN_POSITIONS=2)
        max_open = FUNDEDNEXT_RULES.get('max_open_positions', 2)
        all_positions = self.mt5.get_open_positions()
        if len(all_positions) >= max_open:
            return False, f"max open positions ({max_open}) reached"
        # Existing position check (1 per symbol max)
        existing = self.mt5.get_open_positions(symbol)
        if len(existing) >= 1:
            return False, "position already open"
        # News filter (Forex Factory high-impact only, 5min before / 15min after)
        try:
            is_news, event = self.news_filter.is_news_time(symbol)
        except Exception as e:
            logger.error(f"News filter error for {symbol}: {e}")
            is_news, event = False, {}
        if is_news:
            mins = event.get('minutes_away', 0)
            label = f"in {mins:.0f}m" if mins > 0 else f"{abs(mins):.0f}m ago"
            logger.info(f"{symbol}: News blocked - {event.get('title', 'Unknown')} {label}")
            # Telegram notification — max once per event per symbol (throttle 10 min)
            last_notify = self._block_notify_sent.get(symbol)
            if last_notify is None or (datetime.now() - last_notify).total_seconds() > 600:
                self.telegram.news_blocked(symbol, event['title'], int(mins))
                self._block_notify_sent[symbol] = datetime.now()
            return False, f"news: {event['title']}"
        return True, "ok"

    # ────────────────────────────────────────────────────────────────────
    #  Execute trade
    # ────────────────────────────────────────────────────────────────────
    def _execute_signal(self, symbol: str, sig: dict):
        """Place market order for ORB signal."""
        account = self.mt5.get_account_info()
        if not account:
            return

        balance = account["balance"]
        free_margin = account["free_margin"]
        risk_pct = self.dd_protection.get_adaptive_risk(balance)

        # C3 FIX: Sync adaptive risk to SL protection emergency threshold
        sl_update_risk(risk_pct / 100)

        order_type = "buy" if sig["direction"] == "bullish" else "sell"
        comment = f"ORB_{symbol}"

        # ── MINIMUM R GUARD ───────────────────────────────────────────
        # Reject signals where R is too small for live execution.
        MIN_R_BY_SYMBOL = {"SPX500": 3.0, "US2000": 3.0}
        MIN_R_POINTS = MIN_R_BY_SYMBOL.get(symbol, 5.0)
        if sig["risk_points"] < MIN_R_POINTS:
            logger.warning(
                f"{symbol}: MIN R GUARD — R={sig['risk_points']:.2f} pts < "
                f"{MIN_R_POINTS} minimum. Trade REJECTED.")
            self.edge_guard.record_missed_trade(
                symbol, f"min_R_{sig['risk_points']:.1f}pts",
                sig.get('entry_price', 0))
            self.telegram.send_message(
                f"⚠️ {symbol}: Trade skipped — R={sig['risk_points']:.1f} pts "
                f"too small (min {MIN_R_POINTS} pts)")
            self._session_recap.setdefault(symbol, {}).update({
                'signal_found': True, 'signal_dir': sig.get('direction', ''),
                'reason': f'min_R ({sig["risk_points"]:.1f} pts < {MIN_R_POINTS})',
                'entry_price': sig.get('entry_price', 0),
            })
            # Mark trade taken to prevent retry loop (same signal won't improve)
            self.trade_taken_today[symbol] = True
            return

        # Random delay 0.3-0.8s (prop-firm detection avoidance)
        delay = random.uniform(0.3, 0.8)
        time.sleep(delay)

        # ── LIVE PRICE for accurate lot sizing ────────────────────────
        # Use current bid/ask (not stale signal entry) so lot size
        # reflects the ACTUAL risk distance at moment of execution.
        bid, ask = self.mt5.get_current_price(symbol)
        expected_price = ask if order_type == "buy" else bid
        spread_pts_entry = 0
        try:
            tick_info = mt5.symbol_info_tick(symbol)
            if tick_info:
                spread_pts_entry = tick_info.spread
        except Exception:
            pass

        # Compute live R (from expected fill to SL)
        if expected_price and sig["stop_loss"]:
            live_risk_points = abs(expected_price - sig["stop_loss"])
        else:
            live_risk_points = sig["risk_points"]
        # Safety: if live R is unreasonably small, use signal R
        if live_risk_points < 0.5:
            live_risk_points = sig["risk_points"]
        # Log if live R differs significantly from signal R
        if sig["risk_points"] > 0:
            r_drift_pct = (live_risk_points - sig["risk_points"]) / sig["risk_points"]
            if abs(r_drift_pct) > 0.10:
                logger.info(
                    f"{symbol}: LOT SIZING R adjusted — signal_R={sig['risk_points']:.2f} → "
                    f"live_R={live_risk_points:.2f} ({r_drift_pct:+.0%})")

        # Position sizing with live broker data — use LIVE R for accurate risk
        sizing = calculate_position_size_with_broker_data(
            symbol, balance, risk_pct, live_risk_points,
            free_margin=free_margin, max_margin_percent=90.0
        )
        lots = sizing["lots"]
        if lots <= 0:
            logger.warning(f"{symbol}: Position size = 0 lots, skipping")
            self.edge_guard.record_missed_trade(symbol, "zero_lot_size", sig.get('entry_price', 0))
            return

        # ── DRIFT GUARD (v3.14.1) ──────────────────────────────────────
        # Reject trades where price has drifted > MAX_DRIFT_R from signal
        # entry.  Backtested on 2.8yr (6 idx) and 10yr (5 idx):
        #   2.8yr: ±0.75R = +402R (best), ±1.00R = +392R (-2.5%)
        #   10yr:  ±1.00R = +1267R,        ±0.75R = +1192R (-6%)
        # ±1.00R balances both datasets and blocks extreme drift (>1.65R).
        MAX_DRIFT_R = 1.00  # R-multiples — validated via run_drift_guard_sim.py
        if expected_price and sig["risk_points"] > 0:
            drift_from_signal = abs(expected_price - sig["entry_price"])
            drift_r = drift_from_signal / sig["risk_points"]
            if drift_r > MAX_DRIFT_R:
                logger.warning(
                    f"{symbol}: DRIFT GUARD — price drifted {drift_from_signal:.2f} pts "
                    f"({drift_r:.2f}R) from signal entry ({sig['entry_price']:.2f} → "
                    f"{expected_price:.2f}). Max allowed: {MAX_DRIFT_R}R. Trade REJECTED.")
                self.edge_guard.record_missed_trade(
                    symbol, f"drift_guard_{drift_r:.2f}R",
                    sig.get('entry_price', 0))
                self.telegram.send_message(
                    f"⚠️ {symbol}: Trade skipped — price drifted {drift_r:.2f}R "
                    f"({drift_from_signal:.1f} pts) from signal. Max {MAX_DRIFT_R}R.")
                self._session_recap.setdefault(symbol, {}).update({
                    'signal_found': True, 'signal_dir': sig.get('direction', ''),
                    'reason': f'drift_guard ({drift_r:.2f}R > {MAX_DRIFT_R}R)',
                    'entry_price': sig.get('entry_price', 0),
                })
                return
            elif drift_r > 0.25:
                logger.info(
                    f"{symbol}: PRE-ORDER DRIFT — {drift_from_signal:.2f} pts "
                    f"({drift_r:.2f}R) from signal entry. Within guard ({MAX_DRIFT_R}R). "
                    f"Proceeding.")

        # ── SPREAD-TO-R GUARD ──────────────────────────────────────────
        # Reject if spread > 30% of risk (R) — spread eats too much edge.
        # US2000 typical spread ~0.9 pts on R~7.7 = 12% → OK
        MAX_SPREAD_R_PCT = 0.30  # 30% of R
        if live_risk_points > 0:
            # Convert tick-based spread to point spread
            sym_info = mt5.symbol_info(symbol)
            if not sym_info or sym_info.point <= 0:
                logger.warning(f"{symbol}: Cannot get symbol info for spread check, skipping trade")
                self.edge_guard.record_missed_trade(
                    symbol, "no_symbol_info_for_spread_check",
                    sig.get('entry_price', 0))
                return
            spread_points = spread_pts_entry * sym_info.point
            spread_r_ratio = spread_points / live_risk_points
            if spread_r_ratio > MAX_SPREAD_R_PCT:
                logger.warning(
                    f"{symbol}: SPREAD GUARD — spread={spread_points:.2f} pts "
                    f"({spread_r_ratio:.0%} of R={live_risk_points:.2f}) > "
                    f"{MAX_SPREAD_R_PCT:.0%} threshold. Trade REJECTED.")
                self.edge_guard.record_missed_trade(
                    symbol, f"spread_guard_{spread_r_ratio:.0%}R",
                    sig.get('entry_price', 0))
                self.telegram.send_message(
                    f"⚠️ {symbol}: Trade skipped — spread {spread_points:.1f} pts "
                    f"= {spread_r_ratio:.0%} of R (max {MAX_SPREAD_R_PCT:.0%})")
                self._session_recap.setdefault(symbol, {}).update({
                    'signal_found': True, 'signal_dir': sig.get('direction', ''),
                    'reason': f'spread_guard ({spread_r_ratio:.0%} of R)',
                    'entry_price': sig.get('entry_price', 0),
                })
                return

        # ── TP RECALCULATION FROM LIVE PRICE (v3.14.1) ────────────────
        # Backtest: TP = entry ± R × tp_r, where entry IS the fill price.
        # Live pre-fix: signal TP was anchored to the strategy's entry_price
        # (a past bar's open), which can drift far from current market.
        # Fix: recalculate TP from expected_price (live bid/ask) so the R:R
        # ratio matches backtest behavior (2.5R from actual execution price).
        _tp_r = 2.5  # default
        if expected_price and live_risk_points > 0 and sig["risk_points"] > 0:
            _tp_r = abs(sig["tp"] - sig["entry_price"]) / sig["risk_points"]
            if _tp_r < 1.0 or _tp_r > 10.0:
                _tp_r = 2.5  # sanity fallback
            original_tp = sig["tp"]
            if order_type == "buy":
                sig["tp"] = expected_price + live_risk_points * _tp_r
            else:
                sig["tp"] = expected_price - live_risk_points * _tp_r
            tp_shift = abs(sig["tp"] - original_tp)
            if tp_shift > 0.01:
                logger.info(
                    f"{symbol}: TP recalculated from live price — "
                    f"{original_tp:.2f} → {sig['tp']:.2f} "
                    f"(shift={tp_shift:.2f} pts, tp_r={_tp_r:.1f})")

        # ── ROUND SL/TP/LOTS to broker tick precision ──────────────
        # Eliminates floating-point noise (e.g. 6700.015000000001 → 6700.02)
        sym_info_round = mt5.symbol_info(symbol)
        if sym_info_round and sym_info_round.digits >= 0:
            _digits = sym_info_round.digits
            sig["stop_loss"] = round(sig["stop_loss"], _digits)
            sig["tp"] = round(sig["tp"], _digits)
            sig["entry_price"] = round(sig["entry_price"], _digits)

        t_order_start = time.perf_counter()
        result = self.mt5.place_order(
            symbol, order_type, lots,
            sl=sig["stop_loss"], tp=sig["tp"],
            comment=comment
        )
        exec_latency_ms = round((time.perf_counter() - t_order_start) * 1000)

        if not result["success"]:
            logger.error(f"{symbol}: Order failed - {result.get('error')}")
            self.telegram.order_failed(symbol, order_type, result.get("error", "Unknown"))
            self.edge_guard.record_missed_trade(
                symbol, f"order_failed: {result.get('error', 'unknown')}",
                sig.get('entry_price', 0))
            # Mark trade taken to prevent infinite retry loop.
            # Failed orders (Invalid stops, price drift) won't succeed on retry.
            self.trade_taken_today[symbol] = True
            return

        ticket = result["ticket"]
        position_id = result.get("position_id", ticket)
        fill_price = result.get("fill_price", result["price"])
        # BUG FIX: IOC fills can return fill_price=0.0 on some brokers
        if not fill_price:
            fill_price = expected_price or result["price"]
            logger.warning(f"{symbol}: fill_price was 0.0, using fallback={fill_price}")

        # Slippage monitoring
        slippage = abs(fill_price - expected_price) if expected_price else 0
        if slippage > 0:
            logger.info(f"{symbol}: Slippage = {slippage:.2f} points "
                        f"(expected {expected_price}, got {fill_price})")

        # ── POST-FILL RISK GUARD ──────────────────────────────────────
        # If actual SL distance exceeds 1.5× the R we sized on (live_risk_points),
        # the fill drifted too far — close immediately to cap risk.
        actual_R = abs(fill_price - sig["stop_loss"])
        if live_risk_points > 0 and actual_R > 1.5 * live_risk_points:
            logger.critical(
                f"{symbol}: POST-FILL RISK BREACH — actual_R={actual_R:.2f} pts "
                f"({actual_R / live_risk_points:.1f}× sized_R={live_risk_points:.2f}). "
                f"Fill={fill_price}, SL={sig['stop_loss']}. CLOSING immediately.")
            self.telegram.critical_alert(
                "POST-FILL RISK BREACH",
                f"{symbol}: Fill drifted too far from sized R.\n"
                f"Sized R: {live_risk_points:.2f} pts\n"
                f"Actual R: {actual_R:.2f} pts ({actual_R / live_risk_points:.1f}×)\n"
                f"Closing position to cap risk.")
            # Close the just-opened position (use position_id, not order ticket)
            closed_ok = self.mt5.close_position(position_id)
            self.trade_taken_today[symbol] = True
            self.state.daily_trades += 1
            if closed_ok:
                details = self._get_closed_trade_details(position_id, expected_symbol=symbol)
                pnl = details['pnl']
                self.state.daily_pnl += pnl
            else:
                # Close failed — still track position so SL management works,
                # but the oversized SL remains. Log critical for manual review.
                logger.critical(f"{symbol}: POST-FILL close FAILED — position {ticket} "
                                f"remains open with oversized SL!")
                self.tracked_positions[ticket] = {
                    "symbol": symbol,
                    "direction": sig["direction"],
                    "entry": fill_price,
                    "sl_init": sig["stop_loss"],
                    "tp": sig["tp"],
                    "R": live_risk_points,
                    "position_id": position_id,
                    "hit_be": False,
                    "max_favorable": 0.0,
                    "max_adverse": 0.0,
                    "volume": lots,
                    "entry_time": str(datetime.now()),
                    "journal_trade_id": None,
                    "slippage_pts": slippage,
                    "fill_drift_pts": abs(fill_price - sig["entry_price"]),
                    "spread_entry": spread_pts_entry,
                    "exec_latency_ms": exec_latency_ms,
                }
            self.edge_guard.record_missed_trade(
                symbol, f"post_fill_risk_breach_{actual_R:.0f}pts",
                sig.get('entry_price', 0))
            self._save_state()
            return

        # ── TP FINE-TUNE FROM ACTUAL FILL (v3.14.1) ──────────────────
        # If fill differs from expected_price (slippage), adjust TP so
        # it's exactly _tp_r × R from the actual fill price.
        # This is an exact match of backtest behavior where entry IS fill.
        fill_R = abs(fill_price - sig["stop_loss"])
        if fill_R > 0:
            if order_type == "buy":
                final_tp = fill_price + fill_R * _tp_r
            else:
                final_tp = fill_price - fill_R * _tp_r
            # Round to broker digits
            _rd = sym_info_round.digits if (sym_info_round and hasattr(sym_info_round, 'digits') and sym_info_round.digits >= 0) else 2
            final_tp = round(final_tp, _rd)
            if abs(final_tp - sig["tp"]) > 0.01:
                ok = self.mt5.modify_position(ticket, tp=final_tp)
                if ok:
                    logger.info(
                        f"{symbol}: TP fine-tuned from fill — "
                        f"{sig['tp']:.2f} → {final_tp:.2f} "
                        f"(fill={fill_price}, fill_R={fill_R:.2f})")
                else:
                    logger.warning(f"{symbol}: Failed to fine-tune TP from fill price")
            sig["tp"] = final_tp
            # Use fill-based R for tracked_positions (matches backtest: R = |entry - SL|)
            live_risk_points = fill_R

        # F2 FIX: Track position and mark trade taken BEFORE SL verification.
        # This prevents re-entry loops if SL verification fails and emergency-closes,
        # and ensures the P&L from any emergency close is captured.
        self.trade_taken_today[symbol] = True
        self.state.daily_trades += 1
        # Reset no-trade streak on successful fill
        self._no_trade_streak = 0
        self._last_trade_date = _get_fn_day()
        self.tracked_positions[ticket] = {
            "symbol": symbol,
            "direction": sig["direction"],
            "entry": fill_price,
            "sl_init": sig["stop_loss"],
            "tp": sig["tp"],
            "R": live_risk_points,
            "position_id": position_id,
            "hit_be": False,
            "max_favorable": 0.0,
            "max_adverse": 0.0,
            "volume": lots,
            "entry_time": str(datetime.now()),
            "journal_trade_id": None,  # set after add_trade
            "slippage_pts": slippage,
            "fill_drift_pts": abs(fill_price - sig["entry_price"]),
            "spread_entry": spread_pts_entry,
            "exec_latency_ms": exec_latency_ms,
        }

        # Verify SL is correctly set (sl_protection: 3 retries + emergency close)
        sl_ok = verify_sl_after_entry(ticket, sig["stop_loss"], sig["tp"], symbol)
        if not sl_ok:
            logger.critical(f"{symbol}: SL verification FAILED - position emergency closed")
            self.telegram.critical_alert("SL VERIFICATION FAILED",
                                         f"Entry on {symbol} closed because SL "
                                         f"could not be verified")
            # Capture PnL from the emergency close and clean up tracking
            self.tracked_positions.pop(ticket, None)
            pnl = self._get_closed_trade_pnl(ticket)
            self.state.daily_pnl += pnl
            self._save_state()
            return

        # Compute ACTUAL risk based on lots × live_R × point_value
        # (sizing["risk_amount"] is the intended budget, but margin caps may reduce lots)
        # MUST be computed BEFORE journal/telegram use it.
        _pt_val = sizing.get("point_value", 10.0)
        actual_risk_amount = round(lots * live_risk_points * _pt_val, 2)

        # Journal + notification
        trade_id = self.journal.add_trade({
            "symbol": symbol,
            "direction": order_type,
            "lot_size": lots,
            "planned_entry": sig["entry_price"],
            "entry_price": fill_price,
            "stop_loss": sig["stop_loss"],
            "tp1": sig["tp"],
            "tp2": sig["tp"],
            "tp3": sig["tp"],
            "risk_amount": actual_risk_amount,
            "risk_points": live_risk_points,
            "signal_risk_points": sig["risk_points"],
            "point_value": sizing.get("point_value", 1.0),
            "planned_rr": 2.5,
            "signal_time": sig.get("signal_time", ""),
            "htf_trend": sig.get("htf_trend", ""),
            "orb_range": sig.get("orb_range", 0),
            "spread_entry": spread_pts_entry,
            "session": self._get_session_name(symbol),
            "weekday": datetime.now().strftime("%A"),
            "notes": f"sigR={sig['risk_points']:.2f} liveR={live_risk_points:.2f} slip={slippage:+.2f} spread={spread_pts_entry} lat={exec_latency_ms}ms",
        })

        # Link journal trade_id to tracked position for close_trade
        if ticket in self.tracked_positions:
            self.tracked_positions[ticket]["journal_trade_id"] = trade_id

        try:
            self.telegram.trade_entry(
                symbol=symbol,
                direction=order_type.upper(),
                entry_price=fill_price,
                sl=sig["stop_loss"],
                tp1=sig["tp"],
                tp2=sig["tp"],
                tp3=sig["tp"],
                lot_size=lots,
                risk_amount=actual_risk_amount,
            )
        except Exception as e:
            logger.error(f"Telegram trade_entry notification failed: {e}")

        logger.info(f"TRADE: {order_type.upper()} {lots} {symbol} @ {fill_price} | "
                     f"SL: {sig['stop_loss']} | TP: {sig['tp']} | "
                     f"sigR: {sig['risk_points']:.2f} | liveR: {live_risk_points:.2f} | "
                     f"risk: ${actual_risk_amount:.2f} ({actual_risk_amount/balance*100:.1f}%)")
        _det_logger.info(f"{symbol}: FILLED {order_type.upper()} {lots} lots @ {fill_price} | "
                         f"SL={sig['stop_loss']} TP={sig['tp']} risk=${actual_risk_amount:.2f}")

        # Record trade in session recap
        self._session_recap.setdefault(symbol, {}).update({
            'signal_found': True, 'signal_dir': sig['direction'],
            'reason': 'traded', 'entry_price': fill_price,
            'trade_taken': True, 'trade_pnl': 0,  # PnL updated on close
        })

        self._save_state()

    # ────────────────────────────────────────────────────────────────────
    #  Position management (ORB: BE at 1R, trail 0.5R buffer, TP at 2.5R)
    # ────────────────────────────────────────────────────────────────────
    def _manage_positions(self):
        """
        For each tracked position:
          1. Check if closed by broker/SL/TP
          2. Move SL to BE when 1R profit reached
          3. Trail SL with 0.5R buffer after BE
          4. Force close near session end (no overnight)
          5. Detect missing SL and re-apply or emergency close
        """
        mt5_positions = self.mt5.get_open_positions()
        mt5_tickets = {p["ticket"]: p for p in mt5_positions}

        closed_tickets = []
        for ticket, info in list(self.tracked_positions.items()):
            symbol = info["symbol"]

            # ── Position closed externally (TP hit, SL hit, manual) ────
            if ticket not in mt5_tickets:
                # Guard: skip if already processed this cycle
                if ticket in closed_tickets:
                    continue
                closed_tickets.append(ticket)
                # Query deal history for actual PnL, exit price, and reason
                # Use position_id if available (may differ from order ticket on some brokers)
                pos_id = info.get("position_id", ticket)
                details = self._get_closed_trade_details(pos_id, expected_symbol=symbol)
                pnl = details['pnl']
                exit_price = details['exit_price']
                exit_reason = details['exit_reason']

                # ── Symbol mismatch fallback: compute PnL from position data ──
                if details.get('symbol_mismatch'):
                    try:
                        # Method 1: balance delta using pre-manage snapshot
                        ref_balance = getattr(self, '_pre_manage_balance', self.state.current_balance)
                        acct = mt5.account_info()
                        if acct:
                            new_balance = acct.balance
                            pnl = round(new_balance - ref_balance, 2)
                            logger.info(f"Symbol mismatch fallback: balance ${ref_balance:.2f} -> "
                                        f"${new_balance:.2f}, PnL=${pnl:.2f}")

                        # Method 2: if balance delta is zero (race condition),
                        # compute expected PnL from tracked position SL/TP + point value
                        sl = info.get('sl_init', 0)
                        tp = info.get('tp', 0)
                        entry = info.get('entry', 0)
                        lots = info.get('volume', 0)
                        direction = info.get('direction', '')

                        if pnl == 0.0 and entry and lots:
                            pv = self._get_point_value(symbol)
                            if direction == 'sell':
                                sl_pnl = round((entry - sl) * lots * pv, 2)  # negative
                                tp_pnl = round((entry - tp) * lots * pv, 2)  # positive
                            else:
                                sl_pnl = round((sl - entry) * lots * pv, 2)  # negative
                                tp_pnl = round((tp - entry) * lots * pv, 2)  # positive
                            # Use whichever is closer to actual balance change
                            if acct:
                                actual_balance = acct.balance
                                bal_delta = actual_balance - ref_balance
                                # If balance dropped, it's SL; if rose, it's TP
                                if bal_delta < -1.0:  # noticeable loss
                                    pnl = sl_pnl
                                    logger.info(f"PnL computed from SL: entry={entry}, SL={sl}, "
                                                f"lots={lots}, pv={pv:.2f}, PnL=${pnl:.2f}")
                                elif bal_delta > 1.0:  # noticeable gain
                                    pnl = tp_pnl
                                    logger.info(f"PnL computed from TP: entry={entry}, TP={tp}, "
                                                f"lots={lots}, pv={pv:.2f}, PnL=${pnl:.2f}")
                                else:
                                    # Truly zero or near-zero — use SL as worst case
                                    pnl = sl_pnl
                                    logger.warning(f"Balance delta ~$0 — defaulting to SL PnL=${pnl:.2f}")

                        if pnl < 0:
                            exit_price = sl
                            exit_reason = 'SL_HIT'
                        else:
                            exit_price = tp
                            exit_reason = 'TP'
                    except Exception as e:
                        logger.error(f"Balance fallback failed for {ticket}: {e}")

                # Deferred re-query: if deal history failed, schedule a
                # background retry after 10s to update journal with real P&L
                if pnl == 0.0 and exit_price == 0:
                    logger.warning(f"Deal history unavailable for {ticket} ({symbol}) "
                                   f"- scheduling deferred re-query")
                    self._schedule_deferred_pnl_update(ticket, symbol, info)

                self.state.daily_pnl += pnl
                logger.info(f"Position {ticket} ({symbol}) closed: {exit_reason}, "
                            f"exit={exit_price}, PnL=${pnl:.2f}")
                _det_logger.info(f"{symbol}: CLOSED {exit_reason} @ {exit_price} | "
                                 f"PnL=${pnl:+.2f} | daily=${self.state.daily_pnl:+.2f}")
                
                # Journal close with MFE/MAE
                pos_R = info.get("R", 0)
                trade_id = info.get("journal_trade_id")
                if trade_id:
                    mfe_pts = round(info.get("max_favorable", 0) * pos_R, 2) if pos_R else 0
                    mae_pts = round(info.get("max_adverse", 0) * pos_R, 2) if pos_R else 0
                    pnl_pts = pnl / (info.get("volume", 1) * self._get_point_value(symbol)) if info.get("volume") else 0
                    # Compute DD% from initial balance
                    _bal_after = self.state.current_balance + pnl
                    _dd_pct = round(((ACCOUNT_SIZE - _bal_after)
                                     / ACCOUNT_SIZE) * 100, 2) if ACCOUNT_SIZE else 0
                    # Exit spread + exit slippage (from current tick at close time)
                    _spread_exit = 0
                    _exit_slip = 0
                    try:
                        _tick = mt5.symbol_info_tick(symbol)
                        if _tick:
                            _spread_exit = _tick.ask - _tick.bid
                            # Exit slippage: diff between expected exit and actual
                            if exit_reason == 'TP' and info.get('tp'):
                                _exit_slip = round(abs(exit_price - info['tp']), 4)
                            elif exit_reason == 'SL_HIT' and info.get('sl_init'):
                                _exit_slip = round(abs(exit_price - info['sl_init']), 4)
                    except Exception:
                        pass
                    # Risk tier label
                    _risk_pct = self.dd_protection.get_adaptive_risk(_bal_after)
                    if _risk_pct <= 0.5:
                        _risk_tier = 'TIER 2'
                    else:
                        _risk_tier = 'TIER 1'
                    self.journal.close_trade(trade_id, {
                        "exit_price": exit_price,
                        "exit_reason": exit_reason,
                        "pnl": pnl,
                        "pnl_percent": round(pnl / self.state.current_balance * 100, 3) if self.state.current_balance else 0,
                        "pnl_points": round(pnl_pts, 2),
                        "mfe_points": mfe_pts,
                        "mae_points": mae_pts,
                        "be_hit": "Yes" if info.get("hit_be") else "No",
                        "balance_after": _bal_after,
                        "drawdown": _dd_pct,
                        "spread_exit": round(_spread_exit, 4),
                        "exit_slippage": round(_exit_slip, 4),
                        "commission": details.get('commission', 0),
                        "swap": details.get('swap', 0),
                        "risk_tier": _risk_tier,
                        "point_value": self._get_point_value(symbol),
                        "lots": info.get('volume', 0),
                    })
                
                # EdgeGuard: record for rolling metrics
                _actual_rr = 0.0
                if pos_R and info.get("volume"):
                    _pv = self._get_point_value(symbol)
                    _denom = pos_R * _pv * info['volume']
                    _actual_rr = round(pnl / _denom, 2) if _denom else 0
                self.edge_guard.record_trade({
                    'date': datetime.now().strftime("%Y-%m-%d"),
                    'symbol': symbol,
                    'direction': info.get('direction', ''),
                    'pnl': pnl,
                    'actual_rr': _actual_rr,
                    'slippage_pts': info.get('slippage_pts', 0),
                    'fill_drift_pts': info.get('fill_drift_pts', 0),
                    'spread_entry': info.get('spread_entry', 0),
                    'balance_after': self.state.current_balance + pnl,
                    'session': self._get_session_name(symbol),
                    'weekday': datetime.now().strftime("%A"),
                })
                if self.edge_guard.is_edge_degraded:
                    self.telegram.critical_alert("EDGE DEGRADED",
                        f"Auto-stop: {'; '.join(self.edge_guard.degradation_reasons)}\n"
                        f"Use /start to override.")

                # Level 1 Audit: send report via Telegram at exactly 20 trades
                if hasattr(self.edge_guard, '_level1_audit_report') and self.edge_guard._level1_audit_report:
                    audit_text = self.edge_guard._level1_audit_report
                    self.telegram.send_message(
                        f"📋 <b>LEVEL 1 EXECUTION AUDIT</b>\n\n"
                        f"<pre>{audit_text[:3800]}</pre>")
                    self.edge_guard._level1_audit_report = None

                self.telegram.trade_closed(
                    symbol=symbol,
                    reason=exit_reason,
                    profit=pnl,
                )
                # Update session recap with actual PnL
                if symbol in self._session_recap:
                    self._session_recap[symbol]['trade_pnl'] = pnl
                continue

            pos = mt5_tickets[ticket]
            direction = info.get("direction", "")
            entry = info.get("entry", 0)
            R = info.get("R", 0) or 0  # guard against None from corrupted state
            current_sl = pos["sl"]

            # Current price
            bid, ask = self.mt5.get_current_price(symbol)
            if not bid or not ask:
                continue

            if direction in ("bullish", "buy"):
                current_price = bid  # exit price for longs
            else:
                current_price = ask  # exit price for shorts

            # Unrealized P&L in R-multiples
            if direction in ("bullish", "buy"):
                unrealized_r = (current_price - entry) / R if R > 0 else 0
            else:
                unrealized_r = (entry - current_price) / R if R > 0 else 0

            # Track MFE and MAE continuously
            if unrealized_r > info.get("max_favorable", 0):
                info["max_favorable"] = unrealized_r
            if unrealized_r < 0 and abs(unrealized_r) > info.get("max_adverse", 0):
                info["max_adverse"] = abs(unrealized_r)

            # ── Force close 15 min before session end ──────────────────
            config = get_index_config(symbol)
            if config:
                import pytz
                force_close_triggered = False
                try:
                    # Use server-time force-close to match session boundaries
                    # and avoid DST-mismatch bugs between US/EU clocks.
                    from datetime import timezone as _fctz, timedelta as _fctd
                    _fc_offset = self._get_broker_utc_offset()
                    _fc_broker_tz = _fctz(_fctd(hours=_fc_offset))
                    _fc_utc_now = datetime.now(pytz.UTC)
                    local_now = _fc_utc_now.astimezone(_fc_broker_tz).replace(tzinfo=None)
                    now_min = local_now.hour * 60 + local_now.minute

                    # Use mt5_ hours (fixed server time, DST-immune)
                    force_h = config.get("force_close_hour", config["market_close_hour"])
                    force_m = config.get("force_close_minute", config.get("market_close_minute", 0))
                    # Convert force_close from local market tz to server tz
                    # by using the mt5_close as anchor and offset back 15 min
                    mt5_close_h = config.get("mt5_close_hour", 23)
                    mt5_close_m = config.get("mt5_close_minute", 0)
                    force_min = mt5_close_h * 60 + mt5_close_m - 15  # 15 min before session end
                    close_min = mt5_close_h * 60 + mt5_close_m

                    if force_min <= now_min < close_min:
                        force_close_triggered = True
                except Exception as tz_err:
                    logger.error(f"Force-close time check failed for {symbol}: {tz_err}")

                # F1+F3 FIX: Single force-close path, use deal history for accurate PnL
                if force_close_triggered:
                    logger.info(f"Force closing {ticket} ({symbol}) - session ending")
                    _fc_pos_id = info.get("position_id", ticket)
                    ok = self.mt5.close_position(_fc_pos_id)
                    if ok:
                        _fc_details = self._get_closed_trade_details(_fc_pos_id, expected_symbol=symbol)
                        pnl = _fc_details['pnl']
                        _fc_exit_price = _fc_details['exit_price'] or current_price
                        self.state.daily_pnl += pnl
                        
                        # Journal close with MFE/MAE
                        trade_id = info.get("journal_trade_id")
                        if trade_id:
                            mfe_pts = round(info.get("max_favorable", 0) * R, 2) if R else 0
                            mae_pts = round(info.get("max_adverse", 0) * R, 2) if R else 0
                            pnl_pts = pnl / (info.get("volume", 1) * self._get_point_value(symbol)) if info.get("volume") else 0
                            # Compute DD% and risk tier for session close
                            _sc_bal = self.state.current_balance + pnl
                            _sc_dd = round(((ACCOUNT_SIZE - _sc_bal) / ACCOUNT_SIZE) * 100, 2) if ACCOUNT_SIZE else 0
                            _sc_risk = self.dd_protection.get_adaptive_risk(_sc_bal)
                            _sc_tier = 'TIER 2' if _sc_risk <= 0.5 else 'TIER 1'
                            self.journal.close_trade(trade_id, {
                                "exit_price": _fc_exit_price,
                                "exit_reason": "SESSION_CLOSE",
                                "pnl": pnl,
                                "pnl_percent": round(pnl / self.state.current_balance * 100, 3) if self.state.current_balance else 0,
                                "pnl_points": round(pnl_pts, 2),
                                "mfe_points": mfe_pts,
                                "mae_points": mae_pts,
                                "be_hit": "Yes" if info.get("hit_be") else "No",
                                "balance_after": _sc_bal,
                                "drawdown": _sc_dd,
                                "commission": 0,
                                "swap": 0,
                                "risk_tier": _sc_tier,
                                "point_value": self._get_point_value(symbol),
                                "lots": info.get('volume', 0),
                            })
                        
                        self.telegram.trade_closed(symbol, "SESSION_CLOSE", pnl)
                        # Update session recap with actual PnL
                        if symbol in self._session_recap:
                            self._session_recap[symbol]['trade_pnl'] = pnl

                        # EdgeGuard: record for rolling metrics
                        _fc_rr = 0.0
                        if R and info.get("volume"):
                            _fc_pv = self._get_point_value(symbol)
                            _fc_denom = R * _fc_pv * info['volume']
                            _fc_rr = round(pnl / _fc_denom, 2) if _fc_denom else 0
                        self.edge_guard.record_trade({
                            'date': datetime.now().strftime("%Y-%m-%d"),
                            'symbol': symbol,
                            'direction': info.get('direction', ''),
                            'pnl': pnl,
                            'actual_rr': _fc_rr,
                            'slippage_pts': info.get('slippage_pts', 0),
                            'fill_drift_pts': info.get('fill_drift_pts', 0),
                            'spread_entry': info.get('spread_entry', 0),
                            'balance_after': self.state.current_balance + pnl,
                            'session': self._get_session_name(symbol),
                            'weekday': datetime.now().strftime("%A"),
                        })
                        if self.edge_guard.is_edge_degraded:
                            self.telegram.critical_alert("EDGE DEGRADED",
                                f"Auto-stop: {'; '.join(self.edge_guard.degradation_reasons)}\n"
                                f"Use /start to override.")

                        closed_tickets.append(ticket)
                    else:
                        logger.error(f"FAILED to force-close {ticket} ({symbol})! "
                                     f"Retrying next cycle.")
                    continue

            # ── SL missing check (broker stripped SL) ──────────────────
            if current_sl == 0.0:
                sl_to_apply = info.get("sl_init", 0)
                logger.critical(f"Position {ticket} ({symbol}) has NO SL! "
                                f"Re-applying {sl_to_apply}")
                if sl_to_apply:
                    ok = self.mt5.modify_position(ticket, sl=sl_to_apply)
                    if not ok:
                        logger.critical(f"CANNOT re-apply SL - emergency closing {ticket}")
                        self.mt5.close_position(ticket)
                        self.telegram.critical_alert("SL DISAPPEARED",
                                                     f"Emergency close {symbol} #{ticket}")
                        closed_tickets.append(ticket)
                else:
                    # No SL on record — emergency close to protect account
                    logger.critical(f"Position {ticket} ({symbol}) has NO SL and no sl_init! "
                                    f"Emergency closing.")
                    self.mt5.close_position(ticket)
                    self.telegram.critical_alert("NO SL ON RECORD",
                                                 f"Emergency close {symbol} #{ticket} — sl_init=0")
                    closed_tickets.append(ticket)
                continue

            # ── BE trigger (at 1R profit) ──────────────────────────────
            if not info["hit_be"] and unrealized_r >= 1.0:
                spread = abs(ask - bid) if bid and ask else 0
                if direction in ("bullish", "buy"):
                    new_sl = entry + spread * 0.5  # BE + half spread
                else:
                    new_sl = entry - spread * 0.5

                ok = self.mt5.modify_position(ticket, sl=new_sl)
                if ok:
                    info["hit_be"] = True
                    info["max_favorable"] = unrealized_r
                    sl_track_original_sl(ticket, new_sl)  # sync SL tracker
                    logger.info(f"BE triggered on {ticket} ({symbol}): SL -> {new_sl:.2f}")
                    self.telegram.sl_moved(symbol, current_sl, new_sl, "breakeven")

            # ── Trailing after BE (0.5R buffer) ────────────────────────
            if info["hit_be"]:
                if unrealized_r > info.get("max_favorable", 0):
                    info["max_favorable"] = unrealized_r

                trail_r = max(0.0, info["max_favorable"] - 0.5)
                if direction in ("bullish", "buy"):
                    trail_sl = entry + trail_r * R
                    if trail_sl > current_sl + 0.01:
                        ok = self.mt5.modify_position(ticket, sl=trail_sl)
                        if ok:
                            sl_track_original_sl(ticket, trail_sl)  # sync SL tracker
                            logger.info(f"Trail SL on {ticket} ({symbol}): "
                                        f"{current_sl:.2f} -> {trail_sl:.2f} "
                                        f"(MFE={info['max_favorable']:.2f}R)")
                else:
                    trail_sl = entry - trail_r * R
                    if trail_sl < current_sl - 0.01:
                        ok = self.mt5.modify_position(ticket, sl=trail_sl)
                        if ok:
                            sl_track_original_sl(ticket, trail_sl)  # sync SL tracker
                            logger.info(f"Trail SL on {ticket} ({symbol}): "
                                        f"{current_sl:.2f} -> {trail_sl:.2f} "
                                        f"(MFE={info['max_favorable']:.2f}R)")

        # Clean up closed tickets
        for t in closed_tickets:
            self.tracked_positions.pop(t, None)

    # ────────────────────────────────────────────────────────────────────
    #  Deferred P&L re-query for failed deal history lookups
    # ────────────────────────────────────────────────────────────────────
    def _schedule_deferred_pnl_update(self, ticket: int, symbol: str, info: dict):
        """Schedule a background thread to re-query deal history after 10s.
        If successful, updates the trade journal and daily P&L with the real values."""
        def _requery():
          try:
            time.sleep(10)  # give MT5 time to register the deal
            pos_id = info.get("position_id", ticket)
            details = self._get_closed_trade_details(pos_id, expected_symbol=symbol)
            pnl = details['pnl']
            exit_price = details['exit_price']
            exit_reason = details['exit_reason']

            if pnl == 0.0 and exit_price == 0:
                # Still nothing — try one more time after 30s
                time.sleep(30)
                details = self._get_closed_trade_details(pos_id, expected_symbol=symbol)
                pnl = details['pnl']
                exit_price = details['exit_price']
                exit_reason = details['exit_reason']

            if pnl != 0.0 or exit_price != 0:
                logger.info(f"Deferred re-query SUCCESS for {ticket} ({symbol}): "
                            f"{exit_reason}, exit={exit_price}, PnL=${pnl:.2f}")
                # Update daily P&L
                self.state.daily_pnl += pnl

                # Update journal
                trade_id = info.get("journal_trade_id")
                if trade_id:
                    pos_R = info.get("R", 0)
                    mfe_pts = round(info.get("max_favorable", 0) * pos_R, 2) if pos_R else 0
                    mae_pts = round(info.get("max_adverse", 0) * pos_R, 2) if pos_R else 0
                    pnl_pts = pnl / (info.get("volume", 1) * self._get_point_value(symbol)) if info.get("volume") else 0
                    self.journal.close_trade(trade_id, {
                        "exit_price": exit_price,
                        "exit_reason": exit_reason,
                        "pnl": pnl,
                        "pnl_percent": round(pnl / self.state.current_balance * 100, 3) if self.state.current_balance else 0,
                        "pnl_points": round(pnl_pts, 2),
                        "mfe_points": mfe_pts,
                        "mae_points": mae_pts,
                        "balance_after": self.state.current_balance + pnl,
                        "drawdown": 0,
                    })
                    logger.info(f"Journal updated for trade #{trade_id} with deferred P&L")

                # Send Telegram correction
                self.telegram.send_message(
                    f"<b>P&L Correction</b> ({symbol})\n"
                    f"Ticket: {ticket}\n"
                    f"Exit: {exit_reason} @ {exit_price}\n"
                    f"P&L: <b>${pnl:+.2f}</b>"
                )
            else:
                logger.error(f"Deferred re-query FAILED for {ticket} ({symbol}) "
                             f"- P&L will remain $0 in journal. Check MT5 manually.")
                self.telegram.send_message(
                    f"⚠️ <b>P&L Recording Failed</b>\n"
                    f"{symbol} ticket {ticket}\n"
                    f"Deal history unavailable. Check MT5 manually."
                )
          except Exception as e:
            logger.error(f"Deferred PnL re-query thread failed for {ticket} ({symbol}): {e}")

        t = threading.Thread(target=_requery, daemon=True, name=f"pnl_requery_{ticket}")
        t.start()

    # ────────────────────────────────────────────────────────────────────
    #  Query deal history for closed trade PnL
    # ────────────────────────────────────────────────────────────────────
    def _get_closed_trade_pnl(self, ticket: int) -> float:
        """Query MT5 deal history to find actual PnL of a closed position.
        Retries up to 3 times with 0.5s delay since deals may not appear
        immediately after close (especially for IOC/emergency closes)."""
        return self._get_closed_trade_details(ticket).get('pnl', 0.0)

    def _get_closed_trade_details(self, ticket: int, expected_symbol: str = None) -> dict:
        """Query MT5 deal history for full exit details: PnL, exit_price, exit_reason.
        Retries up to 6 times with 1.0s delay (total ~6s wait) since deals
        may not appear immediately after close on some brokers.
        Uses 7-day lookback window to handle weekend rollover."""
        result = {'pnl': 0.0, 'exit_price': 0, 'exit_reason': 'Unknown'}
        try:
            from datetime import timezone
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=7)  # wider window for weekend rollover
            for attempt in range(6):
                deals = mt5.history_deals_get(start, now, position=ticket)
                if deals:
                    # Sanity check: verify deals belong to the expected symbol
                    if expected_symbol and hasattr(deals[0], 'symbol'):
                        deal_symbol = deals[0].symbol
                        if deal_symbol != expected_symbol:
                            logger.warning(
                                f"Deal symbol mismatch for position {ticket}: "
                                f"expected {expected_symbol}, got {deal_symbol}. "
                                f"Deals may belong to a different position.")
                            # Filter deals to only include the expected symbol
                            filtered = [d for d in deals if d.symbol == expected_symbol]
                            if filtered:
                                deals = filtered
                                logger.info(f"Filtered to {len(deals)} deals for {expected_symbol}")
                            else:
                                # Try alternative: search ALL recent deals for this symbol
                                # and match by order ticket or close time proximity
                                alt_deals = mt5.history_deals_get(start, now, group=f"*{expected_symbol}*")
                                if alt_deals:
                                    # Find deals with matching position or order ticket
                                    matched = [d for d in alt_deals
                                               if d.position_id == ticket or d.order == ticket]
                                    if matched:
                                        deals = matched
                                        logger.info(f"Alternative lookup found {len(deals)} deals "
                                                    f"for {expected_symbol} via group filter")
                                    else:
                                        # Last resort: find the most recent DEAL_ENTRY_OUT for this symbol
                                        exit_deals = [d for d in alt_deals if d.entry == 1]
                                        if exit_deals:
                                            # Pick the most recent exit within last 60s
                                            recent = [d for d in exit_deals
                                                      if (now.timestamp() - d.time) < 60]
                                            if recent:
                                                deals = recent[-1:]  # most recent
                                                logger.info(f"Alternative lookup: using most recent "
                                                            f"{expected_symbol} exit deal (within 60s)")
                                            else:
                                                logger.warning(f"No recent {expected_symbol} exit deals found")
                                                result['symbol_mismatch'] = True
                                                return result
                                        else:
                                            result['symbol_mismatch'] = True
                                            return result
                                else:
                                    # NO deals match expected symbol — return mismatch flag
                                    logger.warning(f"No deals found for {expected_symbol} in position "
                                                   f"{ticket} — returning symbol_mismatch")
                                    result['symbol_mismatch'] = True
                                    return result

                    result['pnl'] = sum(d.profit + d.commission + d.swap for d in deals)
                    result['commission'] = sum(d.commission for d in deals)
                    result['swap'] = sum(d.swap for d in deals)
                    result['gross_pnl'] = sum(d.profit for d in deals)
                    # Find the closing deal (DEAL_ENTRY_OUT = 1)
                    for d in deals:
                        if d.entry == 1:  # DEAL_ENTRY_OUT
                            result['exit_price'] = d.price
                            # Determine exit reason from deal reason
                            # MT5 constants: DEAL_REASON_CLIENT=0, DEAL_REASON_MOBILE=1,
                            # DEAL_REASON_WEB=2, DEAL_REASON_EXPERT=3,
                            # DEAL_REASON_SL=4, DEAL_REASON_TP=5, DEAL_REASON_SO=6
                            if d.reason == 4:  # DEAL_REASON_SL
                                result['exit_reason'] = 'SL_HIT'
                            elif d.reason == 5:  # DEAL_REASON_TP
                                result['exit_reason'] = 'TP'
                            elif d.reason in (0, 1, 2):  # CLIENT, MOBILE, WEB
                                result['exit_reason'] = 'MANUAL'
                            elif d.reason == 3:  # DEAL_REASON_EXPERT (EA/bot)
                                result['exit_reason'] = 'EA_CLOSE'
                            elif d.reason == 6:  # DEAL_REASON_SO (stop-out)
                                result['exit_reason'] = 'STOP_OUT'
                            else:
                                result['exit_reason'] = f'BROKER(r={d.reason})'
                            break
                    if attempt > 0:
                        logger.info(f"Deal history for {ticket} found on attempt {attempt+1}")
                    return result
                time.sleep(1.0)
            logger.warning(f"No deals found for ticket {ticket} after 6 attempts")
        except Exception as e:
            logger.error(f"Failed to query deal history for {ticket}: {e}")
        return result

    def _get_point_value(self, symbol: str) -> float:
        """Get the dollar value per 1 index point for 1 lot from MT5.

        Uses order_calc_profit (most accurate — includes contract size +
        currency conversion) with tick_value/tick_size as fallback.
        """
        try:
            info = mt5.symbol_info(symbol)
            if info:
                # Primary: ask MT5 directly (handles contract_size + FX)
                pv = mt5.order_calc_profit(
                    mt5.ORDER_TYPE_BUY, symbol, 1.0,
                    info.ask, info.ask + 1.0)
                if pv and pv > 0:
                    return pv
                # Fallback: tick_value / tick_size (may miss contract_size)
                if info.trade_tick_size > 0:
                    tv = info.trade_tick_value / info.trade_tick_size
                    if tv > 0:
                        return tv
        except Exception:
            pass
        return 1.0

    # ────────────────────────────────────────────────────────────────────
    #  Market notifications
    # ────────────────────────────────────────────────────────────────────
    def _check_market_notifications(self):
        active = get_active_indices()
        for s in active:
            if s not in self._prev_active:
                logger.info(f"Market opened: {s}")
                # Reset recap for newly opened session
                self._session_recap[s] = {
                    'signal_found': False, 'signal_dir': '', 'reason': 'no_signal',
                    'entry_price': 0, 'trade_taken': False, 'trade_pnl': 0,
                }
                self._recap_sent_today[s] = False
        closed_now = []
        for s in self._prev_active:
            if s not in active:
                logger.info(f"Market closed: {s}")
                closed_now.append(s)
        # Send session-end recap for markets that just closed
        if closed_now:
            self._send_session_recap(closed_now)
        self._prev_active = active

    def _send_session_recap(self, symbols: list):
        """Send Telegram session-end recap for closed markets."""
        try:
            # Dedup: skip symbols already recapped today
            symbols = [s for s in symbols if not self._recap_sent_today.get(s, False)]
            if not symbols:
                return
            lines = []
            for sym in symbols:
                recap = self._session_recap.get(sym, {})
                traded = self.trade_taken_today.get(sym, False)
                detection = self._detection_logged_today.get(sym, False)

                if traded:
                    emoji = "\U0001f4b0"  # moneybag
                    status = f"TRADED ({recap.get('signal_dir', '?').upper()})"
                    pnl = recap.get('trade_pnl', 0)
                    detail = f"P&L: ${pnl:+.2f}" if pnl != 0 else "Position may still be open"
                elif recap.get('signal_found'):
                    emoji = "\u26a0\ufe0f"  # warning
                    reason = recap.get('reason', 'unknown')
                    status = f"Signal found but NOT taken"
                    detail = f"Reason: {reason}"
                elif detection:
                    emoji = "\U0001f50d"  # magnifier
                    status = "ORB detected, no valid signal"
                    detail = "Filters rejected (HTF/body/disp/margin)"
                else:
                    emoji = "\u2796"  # minus
                    status = "No ORB detection"
                    detail = "Insufficient data or market quiet"

                lines.append(f"{emoji} <b>{sym}</b>: {status}\n   {detail}")

            account = self.mt5.get_account_info()
            bal = f"${account['balance']:,.2f}" if account else "N/A"

            msg = (
                f"\U0001f4cb <b>SESSION RECAP</b>\n\n"
                + "\n\n".join(lines)
                + f"\n\n\U0001f4b0 Balance: {bal}"
                + f"\n\U0001f4c5 Daily P&L: ${self.state.daily_pnl:+.2f}"
                + f"\n\u23f0 {ist_time()}"
            )
            self.telegram.send_message(msg)
            # Mark as sent to prevent duplicate recaps
            for sym in symbols:
                self._recap_sent_today[sym] = True
        except Exception as e:
            logger.error(f"Session recap failed: {e}")

    # ────────────────────────────────────────────────────────────────────
    #  Emergency: close all on max DD breach
    # ────────────────────────────────────────────────────────────────────
    def _emergency_close_all(self, reason: str):
        logger.critical(f"EMERGENCY CLOSE ALL: {reason}")
        self.telegram.critical_alert("EMERGENCY CLOSE ALL", reason)
        positions = self.mt5.get_open_positions()
        for p in positions:
            ticket = p["ticket"]
            for attempt in range(5):
                if self.mt5.close_position(ticket):
                    break
                time.sleep(1)
            # Record PnL for journal + edge guard
            try:
                info = self.tracked_positions.get(ticket, {})
                symbol = info.get("symbol", p.get("symbol", ""))
                pos_id = info.get("position_id", ticket)
                details = self._get_closed_trade_details(pos_id, expected_symbol=symbol)
                pnl = details['pnl']
                self.state.daily_pnl += pnl
                trade_id = info.get("journal_trade_id")
                if trade_id:
                    pos_R = info.get('R', 0)
                    mfe_pts = round(info.get('max_favorable', 0) * pos_R, 2) if pos_R else 0
                    mae_pts = round(info.get('max_adverse', 0) * pos_R, 2) if pos_R else 0
                    pnl_pts = pnl / (info.get('volume', 1) * self._get_point_value(symbol)) if info.get('volume') else 0
                    self.journal.close_trade(trade_id, {
                        "exit_price": details['exit_price'],
                        "exit_reason": f"EMERGENCY: {reason}",
                        "pnl": pnl,
                        "pnl_percent": round(pnl / self.state.current_balance * 100, 3) if self.state.current_balance else 0,
                        "pnl_points": round(pnl_pts, 2),
                        "mfe_points": mfe_pts,
                        "mae_points": mae_pts,
                        "be_hit": 'Yes' if info.get('hit_be') else 'No',
                        "balance_after": self.state.current_balance + pnl,
                        "drawdown": 0,
                    })
                self.edge_guard.record_trade({
                    'date': datetime.now().strftime("%Y-%m-%d"),
                    'symbol': symbol,
                    'direction': info.get('direction', p.get('type', '')),
                    'pnl': pnl,
                    'balance_after': self.state.current_balance + pnl,
                    'session': 'emergency',
                })
                logger.info(f"Emergency close {ticket} ({symbol}): PnL=${pnl:.2f}")
            except Exception as e:
                logger.error(f"Emergency close PnL recording failed for {ticket}: {e}")
        self.tracked_positions.clear()
        self._trading_enabled = False

    # ────────────────────────────────────────────────────────────────────
    #  Main loop
    # ────────────────────────────────────────────────────────────────────
    def run(self):
        """24/7 trading loop with comprehensive edge-case handling."""
        last_status_log = datetime.now()
        last_hourly_status = datetime.now()

        while not self._shutdown_requested:
            try:
                self._write_heartbeat()

                # ── Connection check (handles network loss + MT5 crash) ─
                if not self._check_connection():
                    time.sleep(30)
                    continue

                # ── Daily reset ────────────────────────────────────────
                self._check_daily_reset()

                # ── Scheduled reports ──────────────────────────────────
                try:
                    account_r = self.mt5.get_account_info()
                    if account_r:
                        dd_r = self.dd_protection.update(
                            account_r.get("equity", account_r["balance"])
                        )
                        self.report_scheduler.check_and_send_reports(
                            account_r["balance"], dd_r["daily_dd"], dd_r["overall_dd"]
                        )
                except Exception as e:
                    logger.warning(f"Report scheduler error: {e}")

                # ── Account + DD check ─────────────────────────────────
                account = self.mt5.get_account_info()
                if account:
                    self.state.current_balance = account["balance"]
                    dd = self.dd_protection.update(
                        account.get("equity", account["balance"])
                    )

                    # Warn at 80% of daily DD limit (throttle: once per 30 min)
                    daily_limit = FUNDEDNEXT_RULES["daily_drawdown_stop"]
                    if dd["daily_dd"] >= daily_limit * 0.8:
                        _dd_last = getattr(self, '_dd_warn_sent', None)
                        if _dd_last is None or (datetime.now() - _dd_last).total_seconds() > 1800:
                            self.telegram.drawdown_warning(
                                dd["daily_dd"], daily_limit,
                                account.get("balance", 0)
                            )
                            self._dd_warn_sent = datetime.now()

                    # MAX DD breach -> emergency close all + stop
                    if dd["is_max_dd_breach"]:
                        self._emergency_close_all(
                            f"MAX DD BREACHED: {dd['overall_dd']:.2f}% "
                            f"(limit: {FUNDEDNEXT_RULES['max_drawdown_percent']}%)"
                        )
                        break

                    # Daily DD breach -> stop new trades, keep managing
                    if dd["is_daily_dd_breach"]:
                        if not self.state.is_daily_limit_hit:
                            # Log and notify only on FIRST breach (not every 10s cycle)
                            logger.warning(f"Daily DD limit hit: {dd['daily_dd']:.2f}%")
                        self.state.is_daily_limit_hit = True

                # ── Market notifications ───────────────────────────────
                self._check_market_notifications()

                # ── Signal generation + execution ──────────────────────
                active = get_active_indices()
                for symbol in active:
                    if self._shutdown_requested:
                        break

                    can, reason = self._can_trade(symbol)
                    if not can:
                        # Send ONE Telegram notification per symbol per day
                        # for non-trivial blocks (skip "market closed" & "already traded")
                        # Also skip "daily limit hit" — DD warning system handles it separately
                        if reason not in ("market closed", "already traded today",
                                          "trading disabled", "trading paused",
                                          "daily limit hit"):
                            block_key = f"{symbol}_{reason}"
                            last_t = self._block_notify_sent.get(block_key)
                            if last_t is None or (datetime.now() - last_t).total_seconds() > 600:
                                self.telegram.send_message(
                                    f"\u26a0\ufe0f <b>Trade Blocked — {symbol}</b>\n\n"
                                    f"<b>Reason:</b> {reason}\n"
                                    f"\u23f0 {ist_time()}")
                                self._block_notify_sent[block_key] = datetime.now()
                        continue

                    sig = self._generate_signal(symbol)
                    if sig:
                        logger.info(f"{symbol}: ORB signal detected - {sig['direction']} "
                                    f"entry={sig['entry_price']:.2f} "
                                    f"SL={sig['stop_loss']:.2f}")
                        _det_logger.info(f"{symbol}: SIGNAL {sig['direction']} "
                                         f"entry={sig['entry_price']:.2f} "
                                         f"SL={sig['stop_loss']:.2f} "
                                         f"TP={sig['tp']:.2f} "
                                         f"R={sig['risk_points']:.2f}")
                        self._execute_signal(symbol, sig)
                    # else: no valid ORB breakout this cycle — silent, normal

                # ── Snapshot balance BEFORE position management ────────
                # Needed for symbol-mismatch fallback: balance delta must compare
                # pre-close balance vs post-close balance to compute PnL correctly.
                self._pre_manage_balance = self.state.current_balance

                # ── Position management (BE, trail, session close) ─────
                self._manage_positions()

                # ── Hourly Telegram status ─────────────────────────────
                if (datetime.now() - last_hourly_status).total_seconds() >= 3600:
                    try:
                        account = self.mt5.get_account_info()
                        if account:
                            dd = self.dd_protection.update(
                                account.get("equity", account["balance"])
                            )
                            self.telegram.system_status(
                                balance=account["balance"],
                                open_positions=len(self.tracked_positions),
                                daily_pnl=self.state.daily_pnl,
                                daily_dd=dd["daily_dd"],
                                overall_dd=dd["overall_dd"],
                                active_markets=active,
                            )
                    except Exception as e:
                        logger.warning(f"Hourly status Telegram failed: {e}")
                    last_hourly_status = datetime.now()

                # ── 5-min status log + state save ──────────────────────
                if (datetime.now() - last_status_log).total_seconds() >= 300:
                    account = self.mt5.get_account_info()
                    if account:
                        dd = self.dd_protection.update(
                            account.get("equity", account["balance"])
                        )
                        logger.info(
                            f"Status - Balance: ${account['balance']:,.2f} | "
                            f"Daily DD: {dd['daily_dd']:.2f}% | "
                            f"Overall DD: {dd['overall_dd']:.2f}% | "
                            f"Active: {active}"
                        )
                    self._save_state()
                    last_status_log = datetime.now()

                # ── Reset error counter on success ─────────────────────
                self._consecutive_errors = 0
                self._error_backoff = 30

                # ── Sleep ──────────────────────────────────────────────
                time.sleep(5)

            except KeyboardInterrupt:
                self._signal_handler(signal.SIGINT, None)
                break

            except Exception as e:
                self._consecutive_errors += 1
                logger.error(f"Loop error #{self._consecutive_errors}: {e}\n"
                             f"{traceback.format_exc()}")

                if self._consecutive_errors >= 5:
                    self.telegram.critical_alert(
                        "TOO MANY ERRORS",
                        f"Error #{self._consecutive_errors}: {str(e)[:200]}\n"
                        f"Bot stopping - manual intervention needed"
                    )
                    break

                sleep_time = min(self._error_backoff, 480)
                self._error_backoff *= 2
                logger.info(f"Backing off {sleep_time}s before retry...")
                time.sleep(sleep_time)

        # ── Shutdown ───────────────────────────────────────────────────
        logger.info("Trading loop ended")
        self._save_state()
        sl_stop_monitoring()
        try:
            self.cmd_handler.stop_polling()
        except Exception:
            pass
        account = self.mt5.get_account_info()
        if account:
            self.telegram.bot_stopped("Loop ended",
                                      account.get("balance", 0),
                                      self.state.daily_pnl)
        self.mt5.disconnect()
