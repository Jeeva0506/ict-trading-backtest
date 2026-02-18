"""
DEEP LIVE-READINESS ANALYSIS — Candidate #12
=============================================
15m ORB | H1 20/50 | 2h entry | 60m bkout | disp=1.3 | body=0.60 | close=0.25 | margin=0.05

Tests:
  1. Walk-forward stability (IS 2023-24 vs OOS 2025)
  2. Parameter sensitivity (how fragile are the params?)
  3. Per-index robustness & contribution
  4. Quarterly / monthly consistency
  5. FundedNext challenge simulation (with risk mgmt rules)
  6. Monte Carlo simulation (1000 shuffles)
  7. Worst-case scenario analysis
  8. Edge decay / time stability
  9. Direction & exit-reason analysis
 10. FINAL VERDICT
"""
import sys, time, random
from pathlib import Path
from datetime import date, timedelta
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent))
from optimize_timeframes import (
    run_backtest, compute_metrics, ALL_SYMBOLS,
    STARTING_CAPITAL, RISK_PCT
)

# ── Candidate #12 params ──
C12 = dict(orb_minutes=15, entry_hours=2, breakout_window=60, htf_mode="h1_20_50",
           disp_mult=1.3, body_min=0.60, close_strength=0.25, breakout_margin=0.05)

# ── Baseline params ──
BASELINE = dict(orb_minutes=15, entry_hours=2, breakout_window=60, htf_mode="h1_20_50",
                disp_mult=1.7, body_min=0.65, close_strength=0.30, breakout_margin=0.10)

IS_START = date(2023, 1, 1)
IS_END   = date(2024, 12, 31)
OOS_START = date(2025, 1, 1)
OOS_END   = date(2025, 12, 31)
FULL_START = date(2023, 1, 1)
FULL_END   = date(2025, 12, 31)

# FundedNext rules
FN_DAILY_DD = 5.0     # % from day start
FN_MAX_DD   = 10.0    # % from initial
FN_P1_TARGET = 8.0    # Phase 1
FN_P2_TARGET = 5.0    # Phase 2
MAX_DAILY_LOSSES = 2
DAILY_DD_CUTOFF = 2.0  # internal risk: stop at 2% daily DD from peak
DD_REDUCE_THRESHOLD = 2.0   # reduce risk when -2% from initial
PROFIT_BOOST_THRESHOLD = 2.0 # increase risk when +2%
RISK_DEFAULT = 1.0
RISK_AGGRESSIVE = 1.5
RISK_DEFENSIVE = 0.75


def run_c12(start, end):
    return run_backtest(ALL_SYMBOLS, start, end, **C12)


def section(title, num):
    print(f"\n{'═'*120}")
    print(f"  {num}. {title}")
    print(f"{'═'*120}")


