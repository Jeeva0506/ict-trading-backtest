"""Main trading orchestrator for ORB Sweep + Expiry Straddle system.

Production-grade version with:
  - Dual strategy: PUT sweep on non-expiry + short straddle on expiry
  - Safety module: reconciliation, kill switch, EOD verification, outage handling
  - Excel journal: comprehensive trade log with summaries
  - Capital: Rs 3,00,000 | Sweep risk 1% | Straddle risk 3%
"""

import json
import logging
import math
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from orb_sweep.config import TradingConfig, IST
from orb_sweep.strategy import ORBSweepStrategy, TradeSignal
from orb_sweep.broker import ZerodhaBroker
from orb_sweep.risk_manager import RiskManager
from orb_sweep.trade_logger import TradeLogger
from orb_sweep.alerts import TelegramAlerts
from orb_sweep.safety import SafetyManager, SafetyAction
from orb_sweep.excel_journal import ExcelJournal, bs_delta
from orb_sweep.expiry_straddle import is_nifty_expiry, _bs_0dte

logger = logging.getLogger(__name__)


def _ist_now() -> datetime:
    return datetime.now(IST)


def _ist_today() -> date:
    return _ist_now().date()


# ---------------------------------------------------------------------------
# Active position dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ActivePosition:
    """Tracks a single-leg (sweep) position."""
    trade_id: int
    signal: TradeSignal
    entry_premium: float
    quantity: int
    remaining_qty: int
    lots: int
    tradingsymbol: str
    tp1_hit: bool = False
    sl_at_breakeven: bool = False
    peak_premium: float = 0.0
    entry_time: datetime = field(default_factory=_ist_now)
    strategy: str = "sweep"

    @property
    def is_call(self) -> bool:
        return self.signal.direction == "CALL"


@dataclass
class ActiveStraddle:
    """Tracks a short straddle position (expiry days)."""
    trade_id: int
    trade_date: date
    strike: int
    spot_entry: float
    entry_call_prem: float
    entry_put_prem: float
    straddle_entry: float
    call_symbol: str
    put_symbol: str
    quantity: int
    lots: int
    peak_straddle: float = 0.0
    trough_straddle: float = float("inf")
    trail_active: bool = False
    entry_time: datetime = field(default_factory=_ist_now)
    vwap_at_entry: float = 0.0
    orb_high: float = 0.0
    orb_low: float = 0.0
    india_vix: float = 15.0

    @property
    def strategy(self) -> str:
        return "short_straddle"


# ---------------------------------------------------------------------------
# Straddle Config (used at runtime for live/paper trading)
# ---------------------------------------------------------------------------

@dataclass
class LiveStraddleConfig:
    """Runtime config for straddle trades on expiry days."""
    sl_pct: float = 0.50        # exit if straddle rises 50% (short side)
    tp_pct: float = 0.40        # exit if straddle falls 40%
    trail_activate_pct: float = 0.25
    trail_pct: float = 0.15
    risk_pct: float = 0.03      # 3% of capital
    force_exit_time: time = field(default_factory=lambda: time(15, 10))
    max_orb_range_pct: float = 0.008
    min_vix: float = 11.0
    max_vix: float = 25.0


# ---------------------------------------------------------------------------
# Main Trader
# ---------------------------------------------------------------------------

