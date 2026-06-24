#!/usr/bin/env python3
"""
Limit-order fill-rate analyzer (STELLAR-2 / STELLAR-15K).

Scans the live trading logs and tallies the limit-order lifecycle per day and
cumulatively, so we can measure the REAL fill rate against the ~97% candle-based
backtest assumption. Reads the non-OneDrive log location and any rotated *.old
files. Run it any time during the 3-day observation window:

    python analyze_limit_fills.py            # all accounts, all dates
    python analyze_limit_fills.py 2026-06-24 # filter to one date

Counts: placed, filled, timeout (no-fill), past-market skip, plus drift/spread/
min-R guard rejections (signals that never became orders).
"""
import re
import sys
import glob
from pathlib import Path
from collections import defaultdict

LOG_BASE = Path(r"C:\Users\Jeeva\AppData\Local\TradingBot\logs")
ACCOUNTS = ["stellar-2", "stellar-15k"]   # limit-order accounts only

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
SYM_RE = re.compile(r"-\s*([A-Z0-9]+):")

PAT = {
    "placed":   re.compile(r"Limit\s+(?:BUY|SELL).*placed \(ticket=", re.I),
    "filled":   re.compile(r"Limit fill (?:confirmed|found)", re.I),
    "timeout":  re.compile(r"cancelled after \d+s \(no fill\)", re.I),  # one line per order
    "past_mkt": re.compile(r"past limit|limit_price_past_market", re.I),
    "drift":    re.compile(r"DRIFT GUARD", re.I),
    "spread":   re.compile(r"SPREAD GUARD", re.I),
    "min_r":    re.compile(r"MIN R GUARD|LIVE R TOO SMALL", re.I),
}


def scan():
    # tally[date][account][metric] = count ; also collect placed-order detail lines
    tally = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    detail = []  # (date, account, symbol, outcome, raw)
    seen_guard = set()  # de-dupe repeated guard spam (same symbol/day)

    for acct in ACCOUNTS:
        files = sorted(glob.glob(str(LOG_BASE / acct / "trading.log*")))
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        m = DATE_RE.match(line)
                        if not m:
                            continue
                        date = m.group(1)
                        sym_m = SYM_RE.search(line)
                        sym = sym_m.group(1) if sym_m else "?"
                        for metric, rx in PAT.items():
                            if rx.search(line):
                                if metric in ("drift", "spread", "min_r"):
                                    key = (date, acct, sym, metric)
                                    if key in seen_guard:
                                        break
                                    seen_guard.add(key)
                                tally[date][acct][metric] += 1
                                if metric in ("placed", "filled", "timeout", "past_mkt"):
                                    detail.append((date, acct, sym, metric, line.strip()))
                                break
            except FileNotFoundError:
                continue
    return tally, detail


def main():
    only_date = sys.argv[1] if len(sys.argv) > 1 else None
    tally, detail = scan()

    tot = defaultdict(int)
    print("=" * 72)
    print("LIMIT-ORDER FILL-RATE REPORT  (STELLAR-2 + STELLAR-15K)")
    print("=" * 72)
    for date in sorted(tally):
        if only_date and date != only_date:
            continue
        day = defaultdict(int)
        for acct in ACCOUNTS:
            for k, v in tally[date][acct].items():
                day[k] += v
        placed = day["placed"]
        filled = day["filled"]
        fr = f"{(filled/placed*100):.0f}%" if placed else "n/a"
        print(f"\n{date}   placed={placed}  filled={filled}  "
              f"timeout={day['timeout']}  past_mkt={day['past_mkt']}  "
              f"FILL RATE={fr}")
        print(f"           guard rejects -> drift={day['drift']} "
              f"spread={day['spread']} min_R={day['min_r']}")
        for k in ("placed", "filled", "timeout", "past_mkt", "drift", "spread", "min_r"):
            tot[k] += day[k]

    p, f = tot["placed"], tot["filled"]
    fr = f"{(f/p*100):.1f}%" if p else "n/a"
    print("\n" + "-" * 72)
    print(f"CUMULATIVE   placed={p}  filled={f}  timeout={tot['timeout']}  "
          f"past_mkt={tot['past_mkt']}  ->  FILL RATE = {fr}")
    print(f"             (backtest assumes ~97%)   guard rejects: "
          f"drift={tot['drift']} spread={tot['spread']} min_R={tot['min_r']}")
    print("-" * 72)

    if detail:
        print("\nPer-order detail:")
        for d, a, s, o, _ in detail:
            tag = {"placed": "PLACED ", "filled": "FILLED ",
                   "timeout": "TIMEOUT", "past_mkt": "PASTMKT"}[o]
            print(f"  {d}  {a:11} {s:7} {tag}")


if __name__ == "__main__":
    main()
