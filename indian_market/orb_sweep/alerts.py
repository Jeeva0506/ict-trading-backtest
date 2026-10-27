"""Telegram alerts for ORB Sweep strategy."""

import logging
from datetime import datetime, timezone, timedelta

import requests

from orb_sweep.config import TradingConfig, PROJECT_ROOT

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> str:
    return datetime.now(IST).strftime("%H:%M:%S")


class TelegramAlerts:
    """Send trading notifications via Telegram."""

    def __init__(self, config: TradingConfig):
        self.cfg = config
        env = self._load_env()
        self.bot_token = env.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = env.get("TELEGRAM_CHAT_ID", "")
        self._url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        self.enabled = bool(
            self.bot_token and self.chat_id
            and self.bot_token != "your_token_here"
            and self.chat_id != "your_chat_id_here"
        )
        if not self.enabled:
            logger.warning("Telegram disabled — set TELEGRAM_BOT_TOKEN / CHAT_ID")

    @staticmethod
    def _load_env() -> dict:
        env = {}
        env_file = PROJECT_ROOT / ".env.indian"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        return env

    def _send(self, text: str) -> bool:
        if not self.enabled:
            logger.info(f"[TG off] {text[:100]}")
            return False
        try:
            r = requests.post(self._url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=10)
            if not r.ok:
                logger.warning(f"Telegram HTTP {r.status_code}: {r.text[:100]}")
                # HTML parse errors (e.g. stray < > &) silently drop alerts.
                # Retry once as plain text so the notification still lands.
                if r.status_code == 400:
                    r = requests.post(self._url, json={
                        "chat_id": self.chat_id,
                        "text": text,
                    }, timeout=10)
                    if not r.ok:
                        logger.warning(
                            f"Telegram plain-text retry HTTP {r.status_code}: {r.text[:100]}"
                        )
            return r.ok
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    # ── Trade Alerts ───────────────────────────────────────────────────────
    def send_entry(self, direction: str, strike: int, entry_spot: float,
                   stop_loss: float, tp1: float, tp2: float,
                   lots: int, quantity: int, r_value: float):
        opt_type = "CE" if direction == "CALL" else "PE"
        mode = "PAPER" if self.cfg.paper_mode else "LIVE"
        text = (
            f"<b>ORB SWEEP ENTRY [{mode}]</b>\n"
            f"Direction: {direction}\n"
            f"Strike: NIFTY {strike} {opt_type}\n"
            f"Spot Entry: {entry_spot:.2f}\n"
            f"SL: {stop_loss:.2f} | TP1: {tp1:.2f} | TP2: {tp2:.2f}\n"
            f"R-Value: {r_value:.2f} pts\n"
            f"Lots: {lots} ({quantity} units)\n"
            f"Time: {_ist_now()}"
        )
        self._send(text)

    def send_tp1_hit(self, direction: str, spot: float, pnl: float):
        text = (
            f"<b>TP1 HIT — 50% EXIT</b>\n"
            f"Direction: {direction}\n"
            f"Spot: {spot:.2f}\n"
            f"Partial P&L: Rs{pnl:+,.0f}\n"
            f"SL moved to breakeven\n"
            f"Time: {_ist_now()}"
        )
        self._send(text)

    def send_exit(self, direction: str, exit_type: str, spot: float,
                  net_pnl: float, r_multiple: float):
        text = (
            f"<b>TRADE EXIT — {exit_type}</b>\n"
            f"Direction: {direction}\n"
            f"Spot: {spot:.2f}\n"
            f"Net P&L: Rs{net_pnl:+,.0f}\n"
            f"R-Multiple: {r_multiple:+.2f}R\n"
            f"Time: {_ist_now()}"
        )
        self._send(text)

    def send_regime_fail(self, range_pct: float, atr_val: float):
        text = (
            f"<b>REGIME FILTER — NO TRADES TODAY</b>\n"
            f"15m Range: {range_pct*100:.3f}% (min {self.cfg.min_range_pct*100:.2f}%)\n"
            f"ATR(14) 5m: {atr_val:.1f} (min {self.cfg.min_atr_5m})\n"
            f"Time: {_ist_now()}"
        )
        self._send(text)

    def send_daily_summary(self, n_trades: int, net_pnl: float,
                           daily_dd: float, weekly_dd: float,
                           win_rate: float = 0):
        emoji = "+" if net_pnl >= 0 else ""
        text = (
            f"<b>DAILY SUMMARY — ORB SWEEP</b>\n"
            f"Trades: {n_trades}\n"
            f"Net P&L: Rs{net_pnl:+,.0f}\n"
            f"Daily DD: {daily_dd:+.2f}%\n"
            f"Weekly DD: {weekly_dd:+.2f}%\n"
            f"Time: {_ist_now()}"
        )
        self._send(text)

    def send_error(self, msg: str):
        text = f"<b>ERROR — ORB SWEEP</b>\n{msg}\nTime: {_ist_now()}"
        self._send(text)

    def send_startup(self):
        mode = "PAPER" if self.cfg.paper_mode else "LIVE"
        text = (
            f"<b>ORB SWEEP STARTED [{mode}]</b>\n"
            f"Capital: Rs{self.cfg.capital:,.0f}\n"
            f"Risk/Trade: {self.cfg.risk_per_trade_pct*100:.1f}%\n"
            f"Max Trades/Day: {self.cfg.max_trades_per_day}\n"
            f"Time: {_ist_now()}"
        )
        self._send(text)
