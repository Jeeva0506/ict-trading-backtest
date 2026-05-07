"""
Live Trading Dashboard — FundedNext ORB Bot
============================================
Reads log files from all 3 accounts and displays live status.

Usage:
    python reports/live_dashboard.py              # one-shot snapshot
    python reports/live_dashboard.py --watch      # auto-refresh every 60s
    python reports/live_dashboard.py --html       # write HTML only (no terminal output)
"""

import re
import sys
import os
import time
import argparse
import psutil
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNTS = [
    {"label": "STELLAR-2",  "slug": "stellar-2",  "size": 6_000,  "env": ".env"},
    {"label": "STELLAR-3",  "slug": "stellar-3",  "size": 6_000,  "env": ".env.stellar3"},
    {"label": "STELLAR-15K","slug": "stellar-15k", "size": 15_000, "env": ".env.stellar15k"},
]

MAX_DD_PCT      = 10.0     # FundedNext trailing DD limit
WARN_DD_PCT     = 7.0      # yellow alert threshold
CRIT_DD_PCT     = 9.0      # red alert threshold
HEARTBEAT_STALE = 300      # seconds — 5 min without heartbeat = frozen
WATCHDOG_STALE  = 900      # seconds — 15 min without watchdog log = dead

LOG_BASE    = Path(__file__).resolve().parent.parent / "live" / "logs"
REPORT_DIR  = Path(__file__).resolve().parent
HTML_OUT    = REPORT_DIR / "live_dashboard.html"

STATUS_RE   = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - INFO - Status - "
    r"Balance: \$([0-9,.]+) \| Daily DD: ([0-9.]+)% \| Overall DD: ([0-9.]+)% \| Active: (\[.*?\])"
)
SIGNAL_RE   = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - .+? - (.+)")
WATCHDOG_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - .+? - (.+)")


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _tail(path: Path, n_bytes: int = 32_768) -> str:
    """Read the last n_bytes of a file efficiently."""
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - n_bytes))
            raw = f.read()
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _pid_alive(lock_path: Path) -> tuple[bool, int]:
    """Return (alive, pid) from a lock file."""
    if not lock_path.exists():
        return False, 0
    try:
        pid = int(lock_path.read_text().strip())
        if not psutil.pid_exists(pid):
            return False, pid
        try:
            proc = psutil.Process(pid)
            return "python" in proc.name().lower(), pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False, pid
    except Exception:
        return False, 0


def _parse_status(log_dir: Path) -> dict:
    """Extract latest Status line from trading.log."""
    text   = _tail(log_dir / "trading.log")
    result = {"balance": None, "daily_dd": None, "overall_dd": None, "active": [], "ts": None}
    for m in STATUS_RE.finditer(text):
        result = {
            "ts":         datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
            "balance":    float(m.group(2).replace(",", "")),
            "daily_dd":   float(m.group(3)),
            "overall_dd": float(m.group(4)),
            "active":     m.group(5),
        }
    return result


def _parse_watchdog(log_dir: Path) -> dict:
    """Extract last watchdog log timestamp and message."""
    text = _tail(log_dir / "watchdog.log", 4096)
    result = {"ts": None, "msg": "no log"}
    for m in WATCHDOG_RE.finditer(text):
        result = {
            "ts":  datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
            "msg": m.group(2)[:80],
        }
    return result


def _heartbeat_age(log_dir: Path) -> float:
    """Age of heartbeat file in seconds (-1 if missing)."""
    hb = log_dir / "heartbeat.txt"
    if not hb.exists():
        return -1
    age = (datetime.now() - datetime.fromtimestamp(hb.stat().st_mtime)).total_seconds()
    return age


def _recent_events(log_dir: Path, n: int = 5) -> list[str]:
    """Return last N significant log lines (signals, warnings, criticals)."""
    text   = _tail(log_dir / "trading.log", 65_536)
    keep   = []
    ignore = {"Session boundaries", "Status - Balance", "Market closed",
              "Market opened", "no signal", "ORB Detection"}
    for m in SIGNAL_RE.finditer(text):
        msg = m.group(2)
        if any(s in msg for s in ignore):
            continue
        keep.append(f"  {m.group(1)}  {msg[:100]}")
    return keep[-n:]


