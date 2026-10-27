"""
Combined Paper Trading Report
==============================
Reads both systems and sends a consolidated Telegram report.

Usage:
    python send_report.py          # send report now
    python send_report.py --print  # print only, no Telegram
"""
import sys, json
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from live.telegram_alert import IndianTelegram

IST = timezone(timedelta(hours=5, minutes=30))

PAPER_START   = date(2026, 5, 25)       # Day paper trading began
LIVE_DEADLINE = date(2026, 8, 25)       # Go-live decision date (3 months)

# ── Go-live thresholds ────────────────────────────────────────────────────────
OPTIONS_THRESH = {"min_trades": 40, "min_wr": 45.0, "min_pf": 1.5}
NR7_THRESH     = {"min_trades": 20, "min_wr": 55.0, "min_pf": 2.5}

# ── Env loader ────────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env = {}
    for fname in [".env", "backtest_engine/.env.zerodha"]:
        fpath = ROOT / fname
        if fpath.exists():
            for line in fpath.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


# ── Options paper trader stats ────────────────────────────────────────────────
def _options_stats() -> dict:
    xl = ROOT / "live" / "reports" / "indian_paper_journal.xlsx"
    if not xl.exists():
        return {}
    try:
        trades = pd.read_excel(xl, sheet_name="Trades")
        daily  = pd.read_excel(xl, sheet_name="Daily Summary")
    except Exception:
        return {}

    if trades.empty:
        return {"trades": 0, "capital": 1_000_000, "days_active": 0}

    trades = trades.dropna(subset=["Net Rs"])
    n      = len(trades)
    wins   = trades[trades["Net Rs"] > 0]
    gw     = wins["Net Rs"].sum()
    gl     = abs(trades[trades["Net Rs"] <= 0]["Net Rs"].sum())
    net    = gw - gl
    wr     = len(wins) / n * 100 if n else 0
    pf     = gw / gl if gl else float("inf")
    cap    = float(trades["Cum Capital Rs"].iloc[-1]) if n else 1_000_000

    days = len(daily.dropna(subset=["Date"])) if not daily.empty else 0

    return {
        "trades":     n,
        "wins":       len(wins),
        "wr":         wr,
        "pf":         pf,
        "net":        net,
        "capital":    cap,
        "days_active": days,
    }


# ── NR7 paper trader stats ────────────────────────────────────────────────────
def _nr7_stats() -> dict:
    state_f = ROOT / "data" / "nr7_paper_state.json"
    if not state_f.exists():
        return {"trades": 0, "capital": 200_000, "positions": 0, "pending": 0}

    state = json.loads(state_f.read_text())
    trades  = [t for t in state.get("trades", []) if t.get("win") is not None]
    n       = len(trades)
    wins    = [t for t in trades if t["win"]]
    gw      = sum(t["net_rs"] for t in wins)
    gl      = sum(abs(t["net_rs"]) for t in trades if not t["win"])
    net     = gw - gl
    wr      = len(wins) / n * 100 if n else 0
    pf      = gw / gl if gl else (float("inf") if gw > 0 else 0)

    # Days active — count unique exit dates
    exit_dates = set(t.get("exit_date", "")[:10] for t in trades if t.get("exit_date"))
    entry_dates = set(t.get("entry_date", "")[:10] for t in trades if t.get("entry_date"))
    all_dates = exit_dates | entry_dates | {str(PAPER_START)}

    return {
        "trades":    n,
        "wins":      len(wins),
        "wr":        wr,
        "pf":        pf,
        "net":       net,
        "capital":   state.get("capital", 200_000),
        "positions": len(state.get("positions", {})),
        "pending":   len(state.get("pending", {})),
        "open_pos":  state.get("positions", {}),
    }


# ── Go-live check ─────────────────────────────────────────────────────────────
def _check_ready(stats: dict, thresh: dict) -> tuple:
    """Returns (is_ready, reason_string)."""
    if stats.get("trades", 0) < thresh["min_trades"]:
        return False, f"need {thresh['min_trades']} trades (have {stats.get('trades',0)})"
    if stats.get("wr", 0) < thresh["min_wr"]:
        return False, f"WR {stats['wr']:.1f}% below {thresh['min_wr']}% threshold"
    if stats.get("pf", 0) < thresh["min_pf"]:
        return False, f"PF {stats['pf']:.2f} below {thresh['min_pf']} threshold"
    return True, "criteria met"


