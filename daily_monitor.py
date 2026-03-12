"""
Daily Bot Monitor — Run after each session to check for missed setups or abnormalities.
Read-only: does NOT modify any trading code or config.

Usage:
    python daily_monitor.py              # today
    python daily_monitor.py 2026-03-12   # specific date
    python daily_monitor.py week         # last 7 days summary
"""

import sys, re, os
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

LOG_DIR = Path(__file__).parent / "live" / "logs"
TRADING_LOG = LOG_DIR / "trading.log"
STDERR_LOG = LOG_DIR / "bot_stderr.log"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
SL_LOG = LOG_DIR / "sl_protection.log"

INDICES = ["SPX500", "NDX100", "GER30", "FRA40", "US30", "US2000"]

# Expected session windows (UTC) — approximate
SESSION_WINDOWS = {
    "GER30":  ("07:00", "12:00"),   # Frankfurt open
    "FRA40":  ("07:00", "12:00"),   # Paris open
    "SPX500": ("13:30", "18:00"),   # NYSE open
    "NDX100": ("13:30", "18:00"),   # NYSE open
    "US30":   ("13:30", "18:00"),   # NYSE open
    "US2000": ("13:30", "18:00"),   # NYSE open
}


def parse_log_lines(log_path, date_str):
    """Extract all lines for a given date from a log file."""
    lines = []
    if not log_path.exists():
        return lines
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if date_str in line:
                    lines.append(line.strip())
    except Exception as e:
        print(f"  [WARN] Could not read {log_path.name}: {e}")
    return lines


