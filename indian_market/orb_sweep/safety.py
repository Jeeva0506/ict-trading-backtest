"""
Production safety layer for ORB Sweep trading system.

Provides:
  1. Position reconciliation — verify internal state matches broker
  2. Daily loss kill switch — hard stop if DD limit breached
  3. EOD force square-off verification — confirm zero positions after 15:15
  4. Partial fill recovery — handle incomplete order fills
  5. Broker/API outage handling — detect, retry with backoff, alert
"""

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from orb_sweep.config import TradingConfig, IST

logger = logging.getLogger(__name__)

IST_TZ = IST


def _ist_now() -> datetime:
    return datetime.now(IST_TZ)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class SafetyAction(Enum):
    NONE = "none"
    FORCE_EXIT = "force_exit"
    KILL_SWITCH = "kill_switch"
    RECONCILE_EXIT = "reconcile_exit"
    ALERT_ONLY = "alert_only"


@dataclass
class ReconciliationResult:
    ok: bool = True
    broker_positions: List[Dict] = field(default_factory=list)
    internal_symbol: str = ""
    internal_qty: int = 0
    broker_qty: int = 0
    mismatch_detail: str = ""


@dataclass
class OrderStatus:
    order_id: str = ""
    status: str = ""
    filled_qty: int = 0
    pending_qty: int = 0
    tradingsymbol: str = ""
    average_price: float = 0.0


# ---------------------------------------------------------------------------
# Broker API wrapper with retry + outage detection
# ---------------------------------------------------------------------------

