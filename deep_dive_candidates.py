"""
Deep-dive analysis of top optimizer candidates.
- Monthly breakdown (win rate, PF, trade count)
- Per-index contribution
- Drawdown profile & recovery
- Fine-tuned hybrid combos around best params
"""
import sys, time
from pathlib import Path
from datetime import date
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from optimize_timeframes import (
    run_backtest, compute_metrics, ALL_SYMBOLS,
    IS_START, IS_END, OOS_START, OOS_END,
    STARTING_CAPITAL, RISK_PCT
)

# ── Candidates to deep-dive ──
CANDIDATES = {
    "BASELINE": dict(orb=15, entry=2, bkout=60, htf="h1_20_50", disp=1.7, body=0.65, close_s=0.30, margin=0.10),
    "#12 Loose disp 1.3": dict(orb=15, entry=2, bkout=60, htf="h1_20_50", disp=1.3, body=0.60, close_s=0.25, margin=0.05),
    "#21 10m loose1.5":   dict(orb=10, entry=2, bkout=60, htf="h1_20_50", disp=1.5, body=0.60, close_s=0.30, margin=0.08),
    "#15 10m 3h loose":   dict(orb=10, entry=3, bkout=90, htf="h1_20_50", disp=1.5, body=0.60, close_s=0.25, margin=0.05),
    "#7  No HTF":         dict(orb=15, entry=2, bkout=60, htf="none",     disp=1.7, body=0.65, close_s=0.30, margin=0.10),
}

# ── Fine-tuned hybrids around #12 ──
HYBRIDS = {
    "H1: disp1.3 body0.65":     dict(orb=15, entry=2, bkout=60, htf="h1_20_50", disp=1.3, body=0.65, close_s=0.30, margin=0.05),
    "H2: disp1.3 close0.30":    dict(orb=15, entry=2, bkout=60, htf="h1_20_50", disp=1.3, body=0.60, close_s=0.30, margin=0.05),
    "H3: disp1.3 margin0.08":   dict(orb=15, entry=2, bkout=60, htf="h1_20_50", disp=1.3, body=0.60, close_s=0.25, margin=0.08),
    "H4: disp1.4 body0.60":     dict(orb=15, entry=2, bkout=60, htf="h1_20_50", disp=1.4, body=0.60, close_s=0.25, margin=0.05),
    "H5: disp1.4 body0.65":     dict(orb=15, entry=2, bkout=60, htf="h1_20_50", disp=1.4, body=0.65, close_s=0.30, margin=0.08),
    "H6: disp1.3 3h entry":     dict(orb=15, entry=3, bkout=60, htf="h1_20_50", disp=1.3, body=0.60, close_s=0.25, margin=0.05),
    "H7: disp1.3 90m bkout":    dict(orb=15, entry=2, bkout=90, htf="h1_20_50", disp=1.3, body=0.60, close_s=0.25, margin=0.05),
    "H8: disp1.3 H4 trend":     dict(orb=15, entry=2, bkout=60, htf="h4_20_50", disp=1.3, body=0.60, close_s=0.25, margin=0.05),
    "H9: disp1.3 close0.30 m08":dict(orb=15, entry=2, bkout=60, htf="h1_20_50", disp=1.3, body=0.60, close_s=0.30, margin=0.08),
}


def run_one(params, start, end):
    """Run a single combo and return trades."""
    return run_backtest(
        ALL_SYMBOLS, start, end,
        params["orb"], params["entry"], params["bkout"], params["htf"],
        params["disp"], params["body"], params["close_s"], params["margin"]
    )