# ── Per-account snapshot ──────────────────────────────────────────────────────

def account_snapshot(acc: dict) -> dict:
    slug    = acc["slug"]
    log_dir = LOG_BASE / slug

    status  = _parse_status(log_dir)
    wdog    = _parse_watchdog(log_dir)
    hb_age  = _heartbeat_age(log_dir)
    bot_alive, bot_pid       = _pid_alive(log_dir / "bot.lock")
    wdog_alive, wdog_pid     = _pid_alive(log_dir / "watchdog.lock")
    events  = _recent_events(log_dir)

    now = datetime.now()

    # Staleness
    status_age = (now - status["ts"]).total_seconds() if status["ts"] else 99999
    wdog_age   = (now - wdog["ts"]).total_seconds()   if wdog["ts"]   else 99999

    # DD headroom
    remaining_dd = MAX_DD_PCT - (status["overall_dd"] or 0)

    return {
        "label":        acc["label"],
        "slug":         slug,
        "size":         acc["size"],
        "status":       status,
        "status_age":   status_age,
        "wdog":         wdog,
        "wdog_age":     wdog_age,
        "hb_age":       hb_age,
        "bot_alive":    bot_alive,
        "bot_pid":      bot_pid,
        "wdog_alive":   wdog_alive,
        "wdog_pid":     wdog_pid,
        "remaining_dd": remaining_dd,
        "events":       events,
    }


# ── Terminal rendering ────────────────────────────────────────────────────────

def _dd_bar(overall_dd: float, width: int = 20) -> str:
    pct   = min(overall_dd / MAX_DD_PCT, 1.0)
    filled = int(pct * width)
    bar   = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {overall_dd:.2f}% / {MAX_DD_PCT:.0f}%"


def _ago(seconds: float) -> str:
    if seconds < 0:
        return "MISSING"
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds/60:.0f}m ago"
    return f"{seconds/3600:.1f}h ago"