# ── Build report ──────────────────────────────────────────────────────────────
def build_report() -> str:
    today     = datetime.now(IST).date()
    days_in   = (today - PAPER_START).days
    days_left = (LIVE_DEADLINE - today).days

    opts = _options_stats()
    nr7  = _nr7_stats()

    opts_ready, opts_reason = _check_ready(opts, OPTIONS_THRESH)
    nr7_ready,  nr7_reason  = _check_ready(nr7,  NR7_THRESH)

    lines = []
    lines.append(f"PAPER TRADING REPORT -- {today}")
    lines.append(f"Day {days_in} of 90 | Live decision: {LIVE_DEADLINE} ({days_left}d left)")
    lines.append("")

    # ── Options section ───────────────────────────────────────────────────────
    lines.append("OPTIONS PAPER TRADER (Nifty+FN ORB)")
    lines.append(f"  Capital: Rs{opts.get('capital', 1_000_000):,.0f} (start: Rs10,00,000)")
    n = opts.get("trades", 0)
    if n == 0:
        lines.append("  No trades yet")
    else:
        net = opts.get("net", 0)
        pnl_pct = net / 1_000_000 * 100
        lines.append(f"  Trades  : {n}  W={opts.get('wins',0)}  WR={opts.get('wr',0):.1f}%")
        lines.append(f"  PF      : {opts.get('pf',0):.2f}")
        lines.append(f"  Net P&L : Rs{net:+,.0f} ({pnl_pct:+.2f}%)")
    if opts_ready:
        lines.append(f"  STATUS  : READY to go live")
    else:
        lines.append(f"  STATUS  : Tracking -- {opts_reason}")
    lines.append("")

    # ── NR7 section ───────────────────────────────────────────────────────────
    lines.append("NR7 POSITIONAL PAPER TRADER (NSE Stocks)")
    lines.append(f"  Capital: Rs{nr7.get('capital', 200_000):,.0f} (start: Rs2,00,000)")
    n2 = nr7.get("trades", 0)
    if n2 == 0:
        lines.append("  No closed trades yet")
    else:
        net2 = nr7.get("net", 0)
        pnl2_pct = net2 / 200_000 * 100
        lines.append(f"  Trades  : {n2}  W={nr7.get('wins',0)}  WR={nr7.get('wr',0):.1f}%")
        lines.append(f"  PF      : {nr7.get('pf',0):.2f}")
        lines.append(f"  Net P&L : Rs{net2:+,.0f} ({pnl2_pct:+.2f}%)")
    lines.append(f"  Open pos: {nr7.get('positions',0)}/5"
                 f"  |  Pending: {nr7.get('pending',0)}")

    # Show open positions if any
    for sym, pos in nr7.get("open_pos", {}).items():
        lines.append(f"    {sym}: entry={pos['entry_price']}  SL={pos['sl']}  qty={pos['qty']}")

    if nr7_ready:
        lines.append(f"  STATUS  : READY to go live")
    else:
        lines.append(f"  STATUS  : Tracking -- {nr7_reason}")
    lines.append("")

    # ── Go-live plan ──────────────────────────────────────────────────────────
    lines.append("3-MONTH GO-LIVE PLAN")
    lines.append(f"  Decision date : {LIVE_DEADLINE}")
    lines.append(f"  Options target: WR>=45%  PF>=1.5  Trades>=40")
    lines.append(f"  NR7 target    : WR>=55%  PF>=2.5  Trades>=20")
    lines.append(f"  Options live  : Rs10,00,000 capital")
    lines.append(f"  NR7 live      : Rs2,00,000 capital")
    both_ready = opts_ready and nr7_ready
    if both_ready:
        lines.append("  DECISION: BOTH READY -- deploy capital!")
    else:
        lines.append("  DECISION: Keep paper trading until Aug 25")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    report = build_report()
    print(report)

    if "--print" in sys.argv:
        return

    env = _load_env()
    tg  = IndianTelegram(
        bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        label="REPORT",
    )
    tg._send(report)
    print("\nSent to Telegram.")


if __name__ == "__main__":
    main()