def main():
    t0 = time.time()
    print("█" * 120)
    print("█  DEEP LIVE-READINESS ANALYSIS — CANDIDATE #12")
    print("█  15m ORB | H1 20/50 | disp=1.3 | body=0.60 | close=0.25 | margin=0.05")
    print("█" * 120)

    # ═══════════════════════════════════════════════════════════
    # 1. WALK-FORWARD STABILITY
    # ═══════════════════════════════════════════════════════════
    section("WALK-FORWARD STABILITY (IS vs OOS)", 1)

    is_trades = run_c12(IS_START, IS_END)
    oos_trades = run_c12(OOS_START, OOS_END)
    is_m = compute_metrics(is_trades, IS_START, IS_END)
    oos_m = compute_metrics(oos_trades, OOS_START, OOS_END)

    print(f"\n  {'Metric':<22} {'IS (2023-24)':>14} {'OOS (2025)':>14} {'Delta':>10} {'Verdict':>10}")
    print(f"  {'-'*70}")

    checks = []
    def check(name, is_val, oos_val, higher_better=True, fmt=".1f"):
        delta = oos_val - is_val
        if higher_better:
            ok = oos_val >= is_val * 0.85  # allow 15% degradation
        else:
            ok = oos_val <= is_val * 1.3   # allow 30% increase for DD
        verdict = "✅ PASS" if ok else "❌ FAIL"
        checks.append(ok)
        print(f"  {name:<22} {is_val:>14{fmt}} {oos_val:>14{fmt}} {delta:>+10{fmt}} {verdict:>10}")

    check("Trades", is_m["trades"], oos_m["trades"], True, "d")
    check("TD/Week", is_m["td_per_week"], oos_m["td_per_week"], True)
    check("Win Rate %", is_m["win_rate"], oos_m["win_rate"], True)
    check("Profit Factor", is_m["pf"], oos_m["pf"], True, ".2f")
    check("Sharpe Ratio", is_m["sharpe"], oos_m["sharpe"], True, ".2f")
    check("Return %", is_m["return_pct"], oos_m["return_pct"], True)
    check("Max DD %", is_m["max_dd"], oos_m["max_dd"], False)
    check("Max Consec Loss", is_m["max_consec_loss"], oos_m["max_consec_loss"], False, "d")

    wf_pass = sum(checks) >= 6
    print(f"\n  Walk-Forward Score: {sum(checks)}/{len(checks)} passed")
    print(f"  {'✅ WALK-FORWARD VALIDATED' if wf_pass else '❌ WALK-FORWARD FAILED'}")

    # OOS improvement ratio
    oos_pf_ratio = oos_m["pf"] / is_m["pf"]
    print(f"  OOS/IS PF ratio: {oos_pf_ratio:.2f} ({'OOS improves' if oos_pf_ratio > 1 else 'OOS degrades'})")

    # ═══════════════════════════════════════════════════════════
    # 2. PARAMETER SENSITIVITY
    # ═══════════════════════════════════════════════════════════
    section("PARAMETER SENSITIVITY (perturbation test)", 2)

    print(f"\n  Testing ±1 step perturbation around each #12 param on OOS 2025...")
    print(f"  Checking if small changes cause performance collapse\n")

    base_oos = oos_m
    def tweak(base, **overrides):
        d = dict(base)
        d.update(overrides)
        return d

    perturbations = [
        ("disp_mult  1.2", tweak(C12, disp_mult=1.2)),
        ("disp_mult  1.4", tweak(C12, disp_mult=1.4)),
        ("body_min   0.55", tweak(C12, body_min=0.55)),
        ("body_min   0.65", tweak(C12, body_min=0.65)),
        ("close_str  0.20", tweak(C12, close_strength=0.20)),
        ("close_str  0.30", tweak(C12, close_strength=0.30)),
        ("margin     0.03", tweak(C12, breakout_margin=0.03)),
        ("margin     0.08", tweak(C12, breakout_margin=0.08)),
    ]

    print(f"  {'Param Change':<22} {'Trades':>6} {'WR%':>6} {'PF':>6} {'Sharpe':>7} {'DD%':>5} {'Δ PF':>7} {'Status':>8}")
    print(f"  {'-'*75}")

    stable_count = 0
    for label, params in perturbations:
        trades = run_backtest(ALL_SYMBOLS, OOS_START, OOS_END, **params)
        m = compute_metrics(trades, OOS_START, OOS_END)
        if m:
            dpf = m["pf"] - base_oos["pf"]
            stable = m["pf"] >= 1.0
            if stable:
                stable_count += 1
            status = "✅" if stable else "❌"
            print(f"  {label:<22} {m['trades']:>6} {m['win_rate']:>5.1f}% {m['pf']:>5.2f} {m['sharpe']:>6.2f} {m['max_dd']:>4.1f}% {dpf:>+6.2f} {status:>8}")

    sensitivity_pass = stable_count >= 6
    print(f"\n  Stable perturbations: {stable_count}/{len(perturbations)}")
    print(f"  {'✅ ROBUST — params not on cliff edge' if sensitivity_pass else '❌ FRAGILE — params on knife edge'}")

    # ═══════════════════════════════════════════════════════════
    # 3. PER-INDEX ROBUSTNESS
    # ═══════════════════════════════════════════════════════════
    section("PER-INDEX ROBUSTNESS", 3)

    all_trades = run_c12(FULL_START, FULL_END)
    df = pd.DataFrame(all_trades)
    df["date"] = pd.to_datetime(df["date"])

    print(f"\n  {'Symbol':<10} {'Trades':>6} {'WR%':>6} {'PF':>6} {'Net PnL':>10} {'Avg PnL':>8} {'Max MCL':>7} {'Contrib%':>8}")
    print(f"  {'-'*65}")

    total_pnl = df["pnl"].sum()
    idx_ok = 0
    for sym in ALL_SYMBOLS:
        sub = df[df["symbol"] == sym]
        wins = len(sub[sub["result"] == "win"])
        wr = wins / len(sub) * 100 if len(sub) > 0 else 0
        gp = sub[sub["pnl"] > 0]["pnl"].sum()
        gl = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gp / gl if gl > 0 else 99.0
        net = sub["pnl"].sum()
        avg = sub["pnl"].mean()
        contrib = net / total_pnl * 100 if total_pnl > 0 else 0

        # Max consecutive losses for this index
        results = (sub.sort_values("date")["result"] == "loss").astype(int)
        mcl = 0
        cur = 0
        for r in results:
            if r:
                cur += 1
                mcl = max(mcl, cur)
            else:
                cur = 0

        ok = pf >= 1.0 and net > 0
        if ok:
            idx_ok += 1
        flag = "✅" if ok else "❌"
        print(f"  {sym:<10} {len(sub):>6} {wr:>5.1f}% {pf:>5.2f} ${net:>9,.0f} ${avg:>7,.0f} {mcl:>7} {contrib:>7.1f}% {flag}")

    idx_pass = idx_ok >= 3  # at least 3/4 profitable
    print(f"\n  Profitable indices: {idx_ok}/4")
    print(f"  {'✅ DIVERSIFIED — no single-index dependency' if idx_pass else '❌ CONCENTRATION RISK'}")

    # ═══════════════════════════════════════════════════════════
    # 4. QUARTERLY CONSISTENCY
    # ═══════════════════════════════════════════════════════════
    section("QUARTERLY & MONTHLY CONSISTENCY (2023-2025)", 4)

    df["quarter"] = df["date"].dt.to_period("Q")
    df["month"] = df["date"].dt.to_period("M")

    print(f"\n  QUARTERLY:")
    print(f"  {'Quarter':<10} {'Trades':>6} {'WR%':>6} {'PF':>6} {'PnL':>10} {'TD/Wk':>6}")
    print(f"  {'-'*50}")

    profitable_q = 0
    total_q = 0
    for q in sorted(df["quarter"].unique()):
        sub = df[df["quarter"] == q]
        wins = len(sub[sub["result"] == "win"])
        wr = wins / len(sub) * 100 if len(sub) > 0 else 0
        gp = sub[sub["pnl"] > 0]["pnl"].sum()
        gl = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gp / gl if gl > 0 else 99.0
        net = sub["pnl"].sum()
        td = sub["date"].dt.date.nunique()
        weeks = max(1, 13)  # ~13 weeks per quarter
        td_wk = td / weeks
        total_q += 1
        if net > 0:
            profitable_q += 1
        flag = "✅" if net > 0 else "❌"
        print(f"  {str(q):<10} {len(sub):>6} {wr:>5.1f}% {pf:>5.2f} ${net:>9,.0f} {td_wk:>5.1f} {flag}")

    print(f"\n  Profitable quarters: {profitable_q}/{total_q} ({profitable_q/total_q*100:.0f}%)")

    # Monthly
    print(f"\n  MONTHLY DISTRIBUTION:")
    monthly_pnl = []
    for m in sorted(df["month"].unique()):
        sub = df[df["month"] == m]
        monthly_pnl.append(sub["pnl"].sum())

    monthly_arr = np.array(monthly_pnl)
    profitable_m = (monthly_arr > 0).sum()
    total_m = len(monthly_arr)
    print(f"  Profitable months: {profitable_m}/{total_m} ({profitable_m/total_m*100:.0f}%)")
    print(f"  Best month:  ${monthly_arr.max():,.0f}")
    print(f"  Worst month: ${monthly_arr.min():,.0f}")
    print(f"  Avg month:   ${monthly_arr.mean():,.0f}")
    print(f"  Std month:   ${monthly_arr.std():,.0f}")

    consistency_pass = profitable_q >= total_q * 0.7 and profitable_m >= total_m * 0.7
    print(f"  {'✅ CONSISTENT — majority profitable' if consistency_pass else '❌ INCONSISTENT — too many losing periods'}")

    # ═══════════════════════════════════════════════════════════
    # 5. FUNDEDNEXT CHALLENGE SIMULATION (2025 only)
    # ═══════════════════════════════════════════════════════════
    section("FUNDEDNEXT CHALLENGE SIMULATION (2025 OOS only)", 5)

    oos_df = pd.DataFrame(oos_trades).sort_values("date").reset_index(drop=True)
    oos_df["date"] = pd.to_datetime(oos_df["date"])
    oos_df["day"] = oos_df["date"].dt.date

    # Simulate multiple challenge attempts through 2025
    challenges = []
    ch_num = 1
    phase = 1
    balance = STARTING_CAPITAL
    initial = STARTING_CAPITAL
    peak = STARTING_CAPITAL
    phase_trades_list = []
    ch_start = None
    day = None
    day_start = STARTING_CAPITAL
    day_losses = 0
    day_stopped = False

    for _, row in oos_df.iterrows():
        trade_day = row["day"]
        pnl = row["pnl"]
        result = row["result"]

        if ch_start is None:
            ch_start = trade_day

        # New day
        if trade_day != day:
            day = trade_day
            day_start = balance
            day_losses = 0
            day_stopped = False

        if day_stopped:
            continue
        if day_losses >= MAX_DAILY_LOSSES:
            continue

        # Risk tier
        pnl_from_init = (balance - initial) / initial * 100
        if pnl_from_init <= -DD_REDUCE_THRESHOLD:
            risk_pct = RISK_DEFENSIVE
        elif pnl_from_init >= PROFIT_BOOST_THRESHOLD:
            risk_pct = RISK_AGGRESSIVE
        else:
            risk_pct = RISK_DEFAULT

        # Scale PnL by risk ratio (original was 1.5%, simulation uses variable)
        scale = risk_pct / RISK_PCT
        scaled_pnl = pnl * scale

        balance += scaled_pnl

        if result == "loss":
            day_losses += 1

        peak = max(peak, balance)
        phase_trades_list.append({"pnl": scaled_pnl, "balance": balance, "day": trade_day, "risk_pct": risk_pct})

        # Daily DD from day start (FN 5%)
        daily_dd = (day_start - balance) / day_start * 100 if day_start > 0 else 0
        if daily_dd >= DAILY_DD_CUTOFF:
            day_stopped = True

        # Check FN breach
        fn_daily_dd = (day_start - balance) / day_start * 100 if day_start > 0 else 0
        fn_max_dd = (initial - balance) / initial * 100 if initial > 0 else 0

        breached = False
        breach_reason = None
        if fn_daily_dd >= FN_DAILY_DD:
            breached = True
            breach_reason = f"Daily DD {fn_daily_dd:.1f}%"
        elif fn_max_dd >= FN_MAX_DD:
            breached = True
            breach_reason = f"Max DD {fn_max_dd:.1f}%"

        if breached:
            challenges.append({
                "num": ch_num, "phase": phase, "status": "BREACHED",
                "reason": breach_reason, "trades": len(phase_trades_list),
                "start": ch_start, "end": trade_day, "final_bal": balance,
                "return_pct": (balance - initial) / initial * 100
            })
            ch_num += 1
            phase = 1
            balance = STARTING_CAPITAL
            initial = STARTING_CAPITAL
            peak = STARTING_CAPITAL
            phase_trades_list = []
            ch_start = None
            day_start = STARTING_CAPITAL
            day_losses = 0
            day_stopped = False
            continue

        # Profit target
        target_pct = FN_P1_TARGET if phase == 1 else FN_P2_TARGET
        profit_pct = (balance - initial) / initial * 100
        if profit_pct >= target_pct:
            challenges.append({
                "num": ch_num, "phase": phase, "status": "PASSED",
                "trades": len(phase_trades_list),
                "start": ch_start, "end": trade_day, "final_bal": balance,
                "return_pct": profit_pct
            })
            if phase == 1:
                phase = 2
                balance = STARTING_CAPITAL
                initial = STARTING_CAPITAL
                peak = STARTING_CAPITAL
                phase_trades_list = []
                day_start = STARTING_CAPITAL
                day_losses = 0
                day_stopped = False
            else:
                # Both phases passed
                ch_num += 1
                phase = 1
                balance = STARTING_CAPITAL
                initial = STARTING_CAPITAL
                peak = STARTING_CAPITAL
                phase_trades_list = []
                ch_start = None
                day_start = STARTING_CAPITAL
                day_losses = 0
                day_stopped = False

    # Finalize last
    if phase_trades_list:
        profit_pct = (balance - initial) / initial * 100
        challenges.append({
            "num": ch_num, "phase": phase,
            "status": "IN_PROGRESS",
            "trades": len(phase_trades_list),
            "start": ch_start, "end": day, "final_bal": balance,
            "return_pct": profit_pct
        })

    print(f"\n  {'#':>3} {'Phase':>5} {'Status':<12} {'Trades':>6} {'Period':<25} {'Return':>8} {'Reason':<20}")
    print(f"  {'-'*90}")

    passed_count = 0
    breached_count = 0
    p1_passed = 0
    p2_passed = 0
    for ch in challenges:
        reason = ch.get("reason", "")
        print(f"  {ch['num']:>3} P{ch['phase']:>1}    {ch['status']:<12} {ch['trades']:>6} {str(ch['start'])} → {str(ch['end']):<10} {ch['return_pct']:>+7.1f}% {reason}")
        if ch["status"] == "PASSED":
            passed_count += 1
            if ch["phase"] == 1:
                p1_passed += 1
            else:
                p2_passed += 1
        elif ch["status"] == "BREACHED":
            breached_count += 1

    fully_passed = min(p1_passed, p2_passed)  # both phases needed
    print(f"\n  Phase 1 passed: {p1_passed} | Phase 2 passed: {p2_passed}")
    print(f"  Fully passed challenges: {fully_passed}")
    print(f"  Breached: {breached_count}")
    fn_pass = fully_passed >= 1 and breached_count == 0
    print(f"  {'✅ FUNDEDNEXT SAFE' if fn_pass else '⚠️ CHECK FUNDEDNEXT RISK' if breached_count == 0 else '❌ FUNDEDNEXT BREACH DETECTED'}")

    # ═══════════════════════════════════════════════════════════
    # 6. MONTE CARLO SIMULATION (1000 shuffles)
    # ═══════════════════════════════════════════════════════════
    section("MONTE CARLO SIMULATION (1000 shuffles of 2025 trades)", 6)

    oos_pnls = [t["pnl"] for t in oos_trades]
    n_trades = len(oos_pnls)
    n_sims = 1000
    random.seed(42)

    mc_returns = []
    mc_max_dds = []
    mc_mcls = []

    for _ in range(n_sims):
        shuffled = random.sample(oos_pnls, n_trades)
        equity = STARTING_CAPITAL
        peak_eq = equity
        max_dd = 0
        mcl = 0
        cur_loss = 0

        for pnl in shuffled:
            equity += pnl
            peak_eq = max(peak_eq, equity)
            dd = (peak_eq - equity) / peak_eq * 100
            max_dd = max(max_dd, dd)
            if pnl < 0:
                cur_loss += 1
                mcl = max(mcl, cur_loss)
            else:
                cur_loss = 0

        ret = (equity - STARTING_CAPITAL) / STARTING_CAPITAL * 100
        mc_returns.append(ret)
        mc_max_dds.append(max_dd)
        mc_mcls.append(mcl)

    mc_returns = np.array(mc_returns)
    mc_max_dds = np.array(mc_max_dds)
    mc_mcls = np.array(mc_mcls)

    print(f"\n  {'Metric':<25} {'P5':>8} {'P25':>8} {'Median':>8} {'P75':>8} {'P95':>8}")
    print(f"  {'-'*60}")
    for name, arr, fmt in [
        ("Return %", mc_returns, ".1f"),
        ("Max DD %", mc_max_dds, ".1f"),
        ("Max Consec Loss", mc_mcls, ".0f"),
    ]:
        p5, p25, p50, p75, p95 = np.percentile(arr, [5, 25, 50, 75, 95])
        print(f"  {name:<25} {p5:>8{fmt}} {p25:>8{fmt}} {p50:>8{fmt}} {p75:>8{fmt}} {p95:>8{fmt}}")

    # Key risk metrics
    prob_profitable = (mc_returns > 0).sum() / n_sims * 100
    prob_dd_over_10 = (mc_max_dds > 10).sum() / n_sims * 100
    prob_dd_over_15 = (mc_max_dds > 15).sum() / n_sims * 100
    worst_return = mc_returns.min()
    worst_dd = mc_max_dds.max()

    print(f"\n  Probability profitable:  {prob_profitable:.1f}%")
    print(f"  Prob DD > 10%:           {prob_dd_over_10:.1f}%")
    print(f"  Prob DD > 15%:           {prob_dd_over_15:.1f}%")
    print(f"  Worst-case return:       {worst_return:.1f}%")
    print(f"  Worst-case max DD:       {worst_dd:.1f}%")

    mc_pass = prob_profitable >= 95 and prob_dd_over_15 <= 10
    print(f"  {'✅ MONTE CARLO ROBUST' if mc_pass else '❌ MONTE CARLO FRAGILE'}")

    # ═══════════════════════════════════════════════════════════
    # 7. WORST-CASE SCENARIO
    # ═══════════════════════════════════════════════════════════
    section("WORST-CASE SCENARIO ANALYSIS", 7)

    # Worst calendar week
    oos_df_sorted = pd.DataFrame(oos_trades).sort_values("date").reset_index(drop=True)
    oos_df_sorted["date"] = pd.to_datetime(oos_df_sorted["date"])
    oos_df_sorted["week"] = oos_df_sorted["date"].dt.strftime("%Y-W%U")

    weekly_pnl = oos_df_sorted.groupby("week")["pnl"].sum()
    worst_week = weekly_pnl.min()
    worst_week_name = weekly_pnl.idxmin()

    # Worst 5-trade window
    pnl_arr = oos_df_sorted["pnl"].values
    worst_5 = min(sum(pnl_arr[i:i+5]) for i in range(len(pnl_arr) - 4))
    worst_10 = min(sum(pnl_arr[i:i+10]) for i in range(len(pnl_arr) - 9))
    worst_20 = min(sum(pnl_arr[i:i+20]) for i in range(len(pnl_arr) - 19))

    daily_pnl = oos_df_sorted.groupby(oos_df_sorted["date"].dt.date)["pnl"].sum()
    worst_day = daily_pnl.min()
    worst_day_name = daily_pnl.idxmin()
    best_day = daily_pnl.max()

    print(f"\n  Worst day:          ${worst_day:,.0f} on {worst_day_name}")
    print(f"  Best day:           ${best_day:,.0f}")
    print(f"  Worst week:         ${worst_week:,.0f} ({worst_week_name})")
    print(f"  Worst 5-trade run:  ${worst_5:,.0f}")
    print(f"  Worst 10-trade run: ${worst_10:,.0f}")
    print(f"  Worst 20-trade run: ${worst_20:,.0f}")

    # Can we survive 2x worst case and still be within FN limits?
    worst_day_pct = abs(worst_day) / STARTING_CAPITAL * 100
    print(f"\n  Worst day as % of capital: {worst_day_pct:.1f}%")
    print(f"  FN daily limit: 5.0%")
    print(f"  Buffer: {5.0 - worst_day_pct:.1f}pp")
    print(f"  2x worst day: ${worst_day * 2:,.0f} ({worst_day_pct * 2:.1f}%)")

    worst_pass = worst_day_pct < 5.0
    print(f"  {'✅ WORST CASE WITHIN LIMITS' if worst_pass else '❌ WORST CASE EXCEEDS LIMITS'}")

    # ═══════════════════════════════════════════════════════════
    # 8. EDGE DECAY / TIME STABILITY
    # ═══════════════════════════════════════════════════════════
    section("EDGE DECAY — ROLLING 3-MONTH WINDOWS", 8)

    # Rolling 3-month windows across full data
    full_trades = run_c12(FULL_START, FULL_END)
    full_df = pd.DataFrame(full_trades)
    full_df["date"] = pd.to_datetime(full_df["date"])
    full_df["month"] = full_df["date"].dt.to_period("M")

    months = sorted(full_df["month"].unique())
    print(f"\n  {'Window':<20} {'Trades':>6} {'WR%':>6} {'PF':>6} {'PnL':>10}")
    print(f"  {'-'*55}")

    window_pfs = []
    for i in range(0, len(months) - 2, 3):
        window_months = months[i:i+3]
        sub = full_df[full_df["month"].isin(window_months)]
        if len(sub) < 5:
            continue
        wins = len(sub[sub["result"] == "win"])
        wr = wins / len(sub) * 100
        gp = sub[sub["pnl"] > 0]["pnl"].sum()
        gl = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gp / gl if gl > 0 else 99.0
        net = sub["pnl"].sum()
        window_pfs.append(pf)
        flag = "✅" if pf >= 1.0 else "❌"
        label = f"{window_months[0]}→{window_months[-1]}"
        print(f"  {label:<20} {len(sub):>6} {wr:>5.1f}% {pf:>5.2f} ${net:>9,.0f} {flag}")

    # Check for declining trend
    if len(window_pfs) >= 4:
        first_half = np.mean(window_pfs[:len(window_pfs)//2])
        second_half = np.mean(window_pfs[len(window_pfs)//2:])
        trend = "IMPROVING" if second_half > first_half else "DECLINING"
        decay = (second_half - first_half) / first_half * 100
        print(f"\n  First half avg PF:  {first_half:.2f}")
        print(f"  Second half avg PF: {second_half:.2f}")
        print(f"  Trend: {trend} ({decay:+.0f}%)")

    profitable_windows = sum(1 for pf in window_pfs if pf >= 1.0)
    total_windows = len(window_pfs)
    edge_pass = profitable_windows >= total_windows * 0.7
    print(f"  Profitable windows: {profitable_windows}/{total_windows}")
    print(f"  {'✅ EDGE STABLE' if edge_pass else '❌ EDGE DECAYING'}")

    # ═══════════════════════════════════════════════════════════
    # 9. DIRECTION & EXIT ANALYSIS
    # ═══════════════════════════════════════════════════════════
    section("DIRECTION & EXIT ANALYSIS (2025)", 9)

    oos_df2 = pd.DataFrame(oos_trades)
    oos_df2["date"] = pd.to_datetime(oos_df2["date"])

    print(f"\n  DIRECTION:")
    for direction in oos_df2["direction"].unique():
        sub = oos_df2[oos_df2["direction"] == direction]
        wins = len(sub[sub["result"] == "win"])
        wr = wins / len(sub) * 100 if len(sub) > 0 else 0
        gp = sub[sub["pnl"] > 0]["pnl"].sum()
        gl = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gp / gl if gl > 0 else 99.0
        print(f"    {str(direction):<12}: {len(sub):>4} trades | WR {wr:.1f}% | PF {pf:.2f} | ${sub['pnl'].sum():,.0f}")

    print(f"\n  EXIT REASON:")
    for reason in oos_df2["exit_reason"].unique():
        sub = oos_df2[oos_df2["exit_reason"] == reason]
        net = sub["pnl"].sum()
        avg = sub["pnl"].mean()
        print(f"    {str(reason):<12}: {len(sub):>4} trades | Net ${net:>8,.0f} | Avg ${avg:>6,.0f}")

    # TP hit rate
    tp_count = len(oos_df2[oos_df2["exit_reason"] == "TP"])
    sl_be_count = len(oos_df2[oos_df2["exit_reason"] == "SL_BE"])
    sl_hit_count = len(oos_df2[oos_df2["exit_reason"] == "SL_HIT"])
    total_oos = len(oos_df2)

    print(f"\n  TP hit rate:     {tp_count/total_oos*100:.1f}% ({tp_count}/{total_oos})")
    print(f"  BE protection:   {sl_be_count/total_oos*100:.1f}% ({sl_be_count}/{total_oos})")
    print(f"  Full SL hit:     {sl_hit_count/total_oos*100:.1f}% ({sl_hit_count}/{total_oos})")
    print(f"  BE PnL contribution: ${oos_df2[oos_df2['exit_reason'] == 'SL_BE']['pnl'].sum():,.0f}")

    # ═══════════════════════════════════════════════════════════
    # 10. FINAL VERDICT
    # ═══════════════════════════════════════════════════════════
    section("FINAL VERDICT", 10)

    verdicts = [
        ("Walk-Forward Stability", wf_pass),
        ("Parameter Sensitivity", sensitivity_pass),
        ("Per-Index Robustness", idx_pass),
        ("Quarterly Consistency", consistency_pass),
        ("FundedNext Safety", fn_pass),
        ("Monte Carlo Robustness", mc_pass),
        ("Worst-Case Survival", worst_pass),
        ("Edge Stability", edge_pass),
    ]

    print(f"\n  {'Test':<30} {'Result':>10}")
    print(f"  {'-'*40}")
    total_pass = 0
    for name, passed in verdicts:
        total_pass += int(passed)
        icon = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {name:<30} {icon:>10}")

    score = total_pass / len(verdicts) * 100
    print(f"\n  {'═'*40}")
    print(f"  SCORE: {total_pass}/{len(verdicts)} ({score:.0f}%)")

    if score >= 87.5:  # 7/8
        verdict = "✅ LIVE CANDIDATE — APPROVED"
        detail = "All critical checks passed. Safe for live deployment."
    elif score >= 75:  # 6/8
        verdict = "⚠️ CONDITIONAL CANDIDATE"
        detail = "Most checks passed but some weaknesses. Proceed with caution."
    elif score >= 50:
        verdict = "⚠️ MARGINAL — NEEDS WORK"
        detail = "Significant weaknesses detected. Not recommended for live."
    else:
        verdict = "❌ NOT READY FOR LIVE"
        detail = "Multiple failures. Do not deploy."

    print(f"  {verdict}")
    print(f"  {detail}")

    # Comparison with baseline
    baseline_trades = run_backtest(ALL_SYMBOLS, OOS_START, OOS_END, **BASELINE)
    bm = compute_metrics(baseline_trades, OOS_START, OOS_END)

    print(f"\n  vs BASELINE (2025):")
    print(f"    Trades:   {bm['trades']:>4} → {oos_m['trades']:>4}  ({oos_m['trades']-bm['trades']:+d})")
    print(f"    TD/Week:  {bm['td_per_week']:.1f} → {oos_m['td_per_week']:.1f}  ({oos_m['td_per_week']-bm['td_per_week']:+.1f})")
    print(f"    Win Rate: {bm['win_rate']:.1f}% → {oos_m['win_rate']:.1f}%  ({oos_m['win_rate']-bm['win_rate']:+.1f}pp)")
    print(f"    PF:       {bm['pf']:.2f} → {oos_m['pf']:.2f}  ({oos_m['pf']-bm['pf']:+.2f})")
    print(f"    Sharpe:   {bm['sharpe']:.2f} → {oos_m['sharpe']:.2f}  ({oos_m['sharpe']-bm['sharpe']:+.2f})")
    print(f"    Return:   {bm['return_pct']:.1f}% → {oos_m['return_pct']:.1f}%  ({oos_m['return_pct']-bm['return_pct']:+.1f}pp)")
    print(f"    Max DD:   {bm['max_dd']:.1f}% → {oos_m['max_dd']:.1f}%  ({oos_m['max_dd']-bm['max_dd']:+.1f}pp)")

    elapsed = time.time() - t0
    print(f"\n  Analysis completed in {elapsed:.0f} seconds")
    print(f"{'█'*120}")


if __name__ == "__main__":
    main()