def render_terminal(snapshots: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("=" * 72)
    lines.append(f"  LIVE TRADING DASHBOARD   {now}")
    lines.append("=" * 72)

    for snap in snapshots:
        s   = snap["status"]
        bal = s["balance"]
        ddd = s["daily_dd"]
        odd = s["overall_dd"]
        rem = snap["remaining_dd"]

        # Account header
        lines.append(f"\n  [{snap['label']}]  size=${snap['size']:,}")
        lines.append("  " + "-" * 68)

        if bal is None:
            lines.append("  !! No status data — check log file")
        else:
            pnl     = bal - snap["size"]
            pnl_pct = pnl / snap["size"] * 100
            sign    = "+" if pnl >= 0 else ""
            lines.append(f"  Balance     : ${bal:>10,.2f}   ({sign}{pnl_pct:.2f}%  {sign}${pnl:,.2f})")
            lines.append(f"  Daily DD    : {ddd:.2f}%   (limit 3.00%)")
            lines.append(f"  Overall DD  : {_dd_bar(odd)}")

            warn = ""
            if odd >= CRIT_DD_PCT:
                warn = "  *** CRITICAL — near 10% breach! Reduce risk ***"
            elif odd >= WARN_DD_PCT:
                warn = "  ** WARNING — approaching DD limit **"
            if warn:
                lines.append(warn)

            lines.append(f"  DD Headroom : {rem:.2f}% remaining")
            lines.append(f"  Status age  : {_ago(snap['status_age'])}")
            active = s.get("active", "[]")
            lines.append(f"  Active mkts : {active}")

        # Bot health
        bot_ok  = snap["bot_alive"]
        hb_age  = snap["hb_age"]
        hb_ok   = 0 <= hb_age < HEARTBEAT_STALE
        bot_str = f"ALIVE (PID {snap['bot_pid']})" if bot_ok else f"DEAD (last PID {snap['bot_pid']})"
        hb_str  = _ago(hb_age) if hb_age >= 0 else "MISSING"
        lines.append(f"\n  Bot process : {bot_str}")
        lines.append(f"  Heartbeat   : {hb_str}  {'[OK]' if hb_ok else '[STALE!]'}")

        # Watchdog health
        wdog_ok  = snap["wdog_alive"]
        wdog_str = f"ALIVE (PID {snap['wdog_pid']})" if wdog_ok else f"DEAD (last PID {snap['wdog_pid']})"
        wlog_str = _ago(snap["wdog_age"])
        lines.append(f"  Watchdog    : {wdog_str}   log {wlog_str}")
        if not wdog_ok:
            lines.append(f"  !! WATCHDOG DOWN -- restart after session: RESTART_{snap['slug'].upper()}_WATCHDOG.bat")

        # Recent events
        if snap["events"]:
            lines.append("\n  Recent activity:")
            for ev in snap["events"]:
                lines.append(ev)

    lines.append("\n" + "=" * 72)
    return "\n".join(lines)


# ── HTML rendering ────────────────────────────────────────────────────────────

_CSS = """
body{font-family:monospace;background:#0d0d0d;color:#c8c8c8;margin:20px}
h1{color:#fff;border-bottom:1px solid #333;padding-bottom:8px}
.card{border:1px solid #333;border-radius:6px;padding:16px;margin:16px 0;background:#151515}
.label{font-size:1.2em;font-weight:bold;color:#fff}
.ok{color:#4caf50}.warn{color:#ff9800}.crit{color:#f44336}.dead{color:#f44336}
.alive{color:#4caf50}
table{width:100%;border-collapse:collapse;margin:8px 0}
td,th{padding:4px 8px;text-align:left}
th{color:#888;font-size:0.85em}
.bar-bg{background:#222;border-radius:3px;height:14px;width:200px;display:inline-block}
.bar-fill{height:14px;border-radius:3px;display:inline-block}
.events{font-size:0.8em;color:#888;border-top:1px solid #222;margin-top:8px;padding-top:8px}
.ts{color:#555;font-size:0.75em}
"""


def _html_dd_bar(pct: float) -> str:
    frac  = min(pct / MAX_DD_PCT, 1.0)
    color = "#4caf50" if pct < WARN_DD_PCT else ("#ff9800" if pct < CRIT_DD_PCT else "#f44336")
    w     = int(frac * 200)
    return (f'<div class="bar-bg"><div class="bar-fill" '
            f'style="width:{w}px;background:{color}"></div></div> '
            f'{pct:.2f}% / {MAX_DD_PCT:.0f}%')


def render_html(snapshots: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cards = []
    for snap in snapshots:
        s   = snap["status"]
        bal = s["balance"]
        odd = s.get("overall_dd") or 0
        rem = snap["remaining_dd"]

        if odd >= CRIT_DD_PCT:
            border = "#f44336"
        elif odd >= WARN_DD_PCT:
            border = "#ff9800"
        else:
            border = "#333"

        bot_cls  = "alive" if snap["bot_alive"]  else "dead"
        wdog_cls = "alive" if snap["wdog_alive"] else "dead"
        hb_age   = snap["hb_age"]
        hb_cls   = "ok" if 0 <= hb_age < HEARTBEAT_STALE else "crit"

        if bal:
            pnl     = bal - snap["size"]
            pnl_pct = pnl / snap["size"] * 100
            sign    = "+" if pnl >= 0 else ""
            pnl_cls = "ok" if pnl >= 0 else "crit"
            bal_row = (f"<tr><th>Balance</th>"
                       f"<td>${bal:,.2f}</td>"
                       f"<td class='{pnl_cls}'>{sign}${pnl:,.2f} ({sign}{pnl_pct:.2f}%)</td></tr>")
            dd_row  = (f"<tr><th>Overall DD</th>"
                       f"<td colspan='2'>{_html_dd_bar(odd)}</td></tr>"
                       f"<tr><th>Daily DD</th>"
                       f"<td>{s['daily_dd']:.2f}%</td>"
                       f"<td class='ts'>limit 3.00%</td></tr>"
                       f"<tr><th>DD Headroom</th>"
                       f"<td class='{'ok' if rem > 3 else 'warn'}'>{rem:.2f}% left</td>"
                       f"<td class='ts'>status {_ago(snap['status_age'])}</td></tr>")
            active_row = f"<tr><th>Active</th><td colspan='2'>{s.get('active','')}</td></tr>"
        else:
            bal_row  = "<tr><td colspan='3' class='crit'>No status data</td></tr>"
            dd_row   = ""
            active_row = ""

        warn_banner = ""
        if odd >= CRIT_DD_PCT:
            warn_banner = "<p class='crit'><b>!!! CRITICAL — near 10% DD breach! Stop trading manually !!!</b></p>"
        elif odd >= WARN_DD_PCT:
            warn_banner = "<p class='warn'><b>!! WARNING — approaching DD limit — reduce risk !!</b></p>"

        wdog_down_note = ""
        if not snap["wdog_alive"]:
            bat = f"start_trading_{snap['slug'].replace('-','')}.bat"
            wdog_down_note = (f"<p class='warn'>Watchdog is down (last log: {_ago(snap['wdog_age'])}). "
                              f"Restart after session using <code>{bat}</code></p>")

        events_html = ""
        if snap["events"]:
            ev_lines = "".join(f"<div>{e}</div>" for e in snap["events"])
            events_html = f'<div class="events"><b>Recent activity:</b><br>{ev_lines}</div>'

        card = f"""
<div class="card" style="border-color:{border}">
  <div class="label">{snap['label']}</div>
  <div class="ts">Account size: ${snap['size']:,}</div>
  {warn_banner}
  <table>
    {bal_row}
    {dd_row}
    {active_row}
    <tr><th>Bot</th>
        <td class="{bot_cls}">{("ALIVE PID " + str(snap["bot_pid"])) if snap["bot_alive"] else ("DEAD last PID " + str(snap["bot_pid"]))}</td>
        <td class="{hb_cls}">heartbeat {_ago(hb_age)}</td></tr>
    <tr><th>Watchdog</th>
        <td class="{wdog_cls}">{("ALIVE PID " + str(snap["wdog_pid"])) if snap["wdog_alive"] else ("DEAD last PID " + str(snap["wdog_pid"]))}</td>
        <td class="ts">log {_ago(snap["wdog_age"])}</td></tr>
  </table>
  {wdog_down_note}
  {events_html}
</div>"""
        cards.append(card)

    refresh_note = '<meta http-equiv="refresh" content="60">'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
{refresh_note}
<title>Live Trading Dashboard</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Live Trading Dashboard</h1>
<p class="ts">Generated: {now} &nbsp;|&nbsp; Auto-refreshes every 60s</p>
{"".join(cards)}
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once(html_only: bool = False) -> None:
    snapshots = [account_snapshot(acc) for acc in ACCOUNTS]

    if not html_only:
        print(render_terminal(snapshots))

    html = render_html(snapshots)
    HTML_OUT.write_text(html, encoding="utf-8")
    if not html_only:
        print(f"\n  HTML report saved -> {HTML_OUT}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch",    action="store_true", help="Auto-refresh every 60s")
    parser.add_argument("--interval", type=int, default=60, help="Refresh interval seconds")
    parser.add_argument("--html",     action="store_true", help="Write HTML only (no terminal)")
    args = parser.parse_args()

    if args.watch:
        while True:
            if sys.platform == "win32":
                os.system("cls")
            else:
                os.system("clear")
            run_once(html_only=False)
            print(f"\n  [refreshing every {args.interval}s — Ctrl+C to stop]")
            time.sleep(args.interval)
    else:
        run_once(html_only=args.html)


if __name__ == "__main__":
    main()
