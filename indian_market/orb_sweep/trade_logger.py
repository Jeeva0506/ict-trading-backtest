"""SQLite trade database for ORB Sweep strategy."""

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np

from orb_sweep.config import TradingConfig

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,
    entry_time      TEXT NOT NULL,
    exit_time       TEXT,
    direction       TEXT NOT NULL,
    instrument      TEXT NOT NULL,
    strike          INTEGER NOT NULL,
    spot_entry      REAL NOT NULL,
    spot_exit       REAL,
    entry_premium   REAL,
    exit_premium    REAL,
    quantity        INTEGER NOT NULL,
    lots            INTEGER NOT NULL,
    gross_pnl       REAL DEFAULT 0,
    costs           REAL DEFAULT 0,
    net_pnl         REAL DEFAULT 0,
    stop_loss       REAL NOT NULL,
    tp1_price       REAL NOT NULL,
    tp2_price       REAL NOT NULL,
    tp1_hit         INTEGER DEFAULT 0,
    exit_type       TEXT,
    orb_high        REAL,
    orb_low         REAL,
    vwap_at_entry   REAL,
    sweep_price     REAL,
    r_value         REAL,
    r_multiple      REAL
);

CREATE TABLE IF NOT EXISTS daily_summary (
    trade_date      TEXT PRIMARY KEY,
    n_trades        INTEGER DEFAULT 0,
    n_wins          INTEGER DEFAULT 0,
    n_losses        INTEGER DEFAULT 0,
    gross_pnl       REAL DEFAULT 0,
    net_pnl         REAL DEFAULT 0,
    max_drawdown    REAL DEFAULT 0,
    regime_pass     INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS weekly_summary (
    week_start      TEXT PRIMARY KEY,
    n_trades        INTEGER DEFAULT 0,
    n_wins          INTEGER DEFAULT 0,
    n_losses        INTEGER DEFAULT 0,
    net_pnl         REAL DEFAULT 0,
    max_drawdown    REAL DEFAULT 0
);
"""


class TradeLogger:
    """SQLite-backed trade journal."""

    def __init__(self, config: TradingConfig):
        self.cfg = config
        db_path = Path(config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        self._conn.close()

    # ── Trade CRUD ─────────────────────────────────────────────────────────
    def insert_trade(self, trade: dict) -> int:
        cols = [
            "trade_date", "entry_time", "direction", "instrument", "strike",
            "spot_entry", "entry_premium", "quantity", "lots",
            "stop_loss", "tp1_price", "tp2_price",
            "orb_high", "orb_low", "vwap_at_entry", "sweep_price", "r_value",
        ]
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(cols)
        vals = [trade.get(c) for c in cols]
        cur = self._conn.execute(
            f"INSERT INTO trades ({col_names}) VALUES ({placeholders})", vals
        )
        self._conn.commit()
        logger.info(f"Trade #{cur.lastrowid} inserted: {trade.get('direction')} "
                     f"@ {trade.get('spot_entry')}")
        return cur.lastrowid

    def update_trade_exit(self, trade_id: int, exit_data: dict):
        sets = ", ".join(f"{k} = ?" for k in exit_data)
        vals = list(exit_data.values()) + [trade_id]
        self._conn.execute(f"UPDATE trades SET {sets} WHERE id = ?", vals)
        self._conn.commit()

    def update_tp1_hit(self, trade_id: int):
        self._conn.execute("UPDATE trades SET tp1_hit = 1 WHERE id = ?", (trade_id,))
        self._conn.commit()

    def get_trade(self, trade_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_trades_for_date(self, trade_date: str) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE trade_date = ? ORDER BY entry_time",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_trades(self) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY entry_time"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Daily Summary ──────────────────────────────────────────────────────
    def update_daily_summary(self, trade_date: str, regime_pass: bool = True):
        trades = self.get_trades_for_date(trade_date)
        completed = [t for t in trades if t["exit_time"] is not None]
        n_trades = len(completed)
        n_wins = sum(1 for t in completed if (t.get("net_pnl") or 0) > 0)
        n_losses = n_trades - n_wins
        gross_pnl = sum(t.get("gross_pnl") or 0 for t in completed)
        net_pnl = sum(t.get("net_pnl") or 0 for t in completed)

        self._conn.execute("""
            INSERT OR REPLACE INTO daily_summary
            (trade_date, n_trades, n_wins, n_losses, gross_pnl, net_pnl, regime_pass)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (trade_date, n_trades, n_wins, n_losses,
              round(gross_pnl, 2), round(net_pnl, 2), int(regime_pass)))
        self._conn.commit()

    def update_weekly_summary(self, week_start: str):
        week_start_dt = datetime.strptime(week_start, "%Y-%m-%d").date()
        week_end_dt = week_start_dt + timedelta(days=4)
        rows = self._conn.execute("""
            SELECT * FROM daily_summary
            WHERE trade_date >= ? AND trade_date <= ?
        """, (week_start, week_end_dt.isoformat())).fetchall()

        n_trades = sum(r["n_trades"] for r in rows)
        n_wins = sum(r["n_wins"] for r in rows)
        n_losses = sum(r["n_losses"] for r in rows)
        net_pnl = sum(r["net_pnl"] for r in rows)

        self._conn.execute("""
            INSERT OR REPLACE INTO weekly_summary
            (week_start, n_trades, n_wins, n_losses, net_pnl)
            VALUES (?, ?, ?, ?, ?)
        """, (week_start, n_trades, n_wins, n_losses, round(net_pnl, 2)))
        self._conn.commit()

    # ── Performance Metrics ────────────────────────────────────────────────
    def compute_metrics(self) -> dict:
        trades = self.get_all_trades()
        completed = [t for t in trades if t["exit_time"] is not None]
        if not completed:
            return {"error": "No completed trades"}

        pnls = np.array([t["net_pnl"] for t in completed])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]

        n_total = len(pnls)
        n_wins = len(wins)
        win_rate = n_wins / n_total if n_total else 0

        gross_profit = wins.sum() if len(wins) else 0
        gross_loss = abs(losses.sum()) if len(losses) else 1e-9
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        avg_win = float(wins.mean()) if len(wins) else 0
        avg_loss = float(losses.mean()) if len(losses) else 0
        expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

        equity = self.cfg.capital + np.cumsum(pnls)
        peak = np.maximum.accumulate(equity)
        dd_pct = ((equity - peak) / peak * 100)
        max_dd = float(dd_pct.min())

        entry_dates = [datetime.fromisoformat(t["entry_time"]) for t in completed]
        if len(entry_dates) >= 2:
            years = (max(entry_dates) - min(entry_dates)).days / 365.25
        else:
            years = 0.01
        total_return = float(pnls.sum()) / self.cfg.capital
        cagr = ((1 + total_return) ** (1 / max(years, 0.01)) - 1) * 100

        rfr_daily = 0.065 / 252
        holding_days = []
        for t in completed:
            entry = datetime.fromisoformat(t["entry_time"])
            exit_ = datetime.fromisoformat(t["exit_time"])
            holding_days.append(max((exit_ - entry).total_seconds() / 86400, 0.01))
        holding_arr = np.array(holding_days)
        daily_ret = pnls / (holding_arr * self.cfg.capital)
        sharpe = (
            float((daily_ret.mean() - rfr_daily) / daily_ret.std() * np.sqrt(252))
            if daily_ret.std() > 0 else 0
        )

        r_multiples = [t.get("r_multiple", 0) or 0 for t in completed]

        return {
            "n_trades": n_total,
            "n_wins": n_wins,
            "n_losses": n_total - n_wins,
            "win_rate_pct": round(win_rate * 100, 2),
            "profit_factor": round(profit_factor, 3),
            "net_pnl": round(float(pnls.sum()), 2),
            "gross_profit": round(float(gross_profit), 2),
            "gross_loss": round(float(gross_loss), 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "cagr_pct": round(cagr, 2),
            "sharpe": round(sharpe, 3),
            "total_return_pct": round(total_return * 100, 2),
            "avg_r_multiple": round(float(np.mean(r_multiples)), 2),
            "years": round(years, 2),
        }

    def print_report(self):
        m = self.compute_metrics()
        if "error" in m:
            print(f"  {m['error']}")
            return
        sep = "-" * 50
        print(f"\n{'=' * 50}")
        print("  ORB Sweep — Performance Report")
        print(f"{'=' * 50}")
        print(f"  Period        : {m['years']:.1f} years")
        print(f"  Trades        : {m['n_trades']} ({m['n_wins']}W / {m['n_losses']}L)")
        print(sep)
        print(f"  Win Rate      : {m['win_rate_pct']:.1f}%")
        print(f"  Profit Factor : {m['profit_factor']:.3f}")
        print(f"  Expectancy    : Rs{m['expectancy']:>10,.2f}")
        print(sep)
        print(f"  Net P&L       : Rs{m['net_pnl']:>12,.2f}")
        print(f"  CAGR          : {m['cagr_pct']:.1f}%")
        print(f"  Sharpe        : {m['sharpe']:.3f}")
        print(f"  Max Drawdown  : {m['max_drawdown_pct']:.1f}%")
        print(f"  Avg R-Multiple: {m['avg_r_multiple']:.2f}R")
        print(f"{'=' * 50}\n")
