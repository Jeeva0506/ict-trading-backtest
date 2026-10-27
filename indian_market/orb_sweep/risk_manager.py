"""Risk management for ORB Sweep strategy."""

import logging
from datetime import date, timedelta
from typing import Optional

from orb_sweep.config import TradingConfig

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Enforces:
      - Max 2 trades per day
      - Daily drawdown limit (1% of capital)
      - Weekly drawdown limit (3% of capital)
    """

    def __init__(self, config: TradingConfig):
        self.cfg = config
        self._daily_trades: int = 0
        self._daily_pnl: float = 0.0
        self._weekly_pnl: float = 0.0
        self._current_date: Optional[date] = None
        self._week_start: Optional[date] = None

    def reset_day(self, today: date):
        if self._current_date != today:
            self._daily_trades = 0
            self._daily_pnl = 0.0
            self._current_date = today

            week_start = today - timedelta(days=today.weekday())
            if self._week_start != week_start:
                self._weekly_pnl = 0.0
                self._week_start = week_start

    def can_trade(self) -> bool:
        if self._daily_trades >= self.cfg.max_trades_per_day:
            logger.info(
                f"Trade blocked: max {self.cfg.max_trades_per_day} trades/day reached"
            )
            return False

        if self._daily_pnl <= -self.cfg.daily_dd_limit:
            logger.info(
                f"Trade blocked: daily DD limit hit "
                f"({self._daily_pnl:.0f} <= -{self.cfg.daily_dd_limit:.0f})"
            )
            return False

        if self._weekly_pnl <= -self.cfg.weekly_dd_limit:
            logger.info(
                f"Trade blocked: weekly DD limit hit "
                f"({self._weekly_pnl:.0f} <= -{self.cfg.weekly_dd_limit:.0f})"
            )
            return False

        return True

    def record_trade(self, net_pnl: float):
        self._daily_trades += 1
        self._daily_pnl += net_pnl
        self._weekly_pnl += net_pnl
        logger.info(
            f"Trade recorded: PnL={net_pnl:+.0f} | "
            f"Daily: {self._daily_trades} trades, {self._daily_pnl:+.0f} | "
            f"Weekly: {self._weekly_pnl:+.0f}"
        )

    def increment_trade_count(self):
        self._daily_trades += 1

    def should_force_exit(self) -> bool:
        """Check if daily drawdown limit is breached mid-trade."""
        return self._daily_pnl <= -self.cfg.daily_dd_limit

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def weekly_pnl(self) -> float:
        return self._weekly_pnl

    def compute_lots(self, r_value_per_unit: float) -> int:
        """
        Calculate number of lots based on risk per trade.
        risk_per_trade = lots * lot_size * R_value
        lots = risk_per_trade / (lot_size * R_value)
        """
        if r_value_per_unit <= 0:
            return 0
        risk_amount = self.cfg.risk_per_trade
        max_lots = int(risk_amount / (self.cfg.lot_size * r_value_per_unit))
        return max(1, max_lots)