class ORBSweepTrader:
    """
    Main event loop for the ORB Sweep + Straddle trading system.

    Phases:
      1. Pre-market: Initialize, connect
      2. ORB window (9:15-9:30): Collect bars, compute ORB levels
      3. Regime check (9:30): Validate market conditions
      4. Expiry check: if expiry day -> short straddle at 9:31
      5. Signal scan (9:30-15:15): Monitor for sweep entry signals
      6. Position management: Track TP1/TP2/SL with safety checks
      7. Force exit (15:15): Close all positions
      8. EOD verification: confirm zero positions with broker
      9. EOD report: generate summary, rebuild Excel, send alerts
    """

    def __init__(self, config: TradingConfig = None):
        self.cfg = config or TradingConfig()
        self.broker = ZerodhaBroker(self.cfg)
        self.strategy = ORBSweepStrategy(self.cfg)
        self.risk_mgr = RiskManager(self.cfg)
        self.db_logger = TradeLogger(self.cfg)
        self.alerts = TelegramAlerts(self.cfg)

        # Production-grade safety
        self.safety = SafetyManager(self.broker, self.alerts, self.cfg)

        # Excel journal
        self.excel = ExcelJournal(self.cfg)

        # Straddle config
        self.straddle_cfg = LiveStraddleConfig()

        # Position state (only one active at a time)
        self.position: Optional[ActivePosition] = None
        self.straddle: Optional[ActiveStraddle] = None

        # Day state
        self._state_path = Path(self.cfg.state_path)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._orb_computed = False
        self._regime_checked = False
        self._eod_done = False
        self._today: Optional[date] = None
        self._is_expiry_day = False
        self._straddle_entered = False
        self._current_vix: float = 15.0

    # =====================================================================
    # State Persistence
    # =====================================================================

    def _save_state(self):
        state = {}
        if self.position:
            p = self.position
            state = {
                "type": "sweep",
                "trade_id": p.trade_id,
                "direction": p.signal.direction,
                "entry_spot": p.signal.entry_spot,
                "stop_loss": p.signal.stop_loss,
                "tp1": p.signal.tp1,
                "tp2": p.signal.tp2,
                "r_value": p.signal.r_value,
                "strike": p.signal.strike,
                "entry_premium": p.entry_premium,
                "quantity": p.quantity,
                "remaining_qty": p.remaining_qty,
                "lots": p.lots,
                "tradingsymbol": p.tradingsymbol,
                "tp1_hit": p.tp1_hit,
                "sl_at_breakeven": p.sl_at_breakeven,
                "peak_premium": p.peak_premium,
                "entry_time": p.entry_time.isoformat(),
            }
        elif self.straddle:
            s = self.straddle
            state = {
                "type": "straddle",
                "trade_id": s.trade_id,
                "trade_date": s.trade_date.isoformat(),
                "strike": s.strike,
                "spot_entry": s.spot_entry,
                "entry_call_prem": s.entry_call_prem,
                "entry_put_prem": s.entry_put_prem,
                "straddle_entry": s.straddle_entry,
                "call_symbol": s.call_symbol,
                "put_symbol": s.put_symbol,
                "quantity": s.quantity,
                "lots": s.lots,
                "peak_straddle": s.peak_straddle,
                "trough_straddle": s.trough_straddle,
                "trail_active": s.trail_active,
                "entry_time": s.entry_time.isoformat(),
                "vwap_at_entry": s.vwap_at_entry,
                "orb_high": s.orb_high,
                "orb_low": s.orb_low,
                "india_vix": s.india_vix,
            }
        self._state_path.write_text(
            json.dumps(state, indent=2), encoding="utf-8"
        )

    def _load_state(self) -> bool:
        if not self._state_path.exists():
            return False
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not state:
                return False

            state_type = state.get("type", "sweep")

            if state_type == "straddle":
                self.straddle = ActiveStraddle(
                    trade_id=state["trade_id"],
                    trade_date=date.fromisoformat(state["trade_date"]),
                    strike=state["strike"],
                    spot_entry=state["spot_entry"],
                    entry_call_prem=state["entry_call_prem"],
                    entry_put_prem=state["entry_put_prem"],
                    straddle_entry=state["straddle_entry"],
                    call_symbol=state["call_symbol"],
                    put_symbol=state["put_symbol"],
                    quantity=state["quantity"],
                    lots=state["lots"],
                    peak_straddle=state.get("peak_straddle", 0),
                    trough_straddle=state.get("trough_straddle", float("inf")),
                    trail_active=state.get("trail_active", False),
                    entry_time=datetime.fromisoformat(state["entry_time"]),
                    vwap_at_entry=state.get("vwap_at_entry", 0),
                    orb_high=state.get("orb_high", 0),
                    orb_low=state.get("orb_low", 0),
                    india_vix=state.get("india_vix", 15.0),
                )
                logger.info(
                    f"Restored straddle: strike={state['strike']} "
                    f"qty={state['quantity']}"
                )
                # Resume-only: mark today's ORB/regime/entry as done so a mid-day
                # restart manages the existing straddle instead of opening a second.
                # Setting _today prevents _reset_day() from wiping these flags on
                # the first tick.
                self._today = date.fromisoformat(state["trade_date"])
                self._orb_computed = True
                self._regime_checked = True
                self._straddle_entered = True
                return True

            # Sweep position
            signal = TradeSignal(
                direction=state["direction"],
                entry_spot=state["entry_spot"],
                stop_loss=state["stop_loss"],
                tp1=state["tp1"],
                tp2=state["tp2"],
                r_value=state["r_value"],
                strike=state["strike"],
                vwap_at_entry=0,
                sweep_price=0,
                bar_time=pd.Timestamp.now(),
            )
            self.position = ActivePosition(
                trade_id=state["trade_id"],
                signal=signal,
                entry_premium=state["entry_premium"],
                quantity=state["quantity"],
                remaining_qty=state["remaining_qty"],
                lots=state["lots"],
                tradingsymbol=state["tradingsymbol"],
                tp1_hit=state["tp1_hit"],
                sl_at_breakeven=state["sl_at_breakeven"],
                peak_premium=state["peak_premium"],
                entry_time=datetime.fromisoformat(state["entry_time"]),
            )
            logger.info(
                f"Restored position: {state['direction']} "
                f"{state['tradingsymbol']} qty={state['remaining_qty']}"
            )
            # Resume-only: mark today's ORB/regime as done so a mid-day restart
            # manages the existing position instead of scanning for a new entry.
            # Setting _today prevents _reset_day() from wiping these flags.
            self._today = datetime.fromisoformat(state["entry_time"]).date()
            self._orb_computed = True
            self._regime_checked = True
            return True
        except Exception as e:
            logger.error(f"State load failed: {e}")
            return False

    def _clear_state(self):
        self.position = None
        self.straddle = None
        if self._state_path.exists():
            self._state_path.write_text("{}", encoding="utf-8")

    # =====================================================================
    # Day Reset
    # =====================================================================

    def _reset_day(self):
        today = _ist_today()
        if self._today == today:
            return
        self._today = today
        self.strategy.reset_day()
        self.risk_mgr.reset_day(today)
        self.safety.reset_day()
        self._orb_computed = False
        self._regime_checked = False
        self._eod_done = False
        self._straddle_entered = False
        self._is_expiry_day = is_nifty_expiry(today)
        self._clear_state()
        logger.info(
            f"Day reset: {today} | "
            f"Expiry day: {self._is_expiry_day}"
        )

    # =====================================================================
    # Sweep Entry (non-expiry or if straddle not taken)
    # =====================================================================

    def _enter_sweep_trade(self, signal: TradeSignal):
        """Enter a single-leg sweep/breakout trade."""
        # Use 1% risk for sweep
        sweep_risk_pct = 0.01
        risk_amount = self.cfg.capital * sweep_risk_pct
        lots = max(1, int(risk_amount / (self.cfg.lot_size * signal.r_value))) \
            if signal.r_value > 0 else 0
        quantity = lots * self.cfg.lot_size
        if quantity <= 0:
            logger.warning("Computed quantity is 0 -- skipping entry")
            return

        opt_type = "CE" if signal.direction == "CALL" else "PE"
        try:
            opt_info = self.broker.find_option_token(signal.strike, opt_type)
        except Exception as e:
            logger.error(f"Option lookup failed: {e}")
            self.alerts.send_error(f"Option lookup failed: {e}")
            return

        tradingsymbol = opt_info["tradingsymbol"]
        actual_lot_size = opt_info["lot_size"]
        quantity = lots * actual_lot_size

        if self.cfg.paper_mode:
            entry_premium = self.broker.ltp(self.cfg.exchange, tradingsymbol)
            if entry_premium <= 0:
                entry_premium = signal.r_value * 0.5
        else:
            order_id = self.broker.place_buy(tradingsymbol, quantity)
            fill = self.safety.wait_for_fill(order_id, quantity, tradingsymbol)
            if fill.filled_qty < quantity:
                quantity = fill.filled_qty
                lots = quantity // actual_lot_size
                if quantity <= 0:
                    logger.warning("Order fill failed completely -- no entry")
                    return
            entry_premium = fill.average_price if fill.average_price > 0 else \
                self.broker.ltp(self.cfg.exchange, tradingsymbol)

        # Compute delta for logging
        vix = self._current_vix
        minutes_left = max(
            (15 * 60 + 30) - (_ist_now().hour * 60 + _ist_now().minute), 1
        )
        delta = bs_delta(
            signal.entry_spot, signal.strike, vix, minutes_left, opt_type
        )

        trade_record = {
            "trade_date": _ist_today().isoformat(),
            "entry_time": _ist_now().isoformat(),
            "direction": signal.direction,
            "instrument": "NIFTY",
            "strike": signal.strike,
            "spot_entry": signal.entry_spot,
            "entry_premium": entry_premium,
            "quantity": quantity,
            "lots": lots,
            "stop_loss": signal.stop_loss,
            "tp1_price": signal.tp1,
            "tp2_price": signal.tp2,
            "orb_high": self.strategy.orb.orb_high if self.strategy.orb else 0,
            "orb_low": self.strategy.orb.orb_low if self.strategy.orb else 0,
            "vwap_at_entry": signal.vwap_at_entry,
            "sweep_price": signal.sweep_price,
            "r_value": signal.r_value,
        }
        trade_id = self.db_logger.insert_trade(trade_record)

        self.position = ActivePosition(
            trade_id=trade_id,
            signal=signal,
            entry_premium=entry_premium,
            quantity=quantity,
            remaining_qty=quantity,
            lots=lots,
            tradingsymbol=tradingsymbol,
            peak_premium=entry_premium,
            strategy="sweep",
        )
        self.risk_mgr.increment_trade_count()
        self._save_state()

        self.alerts.send_entry(
            signal.direction, signal.strike, signal.entry_spot,
            signal.stop_loss, signal.tp1, signal.tp2,
            lots, quantity, signal.r_value,
        )
        logger.info(
            f"SWEEP ENTRY: {signal.direction} NIFTY {signal.strike} {opt_type} | "
            f"{lots} lots ({quantity} units) @ prem={entry_premium:.2f} | "
            f"SL={signal.stop_loss:.2f} TP1={signal.tp1:.2f} TP2={signal.tp2:.2f}"
        )

    # =====================================================================
    # Straddle Entry (expiry day)
    # =====================================================================

    def _enter_straddle(self, spot: float, vix: float,
                        orb_high: float, orb_low: float, vwap_entry: float):
        """Enter a short straddle on expiry day."""
        strike = int(round(spot / self.cfg.strike_step) * self.cfg.strike_step)

        # Look up both legs
        try:
            call_info = self.broker.find_option_token(strike, "CE")
            put_info = self.broker.find_option_token(strike, "PE")
        except Exception as e:
            logger.error(f"Straddle option lookup failed: {e}")
            self.alerts.send_error(f"Straddle option lookup failed: {e}")
            return

        call_sym = call_info["tradingsymbol"]
        put_sym = put_info["tradingsymbol"]
        actual_lot_size = call_info["lot_size"]

        if self.cfg.paper_mode:
            call_prem = self.broker.ltp(self.cfg.exchange, call_sym)
            put_prem = self.broker.ltp(self.cfg.exchange, put_sym)
            if call_prem <= 0 or put_prem <= 0:
                # Fallback: compute with BS
                minutes_left = max(
                    (15 * 60 + 30) - (_ist_now().hour * 60 + _ist_now().minute), 1
                )
                call_prem = _bs_0dte(spot, strike, minutes_left, vix, "call")
                put_prem = _bs_0dte(spot, strike, minutes_left, vix, "put")
        else:
            # Place sell orders for short straddle
            call_order = self.broker.place_sell(call_sym, 0)  # qty set below
            put_order = self.broker.place_sell(put_sym, 0)
            call_prem = self.broker.ltp(self.cfg.exchange, call_sym)
            put_prem = self.broker.ltp(self.cfg.exchange, put_sym)

        straddle_entry = call_prem + put_prem
        if straddle_entry < 5:
            logger.warning(f"Straddle premium too low ({straddle_entry:.2f}) -- skipping")
            return

        # Position sizing: 3% risk
        risk_amount = self.cfg.capital * self.straddle_cfg.risk_pct
        max_loss = straddle_entry * self.straddle_cfg.sl_pct
        lots = max(1, int(risk_amount / (max_loss * actual_lot_size)))
        quantity = lots * actual_lot_size

        # In live mode, actually place the orders with correct quantity
        if not self.cfg.paper_mode:
            try:
                self.broker.place_sell(call_sym, quantity)
                self.broker.place_sell(put_sym, quantity)
            except Exception as e:
                logger.error(f"Straddle order placement failed: {e}")
                self.alerts.send_error(f"Straddle order failed: {e}")
                return

        # Log to SQLite
        trade_record = {
            "trade_date": _ist_today().isoformat(),
            "entry_time": _ist_now().isoformat(),
            "direction": "straddle",
            "instrument": "NIFTY",
            "strike": strike,
            "spot_entry": spot,
            "entry_premium": straddle_entry,
            "quantity": quantity,
            "lots": lots,
            "stop_loss": straddle_entry * (1 + self.straddle_cfg.sl_pct),
            "tp1_price": straddle_entry * (1 - self.straddle_cfg.tp_pct),
            "tp2_price": 0,
            "orb_high": orb_high,
            "orb_low": orb_low,
            "vwap_at_entry": vwap_entry,
            "sweep_price": 0,
            "r_value": max_loss,
        }
        trade_id = self.db_logger.insert_trade(trade_record)

        self.straddle = ActiveStraddle(
            trade_id=trade_id,
            trade_date=_ist_today(),
            strike=strike,
            spot_entry=spot,
            entry_call_prem=call_prem,
            entry_put_prem=put_prem,
            straddle_entry=straddle_entry,
            call_symbol=call_sym,
            put_symbol=put_sym,
            quantity=quantity,
            lots=lots,
            peak_straddle=straddle_entry,
            trough_straddle=straddle_entry,
            vwap_at_entry=vwap_entry,
            orb_high=orb_high,
            orb_low=orb_low,
            india_vix=vix,
        )
        self._straddle_entered = True
        self.risk_mgr.increment_trade_count()
        self._save_state()

        self.alerts._send(
            f"<b>SHORT STRADDLE ENTRY [{'PAPER' if self.cfg.paper_mode else 'LIVE'}]</b>\n"
            f"Strike: NIFTY {strike}\n"
            f"Call Prem: {call_prem:.2f} | Put Prem: {put_prem:.2f}\n"
            f"Total: {straddle_entry:.2f}\n"
            f"Lots: {lots} ({quantity} units)\n"
            f"VIX: {vix:.1f}\n"
            f"SL: straddle above {straddle_entry * (1 + self.straddle_cfg.sl_pct):.2f}\n"
            f"TP: straddle below {straddle_entry * (1 - self.straddle_cfg.tp_pct):.2f}"
        )
        logger.info(
            f"STRADDLE ENTRY: NIFTY {strike} | {lots} lots ({quantity} units) | "
            f"Call={call_prem:.2f} Put={put_prem:.2f} Total={straddle_entry:.2f}"
        )

    # =====================================================================
    # Sweep Position Management
    # =====================================================================

    def _manage_sweep_position(self, current_spot: float):
        if self.position is None:
            return

        p = self.position

        current_premium = self.broker.ltp(self.cfg.exchange, p.tradingsymbol)
        if current_premium > p.peak_premium:
            p.peak_premium = current_premium

        # Kill switch check: compute unrealised P&L
        unrealised = (current_premium - p.entry_premium) * p.remaining_qty
        action = self.safety.check_kill_switch(
            self.risk_mgr.daily_pnl, unrealised
        )
        if action in (SafetyAction.FORCE_EXIT, SafetyAction.KILL_SWITCH):
            logger.critical("Kill switch triggered -- force exiting sweep position")
            self._force_exit_sweep()
            return

        # Position reconciliation (live only, every tick)
        if not self.cfg.paper_mode:
            recon = self.safety.reconcile_position(
                p.tradingsymbol, p.remaining_qty
            )
            if not recon.ok:
                logger.warning(f"Reconciliation mismatch: {recon.mismatch_detail}")

        if self.cfg.use_premium_exits:
            self._manage_premium_exits(current_premium)
            return

        sig = p.signal
        sl = sig.stop_loss if not p.sl_at_breakeven else sig.entry_spot
        hit_sl = False
        hit_tp1 = False
        hit_tp2 = False

        if sig.direction == "CALL":
            hit_sl = current_spot <= sl
            hit_tp1 = current_spot >= sig.tp1
            hit_tp2 = current_spot >= sig.tp2
        else:
            hit_sl = current_spot >= sl
            hit_tp1 = current_spot <= sig.tp1
            hit_tp2 = current_spot <= sig.tp2

        # TP1: exit 50%, move SL to breakeven
        if hit_tp1 and not p.tp1_hit:
            exit_qty = int(p.remaining_qty * self.cfg.tp1_exit_pct)
            exit_qty = max(self.cfg.lot_size, exit_qty)
            exit_qty = (exit_qty // self.cfg.lot_size) * self.cfg.lot_size
            if exit_qty > p.remaining_qty:
                exit_qty = p.remaining_qty

            if not self.cfg.paper_mode:
                self.broker.place_sell(p.tradingsymbol, exit_qty)

            p.tp1_hit = True
            p.sl_at_breakeven = True
            p.remaining_qty -= exit_qty

            partial_pnl = (current_premium - p.entry_premium) * exit_qty
            self.db_logger.update_tp1_hit(p.trade_id)
            self._save_state()

            self.alerts.send_tp1_hit(sig.direction, current_spot, partial_pnl)
            logger.info(
                f"TP1 HIT: exited {exit_qty} units @ spot={current_spot:.2f} | "
                f"SL moved to BE ({sig.entry_spot:.2f})"
            )

            if p.remaining_qty <= 0:
                self._close_sweep_trade("TP1", current_spot, current_premium)
                return

        # TP2: exit remaining
        if hit_tp2 and p.tp1_hit:
            if not self.cfg.paper_mode and p.remaining_qty > 0:
                self.broker.place_sell(p.tradingsymbol, p.remaining_qty)
            self._close_sweep_trade("TP2", current_spot, current_premium)
            return

        # SL hit
        if hit_sl:
            if not self.cfg.paper_mode and p.remaining_qty > 0:
                self.broker.place_sell(p.tradingsymbol, p.remaining_qty)
            exit_type = "SL_BE" if p.sl_at_breakeven else "SL"
            self._close_sweep_trade(exit_type, current_spot, current_premium)
            return

        self._save_state()

    def _manage_premium_exits(self, current_premium: float):
        """Premium-based exit with direction-specific params."""
        p = self.position
        if p is None:
            return

        is_call = p.signal.direction == "CALL"
        sl_p = self.cfg.call_sl_pct if is_call else self.cfg.put_sl_pct
        tp_p = self.cfg.call_tp_pct if is_call else self.cfg.put_tp_pct
        ta_p = self.cfg.call_trail_activate if is_call else self.cfg.put_trail_activate
        tr_p = self.cfg.call_trail_pct if is_call else self.cfg.put_trail_pct

        sl_prem = p.entry_premium * (1 - sl_p)
        tp_prem = p.entry_premium * (1 + tp_p)
        trail_active = p.peak_premium >= p.entry_premium * (1 + ta_p)

        spot = self.broker.spot_ltp()

        if current_premium >= tp_prem:
            if not self.cfg.paper_mode and p.remaining_qty > 0:
                self.broker.place_sell(p.tradingsymbol, p.remaining_qty)
            self._close_sweep_trade("TP", spot, current_premium)
            return

        if trail_active:
            trail_stop = p.peak_premium * (1 - tr_p)
            if current_premium <= trail_stop:
                if not self.cfg.paper_mode and p.remaining_qty > 0:
                    self.broker.place_sell(p.tradingsymbol, p.remaining_qty)
                self._close_sweep_trade("TRAIL", spot, current_premium)
                return

        if current_premium <= sl_prem:
            if not self.cfg.paper_mode and p.remaining_qty > 0:
                self.broker.place_sell(p.tradingsymbol, p.remaining_qty)
            self._close_sweep_trade("SL", spot, current_premium)
            return

        self._save_state()

    def _close_sweep_trade(self, exit_type: str, spot: float, premium: float):
        """Close a sweep trade, log to DB + Excel."""
        if self.position is None:
            return

        p = self.position
        from costs.zerodha import round_trip_cost
        costs = round_trip_cost(p.entry_premium, premium, p.quantity)
        gross_pnl = (premium - p.entry_premium) * p.quantity
        net_pnl = gross_pnl - costs
        r_multiple = net_pnl / (p.signal.r_value * p.quantity) \
            if p.signal.r_value > 0 else 0

        # Update SQLite
        self.db_logger.update_trade_exit(p.trade_id, {
            "exit_time": _ist_now().isoformat(),
            "spot_exit": spot,
            "exit_premium": premium,
            "gross_pnl": round(gross_pnl, 2),
            "costs": round(costs, 2),
            "net_pnl": round(net_pnl, 2),
            "exit_type": exit_type,
            "r_multiple": round(r_multiple, 2),
        })

        # Excel journal
        vix = self._current_vix
        minutes_left = max(
            (15 * 60 + 30) - (_ist_now().hour * 60 + _ist_now().minute), 1
        )
        opt_type = "CE" if p.signal.direction == "CALL" else "PE"
        delta = bs_delta(
            p.signal.entry_spot, p.signal.strike, vix, minutes_left, opt_type
        )

        self.excel.log_trade({
            "trade_id": p.trade_id,
            "trade_date": _ist_today().isoformat(),
            "entry_time": p.entry_time.isoformat(),
            "exit_time": _ist_now().isoformat(),
            "strategy": "sweep",
            "signal_reason": getattr(p.signal, "entry_mode", "sweep_reversal"),
            "direction": p.signal.direction,
            "strike": p.signal.strike,
            "spot_entry": p.signal.entry_spot,
            "spot_exit": spot,
            "entry_premium": p.entry_premium,
            "exit_premium": premium,
            "sl_amount": abs(p.signal.entry_spot - p.signal.stop_loss),
            "tp_amount": abs(p.signal.tp1 - p.signal.entry_spot),
            "india_vix": vix,
            "bs_delta": round(delta, 3),
            "is_expiry_day": self._is_expiry_day,
            "orb_high": self.strategy.orb.orb_high if self.strategy.orb else 0,
            "orb_low": self.strategy.orb.orb_low if self.strategy.orb else 0,
            "vwap_at_entry": p.signal.vwap_at_entry,
            "quantity": p.quantity,
            "lots": p.lots,
            "gross_pnl": round(gross_pnl, 2),
            "costs": round(costs, 2),
            "net_pnl": round(net_pnl, 2),
            "r_value": p.signal.r_value,
            "r_multiple": round(r_multiple, 2),
            "exit_type": exit_type,
        })

        self.risk_mgr.record_trade(net_pnl)

        self.alerts.send_exit(
            p.signal.direction, exit_type, spot, net_pnl, r_multiple
        )
        logger.info(
            f"SWEEP EXIT [{exit_type}]: spot={spot:.2f} prem={premium:.2f} | "
            f"Net P&L=Rs{net_pnl:+,.0f} ({r_multiple:+.2f}R)"
        )
        self._clear_state()

    def _force_exit_sweep(self):
        """Force exit sweep position."""
        if self.position is None:
            return
        spot = self.broker.spot_ltp()
        premium = self.broker.ltp(
            self.cfg.exchange, self.position.tradingsymbol
        )
        if not self.cfg.paper_mode and self.position.remaining_qty > 0:
            self.broker.place_sell(
                self.position.tradingsymbol, self.position.remaining_qty
            )
        self._close_sweep_trade("TIME", spot, premium)

    # =====================================================================
    # Straddle Position Management
    # =====================================================================

    def _manage_straddle(self, current_spot: float):
        """Manage an active short straddle position."""
        if self.straddle is None:
            return

        s = self.straddle

        # Fetch current premiums
        call_prem = self.broker.ltp(self.cfg.exchange, s.call_symbol)
        put_prem = self.broker.ltp(self.cfg.exchange, s.put_symbol)

        # Fallback to BS if LTP fails (paper mode)
        if call_prem <= 0 or put_prem <= 0:
            minutes_left = max(
                (15 * 60 + 30) - (_ist_now().hour * 60 + _ist_now().minute), 1
            )
            call_prem = _bs_0dte(current_spot, s.strike, minutes_left,
                                 s.india_vix, "call")
            put_prem = _bs_0dte(current_spot, s.strike, minutes_left,
                                s.india_vix, "put")

        cur_straddle = call_prem + put_prem

        # Track peaks and troughs
        if cur_straddle > s.peak_straddle:
            s.peak_straddle = cur_straddle
        if cur_straddle < s.trough_straddle:
            s.trough_straddle = cur_straddle

        trail_on_value = s.straddle_entry * (1 - self.straddle_cfg.trail_activate_pct)
        if s.trough_straddle <= trail_on_value:
            s.trail_active = True

        # Kill switch check
        # For SHORT straddle: P&L = (entry - current) * qty
        unrealised = (s.straddle_entry - cur_straddle) * s.quantity
        action = self.safety.check_kill_switch(
            self.risk_mgr.daily_pnl, unrealised
        )
        if action in (SafetyAction.FORCE_EXIT, SafetyAction.KILL_SWITCH):
            logger.critical("Kill switch triggered -- force exiting straddle")
            self._close_straddle("KILL", current_spot, call_prem, put_prem)
            return

        # Straddle-specific exit times
        now_time = _ist_now().time()
        if now_time >= self.straddle_cfg.force_exit_time:
            self._close_straddle("TIME", current_spot, call_prem, put_prem)
            return

        # SHORT TP: straddle fell enough (premium decay collected)
        tp_value = s.straddle_entry * (1 - self.straddle_cfg.tp_pct)
        if cur_straddle <= tp_value:
            self._close_straddle("TP", current_spot, call_prem, put_prem)
            return

        # SHORT trail: lock in gains
        if s.trail_active:
            trail_stop = s.trough_straddle * (1 + self.straddle_cfg.trail_pct)
            if cur_straddle >= trail_stop:
                self._close_straddle("TRAIL", current_spot, call_prem, put_prem)
                return

        # SHORT SL: straddle rose too much
        sl_value = s.straddle_entry * (1 + self.straddle_cfg.sl_pct)
        if cur_straddle >= sl_value:
            self._close_straddle("SL", current_spot, call_prem, put_prem)
            return

        self._save_state()

    def _close_straddle(self, exit_type: str, spot: float,
                        exit_call: float, exit_put: float):
        """Close straddle, log to DB + Excel."""
        if self.straddle is None:
            return

        s = self.straddle
        straddle_exit = exit_call + exit_put

        # SHORT P&L: profit when exit < entry
        gross_pnl = (s.straddle_entry - straddle_exit) * s.quantity

        from costs.zerodha import round_trip_cost
        cost_call = round_trip_cost(s.entry_call_prem, exit_call, s.quantity)
        cost_put = round_trip_cost(s.entry_put_prem, exit_put, s.quantity)
        total_costs = cost_call + cost_put
        net_pnl = gross_pnl - total_costs

        r_value = s.straddle_entry * self.straddle_cfg.sl_pct
        r_multiple = net_pnl / (r_value * s.quantity) if r_value > 0 else 0

        if not self.cfg.paper_mode:
            # Buy back both legs
            try:
                self.broker.place_buy(s.call_symbol, s.quantity)
                self.broker.place_buy(s.put_symbol, s.quantity)
            except Exception as e:
                logger.error(f"Straddle close order failed: {e}")
                self.alerts.send_error(f"Straddle close failed: {e}")

        # Update SQLite
        self.db_logger.update_trade_exit(s.trade_id, {
            "exit_time": _ist_now().isoformat(),
            "spot_exit": spot,
            "exit_premium": straddle_exit,
            "gross_pnl": round(gross_pnl, 2),
            "costs": round(total_costs, 2),
            "net_pnl": round(net_pnl, 2),
            "exit_type": exit_type,
            "r_multiple": round(r_multiple, 2),
        })

        # Compute delta at entry for logging
        minutes_left = max(
            (15 * 60 + 30) - (s.entry_time.hour * 60 + s.entry_time.minute), 1
        )
        delta_call = bs_delta(
            s.spot_entry, s.strike, s.india_vix, minutes_left, "CE"
        )
        delta_put = bs_delta(
            s.spot_entry, s.strike, s.india_vix, minutes_left, "PE"
        )

        # Excel journal
        self.excel.log_trade({
            "trade_id": s.trade_id,
            "trade_date": s.trade_date.isoformat(),
            "entry_time": s.entry_time.isoformat(),
            "exit_time": _ist_now().isoformat(),
            "strategy": "short_straddle",
            "signal_reason": "expiry_theta_decay",
            "direction": "straddle",
            "strike": s.strike,
            "spot_entry": s.spot_entry,
            "spot_exit": spot,
            "entry_premium": s.straddle_entry,
            "exit_premium": straddle_exit,
            "sl_amount": s.straddle_entry * self.straddle_cfg.sl_pct,
            "tp_amount": s.straddle_entry * self.straddle_cfg.tp_pct,
            "india_vix": s.india_vix,
            "bs_delta": round(delta_call + delta_put, 3),
            "is_expiry_day": True,
            "orb_high": s.orb_high,
            "orb_low": s.orb_low,
            "vwap_at_entry": s.vwap_at_entry,
            "quantity": s.quantity,
            "lots": s.lots,
            "gross_pnl": round(gross_pnl, 2),
            "costs": round(total_costs, 2),
            "net_pnl": round(net_pnl, 2),
            "r_value": r_value,
            "r_multiple": round(r_multiple, 2),
            "exit_type": exit_type,
        })

        self.risk_mgr.record_trade(net_pnl)

        self.alerts._send(
            f"<b>STRADDLE EXIT -- {exit_type}</b>\n"
            f"Strike: NIFTY {s.strike}\n"
            f"Spot: {spot:.2f}\n"
            f"Straddle: {s.straddle_entry:.2f} -> {straddle_exit:.2f}\n"
            f"Net P&L: Rs{net_pnl:+,.0f}\n"
            f"R-Multiple: {r_multiple:+.2f}R"
        )
        logger.info(
            f"STRADDLE EXIT [{exit_type}]: spot={spot:.2f} | "
            f"Entry={s.straddle_entry:.2f} Exit={straddle_exit:.2f} | "
            f"Net P&L=Rs{net_pnl:+,.0f} ({r_multiple:+.2f}R)"
        )
        self._clear_state()

    def _force_exit_straddle(self):
        """Force close straddle at current market."""
        if self.straddle is None:
            return
        s = self.straddle
        spot = self.broker.spot_ltp()
        call_prem = self.broker.ltp(self.cfg.exchange, s.call_symbol)
        put_prem = self.broker.ltp(self.cfg.exchange, s.put_symbol)

        if call_prem <= 0 or put_prem <= 0:
            minutes_left = max(
                (15 * 60 + 30) - (_ist_now().hour * 60 + _ist_now().minute), 1
            )
            if call_prem <= 0:
                call_prem = _bs_0dte(spot, s.strike, minutes_left,
                                     s.india_vix, "call")
            if put_prem <= 0:
                put_prem = _bs_0dte(spot, s.strike, minutes_left,
                                    s.india_vix, "put")

        self._close_straddle("TIME", spot, call_prem, put_prem)

    # =====================================================================
    # Force exit all positions
    # =====================================================================

    def _force_exit_all(self):
        """Force exit whatever position is active."""
        if self.position:
            self._force_exit_sweep()
        if self.straddle:
            self._force_exit_straddle()

    # =====================================================================
    # EOD Report + Verification
    # =====================================================================

    def _run_eod(self):
        """End-of-day sequence: verify, summarise, log."""
        # Step 1: Verify zero positions
        verified = self.safety.verify_eod_flat()
        if not verified:
            logger.warning("EOD flat verification FAILED")

        # Step 2: Update DB summaries
        today_str = _ist_today().isoformat()
        self.db_logger.update_daily_summary(today_str, self.strategy.regime_pass)

        week_start = _ist_today() - timedelta(days=_ist_today().weekday())
        self.db_logger.update_weekly_summary(week_start.isoformat())

        # Step 3: Rebuild Excel summaries
        try:
            self.excel.rebuild_summaries()
        except Exception as e:
            logger.error(f"Excel summary rebuild failed: {e}")

        # Step 4: Send Telegram summary
        trades = self.db_logger.get_trades_for_date(today_str)
        completed = [t for t in trades if t["exit_time"] is not None]
        n_trades = len(completed)
        net_pnl = sum(t.get("net_pnl", 0) for t in completed)
        daily_dd = net_pnl / self.cfg.capital * 100
        weekly_dd = self.risk_mgr.weekly_pnl / self.cfg.capital * 100

        self.alerts.send_daily_summary(n_trades, net_pnl, daily_dd, weekly_dd)

        self._eod_done = True
        logger.info(
            f"EOD complete: {n_trades} trades, P&L=Rs{net_pnl:+,.0f}, "
            f"Verified={verified}"
        )

    # =====================================================================
    # Main Loop
    # =====================================================================

    def run(self):
        log_dir = Path(self.cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler(
                    log_dir / "orb_sweep.log", encoding="utf-8"
                ),
            ],
        )

        logger.info("ORB Sweep + Straddle Trader starting...")
        logger.info(
            f"Capital: Rs{self.cfg.capital:,.0f} | "
            f"Sweep risk: 1% | Straddle risk: 3% | "
            f"Paper mode: {self.cfg.paper_mode}"
        )

        self.broker.connect()
        self._load_state()
        self.alerts.send_startup()

        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                self._force_exit_all()
                self.db_logger.close()
                break
            except Exception as e:
                logger.error(f"Tick error: {e}", exc_info=True)
                self.alerts.send_error(str(e))

                # If in outage, wait longer
                if self.safety.is_outage:
                    _time.sleep(60)
                else:
                    _time.sleep(30)

    def _tick(self):
        now = _ist_now()
        now_time = now.time()

        # Skip weekends
        if now.weekday() >= 5:
            _time.sleep(60)
            return

        self._reset_day()

        # Check kill switch first -- if triggered, skip all trading logic
        if self.safety.is_killed:
            if self.position or self.straddle:
                self._force_exit_all()
            _time.sleep(self.cfg.poll_interval_s)
            return

        # Before market
        if now_time < self.cfg.orb_start:
            _time.sleep(self.cfg.poll_interval_s)
            return

        # ORB window (9:15-9:30)
        if self.cfg.orb_start <= now_time < self.cfg.orb_end:
            _time.sleep(self.cfg.poll_interval_s)
            return

        # Compute ORB + regime check (at 9:30)
        if not self._orb_computed and now_time >= self.cfg.orb_end:
            df_1m = self.broker.fetch_1m(days_back=5)
            if df_1m.empty:
                logger.error("No 1m data -- retrying")
                _time.sleep(30)
                return

            today = _ist_today()
            orb = self.strategy.compute_orb(df_1m, today)
            if orb is None:
                logger.warning("ORB computation failed -- insufficient bars")
                _time.sleep(30)
                return

            self._orb_computed = True
            logger.info(
                f"ORB computed: H={orb.orb_high:.2f} L={orb.orb_low:.2f} "
                f"Range={orb.first_15m_range_pct*100:.3f}%"
            )

            # Fetch VIX for the day
            self._current_vix = self.broker.fetch_vix()
            logger.info(f"India VIX: {self._current_vix:.1f}")

            regime_ok = self.strategy.check_regime(df_1m, today)
            self._regime_checked = True

            if not regime_ok:
                from orb_sweep.indicators import atr, resample_to_5m
                df_5m = resample_to_5m(df_1m)
                atr_vals = atr(df_5m, self.cfg.atr_period)
                atr_val = float(atr_vals.iloc[-1]) if not atr_vals.empty else 0
                self.alerts.send_regime_fail(orb.first_15m_range_pct, atr_val)
                logger.info("Regime filter FAILED -- no sweep trades today")
            else:
                logger.info("Regime filter PASSED -- scanning for signals")

            # Expiry day: enter straddle at ~9:31
            if (
                self._is_expiry_day
                and not self._straddle_entered
                and self.straddle is None
                and self.risk_mgr.can_trade()
            ):
                spot = self.broker.spot_ltp()
                vix = self._current_vix
                orb_range_pct = orb.first_15m_range_pct

                if (
                    orb_range_pct <= self.straddle_cfg.max_orb_range_pct
                    and self.straddle_cfg.min_vix <= vix <= self.straddle_cfg.max_vix
                ):
                    # Compute VWAP
                    today_bars = df_1m[df_1m.index.date == today]
                    orb_bars = today_bars[
                        today_bars.index.time < pd.Timestamp("09:30").time()
                    ]
                    if not orb_bars.empty:
                        tp = (orb_bars["High"] + orb_bars["Low"] + orb_bars["Close"]) / 3
                        vol = orb_bars["Volume"]
                        vwap_entry = float(
                            (tp * vol).sum() / vol.sum()
                        ) if vol.sum() > 0 else spot
                    else:
                        vwap_entry = spot

                    self._enter_straddle(
                        spot, vix, orb.orb_high, orb.orb_low, vwap_entry
                    )
                else:
                    logger.info(
                        f"Straddle skipped: ORB range={orb_range_pct*100:.3f}% "
                        f"(max {self.straddle_cfg.max_orb_range_pct*100:.1f}%), "
                        f"VIX={vix:.1f} "
                        f"(range {self.straddle_cfg.min_vix}-{self.straddle_cfg.max_vix})"
                    )

        # Force exit at 15:15
        if now_time >= self.cfg.force_exit:
            if self.position or self.straddle:
                self._force_exit_all()

            if not self._eod_done and now_time >= time(15, 20):
                self._run_eod()

            if now_time >= self.cfg.market_close:
                _time.sleep(60)
            else:
                _time.sleep(self.cfg.poll_interval_s)
            return

        # Manage active straddle
        if self.straddle:
            spot = self.broker.spot_ltp()
            self._manage_straddle(spot)

        # Signal scan + position management (9:30-15:15)
        if (
            self._orb_computed
            and self._regime_checked
            and self.strategy.regime_pass
        ):
            df_1m = self.broker.fetch_1m(days_back=2)
            if df_1m.empty:
                _time.sleep(self.cfg.poll_interval_s)
                return

            spot = float(df_1m.iloc[-1]["Close"])

            # Only scan for sweep signals if no active position and no straddle
            if (
                self.position is None
                and self.straddle is None
                and self.risk_mgr.can_trade()
                and not self.safety.is_killed
            ):
                today = _ist_today()
                today_bars = df_1m[df_1m.index.date == today]
                post_orb = today_bars[
                    today_bars.index.time >= pd.Timestamp("09:30").time()
                ]
                for i in range(len(post_orb)):
                    bar_idx = df_1m.index.get_loc(post_orb.index[i])
                    if isinstance(bar_idx, slice):
                        bar_idx = bar_idx.start
                    signal = self.strategy.scan_bar(df_1m, bar_idx)
                    if signal:
                        self._enter_sweep_trade(signal)
                        break

            if self.position:
                self._manage_sweep_position(spot)

        _time.sleep(self.cfg.poll_interval_s)
