#!/usr/bin/env python3
"""
FundedNext challenge simulation on a per-trade R-multiple ledger.

Reads a CSV (date, net_R per trade) exported from backtest_live_setup.py
(EXPORT_TRADES=...), then runs rolling-start challenge attempts under FundedNext
Stellar rules + the live STELLAR-15K adaptive risk tiers, and reports pass /
breach rates. net_R already includes the marketable-limit cap cost (ENTRY_CAP_R),
so this measures the marketable model against the prop-firm rules.

Usage:
  python run_fundednext_challenge.py <ledger.csv> [label]

Rules (FundedNext Stellar, 2-phase eval):
  Phase 1 target +8% | Phase 2 target +5%
  Max overall loss 10% (from initial)  | Max daily loss 3% (from day-start balance)
Risk tiers (STELLAR-15K): 1.0% per trade, drop to 0.5% once overall DD >= 4%.
"""
import sys, csv
from datetime import datetime

P1_TARGET = 0.08
P2_TARGET = 0.05
MAX_DD = 0.10          # from initial balance
DAILY_DD = 0.03        # from day-start balance
RISK_T1 = 0.010        # 1.0%
RISK_T2 = 0.005        # 0.5%
TIER2_DD = 0.04        # switch to 0.5% once overall DD >= 4%


def load_ledger(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                d = datetime.fromisoformat(str(r["date"])[:19]).date()
            except Exception:
                d = str(r["date"])[:10]
            rows.append((d, float(r["net_R"])))
    return rows


def run_attempt(trades, start, target):
    """Simulate one challenge attempt from index `start`. Returns (outcome, n_trades)."""
    bal = 1.0
    day = trades[start][0]
    day_start = 1.0
    for i in range(start, len(trades)):
        d, net_r = trades[i]
        if d != day:
            day, day_start = d, bal
        overall_dd = max(0.0, 1.0 - bal)          # fraction below initial
        risk = RISK_T2 if overall_dd >= TIER2_DD else RISK_T1
        bal *= (1.0 + risk * net_r)               # risk% of balance, scaled by R outcome
        if bal <= day_start * (1.0 - DAILY_DD):
            return "breach_daily", i - start + 1
        if bal <= 1.0 - MAX_DD:
            return "breach_max", i - start + 1
        if bal >= 1.0 + target:
            return "pass", i - start + 1
    return "incomplete", len(trades) - start


def simulate(trades, label):
    n = len(trades)
    # Phase 1: attempt from every start index that has room
    outc = {"pass": 0, "breach_daily": 0, "breach_max": 0, "incomplete": 0}
    p1_pass_end = []   # (start, end_index) for passers -> feed Phase 2
    pass_lens = []
    for s in range(n):
        o, k = run_attempt(trades, s, P1_TARGET)
        outc[o] += 1
        if o == "pass":
            p1_pass_end.append(s + k)
            pass_lens.append(k)
    completed = outc["pass"] + outc["breach_daily"] + outc["breach_max"]
    p1_rate = outc["pass"] / completed * 100 if completed else 0

    # Phase 2: continue from where each P1 pass ended
    p2 = {"pass": 0, "breach_daily": 0, "breach_max": 0, "incomplete": 0}
    for end in p1_pass_end:
        if end >= n:
            p2["incomplete"] += 1; continue
        o, _ = run_attempt(trades, end, P2_TARGET)
        p2[o] += 1
    p2_completed = p2["pass"] + p2["breach_daily"] + p2["breach_max"]
    p2_rate = p2["pass"] / p2_completed * 100 if p2_completed else 0
    funded = (p1_rate / 100) * (p2_rate / 100) * 100

    print(f"\n================ {label} ================")
    print(f"  trades in ledger: {n}")
    print(f"  -- PHASE 1 (+8%) over {n} rolling starts --")
    print(f"     PASS           : {outc['pass']:4d}")
    print(f"     breach (daily) : {outc['breach_daily']:4d}")
    print(f"     breach (max)   : {outc['breach_max']:4d}")
    print(f"     incomplete     : {outc['incomplete']:4d}  (ran out of data)")
    print(f"     PASS RATE      : {p1_rate:5.1f}%  (of completed attempts)")
    if pass_lens:
        pass_lens.sort()
        print(f"     trades to pass : median {pass_lens[len(pass_lens)//2]}, "
              f"range {pass_lens[0]}-{pass_lens[-1]}")
    print(f"  -- PHASE 2 (+5%) from each P1 pass --")
    print(f"     PASS RATE      : {p2_rate:5.1f}%  ({p2['pass']}/{p2_completed})")
    print(f"  -- COMBINED FUNDED RATE : {funded:5.1f}%  (P1 x P2) --")
    breach = (outc['breach_daily'] + outc['breach_max'])
    print(f"  -- breach rate (P1)     : {breach/completed*100:5.1f}%  "
          f"(daily {outc['breach_daily']/completed*100:.0f}% / max {outc['breach_max']/completed*100:.0f}%)"
          if completed else "")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: run_fundednext_challenge.py <ledger.csv> [label]"); sys.exit(1)
    label = sys.argv[2] if len(sys.argv) > 2 else sys.argv[1]
    simulate(load_ledger(sys.argv[1]), label)