def per_index_breakdown(trades):
    """Per-index stats."""
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    results = {}
    for sym in ALL_SYMBOLS:
        sub = df[df["symbol"] == sym]
        if len(sub) == 0:
            results[sym] = {"trades": 0, "wr": 0, "pf": 0, "pnl": 0}
            continue
        wins = len(sub[sub["result"] == "win"])
        wr = wins / len(sub) * 100
        gross_p = sub[sub["pnl"] > 0]["pnl"].sum()
        gross_l = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gross_p / gross_l if gross_l > 0 else 99.0
        results[sym] = {"trades": len(sub), "wr": round(wr, 1), "pf": round(pf, 2), "pnl": round(sub["pnl"].sum(), 2)}
    return results


def monthly_breakdown(trades, start, end):
    """Monthly stats: trades, WR, PF, PnL."""
    if not trades:
        return []
    df = pd.DataFrame(trades)
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M")
    
    months = []
    for period in sorted(df["month"].unique()):
        sub = df[df["month"] == period]
        wins = len(sub[sub["result"] == "win"])
        wr = wins / len(sub) * 100 if len(sub) > 0 else 0
        gross_p = sub[sub["pnl"] > 0]["pnl"].sum()
        gross_l = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gross_p / gross_l if gross_l > 0 else 99.0
        td = sub["date"].dt.date.nunique()
        months.append({
            "month": str(period),
            "trades": len(sub),
            "trading_days": td,
            "wr": round(wr, 1),
            "pf": round(pf, 2),
            "pnl": round(sub["pnl"].sum(), 2)
        })
    return months


def drawdown_profile(trades):
    """Compute equity curve and drawdown stats."""
    if not trades:
        return {}
    df = pd.DataFrame(trades).sort_values("date")
    equity = STARTING_CAPITAL + df["pnl"].cumsum()
    peak = equity.cummax()
    dd = (peak - equity) / peak * 100
    
    max_dd = dd.max()
    max_dd_idx = dd.idxmax()
    max_dd_date = df.loc[max_dd_idx, "date"] if max_dd_idx in df.index else "N/A"
    
    # Time in drawdown
    in_dd = (dd > 1.0).sum()
    pct_time_in_dd = in_dd / len(dd) * 100
    
    # Recovery time (trades to recover from max DD)
    recovery = 0
    if max_dd > 0:
        dd_after = dd.loc[max_dd_idx:]
        recovered = dd_after[dd_after < 0.5]
        if len(recovered) > 0:
            recovery = recovered.index[0] - max_dd_idx
        else:
            recovery = len(dd_after)  # didn't fully recover
    
    return {
        "max_dd_pct": round(max_dd, 1),
        "max_dd_date": str(max_dd_date)[:10] if max_dd_date != "N/A" else "N/A",
        "pct_time_in_dd": round(pct_time_in_dd, 1),
        "recovery_trades": recovery,
    }


def print_section(title):
    print(f"\n{'='*120}")
    print(f"  {title}")
    print(f"{'='*120}")


