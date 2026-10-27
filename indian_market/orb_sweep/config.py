"""Configuration for ORB Sweep trading system."""

from dataclasses import dataclass, field
from datetime import time, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent


@dataclass
class TradingConfig:
    # ── Capital & Risk ─────────────────────────────────────────────────────
    capital: float = 300_000
    risk_per_trade_pct: float = 0.01        # 1% sweep risk (straddle uses 3% separately)
    max_trades_per_day: int = 3             # straddle + up to 2 sweep trades
    daily_dd_limit_pct: float = 0.02        # 2% of 3L = Rs 6,000
    weekly_dd_limit_pct: float = 0.05       # 5% of 3L = Rs 15,000

    # ── Market Schedule (IST) ──────────────────────────────────────────────
    orb_start: time = field(default_factory=lambda: time(9, 15))
    orb_end: time = field(default_factory=lambda: time(9, 30))
    entry_start: time = field(default_factory=lambda: time(9, 30))
    force_exit: time = field(default_factory=lambda: time(15, 15))
    market_close: time = field(default_factory=lambda: time(15, 30))

    # ── Regime Filter ──────────────────────────────────────────────────────
    min_range_pct: float = 0.0025
    min_atr_5m: float = 18.0
    atr_period: int = 14

    # ── Sweep Parameters ───────────────────────────────────────────────────
    sweep_closeback_bars: int = 5

    # ── Volume Filter ──────────────────────────────────────────────────────
    volume_sma_period: int = 20
    volume_multiplier: float = 1.5

    # ── Swing Detection ────────────────────────────────────────────────────
    swing_pivot_n: int = 1

    # ── Exit Parameters (premium-based, asymmetric per direction) ────────
    tp1_r_multiple: float = 1.0
    tp2_r_multiple: float = 3.0
    tp1_exit_pct: float = 0.50
    use_premium_exits: bool = True
    # CALL exits (momentum breakout)
    call_sl_pct: float = 0.30
    call_tp_pct: float = 0.50
    call_trail_activate: float = 0.20
    call_trail_pct: float = 0.15
    # PUT exits (sweep reversal)
    put_sl_pct: float = 0.30
    put_tp_pct: float = 0.75
    put_trail_activate: float = 0.25
    put_trail_pct: float = 0.20

    # ── Instrument ─────────────────────────────────────────────────────────
    instrument_name: str = "NIFTY 50"
    nifty_token: int = 256265
    vix_token: int = 264969
    lot_size: int = 75                          # updated Nov 2024 (was 50)
    strike_step: int = 50
    exchange: str = "NFO"

    # ── Polling ────────────────────────────────────────────────────────────
    poll_interval_s: int = 15

    # ── Slippage (backtest) ────────────────────────────────────────────────
    slippage_pct: float = 0.005

    # ── Mode ───────────────────────────────────────────────────────────────
    paper_mode: bool = True

    # ── Production Safety ──────────────────────────────────────────────────
    order_freeze_qty: int = 1800                # NSE max qty per order
    force_exit_time_hard: time = field(default_factory=lambda: time(15, 10))
    stt_trap_exit_time: time = field(default_factory=lambda: time(15, 5))
    broker_auto_sqoff: time = field(default_factory=lambda: time(15, 26))
    no_new_entry_after: time = field(default_factory=lambda: time(14, 0))
    margin_buffer_pct: float = 0.40             # keep 40% above required margin
    vix_halt_threshold: float = 25.0            # stop new entries if VIX > 25
    max_spread_pct: float = 0.05                # reject if bid-ask > 5% of mid
    straddle_risk_pct: float = 0.03             # 3% for straddles
    elm_extra_pct: float = 0.02                 # 2% extra ELM on expiry shorts

    # ── Rate Limits (Zerodha) ──────────────────────────────────────────────
    max_orders_per_second: int = 10
    max_orders_per_minute: int = 400
    max_orders_per_day: int = 5000

    # ── Paths ──────────────────────────────────────────────────────────────
    db_path: str = field(default_factory=lambda: str(ROOT / "data" / "trades.db"))
    state_path: str = field(default_factory=lambda: str(ROOT / "data" / "state.json"))
    log_dir: str = field(default_factory=lambda: str(ROOT / "logs"))

    @property
    def risk_per_trade(self) -> float:
        return self.capital * self.risk_per_trade_pct

    @property
    def daily_dd_limit(self) -> float:
        return self.capital * self.daily_dd_limit_pct

    @property
    def weekly_dd_limit(self) -> float:
        return self.capital * self.weekly_dd_limit_pct
