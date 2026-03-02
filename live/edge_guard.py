"""
Edge Guard — Live Trading Edge Monitor
=======================================
Tracks rolling performance metrics and enforces hard risk rules
to auto-disable trading when edge degrades.

Metrics tracked (Sections 2, 3, 4, 5 of audit):
  - Rolling win_rate, profit_factor, expectancy (last N trades)
  - Average R-multiple, trades_per_week
  - Loss streak (current + max)
  - Equity curve with peak tracking
  - Missed trade log
  - Slippage baseline + anomaly detection

Hard stop rules (auto-disable new trades):
  - PF < 1.2 over last 50 trades
  - Win rate < 48% over last 50 trades
  - Avg slippage > 2× baseline (last 10 vs first 20)
  - Drawdown > 8% from initial balance (soft stop before FN's 10%)

Manual override via Telegram /start to resume after auto-stop.
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("EdgeGuard")


class EdgeGuard:
    """Rolling metrics tracker + hard risk rule enforcer."""

    # ── Configuration ──────────────────────────────────────────────────
    ROLLING_WINDOW = 50          # last N trades for rolling metrics
    MIN_TRADES_FOR_RULES = 20    # don't enforce rules until this many trades

    # Hard stop thresholds
    MIN_PROFIT_FACTOR = 1.2
    MIN_WIN_RATE_PCT = 48.0
    MAX_SLIPPAGE_MULT = 2.0      # 2× baseline
    MAX_DRAWDOWN_SOFT_PCT = 8.0   # soft stop at 8% (before FN's 10% hard limit)

    def __init__(self, data_dir: Path = None, initial_balance: float = 6000.0):
        if data_dir is None:
            data_dir = Path(__file__).parent / "logs"
        data_dir.mkdir(parents=True, exist_ok=True)

        self.data_file = data_dir / "edge_guard.json"
        self.trades: List[Dict] = []
        self.equity_curve: List[float] = []
        self.missed_trades: List[Dict] = []
        self.initial_balance: float = initial_balance

        # Streak tracking
        self.loss_streak: int = 0
        self.max_loss_streak: int = 0
        self.win_streak: int = 0

        # Slippage baseline (calibrated from first 20 trades)
        self.baseline_slippage: Optional[float] = None

        # Edge degradation state
        self.is_edge_degraded: bool = False
        self.degradation_reasons: List[str] = []
        self.degraded_since: Optional[str] = None

        self._load()

    # ── Record a completed trade ───────────────────────────────────────
    def record_trade(self, trade_data: Dict):
        """
        Record a completed trade for rolling metrics.

        Required keys: pnl, symbol, direction
        Optional keys: date, actual_rr, slippage_pts, spread_entry,
                       balance_after, session, weekday
        """
        record = {
            'date': trade_data.get('date', datetime.now().strftime("%Y-%m-%d")),
            'time': trade_data.get('time', datetime.now().strftime("%H:%M:%S")),
            'symbol': trade_data.get('symbol', ''),
            'direction': trade_data.get('direction', ''),
            'pnl': float(trade_data.get('pnl', 0)),
            'actual_rr': float(trade_data.get('actual_rr', 0)),
            'slippage_pts': float(trade_data.get('slippage_pts', 0)),
            'spread_entry': float(trade_data.get('spread_entry', 0)),
            'balance_after': float(trade_data.get('balance_after', 0)),
            'session': trade_data.get('session', ''),
            'weekday': trade_data.get('weekday', ''),
        }
        self.trades.append(record)

        # Update streaks
        if record['pnl'] < 0:
            self.loss_streak += 1
            self.win_streak = 0
            self.max_loss_streak = max(self.max_loss_streak, self.loss_streak)
        elif record['pnl'] > 0:
            self.win_streak += 1
            self.loss_streak = 0
        else:  # pnl == 0 (breakeven) — reset both streaks
            self.loss_streak = 0
            self.win_streak = 0

        # Update equity curve
        if record['balance_after'] > 0:
            self.equity_curve.append(record['balance_after'])
        else:
            logger.warning("EdgeGuard: balance_after not provided — equity tracking disabled for this trade")

        # Calibrate slippage baseline from first 20 trades
        if len(self.trades) >= self.MIN_TRADES_FOR_RULES and self.baseline_slippage is None:
            slips = [abs(t['slippage_pts']) for t in self.trades[:20]]
            self.baseline_slippage = sum(slips) / len(slips) if slips else 0
            logger.info(f"EdgeGuard: Slippage baseline set to {self.baseline_slippage:.2f} pts "
                        f"(from first {len(slips)} trades)")

        # Evaluate edge after every trade
        self._evaluate_edge()
        self._save()

        # Log rolling metrics
        metrics = self.get_rolling_metrics()
        logger.info(f"EdgeGuard: Trade #{len(self.trades)} | "
                     f"Rolling({metrics.get('window', 0)}): "
                     f"WR={metrics.get('win_rate', 0):.1f}% "
                     f"PF={metrics.get('profit_factor', 0):.2f} "
                     f"Exp=${metrics.get('expectancy', 0):.2f} "
                     f"LossStrk={self.loss_streak} "
                     f"MaxLossStrk={self.max_loss_streak}")

    # ── Record a missed trade ──────────────────────────────────────────
    def record_missed_trade(self, symbol: str, reason: str,
                            signal_price: float = 0):
        """Log a trade signal that was generated but not executed."""
        record = {
            'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'symbol': symbol,
            'reason': reason,
            'signal_price': signal_price,
        }
        self.missed_trades.append(record)
        # Keep only last 200 missed trades
        if len(self.missed_trades) > 200:
            self.missed_trades = self.missed_trades[-200:]
        logger.info(f"EdgeGuard: Missed trade — {symbol} ({reason})")
        self._save()

    # ── Rolling metrics ────────────────────────────────────────────────
    def get_rolling_metrics(self, n: int = None) -> Dict:
        """
        Calculate performance metrics over last n trades.

        Returns dict with: window, win_rate, profit_factor, expectancy,
        avg_r, avg_win, avg_loss, trades_per_week, avg_slippage,
        loss_streak, max_loss_streak, total_trades, missed_trades
        """
        if n is None:
            n = self.ROLLING_WINDOW

        recent = self.trades[-n:] if len(self.trades) >= n else self.trades
        if not recent:
            return {
                'window': 0, 'win_rate': 0, 'profit_factor': 0,
                'expectancy': 0, 'avg_r': 0, 'avg_win': 0, 'avg_loss': 0,
                'trades_per_week': 0, 'avg_slippage': 0,
                'loss_streak': self.loss_streak,
                'max_loss_streak': self.max_loss_streak,
                'total_trades': 0, 'missed_trades': len(self.missed_trades),
            }

        wins = [t for t in recent if t['pnl'] > 0]
        losses = [t for t in recent if t['pnl'] < 0]

        win_count = len(wins)
        loss_count = len(losses)
        total = len(recent)

        win_rate = (win_count / total) * 100 if total > 0 else 0

        gross_wins = sum(t['pnl'] for t in wins)
        gross_losses = abs(sum(t['pnl'] for t in losses))
        profit_factor = (gross_wins / gross_losses
                         if gross_losses > 0
                         else (999.0 if gross_wins > 0 else 0))

        avg_win = gross_wins / win_count if win_count > 0 else 0
        avg_loss = gross_losses / loss_count if loss_count > 0 else 0

        # Expectancy = (WR × avg_win) - (LR × avg_loss)
        wr_dec = win_rate / 100
        expectancy = (wr_dec * avg_win) - ((1 - wr_dec) * avg_loss)

        # Average R-multiple
        r_vals = [t['actual_rr'] for t in recent if t.get('actual_rr')]
        avg_r = sum(r_vals) / len(r_vals) if r_vals else 0

        # Trades per week
        trades_per_week = 0.0
        dates = sorted(set(t.get('date', '') for t in recent if t.get('date')))
        if len(dates) >= 2:
            try:
                first = datetime.strptime(dates[0], "%Y-%m-%d")
                last = datetime.strptime(dates[-1], "%Y-%m-%d")
                weeks = max(1, (last - first).days / 7)
                trades_per_week = total / weeks
            except Exception:
                trades_per_week = float(total)
        else:
            trades_per_week = float(total)

        # Avg slippage
        slips = [abs(t.get('slippage_pts', 0)) for t in recent]
        avg_slippage = sum(slips) / len(slips) if slips else 0

        return {
            'window': total,
            'win_rate': round(win_rate, 1),
            'profit_factor': round(profit_factor, 2),
            'expectancy': round(expectancy, 2),
            'avg_r': round(avg_r, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'trades_per_week': round(trades_per_week, 1),
            'avg_slippage': round(avg_slippage, 2),
            'loss_streak': self.loss_streak,
            'max_loss_streak': self.max_loss_streak,
            'total_trades': len(self.trades),
            'missed_trades': len(self.missed_trades),
        }

    # ── Equity curve analysis ──────────────────────────────────────────
    def get_equity_stats(self) -> Dict:
        """Return equity curve statistics."""
        if not self.equity_curve:
            return {'peak': 0, 'current': 0, 'dd_from_peak': 0,
                    'datapoints': 0}
        peak = max(self.equity_curve)
        current = self.equity_curve[-1]
        dd_from_peak = ((peak - current) / peak * 100) if peak > 0 else 0
        return {
            'peak': round(peak, 2),
            'current': round(current, 2),
            'dd_from_peak': round(dd_from_peak, 2),
            'datapoints': len(self.equity_curve),
        }

    # ── Edge evaluation (hard rules) ──────────────────────────────────
    def _evaluate_edge(self):
        """Check hard stop rules. Sets is_edge_degraded flag."""
        if len(self.trades) < self.MIN_TRADES_FOR_RULES:
            self.is_edge_degraded = False
            self.degradation_reasons = []
            return

        metrics = self.get_rolling_metrics()
        reasons = []

        # Rule 1: Profit factor < 1.2
        if metrics['profit_factor'] < self.MIN_PROFIT_FACTOR:
            reasons.append(
                f"PF={metrics['profit_factor']:.2f} < {self.MIN_PROFIT_FACTOR}")

        # Rule 2: Win rate < 48%
        if metrics['win_rate'] < self.MIN_WIN_RATE_PCT:
            reasons.append(
                f"WR={metrics['win_rate']:.1f}% < {self.MIN_WIN_RATE_PCT}%")

        # Rule 3: Slippage anomaly (last 10 trades vs baseline)
        if self.baseline_slippage and self.baseline_slippage > 0:
            recent_slips = [abs(t.get('slippage_pts', 0))
                            for t in self.trades[-10:]]
            avg_recent = (sum(recent_slips) / len(recent_slips)
                          if recent_slips else 0)
            if avg_recent > self.baseline_slippage * self.MAX_SLIPPAGE_MULT:
                reasons.append(
                    f"Slippage={avg_recent:.2f} > "
                    f"{self.MAX_SLIPPAGE_MULT}x baseline "
                    f"({self.baseline_slippage:.2f})")

        # Rule 4: Drawdown > 8% from initial balance (soft stop before FN 10%)
        if self.equity_curve:
            current_equity = self.equity_curve[-1]
            dd_from_initial = ((self.initial_balance - current_equity)
                               / self.initial_balance * 100) if self.initial_balance > 0 else 0
            if dd_from_initial >= self.MAX_DRAWDOWN_SOFT_PCT:
                reasons.append(
                    f"DD={dd_from_initial:.1f}% from initial "
                    f"(${self.initial_balance:,.0f}) >= {self.MAX_DRAWDOWN_SOFT_PCT}%")

        was_degraded = self.is_edge_degraded
        self.is_edge_degraded = len(reasons) > 0
        self.degradation_reasons = reasons

        if self.is_edge_degraded and not was_degraded:
            self.degraded_since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.warning(
                f"EdgeGuard: EDGE DEGRADED — {'; '.join(reasons)}")
        elif not self.is_edge_degraded and was_degraded:
            logger.info("EdgeGuard: Edge recovered — all rules passed")
            self.degraded_since = None

    # ── Can trade? (called by LiveTrader._can_trade) ──────────────────
    def can_trade(self) -> Tuple[bool, str]:
        """Check if edge is acceptable for trading."""
        if self.is_edge_degraded:
            return False, (
                f"Edge degraded: {'; '.join(self.degradation_reasons)}")
        return True, "ok"

    # ── Manual override ────────────────────────────────────────────────
    def force_override(self):
        """Manual override to re-enable trading after edge degradation."""
        logger.info("EdgeGuard: Manual override — edge degradation cleared")
        self.is_edge_degraded = False
        self.degradation_reasons = []
        self.degraded_since = None
        self._save()

    # ── Persistence ────────────────────────────────────────────────────
    def _save(self):
        try:
            data = {
                'trades': self.trades[-500:],       # keep last 500 max
                'equity_curve': self.equity_curve[-500:],
                'missed_trades': self.missed_trades[-200:],
                'loss_streak': self.loss_streak,
                'max_loss_streak': self.max_loss_streak,
                'win_streak': self.win_streak,
                'baseline_slippage': self.baseline_slippage,
                'initial_balance': self.initial_balance,
                'is_edge_degraded': self.is_edge_degraded,
                'degradation_reasons': self.degradation_reasons,
                'degraded_since': self.degraded_since,
                'last_updated': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            tmp = self.data_file.with_suffix('.tmp')
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)

            # Retry rename — OneDrive can lock the target file during sync
            for attempt in range(5):
                try:
                    tmp.replace(self.data_file)
                    return  # success
                except (PermissionError, OSError):
                    if attempt < 4:
                        time.sleep(0.3 * (attempt + 1))
            # All retries failed — fall back to direct overwrite
            try:
                if self.data_file.exists():
                    os.remove(str(self.data_file))
                    time.sleep(0.2)
                tmp.replace(self.data_file)
            except Exception:
                # Last resort: write directly (non-atomic but better than losing data)
                with open(self.data_file, 'w') as f:
                    json.dump(data, f, indent=2)
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"EdgeGuard: Failed to save: {e}")

    def _load(self):
        if not self.data_file.exists():
            return
        try:
            with open(self.data_file) as f:
                data = json.load(f)
            self.trades = data.get('trades', [])
            self.equity_curve = data.get('equity_curve', [])
            self.missed_trades = data.get('missed_trades', [])
            self.loss_streak = data.get('loss_streak', 0)
            self.max_loss_streak = data.get('max_loss_streak', 0)
            self.win_streak = data.get('win_streak', 0)
            self.baseline_slippage = data.get('baseline_slippage')
            self.initial_balance = data.get('initial_balance', self.initial_balance)
            self.is_edge_degraded = data.get('is_edge_degraded', False)
            self.degradation_reasons = data.get('degradation_reasons', [])
            self.degraded_since = data.get('degraded_since')
            logger.info(f"EdgeGuard: Loaded {len(self.trades)} trades, "
                        f"edge_degraded={self.is_edge_degraded}, "
                        f"loss_streak={self.loss_streak}")
        except Exception as e:
            logger.error(f"EdgeGuard: Failed to load: {e}")