class BrokerAPIGuard:
    """
    Wraps broker calls with retry logic and outage detection.
    Tracks consecutive failures and alerts on Telegram when an outage
    is detected or recovered.
    """

    MAX_RETRIES = 3
    BASE_DELAY_S = 2.0       # exponential backoff base
    MAX_DELAY_S = 30.0
    OUTAGE_THRESHOLD = 5     # consecutive failures before declaring outage

    def __init__(self, broker, alerts, config: TradingConfig):
        self.broker = broker
        self.alerts = alerts
        self.cfg = config
        self._consecutive_failures: int = 0
        self._in_outage: bool = False
        self._outage_start: Optional[datetime] = None
        self._last_success: Optional[datetime] = None

    # -- public api --------------------------------------------------------

    def call(self, method_name: str, *args, **kwargs) -> Any:
        """
        Call a broker method with retry + backoff.
        Returns the result on success or raises after MAX_RETRIES.
        """
        method = getattr(self.broker, method_name)
        last_err = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                result = method(*args, **kwargs)
                self._on_success()
                return result
            except Exception as e:
                last_err = e
                self._on_failure(method_name, e, attempt)
                if attempt < self.MAX_RETRIES:
                    delay = min(
                        self.BASE_DELAY_S * (2 ** (attempt - 1)),
                        self.MAX_DELAY_S,
                    )
                    logger.warning(
                        f"Broker call {method_name} attempt {attempt} failed: {e} "
                        f"— retrying in {delay:.1f}s"
                    )
                    _time.sleep(delay)

        # All retries exhausted
        raise last_err

    def safe_call(self, method_name: str, default: Any = None,
                  *args, **kwargs) -> Any:
        """
        Like call() but returns *default* on failure instead of raising.
        """
        try:
            return self.call(method_name, *args, **kwargs)
        except Exception as e:
            logger.error(f"Broker call {method_name} failed after retries: {e}")
            return default

    @property
    def is_outage(self) -> bool:
        return self._in_outage

    @property
    def outage_duration_s(self) -> float:
        if not self._in_outage or self._outage_start is None:
            return 0.0
        return (_ist_now() - self._outage_start).total_seconds()

    # -- internal ----------------------------------------------------------

    def _on_success(self):
        if self._in_outage:
            duration = self.outage_duration_s
            self._in_outage = False
            self._outage_start = None
            logger.info(f"Broker connection RECOVERED after {duration:.0f}s")
            try:
                self.alerts.send_error(
                    f"Broker RECOVERED after {duration:.0f}s outage"
                )
            except Exception:
                pass
        self._consecutive_failures = 0
        self._last_success = _ist_now()

    def _on_failure(self, method: str, error: Exception, attempt: int):
        self._consecutive_failures += 1
        if (
            self._consecutive_failures >= self.OUTAGE_THRESHOLD
            and not self._in_outage
        ):
            self._in_outage = True
            self._outage_start = _ist_now()
            logger.critical(
                f"BROKER OUTAGE DETECTED — {self._consecutive_failures} "
                f"consecutive failures on {method}: {error}"
            )
            try:
                self.alerts.send_error(
                    f"BROKER OUTAGE DETECTED\n"
                    f"Method: {method}\n"
                    f"Failures: {self._consecutive_failures}\n"
                    f"Last error: {error}"
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Position Reconciliation
# ---------------------------------------------------------------------------

class PositionReconciler:
    """
    Compares internal position state against broker-reported positions
    on every tick. Detects mismatches and triggers corrective action.
    """

    def __init__(self, broker, api_guard: BrokerAPIGuard, alerts,
                 config: TradingConfig):
        self.broker = broker
        self.api = api_guard
        self.alerts = alerts
        self.cfg = config
        self._last_check: Optional[datetime] = None
        self._mismatch_count: int = 0

    def reconcile(self, internal_symbol: str,
                  internal_qty: int) -> ReconciliationResult:
        """
        Fetch net positions from the broker and compare with our internal
        state. Returns a ReconciliationResult.
        """
        result = ReconciliationResult(
            internal_symbol=internal_symbol,
            internal_qty=internal_qty,
        )

        if self.cfg.paper_mode:
            # In paper mode, we trust our internal state
            result.ok = True
            return result

        try:
            positions = self.api.call("_get_positions")
        except Exception as e:
            logger.error(f"Position fetch failed during reconciliation: {e}")
            result.ok = False
            result.mismatch_detail = f"Position fetch failed: {e}"
            return result

        result.broker_positions = positions
        self._last_check = _ist_now()

        # Find our symbol in broker positions
        broker_qty = 0
        for pos in positions:
            if pos.get("tradingsymbol") == internal_symbol:
                broker_qty = pos.get("quantity", 0)
                break

        result.broker_qty = broker_qty

        if broker_qty != internal_qty:
            self._mismatch_count += 1
            result.ok = False
            result.mismatch_detail = (
                f"MISMATCH: internal={internal_qty} broker={broker_qty} "
                f"for {internal_symbol} (count={self._mismatch_count})"
            )
            logger.warning(result.mismatch_detail)
            try:
                self.alerts.send_error(
                    f"POSITION MISMATCH\n"
                    f"Symbol: {internal_symbol}\n"
                    f"Internal qty: {internal_qty}\n"
                    f"Broker qty: {broker_qty}\n"
                    f"Mismatch #{self._mismatch_count}"
                )
            except Exception:
                pass
        else:
            self._mismatch_count = 0
            result.ok = True

        return result

    def check_orphan_positions(self, our_symbol: Optional[str] = None
                                ) -> List[Dict]:
        """
        Find broker positions that we don't know about (orphans).
        Returns list of orphan position dicts.
        """
        if self.cfg.paper_mode:
            return []

        try:
            positions = self.api.call("_get_positions")
        except Exception:
            return []

        orphans = []
        for pos in positions:
            qty = pos.get("quantity", 0)
            sym = pos.get("tradingsymbol", "")
            if qty != 0 and sym != our_symbol:
                orphans.append(pos)

        if orphans:
            syms = [p.get("tradingsymbol", "?") for p in orphans]
            logger.warning(f"Orphan positions detected: {syms}")
            try:
                self.alerts.send_error(
                    f"ORPHAN POSITIONS DETECTED\n"
                    f"Symbols: {syms}\n"
                    f"Action: manual review required"
                )
            except Exception:
                pass

        return orphans


# ---------------------------------------------------------------------------
# Daily Loss Kill Switch
# ---------------------------------------------------------------------------

class DailyKillSwitch:
    """
    Hard kill switch that halts ALL trading for the day when daily loss
    exceeds the configured limit. More aggressive than risk_manager.can_trade()
    because it also force-exits any open position.
    """

    def __init__(self, config: TradingConfig, alerts):
        self.cfg = config
        self.alerts = alerts
        self._triggered = False
        self._trigger_time: Optional[datetime] = None

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    def reset(self):
        self._triggered = False
        self._trigger_time = None

    def check(self, daily_pnl: float, unrealised_pnl: float = 0.0
              ) -> SafetyAction:
        """
        Check if daily loss (realised + unrealised) exceeds limit.
        Returns SafetyAction indicating what the caller should do.
        """
        if self._triggered:
            return SafetyAction.KILL_SWITCH

        total_pnl = daily_pnl + unrealised_pnl
        limit = self.cfg.daily_dd_limit

        if total_pnl <= -limit:
            self._triggered = True
            self._trigger_time = _ist_now()
            logger.critical(
                f"KILL SWITCH TRIGGERED: daily P&L={total_pnl:+,.0f} "
                f"exceeds limit of -{limit:,.0f}"
            )
            try:
                self.alerts.send_error(
                    f"DAILY KILL SWITCH TRIGGERED\n"
                    f"Daily P&L: Rs{total_pnl:+,.0f}\n"
                    f"Limit: Rs{-limit:,.0f}\n"
                    f"All trading halted for today.\n"
                    f"Open position will be force-exited."
                )
            except Exception:
                pass
            return SafetyAction.FORCE_EXIT

        # Warn at 75% of limit
        if total_pnl <= -limit * 0.75:
            logger.warning(
                f"Kill switch WARNING: daily P&L={total_pnl:+,.0f} "
                f"at {abs(total_pnl)/limit*100:.0f}% of limit"
            )

        return SafetyAction.NONE


# ---------------------------------------------------------------------------
# EOD Force Square-Off Verification
# ---------------------------------------------------------------------------

class EODVerifier:
    """
    After the 15:15 force exit, verify that no positions remain open
    with the broker. Retries up to 3 times with delays.
    """

    MAX_VERIFY_ATTEMPTS = 3
    VERIFY_DELAY_S = 10

    def __init__(self, broker, api_guard: BrokerAPIGuard, alerts,
                 config: TradingConfig):
        self.broker = broker
        self.api = api_guard
        self.alerts = alerts
        self.cfg = config
        self._verified_today = False

    def reset(self):
        self._verified_today = False

    @property
    def verified(self) -> bool:
        return self._verified_today

    def verify_zero_positions(self) -> bool:
        """
        Check that no NIFTY option positions remain with the broker.
        Returns True if zero or paper mode, False if positions remain.
        """
        if self.cfg.paper_mode:
            logger.info("EOD verification: paper mode — assumed clean")
            self._verified_today = True
            return True

        for attempt in range(1, self.MAX_VERIFY_ATTEMPTS + 1):
            try:
                positions = self.api.call("_get_positions")
            except Exception as e:
                logger.error(
                    f"EOD verify attempt {attempt}: position fetch failed: {e}"
                )
                if attempt < self.MAX_VERIFY_ATTEMPTS:
                    _time.sleep(self.VERIFY_DELAY_S)
                continue

            open_positions = [
                p for p in positions
                if p.get("quantity", 0) != 0
                and "NIFTY" in p.get("tradingsymbol", "")
            ]

            if not open_positions:
                logger.info("EOD verification PASSED: zero NIFTY positions")
                self._verified_today = True
                try:
                    self.alerts.send_error(
                        "EOD VERIFICATION PASSED\nZero open positions confirmed."
                    )
                except Exception:
                    pass
                return True

            logger.warning(
                f"EOD verify attempt {attempt}: {len(open_positions)} "
                f"positions still open"
            )

            # Try to close remaining positions
            for pos in open_positions:
                sym = pos.get("tradingsymbol", "")
                qty = abs(pos.get("quantity", 0))
                if qty > 0:
                    try:
                        if pos.get("quantity", 0) > 0:
                            self.api.call("place_sell", sym, qty)
                        else:
                            self.api.call("place_buy", sym, qty)
                        logger.info(f"EOD cleanup: closed {sym} qty={qty}")
                    except Exception as e:
                        logger.error(f"EOD cleanup failed for {sym}: {e}")

            if attempt < self.MAX_VERIFY_ATTEMPTS:
                _time.sleep(self.VERIFY_DELAY_S)

        # All attempts exhausted, positions remain
        logger.critical("EOD VERIFICATION FAILED: positions remain open")
        try:
            self.alerts.send_error(
                "EOD VERIFICATION FAILED\n"
                "Open positions remain after force exit.\n"
                "MANUAL INTERVENTION REQUIRED."
            )
        except Exception:
            pass
        self._verified_today = False
        return False


# ---------------------------------------------------------------------------
# Partial Fill Recovery
# ---------------------------------------------------------------------------

class PartialFillHandler:
    """
    Monitors order status and handles partial fills:
      - If an order is partially filled, retry for the remaining quantity
      - If a fill is stuck, cancel + replace with market order
      - Tracks fill quality for analytics
    """

    FILL_WAIT_S = 5
    MAX_WAIT_CYCLES = 6  # 30 seconds total before intervention

    def __init__(self, broker, api_guard: BrokerAPIGuard, alerts,
                 config: TradingConfig):
        self.broker = broker
        self.api = api_guard
        self.alerts = alerts
        self.cfg = config

    def wait_for_fill(self, order_id: str, expected_qty: int,
                      tradingsymbol: str) -> OrderStatus:
        """
        Wait for an order to fill completely. If partial fill persists,
        cancel the remainder and place a fresh market order.

        Returns the final OrderStatus.
        """
        if self.cfg.paper_mode:
            return OrderStatus(
                order_id=order_id,
                status="COMPLETE",
                filled_qty=expected_qty,
                pending_qty=0,
                tradingsymbol=tradingsymbol,
                average_price=0.0,
            )

        for cycle in range(self.MAX_WAIT_CYCLES):
            status = self._get_order_status(order_id)
            if status is None:
                _time.sleep(self.FILL_WAIT_S)
                continue

            if status.status == "COMPLETE":
                logger.info(
                    f"Order {order_id} COMPLETE: filled {status.filled_qty} "
                    f"@ avg {status.average_price:.2f}"
                )
                return status

            if status.status in ("CANCELLED", "REJECTED"):
                logger.warning(
                    f"Order {order_id} {status.status}: "
                    f"filled={status.filled_qty}/{expected_qty}"
                )
                if status.filled_qty > 0 and status.filled_qty < expected_qty:
                    return self._handle_partial(
                        status, expected_qty, tradingsymbol
                    )
                return status

            # Still open — check for partial fill
            if status.filled_qty > 0 and status.pending_qty > 0:
                logger.info(
                    f"Order {order_id} partial fill: {status.filled_qty}/"
                    f"{expected_qty} — waiting..."
                )

            _time.sleep(self.FILL_WAIT_S)

        # Timed out — handle partial
        final_status = self._get_order_status(order_id)
        if final_status and final_status.pending_qty > 0:
            logger.warning(
                f"Order {order_id} timed out with partial fill: "
                f"{final_status.filled_qty}/{expected_qty}"
            )
            return self._handle_partial(
                final_status, expected_qty, tradingsymbol
            )

        return final_status or OrderStatus(
            order_id=order_id, status="UNKNOWN",
            tradingsymbol=tradingsymbol,
        )

    def _handle_partial(self, status: OrderStatus, expected_qty: int,
                        tradingsymbol: str) -> OrderStatus:
        """Cancel pending portion and re-submit as market order."""
        remaining = expected_qty - status.filled_qty
        if remaining <= 0:
            return status

        # Try to cancel the original order
        try:
            self.api.call("_cancel_order", status.order_id)
            logger.info(f"Cancelled partial order {status.order_id}")
        except Exception as e:
            logger.warning(f"Cancel attempt on {status.order_id}: {e}")

        _time.sleep(1)

        # Place fresh market order for the remainder
        try:
            new_id = self.api.call("place_buy", tradingsymbol, remaining)
            logger.info(
                f"Replacement order {new_id} for {remaining} units of "
                f"{tradingsymbol}"
            )
            try:
                self.alerts.send_error(
                    f"PARTIAL FILL RECOVERY\n"
                    f"Original order: {status.order_id}\n"
                    f"Filled: {status.filled_qty}/{expected_qty}\n"
                    f"New order: {new_id} for {remaining} units"
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Replacement order failed: {e}")
            try:
                self.alerts.send_error(
                    f"PARTIAL FILL RECOVERY FAILED\n"
                    f"Symbol: {tradingsymbol}\n"
                    f"Filled: {status.filled_qty}/{expected_qty}\n"
                    f"Unfilled: {remaining}\n"
                    f"Error: {e}"
                )
            except Exception:
                pass

        # Update status to reflect what we have
        status.pending_qty = remaining
        return status

    def _get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """Fetch order status from broker."""
        try:
            orders = self.api.call("_get_orders")
            for o in orders:
                if str(o.get("order_id")) == str(order_id):
                    return OrderStatus(
                        order_id=str(o["order_id"]),
                        status=o.get("status", "UNKNOWN"),
                        filled_qty=o.get("filled_quantity", 0),
                        pending_qty=o.get("pending_quantity", 0),
                        tradingsymbol=o.get("tradingsymbol", ""),
                        average_price=o.get("average_price", 0.0),
                    )
        except Exception as e:
            logger.error(f"Order status fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Unified Safety Manager
# ---------------------------------------------------------------------------

class SafetyManager:
    """
    Single entry point that aggregates all safety subsystems.
    The trader calls safety_manager.tick() every cycle.
    """

    def __init__(self, broker, alerts, config: TradingConfig):
        self.cfg = config
        self.broker = broker
        self.alerts = alerts

        self.api_guard = BrokerAPIGuard(broker, alerts, config)
        self.reconciler = PositionReconciler(
            broker, self.api_guard, alerts, config
        )
        self.kill_switch = DailyKillSwitch(config, alerts)
        self.eod_verifier = EODVerifier(
            broker, self.api_guard, alerts, config
        )
        self.partial_fill = PartialFillHandler(
            broker, self.api_guard, alerts, config
        )

    def reset_day(self):
        """Reset all daily state."""
        self.kill_switch.reset()
        self.eod_verifier.reset()

    def check_kill_switch(self, daily_pnl: float,
                          unrealised_pnl: float = 0.0) -> SafetyAction:
        """Check if trading should be halted."""
        return self.kill_switch.check(daily_pnl, unrealised_pnl)

    def reconcile_position(self, symbol: str, qty: int
                           ) -> ReconciliationResult:
        """Reconcile internal position with broker."""
        return self.reconciler.reconcile(symbol, qty)

    def verify_eod_flat(self) -> bool:
        """Verify zero positions after force exit."""
        return self.eod_verifier.verify_zero_positions()

    def wait_for_fill(self, order_id: str, expected_qty: int,
                      tradingsymbol: str) -> OrderStatus:
        """Wait for order to fill, handle partials."""
        return self.partial_fill.wait_for_fill(
            order_id, expected_qty, tradingsymbol
        )

    @property
    def is_killed(self) -> bool:
        return self.kill_switch.is_triggered

    @property
    def is_outage(self) -> bool:
        return self.api_guard.is_outage

    def broker_call(self, method: str, *args, **kwargs) -> Any:
        """Broker call with retry + outage detection."""
        return self.api_guard.call(method, *args, **kwargs)

    def safe_broker_call(self, method: str, default: Any = None,
                         *args, **kwargs) -> Any:
        """Broker call that returns default on failure."""
        return self.api_guard.safe_call(method, default, *args, **kwargs)


# ---------------------------------------------------------------------------
# Indian Market-Specific Production Guards
# ---------------------------------------------------------------------------

class IndianMarketGuard:
    """
    Production safety checks specific to Indian options markets.
    Based on research into NSE/BSE microstructure, SEBI rules, and
    Zerodha-specific risks.

    Covers:
      - Order freeze quantity (1800 for NIFTY)
      - Rate limiter (10 OPS, 400/min, 5000/day)
      - VIX spike halt (no new entries if VIX > threshold)
      - STT trap prevention (exit ITM options by 15:05)
      - Margin adequacy check (40% buffer)
      - Spread check (reject if bid-ask > 5% of mid)
      - Liquidity window enforcement (no entries after 14:00)
      - Token expiry detection (403 → alert for re-auth)
      - Circuit breaker awareness
    """

    def __init__(self, broker, alerts, config: TradingConfig):
        self.broker = broker
        self.alerts = alerts
        self.cfg = config
        self._orders_this_second = 0
        self._orders_this_minute = 0
        self._orders_today = 0
        self._last_second = 0
        self._last_minute = 0
        self._token_expired = False
        self._vix_halted = False
        self._circuit_halt = False

    def reset_day(self):
        self._orders_today = 0
        self._token_expired = False
        self._vix_halted = False
        self._circuit_halt = False

    # ── Rate Limiter ──────────────────────────────────────────────────
    def check_rate_limit(self) -> bool:
        now = _time.time()
        cur_sec = int(now)
        cur_min = int(now / 60)

        if cur_sec != self._last_second:
            self._orders_this_second = 0
            self._last_second = cur_sec
        if cur_min != self._last_minute:
            self._orders_this_minute = 0
            self._last_minute = cur_min

        if self._orders_this_second >= self.cfg.max_orders_per_second:
            logger.warning("Rate limit: 10 OPS reached, waiting...")
            return False
        if self._orders_this_minute >= self.cfg.max_orders_per_minute:
            logger.warning("Rate limit: 400/min reached")
            return False
        if self._orders_today >= self.cfg.max_orders_per_day:
            logger.error("Rate limit: 5000/day reached — no more orders today")
            return False
        return True

    def record_order(self):
        self._orders_this_second += 1
        self._orders_this_minute += 1
        self._orders_today += 1

    # ── Order Freeze Qty ──────────────────────────────────────────────
    def validate_order_qty(self, quantity: int) -> int:
        if quantity > self.cfg.order_freeze_qty:
            logger.warning(
                f"Qty {quantity} exceeds freeze limit {self.cfg.order_freeze_qty}, "
                f"capping to {self.cfg.order_freeze_qty}"
            )
            return self.cfg.order_freeze_qty
        return quantity

    # ── VIX Spike Halt ────────────────────────────────────────────────
    def check_vix_safe(self, current_vix: float) -> bool:
        if current_vix > self.cfg.vix_halt_threshold:
            if not self._vix_halted:
                self._vix_halted = True
                msg = (f"VIX HALT: VIX={current_vix:.1f} > "
                       f"{self.cfg.vix_halt_threshold}. No new entries.")
                logger.warning(msg)
                self.alerts._send(f"<b>VIX HALT</b>\n{msg}")
            return False
        if self._vix_halted and current_vix < self.cfg.vix_halt_threshold * 0.9:
            self._vix_halted = False
            logger.info(f"VIX halt cleared: VIX={current_vix:.1f}")
        return not self._vix_halted

    # ── STT Trap Prevention ───────────────────────────────────────────
    def check_stt_trap(self, spot: float, strike: int, direction: str,
                       now_time: time) -> bool:
        """
        If option is ITM and time > 15:05, force exit immediately.
        Exercise STT is 100x more than selling STT.
        """
        if now_time < self.cfg.stt_trap_exit_time:
            return False

        is_itm = False
        if direction in ("CALL", "CE"):
            is_itm = spot > strike
        elif direction in ("PUT", "PE"):
            is_itm = spot < strike

        if is_itm:
            logger.warning(
                f"STT TRAP: {direction} {strike} is ITM (spot={spot:.2f}) "
                f"at {now_time}. Must exit NOW to avoid exercise STT."
            )
            return True
        return False

    # ── Margin Check ──────────────────────────────────────────────────
    def check_margin_adequate(self, required_margin: float) -> bool:
        if self.cfg.paper_mode:
            return True
        try:
            margins = self.broker.kite.margins("equity")
            available = float(margins.get("available", {}).get("live_balance", 0))
            buffered_req = required_margin * (1 + self.cfg.margin_buffer_pct)
            if available < buffered_req:
                logger.warning(
                    f"Margin insufficient: available={available:.0f}, "
                    f"required (with {self.cfg.margin_buffer_pct*100:.0f}% buffer)="
                    f"{buffered_req:.0f}"
                )
                self.alerts._send(
                    f"<b>MARGIN WARNING</b>\n"
                    f"Available: Rs {available:,.0f}\n"
                    f"Required (buffered): Rs {buffered_req:,.0f}"
                )
                return False
            return True
        except Exception as e:
            logger.error(f"Margin check failed: {e}")
            return True  # allow in doubt

    # ── Spread Check ──────────────────────────────────────────────────
    def check_spread_acceptable(self, bid: float, ask: float) -> bool:
        if bid <= 0 or ask <= 0:
            return False
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid
        if spread_pct > self.cfg.max_spread_pct:
            logger.warning(
                f"Spread too wide: bid={bid:.2f} ask={ask:.2f} "
                f"spread={spread_pct*100:.1f}% > {self.cfg.max_spread_pct*100:.0f}%"
            )
            return False
        return True

    # ── Liquidity Window ──────────────────────────────────────────────
    def is_in_entry_window(self, now_time: time) -> bool:
        if now_time > self.cfg.no_new_entry_after:
            return False
        return True

    # ── Token Expiry Detection ────────────────────────────────────────
    def handle_token_expired(self):
        if not self._token_expired:
            self._token_expired = True
            msg = ("TOKEN EXPIRED: Zerodha session invalid. "
                   "Run zerodha_auth.py to re-authenticate.")
            logger.critical(msg)
            self.alerts._send(f"<b>CRITICAL: TOKEN EXPIRED</b>\n{msg}")

    # ── ELM Margin for Expiry Shorts ──────────────────────────────────
    def estimate_expiry_margin(self, spot: float, lots: int) -> float:
        """
        Estimate margin needed for short straddle on expiry day.
        Includes 2% extra ELM effective Nov 2024.
        """
        contract_value = spot * self.cfg.lot_size * lots
        base_margin = contract_value * 0.11  # ~11% SPAN+ELM
        extra_elm = contract_value * self.cfg.elm_extra_pct  # 2% extra
        return base_margin + extra_elm

    # ── Pre-Trade Safety Gate ─────────────────────────────────────────
    def pre_trade_check(self, now_time: time, vix: float,
                        spot: float = 0, lots: int = 1,
                        is_expiry: bool = False) -> Tuple[bool, str]:
        """
        Run all pre-trade safety checks. Returns (ok, reason).
        """
        if self._token_expired:
            return False, "Token expired"
        if self._circuit_halt:
            return False, "Circuit breaker active"
        if not self.check_rate_limit():
            return False, "Rate limit"
        if not self.check_vix_safe(vix):
            return False, f"VIX too high ({vix:.1f})"
        if not self.is_in_entry_window(now_time):
            return False, f"Past entry cutoff ({self.cfg.no_new_entry_after})"
        if is_expiry and spot > 0:
            margin_needed = self.estimate_expiry_margin(spot, lots)
            if not self.check_margin_adequate(margin_needed):
                return False, f"Insufficient margin (need Rs {margin_needed:,.0f})"
        return True, "OK"