def analyze_day(date_str):
    """Full analysis for a single day."""
    print(f"\n{'='*70}")
    print(f"  DAILY BOT MONITOR — {date_str}")
    print(f"{'='*70}")

    trading_lines = parse_log_lines(TRADING_LOG, date_str)
    stderr_lines = parse_log_lines(STDERR_LOG, date_str)
    watchdog_lines = parse_log_lines(WATCHDOG_LOG, date_str)
    sl_lines = parse_log_lines(SL_LOG, date_str)

    if not trading_lines:
        print(f"\n  [!] NO LOG ENTRIES for {date_str}.")
        print(f"      Possible causes: bot not running, market holiday, or date format mismatch.")
        return {}

    # --- 1. ORB DETECTIONS ---
    print(f"\n{'─'*70}")
    print(f"  1. ORB SIGNAL ANALYSIS")
    print(f"{'─'*70}")

    orb_events = []
    for line in trading_lines:
        if "ORB Detection" in line:
            orb_events.append(line)

    index_signals = defaultdict(list)
    for evt in orb_events:
        for idx in INDICES:
            if idx in evt:
                index_signals[idx].append(evt)
                break

    day_summary = {}
    for idx in INDICES:
        signals = index_signals.get(idx, [])
        if not signals:
            print(f"\n  {idx}: No ORB detection logged")
            day_summary[idx] = "NO_DATA"
            continue

        # Parse the first meaningful signal
        first = signals[0]
        status = "UNKNOWN"
        details = ""

        if "REJECT" in first:
            reject_match = re.search(r"REJECT=([^|]+)", first)
            reason = reject_match.group(1).strip() if reject_match else "unknown"
            status = "REJECTED"
            details = reason
        elif "no signal" in first.lower() and "REJECT" not in first:
            status = "NO_DISPLACEMENT"
            details = "ORB formed, no qualifying breakout candle"
        elif "SIGNAL" in first.upper() or "entry" in first.lower():
            status = "SIGNAL_GENERATED"
            details = "Setup triggered"

        # Extract ORB details
        orb_match = re.search(r"ORB=([0-9.]+)-([0-9.]+)\s+rng=([0-9.]+)", first)
        htf_match = re.search(r"HTF=(\w+)", first)
        bars_match = re.search(r"bars=(\d+)", first)

        orb_info = ""
        if orb_match:
            orb_info = f"Range: {orb_match.group(1)}-{orb_match.group(2)} ({orb_match.group(3)}pts)"
        htf_info = htf_match.group(1) if htf_match else "?"

        print(f"\n  {idx}:")
        print(f"    Status      : {status}")
        print(f"    HTF Trend   : {htf_info}")
        if orb_info:
            print(f"    ORB         : {orb_info}")
        if details:
            print(f"    Details     : {details}")
        print(f"    Checks      : {len(signals)} ORB scans logged")

        day_summary[idx] = status

    # --- 2. TRADE EXECUTIONS ---
    print(f"\n{'─'*70}")
    print(f"  2. TRADE EXECUTIONS")
    print(f"{'─'*70}")

    trade_lines = [l for l in trading_lines if any(kw in l for kw in
                   ["ENTRY", "entry_price", "ORDER", "position opened", "trade executed",
                    "Placing", "FILLED", "SL hit", "TP hit", "closed", "exit"])]
    if trade_lines:
        for tl in trade_lines:
            # Extract timestamp
            ts_match = re.search(r"(\d{2}:\d{2}:\d{2})", tl)
            ts = ts_match.group(1) if ts_match else "??:??:??"
            # Shorten the line
            short = tl.split(" - ", 2)[-1] if " - " in tl else tl
            print(f"    [{ts}] {short[:100]}")
    else:
        print(f"    No trades executed.")

    # --- 3. ABNORMALITY CHECKS ---
    print(f"\n{'─'*70}")
    print(f"  3. ABNORMALITY CHECKS")
    print(f"{'─'*70}")

    issues = []

    # Check for bot restarts
    restart_lines = [l for l in trading_lines if any(kw in l.lower() for kw in
                     ["starting", "initialized", "bot started", "connected to mt5", "startup"])]
    if len(restart_lines) > 1:
        issues.append(f"[!] Bot restarted {len(restart_lines)} times — check stability")
    elif len(restart_lines) == 1:
        ts = re.search(r"(\d{2}:\d{2}:\d{2})", restart_lines[0])
        ts_str = ts.group(1) if ts else "?"
        print(f"    Bot started at {ts_str} — normal")

    # Check for connection errors
    error_lines = [l for l in trading_lines + stderr_lines if any(kw in l.lower() for kw in
                   ["error", "exception", "timeout", "disconnect", "failed", "connection lost"])]
    if error_lines:
        unique_errors = set()
        for el in error_lines:
            # Deduplicate similar errors
            short_err = re.sub(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,.\d]*", "", el)
            short_err = short_err.strip()[:80]
            unique_errors.add(short_err)
        issues.append(f"[!] {len(error_lines)} error/warning lines ({len(unique_errors)} unique)")
        for ue in list(unique_errors)[:5]:
            issues.append(f"    → {ue}")

    # Check for monitoring gaps (>10 min between status lines)
    status_times = []
    for line in trading_lines:
        if "Status -" in line:
            ts_match = re.search(r"(\d{2}):(\d{2}):(\d{2})", line)
            if ts_match:
                t = int(ts_match.group(1)) * 3600 + int(ts_match.group(2)) * 60 + int(ts_match.group(3))
                status_times.append(t)

    if len(status_times) > 1:
        max_gap = 0
        gap_at = ""
        for i in range(1, len(status_times)):
            gap = status_times[i] - status_times[i-1]
            if gap > max_gap:
                max_gap = gap
                h, m = divmod(status_times[i] // 60, 60)
                gap_at = f"{h:02d}:{m:02d}"
        if max_gap > 600:  # >10 min
            issues.append(f"[!] Largest monitoring gap: {max_gap//60}m {max_gap%60}s (at ~{gap_at})")
            issues.append(f"    Normal is 5min. Gap >10min may indicate restart or freeze.")
        else:
            print(f"    Monitoring gaps: OK (max {max_gap//60}m {max_gap%60}s)")

    # Check for SL protection events
    if sl_lines:
        sl_alerts = [l for l in sl_lines if any(kw in l.lower() for kw in
                     ["emergency", "retry", "failed", "missing", "unprotected"])]
        if sl_alerts:
            issues.append(f"[!] {len(sl_alerts)} SL protection alerts")
            for sa in sl_alerts[:3]:
                short = sa.split(" - ", 2)[-1] if " - " in sa else sa
                issues.append(f"    → {short[:90]}")

    # Check watchdog
    if watchdog_lines:
        wd_alerts = [l for l in watchdog_lines if any(kw in l.lower() for kw in
                     ["restart", "crash", "hung", "unresponsive", "killed"])]
        if wd_alerts:
            issues.append(f"[!] Watchdog triggered {len(wd_alerts)} times")

    # Check for spread rejections
    spread_lines = [l for l in trading_lines if "spread" in l.lower() and ("reject" in l.lower() or "too wide" in l.lower() or "guard" in l.lower())]
    if spread_lines:
        issues.append(f"[!] {len(spread_lines)} trades rejected for spread")
        for sl_item in spread_lines[:3]:
            short = sl_item.split(" - ", 2)[-1] if " - " in sl_item else sl_item
            issues.append(f"    → {short[:90]}")

    # Check for edge guard triggers
    edge_lines = [l for l in trading_lines if "edge" in l.lower() and ("degrad" in l.lower() or "stop" in l.lower() or "threshold" in l.lower())]
    if edge_lines:
        issues.append(f"[!] Edge guard triggered!")
        for el in edge_lines[:3]:
            short = el.split(" - ", 2)[-1] if " - " in el else el
            issues.append(f"    → {short[:90]}")

    if issues:
        print(f"\n  ⚠ ISSUES FOUND:")
        for issue in issues:
            print(f"    {issue}")
    else:
        print(f"    All checks passed — no abnormalities detected.")

    # --- 4. DAILY SUMMARY ---
    print(f"\n{'─'*70}")
    print(f"  4. DAY SUMMARY")
    print(f"{'─'*70}")

    # Balance
    balance_lines = [l for l in trading_lines if "Balance:" in l]
    if balance_lines:
        last_bal = balance_lines[-1]
        bal_match = re.search(r"Balance:\s*\$([0-9,.]+)", last_bal)
        dd_match = re.search(r"Overall DD:\s*([0-9.]+)%", last_bal)
        daily_dd_match = re.search(r"Daily DD:\s*([0-9.]+)%", last_bal)
        if bal_match:
            print(f"    Balance     : ${bal_match.group(1)}")
        if daily_dd_match:
            print(f"    Daily DD    : {daily_dd_match.group(1)}%")
        if dd_match:
            print(f"    Overall DD  : {dd_match.group(1)}%")

    print(f"    Log entries : {len(trading_lines)}")
    print(f"    Status pings: {len(status_times)}")
    print(f"    ORB scans   : {len(orb_events)}")

    traded = sum(1 for v in day_summary.values() if v == "SIGNAL_GENERATED")
    rejected = sum(1 for v in day_summary.values() if v == "REJECTED")
    no_disp = sum(1 for v in day_summary.values() if v == "NO_DISPLACEMENT")
    no_data = sum(1 for v in day_summary.values() if v == "NO_DATA")

    print(f"\n    Index Status:")
    for idx in INDICES:
        s = day_summary.get(idx, "N/A")
        marker = {"SIGNAL_GENERATED": "✓ TRADED", "REJECTED": "✗ REJECTED",
                  "NO_DISPLACEMENT": "— No breakout", "NO_DATA": "? No data"}.get(s, s)
        print(f"      {idx:8s} : {marker}")

    print(f"\n{'='*70}\n")
    return day_summary


def weekly_summary():
    """Run analysis for last 7 days and show trend."""
    print(f"\n{'='*70}")
    print(f"  WEEKLY BOT MONITOR — Last 7 Days")
    print(f"{'='*70}")

    today = datetime.now()
    all_summaries = {}

    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        day_name = d.strftime("%a")

        # Quick check if any log lines exist
        lines = parse_log_lines(TRADING_LOG, date_str)
        if not lines:
            all_summaries[date_str] = {"day": day_name, "status": "NO_ACTIVITY"}
            continue

        # Count key events
        orb_count = sum(1 for l in lines if "ORB Detection" in l)
        trade_count = sum(1 for l in lines if any(kw in l for kw in ["ENTRY", "entry_price", "position opened"]))
        reject_count = sum(1 for l in lines if "REJECT" in l)
        error_count = sum(1 for l in lines if "error" in l.lower() or "exception" in l.lower())

        bal_match = None
        for l in reversed(lines):
            bal_match = re.search(r"Balance:\s*\$([0-9,.]+)", l)
            if bal_match:
                break

        all_summaries[date_str] = {
            "day": day_name,
            "status": "ACTIVE",
            "orb_scans": orb_count,
            "trades": trade_count,
            "rejects": reject_count,
            "errors": error_count,
            "balance": bal_match.group(1) if bal_match else "?",
        }

    print(f"\n  {'Date':12s} {'Day':4s} {'ORBs':>5s} {'Trades':>7s} {'Rejects':>8s} {'Errors':>7s} {'Balance':>10s}")
    print(f"  {'─'*12} {'─'*4} {'─'*5} {'─'*7} {'─'*8} {'─'*7} {'─'*10}")
    for date_str, info in all_summaries.items():
        if info["status"] == "NO_ACTIVITY":
            print(f"  {date_str:12s} {info['day']:4s}   — no activity (weekend/holiday/bot off) —")
        else:
            print(f"  {date_str:12s} {info['day']:4s} {info['orb_scans']:5d} {info['trades']:7d} {info['rejects']:8d} {info['errors']:7d} ${info['balance']:>9s}")

    total_trades = sum(v.get("trades", 0) for v in all_summaries.values())
    total_errors = sum(v.get("errors", 0) for v in all_summaries.values())
    active_days = sum(1 for v in all_summaries.values() if v["status"] == "ACTIVE")

    print(f"\n  Week Totals: {active_days} active days, {total_trades} trades, {total_errors} errors")

    if total_errors > 20:
        print(f"  ⚠ High error count — run daily_monitor.py <date> on specific days to investigate")
    if total_trades == 0 and active_days > 3:
        print(f"  ⚠ No trades in {active_days} active days — check if strategy filters are too strict")

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.lower() == "week":
            weekly_summary()
        else:
            analyze_day(arg)
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        analyze_day(today)
