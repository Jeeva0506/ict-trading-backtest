"""
Telegram Command Handler — Indian Options Paper Trader
=======================================================
Commands (all Indian-specific, paper trading context):

  /status      — Bot status, open positions, running capital, monthly DD
  /position    — Current open paper position(s) with live option value
  /capital     — Running capital, return %, monthly P&L
  /trades      — Today's completed paper trades
  /risk        — Monthly DD circuit breaker status
  /orb         — Today's ORB levels for NIFTY and FINNIFTY
  /pause       — Pause new entries (keeps tracking open positions)
  /resume      — Resume new entries
  /report      — Force today's daily report
  /help        — Show all commands
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

import requests

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> datetime:
    return datetime.now(IST)


def _ist_str(fmt: str = "%Y-%m-%d %H:%M:%S IST") -> str:
    return _ist_now().strftime(fmt)


def _bar(current: float, limit: float, length: int = 10) -> str:
    """Text progress bar for DD display."""
    if limit <= 0:
        return ""
    ratio = min(abs(current) / abs(limit), 1.0)
    filled = int(ratio * length)
    empty  = length - filled
    char   = "X" if ratio >= 0.8 else ("!" if ratio >= 0.5 else "=")
    return f"[{'#' * filled}{'-' * empty}] {ratio*100:.0f}%"


class IndianCommandHandler:
    """
    Long-polling Telegram command handler for the Indian paper trader.
    Runs in a background daemon thread so it does not block the main loop.
    """

    def __init__(self, bot_token: str, chat_id: str, label: str = "NIFTY-PAPER"):
        self.bot_token = bot_token
        self.chat_id   = chat_id
        self.label     = label
        self._url      = f"https://api.telegram.org/bot{bot_token}"
        self.enabled   = bool(bot_token and chat_id and
                              bot_token != "your_token_here")

        self.running          = False
        self._thread: Optional[threading.Thread] = None
        self._last_update_id  = 0
        self._paused          = False
        self._started_at      = _ist_str()

        # State file (survives restarts)
        self._state_file = Path(__file__).parent / "state" / "cmd_state.json"
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_state()

        # Callbacks — set by paper_trader after construction
        self.get_status:    Optional[Callable[[], Dict]] = None
        self.get_positions: Optional[Callable[[], list]] = None
        self.get_capital:   Optional[Callable[[], Dict]] = None
        self.get_trades:    Optional[Callable[[], Dict]] = None
        self.get_risk:      Optional[Callable[[], Dict]] = None
        self.get_orb:       Optional[Callable[[], Dict]] = None
        self.do_pause:      Optional[Callable]           = None
        self.do_resume:     Optional[Callable]           = None
        self.do_report:     Optional[Callable]           = None
        # Called with request_token string — generates session, updates .env, reloads kite
        self.do_token_auth: Optional[Callable[[str], str]] = None

        if not self.enabled:
            logger.warning("Command handler disabled — set TELEGRAM_BOT_TOKEN / CHAT_ID")

    # ── State persistence ──────────────────────────────────────────────────────
    def _load_state(self):
        try:
            if self._state_file.exists():
                s = json.loads(self._state_file.read_text())
                self._last_update_id = s.get("last_update_id", 0)
        except Exception:
            pass

    def _save_state(self):
        try:
            self._state_file.write_text(json.dumps({"last_update_id": self._last_update_id}))
        except Exception:
            pass

    # ── Telegram API helpers ───────────────────────────────────────────────────
    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            r = requests.post(f"{self._url}/sendMessage", json={
                "chat_id":    self.chat_id,
                "text":       text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=10)
            return r.ok
        except Exception as e:
            logger.error(f"send failed: {e}")
            return False

    def _get_updates(self, timeout: int = 30) -> list:
        try:
            r = requests.get(f"{self._url}/getUpdates", params={
                "offset":          self._last_update_id + 1,
                "timeout":         timeout,
                "allowed_updates": ["message"],
            }, timeout=timeout + 10)
            if r.ok:
                data = r.json()
                if data.get("ok"):
                    return data.get("result", [])
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            logger.error(f"getUpdates failed: {e}")
        return []

    def _skip_old(self):
        """Mark all pending messages as read so stale commands aren't replayed."""
        try:
            r = requests.get(f"{self._url}/getUpdates", timeout=10)
            if r.ok:
                updates = r.json().get("result", [])
                if updates:
                    self._last_update_id = updates[-1]["update_id"]
                    self._save_state()
                    logger.info(f"Skipped {len(updates)} old Telegram messages")
        except Exception:
            pass

    # ── Command dispatcher ─────────────────────────────────────────────────────
    def _dispatch(self, message: dict):
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        parts = text.split(None, 1)                       # ["/cmd", "arg..."]
        cmd   = parts[0].lower().split("@")[0]            # strip @botname suffix
        arg   = parts[1].strip() if len(parts) > 1 else ""
        name  = message.get("from", {}).get("first_name", "User")

        logger.info(f"Command from {name}: {cmd} {arg[:40] if arg else ''}")

        # /token needs the argument inline — handle separately
        if cmd == "/token":
            try:
                reply = self._cmd_token(arg, name)
                if reply:
                    self.send(reply)
            except Exception as e:
                logger.error(f"Command /token failed: {e}")
                self.send(f"[{self.label}] /token error: {e}")
            return

        handlers = {
            "/start":    self._cmd_help,
            "/help":     self._cmd_help,
            "/status":   self._cmd_status,
            "/position": self._cmd_position,
            "/positions":self._cmd_position,
            "/capital":  self._cmd_capital,
            "/trades":   self._cmd_trades,
            "/risk":     self._cmd_risk,
            "/orb":      self._cmd_orb,
            "/pause":    lambda: self._cmd_pause(name),
            "/resume":   lambda: self._cmd_resume(name),
            "/report":   self._cmd_report,
        }

        fn = handlers.get(cmd)
        if fn:
            try:
                reply = fn()
                if reply:
                    self.send(reply)
            except Exception as e:
                logger.error(f"Command {cmd} failed: {e}")
                self.send(f"[{self.label}] Error in {cmd}: {e}")
        else:
            self.send(f"[{self.label}] Unknown command: {cmd}\n\nType /help for list.")

    # ── Command handlers ───────────────────────────────────────────────────────
    def _cmd_help(self) -> str:
        return (
            f"[{self.label}] AVAILABLE COMMANDS\n\n"
            "-- Auth (daily) --\n"
            "/token REQUEST_TOKEN  -- Zerodha auth (sent each morning)\n\n"
            "-- Monitoring --\n"
            "/status   -- Bot status, capital, monthly DD\n"
            "/position -- Open paper position(s) + live P&L\n"
            "/capital  -- Running capital and return %\n"
            "/trades   -- Today's completed paper trades\n"
            "/risk     -- Monthly DD circuit breaker status\n"
            "/orb      -- Today's ORB levels (Nifty + FinNifty)\n\n"
            "-- Control --\n"
            "/pause    -- Pause new entries (still tracks open)\n"
            "/resume   -- Resume new entries\n"
            "/report   -- Force send today's report now\n\n"
            "/help     -- Show this message\n\n"
            "Paper trading only — no real orders."
        )

    def _cmd_token(self, arg: str, name: str) -> str:
        """Handle /token REQUEST_TOKEN — also accepts full redirect URL."""
        if not arg:
            return (
                f"[{self.label}] Usage: /token REQUEST_TOKEN\n\n"
                "After Zerodha login, copy the request_token from the redirect URL\n"
                "(the value after ?request_token=) and send it here."
            )

        # Accept full redirect URL: extract request_token param
        request_token = arg.strip()
        if "request_token=" in request_token:
            import urllib.parse as _up
            qs = _up.parse_qs(_up.urlparse(request_token).query)
            rt = qs.get("request_token", [""])[0]
            if rt:
                request_token = rt

        if not request_token or len(request_token) < 8:
            return f"[{self.label}] Invalid request_token: '{arg[:40]}'"

        if not self.do_token_auth:
            return f"[{self.label}] Token auth callback not configured."

        self.send(f"[{self.label}] Authenticating with Zerodha, please wait...")
        try:
            result = self.do_token_auth(request_token)
            return result
        except Exception as e:
            logger.error(f"do_token_auth failed: {e}")
            return f"[{self.label}] Auth FAILED: {e}"

    def _cmd_status(self) -> str:
        state = "PAUSED" if self._paused else "RUNNING"
        lines = [
            f"[{self.label}] BOT STATUS: {state}",
            f"Started  : {self._started_at}",
            f"Time     : {_ist_str()}",
        ]
        if self.get_status:
            try:
                d = self.get_status()
                lines += [
                    f"Capital  : Rs{d.get('running_capital', 0):,.0f}",
                    f"Return % : {d.get('return_pct', 0):+.2f}%",
                    f"Open pos : {d.get('open_positions', 0)}",
                    f"Month DD : Rs{d.get('monthly_pnl', 0):,.0f}",
                    f"Paused M : {'Yes' if d.get('month_paused') else 'No'}",
                ]
            except Exception as e:
                lines.append(f"(Status error: {e})")
        return "\n".join(lines)

    def _cmd_position(self) -> str:
        if not self.get_positions:
            return f"[{self.label}] No positions callback set."
        positions = self.get_positions()
        if not positions:
            return f"[{self.label}] No open paper positions."
        lines = [f"[{self.label}] OPEN POSITIONS ({len(positions)})"]
        for p in positions:
            win = "WIN" if p.get("live_pnl", 0) >= 0 else "LOSS"
            lines.append(
                f"\n{p['instrument']} {p['direction'].upper()} [{p['quality']}]\n"
                f"  Strike={p['strike']:.0f}  DTE={p['dte']}\n"
                f"  Entry=Rs{p['entry_prem']:.2f}  "
                f"Current=Rs{p.get('current_prem', 0):.2f}\n"
                f"  Peak=Rs{p.get('peak', 0):.2f}  "
                f"Lots={p['lots']}  Qty={p['qty']}\n"
                f"  Live P&L: Rs{p.get('live_pnl', 0):+,.0f}  {win}\n"
                f"  BEP: {'ON' if p.get('bep_active') else 'off'}  "
                f"Trail: {'ON' if p.get('trail_active') else 'off'}"
            )
        lines.append(f"\n{_ist_str()}")
        return "\n".join(lines)

    def _cmd_capital(self) -> str:
        if not self.get_capital:
            return f"[{self.label}] No capital callback."
        d = self.get_capital()
        return (
            f"[{self.label}] CAPITAL SUMMARY\n"
            f"Initial : Rs{d.get('initial', 0):,.0f}\n"
            f"Running : Rs{d.get('running', 0):,.0f}\n"
            f"Net P&L : Rs{d.get('net_pnl', 0):+,.0f}\n"
            f"Return  : {d.get('return_pct', 0):+.2f}%\n"
            f"Month   : Rs{d.get('monthly_pnl', 0):+,.0f}\n"
            f"{_ist_str()}"
        )

    def _cmd_trades(self) -> str:
        if not self.get_trades:
            return f"[{self.label}] No trades callback."
        d = self.get_trades()
        n   = d.get("trades", 0)
        if n == 0:
            return f"[{self.label}] No completed trades today."
        wins = d.get("wins", 0)
        net  = d.get("net_rs", 0)
        em   = "WIN" if net >= 0 else "LOSS"
        lines = [
            f"[{self.label}] TODAY'S TRADES {em}",
            f"Trades  : {n}  ({wins}W / {n-wins}L)",
            f"WR      : {d.get('win_rate', 0):.0f}%",
            f"Net P&L : Rs{net:+,.0f}",
            f"Best    : Rs{d.get('best_rs', 0):+,.0f}",
            f"Worst   : Rs{d.get('worst_rs', 0):+,.0f}",
        ]
        for t in d.get("details", []):
            icon = "W" if t.get("net_rs", 0) > 0 else "L"
            lines.append(
                f"  [{icon}] {t['instrument']} {t['direction'].upper()} "
                f"Rs{t.get('net_rs', 0):+,.0f} ({t.get('exit_reason', '?')})"
            )
        lines.append(_ist_str())
        return "\n".join(lines)

    def _cmd_risk(self) -> str:
        if not self.get_risk:
            return f"[{self.label}] No risk callback."
        d = self.get_risk()
        monthly = d.get("monthly_pnl", 0)
        limit   = d.get("monthly_dd_limit_rs", 0)
        paused  = d.get("month_paused", False)
        status  = "PAUSED (DD limit hit)" if paused else ("PAUSED (manual)" if self._paused else "ACTIVE")
        pct_used = abs(monthly) / abs(limit) * 100 if limit else 0
        bar = _bar(monthly, limit)
        return (
            f"[{self.label}] RISK STATUS\n"
            f"Status      : {status}\n\n"
            f"Monthly P&L : Rs{monthly:+,.0f}\n"
            f"DD Limit    : Rs{limit:,.0f} ({abs(d.get('monthly_dd_pct', 5)):.0f}%)\n"
            f"Used        : {bar}\n\n"
            f"VIX Gate    : {d.get('vix_buy_min', 12.0)}\n"
            f"Capital     : Rs{d.get('capital', 0):,.0f}\n"
            f"{_ist_str()}"
        )

    def _cmd_orb(self) -> str:
        if not self.get_orb:
            return f"[{self.label}] ORB levels not available yet."
        d = self.get_orb()
        if not d:
            return f"[{self.label}] ORB not locked yet (locks at 9:30 IST)."
        lines = [f"[{self.label}] ORB LEVELS — {_ist_str('%Y-%m-%d')}"]
        for inst, v in d.items():
            rng = v.get("h", 0) - v.get("l", 0)
            lines.append(
                f"\n{inst}\n"
                f"  High  : {v.get('h', 0):.0f}\n"
                f"  Low   : {v.get('l', 0):.0f}\n"
                f"  Range : {rng:.0f} pts"
            )
        lines.append(f"\n{_ist_str()}")
        return "\n".join(lines)

    def _cmd_pause(self, name: str) -> str:
        self._paused = True
        if self.do_pause:
            self.do_pause()
        return (
            f"[{self.label}] PAUSED by {name}\n"
            f"No new entries until /resume.\n"
            f"Open positions still tracked.\n"
            f"{_ist_str()}"
        )

    def _cmd_resume(self, name: str) -> str:
        self._paused = False
        if self.do_resume:
            self.do_resume()
        return (
            f"[{self.label}] RESUMED by {name}\n"
            f"New entries active again.\n"
            f"{_ist_str()}"
        )

    def _cmd_report(self) -> str:
        if self.do_report:
            self.do_report()
            return f"[{self.label}] Daily report sent. Check messages above."
        return f"[{self.label}] Report callback not set."

    # ── Polling thread ─────────────────────────────────────────────────────────
    def start_polling(self):
        if not self.enabled or self.running:
            return
        self._skip_old()
        self.running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="TgCmdPoll")
        self._thread.start()
        logger.info("Telegram command polling started")

    def stop_polling(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Telegram command polling stopped")

    def _poll_loop(self):
        while self.running:
            try:
                for update in self._get_updates(timeout=30):
                    uid = update.get("update_id", 0)
                    if uid > self._last_update_id:
                        self._last_update_id = uid
                        self._save_state()
                    msg = update.get("message", {})
                    if msg and "text" in msg:
                        self._dispatch(msg)
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
                time.sleep(5)

    @property
    def is_paused(self) -> bool:
        return self._paused
