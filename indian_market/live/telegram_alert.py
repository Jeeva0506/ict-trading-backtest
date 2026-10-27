"""
Telegram alerts for Indian options paper trader.
Sends entry/exit/daily/weekly/error messages.
"""
import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> datetime:
    return datetime.now(IST)


def _ist_str(fmt: str = "%H:%M:%S") -> str:
    return _ist_now().strftime(fmt)


class IndianTelegram:
    """Send paper-trading notifications via Telegram."""

    def __init__(self, bot_token: str, chat_id: str, label: str = "NIFTY-PAPER"):
        self.bot_token = bot_token
        self.chat_id   = chat_id
        self.label     = label
        self._url      = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.enabled   = bool(bot_token and chat_id and
                              bot_token != "your_token_here" and
                              chat_id   != "your_chat_id_here")
        if not self.enabled:
            logger.warning("Telegram disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    def _send(self, text: str) -> bool:
        if not self.enabled:
            logger.info(f"[Telegram disabled] {text[:80]}")
            return False
        try:
            r = requests.post(self._url, json={
                "chat_id":    self.chat_id,
                "text":       text,
                "parse_mode": "HTML",
            }, timeout=10)
            if not r.ok:
                logger.warning(f"Telegram HTTP {r.status_code}: {r.text[:100]}")
            return r.ok
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    # ── Trade alerts ───────────────────────────────────────────────────────────
    def send_entry(self, trade: dict):
        q  = trade.get("quality", "regular")
        em = "STAR" if q == "A+" else ""
        inst = trade.get("instrument", "")
        dir_ = trade.get("direction", "").upper()
        ep   = trade.get("entry_prem", 0)
        K    = trade.get("strike", 0)
        lots = trade.get("lots", 0)
        risk = trade.get("option_sl", 0) * trade.get("qty", 0)
        vix  = trade.get("vix", 0)
        orb_rng = round(trade.get("orb_h", 0) - trade.get("orb_l", 0), 0)
        text = (
            f"[{self.label}] PAPER ENTRY {em}\n"
            f"Time  : {_ist_str()}\n"
            f"Instr : {inst}  {dir_}  ({q})\n"
            f"Strike: {K}  DTE={trade.get('dte', 0)}\n"
            f"Prem  : Rs{ep:.2f}  Lots={lots}\n"
            f"SL    : Rs{trade.get('option_sl', 0):.2f}/unit | Risk=Rs{risk:,.0f}\n"
            f"VIX   : {vix:.1f}  ORB range={orb_rng:.0f}pts\n"
            f"Retest: spot={trade.get('entry_spot', 0):.0f}"
        )
        self._send(text)

    def send_exit(self, trade_id: int, exit_data: dict, instrument: str, direction: str):
        reason = exit_data.get("exit_reason", "?")
        net    = exit_data.get("net_rs", 0)
        em     = "WIN" if net > 0 else "LOSS"
        text = (
            f"[{self.label}] PAPER EXIT #{trade_id} {em}\n"
            f"Time  : {_ist_str()}\n"
            f"Instr : {instrument}  {direction.upper()}\n"
            f"Prem  : Rs{exit_data.get('exit_prem', 0):.2f}  "
            f"Peak=Rs{exit_data.get('peak_prem', 0):.2f}\n"
            f"Reason: {reason}\n"
            f"Net   : Rs{net:+,.0f}\n"
            f"MFE   : Rs{exit_data.get('mfe_rs', 0):+,.0f}  "
            f"MAE=Rs{exit_data.get('mae_rs', 0):+,.0f}"
        )
        self._send(text)

    def send_signal_skip(self, reason: str, instrument: str, direction: str):
        text = (
            f"[{self.label}] Signal SKIPPED\n"
            f"Time  : {_ist_str()}\n"
            f"Instr : {instrument}  {direction.upper()}\n"
            f"Reason: {reason}"
        )
        self._send(text)

    # ── Daily report ───────────────────────────────────────────────────────────
    def send_daily_report(self, stats: dict, capital: float, cum_dd_pct: float):
        date   = stats.get("date", _ist_now().strftime("%Y-%m-%d"))
        n      = stats.get("trades", 0)
        wins   = stats.get("wins", 0)
        net    = stats.get("net_rs", 0)
        em     = "UP" if net >= 0 else "DOWN"
        wr     = stats.get("win_rate", 0)
        ret    = net / capital * 100 if capital else 0
        text = (
            f"[{self.label}] DAILY REPORT {em} — {date}\n"
            f"Trades: {n}  W={wins} L={n-wins}  WR={wr:.0f}%\n"
            f"Net P&L : Rs{net:+,.0f} ({ret:+.2f}%)\n"
            f"Best    : Rs{stats.get('best_rs', 0):+,.0f}\n"
            f"Worst   : Rs{stats.get('worst_rs', 0):+,.0f}\n"
            f"Capital : Rs{capital:,.0f}\n"
            f"Cum DD  : {cum_dd_pct:.2f}%"
        )
        self._send(text)

    # ── Weekly report ──────────────────────────────────────────────────────────
    def send_weekly_report(self, week_stats: dict, capital: float):
        net  = week_stats.get("net_rs", 0)
        em   = "UP" if net >= 0 else "DOWN"
        text = (
            f"[{self.label}] WEEKLY REPORT {em}\n"
            f"Week    : {week_stats.get('week_start','')} - {week_stats.get('week_end','')}\n"
            f"Trades  : {week_stats.get('trades', 0)}\n"
            f"WR      : {week_stats.get('win_rate', 0):.0f}%\n"
            f"Net P&L : Rs{net:+,.0f} ({net/capital*100:+.2f}%)\n"
            f"Capital : Rs{capital:,.0f}"
        )
        self._send(text)

    # ── Error / info ───────────────────────────────────────────────────────────
    def send_error(self, msg: str):
        self._send(f"[{self.label}] ERROR\n{_ist_str()}\n{msg}")

    def send_info(self, msg: str):
        self._send(f"[{self.label}] {_ist_str()}\n{msg}")