def main():
    print("=" * 120)
    print("DEEP-DIVE: TOP OPTIMIZER CANDIDATES")
    print(f"IS: {IS_START} to {IS_END}  |  OOS: {OOS_START} to {OOS_END}")
    print("=" * 120)

    # ═══════════════════════════════════════════════════════
    # SECTION 1: Full-period analysis of top candidates
    # ═══════════════════════════════════════════════════════
    print_section("1. CANDIDATE OVERVIEW (IS + OOS combined 2023-2025)")
    
    full_start = date(2023, 1, 1)
    full_end = date(2025, 12, 31)
    
    for name, params in CANDIDATES.items():
        print(f"\n  ── {name} ──")
        
        # IS trades
        is_trades = run_one(params, IS_START, IS_END)
        is_m = compute_metrics(is_trades, IS_START, IS_END)
        
        # OOS trades
        oos_trades = run_one(params, OOS_START, OOS_END)
        oos_m = compute_metrics(oos_trades, OOS_START, OOS_END)
        
        if not is_m or not oos_m:
            print("    Insufficient trades")
            continue
        
        # Summary line
        print(f"    IS  : {is_m['trades']:>3} trades | {is_m['td_per_week']:.1f} TD/wk | WR {is_m['win_rate']:.1f}% | PF {is_m['pf']:.2f} | Sharpe {is_m['sharpe']:.2f} | MaxDD {is_m['max_dd']:.1f}% | ${is_m['net_pnl']:,.0f}")
        print(f"    OOS : {oos_m['trades']:>3} trades | {oos_m['td_per_week']:.1f} TD/wk | WR {oos_m['win_rate']:.1f}% | PF {oos_m['pf']:.2f} | Sharpe {oos_m['sharpe']:.2f} | MaxDD {oos_m['max_dd']:.1f}% | ${oos_m['net_pnl']:,.0f}")
        
        # Degradation check
        pf_deg = (oos_m["pf"] - is_m["pf"]) / is_m["pf"] * 100
        wr_deg = oos_m["win_rate"] - is_m["win_rate"]
        print(f"    Δ   : PF {'+'if pf_deg>0 else ''}{pf_deg:.0f}% | WR {'+'if wr_deg>0 else ''}{wr_deg:.1f}pp | {'✅ OOS improves' if pf_deg > 0 else '⚠️ OOS degrades'}")
        
        # Per-index
        print(f"\n    Per-Index (IS+OOS combined):")
        all_trades = is_trades + oos_trades
        idx_stats = per_index_breakdown(all_trades)
        for sym, s in idx_stats.items():
            bar = "█" * int(s["trades"] / 5)
            print(f"      {sym:>8}: {s['trades']:>4} trades | WR {s['wr']:>5.1f}% | PF {s['pf']:>5.2f} | ${s['pnl']:>8,.0f} | {bar}")
        
        # Drawdown
        dd_info = drawdown_profile(all_trades)
        print(f"\n    Drawdown: Max {dd_info['max_dd_pct']:.1f}% around {dd_info['max_dd_date']} | {dd_info['pct_time_in_dd']:.0f}% of time in DD | Recovery: {dd_info['recovery_trades']} trades")

    # ═══════════════════════════════════════════════════════
    # SECTION 2: Monthly breakdown for #12 (best candidate)
    # ═══════════════════════════════════════════════════════
    print_section("2. MONTHLY BREAKDOWN: #12 (Loose disp 1.3)")
    
    params_12 = CANDIDATES["#12 Loose disp 1.3"]
    trades_12 = run_one(params_12, date(2023, 1, 1), date(2025, 12, 31))
    monthly = monthly_breakdown(trades_12, date(2023, 1, 1), date(2025, 12, 31))
    
    print(f"\n  {'Month':<10} {'Trades':>6} {'TDs':>4} {'WR%':>6} {'PF':>6} {'PnL':>10}")
    print(f"  {'-'*46}")
    
    cum_pnl = 0
    profitable_months = 0
    for m in monthly:
        cum_pnl += m["pnl"]
        status = "✅" if m["pnl"] > 0 else "❌"
        if m["pnl"] > 0:
            profitable_months += 1
        print(f"  {m['month']:<10} {m['trades']:>6} {m['trading_days']:>4} {m['wr']:>5.1f}% {m['pf']:>5.2f} ${m['pnl']:>9,.0f} {status}")
    
    total_months = len(monthly)
    pct_profitable = profitable_months / total_months * 100 if total_months > 0 else 0
    print(f"\n  Profitable months: {profitable_months}/{total_months} ({pct_profitable:.0f}%)")
    
    # IS vs OOS monthly averages
    is_months = [m for m in monthly if m["month"] < "2025"]
    oos_months = [m for m in monthly if m["month"] >= "2025"]
    
    if is_months:
        is_avg_trades = sum(m["trades"] for m in is_months) / len(is_months)
        is_avg_pnl = sum(m["pnl"] for m in is_months) / len(is_months)
        print(f"  IS avg:  {is_avg_trades:.1f} trades/month, ${is_avg_pnl:,.0f}/month")
    if oos_months:
        oos_avg_trades = sum(m["trades"] for m in oos_months) / len(oos_months)
        oos_avg_pnl = sum(m["pnl"] for m in oos_months) / len(oos_months)
        print(f"  OOS avg: {oos_avg_trades:.1f} trades/month, ${oos_avg_pnl:,.0f}/month")

    # ═══════════════════════════════════════════════════════
    # SECTION 3: Fine-tuned hybrids around #12
    # ═══════════════════════════════════════════════════════
    print_section("3. FINE-TUNED HYBRIDS (around #12 parameters)")
    
    print(f"\n  {'Label':<30} {'IS-Tr':>5} {'IS-TD':>5} {'IS-WR':>6} {'IS-PF':>6} {'IS-Sh':>6} {'IS-DD':>6} | {'OOS-Tr':>6} {'OOS-TD':>6} {'OOS-WR':>7} {'OOS-PF':>7} {'OOS-Sh':>7} {'OOS-DD':>7} {'OOS-Ret':>8}")
    print(f"  {'-'*130}")
    
    # Include baseline and #12 for reference
    ref_combos = {"BASELINE": CANDIDATES["BASELINE"], "#12 Original": CANDIDATES["#12 Loose disp 1.3"]}
    all_combos = {**ref_combos, **HYBRIDS}
    
    best_score = 0
    best_name = ""
    
    for name, params in all_combos.items():
        is_trades = run_one(params, IS_START, IS_END)
        is_m = compute_metrics(is_trades, IS_START, IS_END)
        oos_trades = run_one(params, OOS_START, OOS_END)
        oos_m = compute_metrics(oos_trades, OOS_START, OOS_END)
        
        if not is_m or not oos_m:
            print(f"  {name:<30}  -- insufficient trades --")
            continue
        
        # Composite score: balance frequency, quality, and consistency
        # Higher IS/OOS agreement = better (less overfit)
        freq_score = min(oos_m["td_per_week"] / 3.0, 1.0)  # target 3 TD/wk
        pf_score = min(oos_m["pf"] / 1.5, 1.0)              # target PF 1.5
        wr_score = min(oos_m["win_rate"] / 55.0, 1.0)       # target WR 55%
        sh_score = min(oos_m["sharpe"] / 3.0, 1.0)          # target Sharpe 3.0
        dd_score = max(0, 1.0 - oos_m["max_dd"] / 15.0)     # penalize >15% DD
        # Consistency bonus: OOS PF within 30% of IS PF
        pf_consistency = max(0, 1.0 - abs(oos_m["pf"] - is_m["pf"]) / is_m["pf"])
        
        score = (freq_score * 0.30 + pf_score * 0.25 + wr_score * 0.15 + 
                 sh_score * 0.15 + dd_score * 0.10 + pf_consistency * 0.05)
        
        flag = ""
        if score > best_score:
            best_score = score
            best_name = name
            flag = " ★"
        
        print(f"  {name:<30} {is_m['trades']:>5} {is_m['td_per_week']:>4.1f} {is_m['win_rate']:>5.1f}% {is_m['pf']:>5.2f} {is_m['sharpe']:>5.2f} {is_m['max_dd']:>5.1f}% | "
              f"{oos_m['trades']:>6} {oos_m['td_per_week']:>5.1f} {oos_m['win_rate']:>6.1f}% {oos_m['pf']:>6.2f} {oos_m['sharpe']:>6.2f} {oos_m['max_dd']:>6.1f}% {oos_m['return_pct']:>7.1f}%{flag}")
    
    print(f"\n  ★ Best composite score: {best_name} (score: {best_score:.3f})")

    # ═══════════════════════════════════════════════════════
    # SECTION 4: FundedNext risk check on best candidate
    # ═══════════════════════════════════════════════════════
    print_section("4. FUNDEDNEXT RISK CHECK")
    
    # Check worst daily PnL for #12
    params_best = CANDIDATES["#12 Loose disp 1.3"]
    all_trades = run_one(params_best, date(2023, 1, 1), date(2025, 12, 31))
    df = pd.DataFrame(all_trades)
    df["date"] = pd.to_datetime(df["date"])
    df["day"] = df["date"].dt.date
    
    daily_pnl = df.groupby("day")["pnl"].sum()
    
    worst_day = daily_pnl.min()
    best_day = daily_pnl.max()
    worst_day_pct = worst_day / STARTING_CAPITAL * 100
    
    # Check daily loss limit (5% of initial balance)
    daily_limit = STARTING_CAPITAL * 0.05  # $300
    violations = daily_pnl[daily_pnl < -daily_limit]
    
    # Max trades per day
    trades_per_day = df.groupby("day").size()
    max_trades_day = trades_per_day.max()
    avg_trades_day = trades_per_day.mean()
    
    print(f"\n  Daily PnL stats:")
    print(f"    Worst day:   ${worst_day:,.0f} ({worst_day_pct:.1f}% of capital)")
    print(f"    Best day:    ${best_day:,.0f}")
    print(f"    Avg daily:   ${daily_pnl.mean():,.0f}")
    print(f"    Daily limit: ${daily_limit:,.0f} (-5%)")
    print(f"    Violations:  {len(violations)} days exceed daily loss limit")
    if len(violations) > 0:
        print(f"    Worst violations:")
        for d, pnl in violations.sort_values().head(5).items():
            print(f"      {d}: ${pnl:,.0f} ({pnl/STARTING_CAPITAL*100:.1f}%)")
    
    print(f"\n  Trades per day:")
    print(f"    Max:  {max_trades_day}")
    print(f"    Avg:  {avg_trades_day:.1f}")
    print(f"    Days with 3+ trades: {(trades_per_day >= 3).sum()}")
    print(f"    Days with 4+ trades: {(trades_per_day >= 4).sum()}")

    # ═══════════════════════════════════════════════════════
    # SECTION 5: RECOMMENDATION
    # ═══════════════════════════════════════════════════════
    print_section("5. FINAL RECOMMENDATION")
    
    # Re-run #12 for clean summary
    is_m = compute_metrics(run_one(params_best, IS_START, IS_END), IS_START, IS_END)
    oos_m = compute_metrics(run_one(params_best, OOS_START, OOS_END), OOS_START, OOS_END)
    
    print(f"""
  RECOMMENDED: #12 — 15m ORB, Loose displacement (1.3)
  
  Parameter changes from BASELINE:
    disp_mult:      1.7 → 1.3  (accept slightly less impulsive breakouts)
    body_min:       0.65 → 0.60 (slightly more wicks allowed)
    close_strength: 0.30 → 0.25 (slightly more flexible close position)
    breakout_margin:0.10 → 0.05 (accept closer breakouts beyond ORB)
  
  Unchanged: 15m ORB | H1 20/50 EMA trend | 2h entry window | 60m breakout window
  
  Expected performance:
    IS  (2023-2024): {is_m['td_per_week']:.1f} TD/wk | WR {is_m['win_rate']:.1f}% | PF {is_m['pf']:.2f} | Sharpe {is_m['sharpe']:.2f} | MaxDD {is_m['max_dd']:.1f}%
    OOS (2025):      {oos_m['td_per_week']:.1f} TD/wk | WR {oos_m['win_rate']:.1f}% | PF {oos_m['pf']:.2f} | Sharpe {oos_m['sharpe']:.2f} | MaxDD {oos_m['max_dd']:.1f}%
  
  Trade frequency: {is_m['td_per_week']:.1f} → {oos_m['td_per_week']:.1f} TD/wk (target was 3.0)
  Risk status: {'✅ Within FundedNext limits' if len(violations) == 0 else '⚠️ ' + str(len(violations)) + ' daily limit violations'}
""")


if __name__ == "__main__":
    main()
