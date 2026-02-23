"""
Monte Carlo Simulation & Stress Tests for ICT ORB Clean Breakout Strategy
=========================================================================
Uses actual trade log from 3-year backtest (551 trades) to:
1. Monte Carlo equity curve simulation (10,000 iterations)
2. Drawdown distribution analysis
3. FundedNext challenge pass rate simulation
4. Parameter degradation stress test
5. Worst-case streak analysis
6. Cost sensitivity analysis
7. Regime-based analysis (monthly bucketing)
"""

import numpy as np
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

# ============================================================
# CONFIG
# ============================================================
STARTING_BALANCE = 6000.0
MC_ITERATIONS = 10000
FN_PHASE1_TARGET = 0.08   # 8%
FN_PHASE2_TARGET = 0.05   # 5%
FN_DAILY_DD_LIMIT = 0.05  # 5%
FN_MAX_DD_LIMIT = 0.10    # 10%
RISK_TIERS = {1: 0.015, 2: 0.0075, 3: 0.005}
DD_THRESHOLDS = {1: 0.03, 2: 0.05}  # tier transitions

# ============================================================
# LOAD TRADE DATA
# ============================================================
def load_trades():
    trades_path = Path(__file__).parent / "reports" / "live_simulation" / "portfolio_trades.csv"
    df = pd.read_csv(trades_path)
    df["date"] = pd.to_datetime(df["date"])
    return df

# ============================================================
# 1. MONTE CARLO — EQUITY CURVE SIMULATION
# ============================================================
def monte_carlo_equity(trades_df, n_iter=MC_ITERATIONS, starting_bal=STARTING_BALANCE):
    """Shuffle trade order, replay with adaptive risk, collect equity curves."""
    # Extract R-multiples (pnl / risk_amount)
    r_multiples = (trades_df["pnl"] / trades_df["risk_amount"].replace(0, np.nan)).dropna().values
    n_trades = len(r_multiples)
    
    final_balances = np.zeros(n_iter)
    max_drawdowns = np.zeros(n_iter)
    max_daily_dds = np.zeros(n_iter)
    peak_balances = np.zeros(n_iter)
    min_balances = np.zeros(n_iter)
    max_consecutive_losses = np.zeros(n_iter, dtype=int)
    
    # Store percentile equity curves
    all_equity = np.zeros((n_iter, n_trades + 1))
    
    for i in range(n_iter):
        shuffled = np.random.permutation(r_multiples)
        balance = starting_bal
        peak = starting_bal
        max_dd = 0.0
        day_start = starting_bal
        max_day_dd = 0.0
        consec_loss = 0
        max_consec = 0
        
        all_equity[i, 0] = balance
        
        for j, r in enumerate(shuffled):
            # Adaptive risk tier
            dd_pct = (peak - balance) / peak if peak > 0 else 0
            if dd_pct < DD_THRESHOLDS[1]:
                risk_pct = RISK_TIERS[1]
            elif dd_pct < DD_THRESHOLDS[2]:
                risk_pct = RISK_TIERS[2]
            else:
                risk_pct = RISK_TIERS[3]
            
            risk_amount = balance * risk_pct
            pnl = r * risk_amount
            balance += pnl
            
            if balance > peak:
                peak = balance
            
            dd = (peak - balance) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
            
            # Approximate daily DD (every ~3.7 trades = 1 day)
            if j % 4 == 0:
                day_dd = (day_start - balance) / day_start if day_start > 0 else 0
                if day_dd > max_day_dd:
                    max_day_dd = day_dd
                day_start = balance
            
            # Consecutive losses
            if r < 0:
                consec_loss += 1
                if consec_loss > max_consec:
                    max_consec = consec_loss
            else:
                consec_loss = 0
            
            all_equity[i, j + 1] = balance
        
        final_balances[i] = balance
        max_drawdowns[i] = max_dd
        max_daily_dds[i] = max_day_dd
        peak_balances[i] = peak
        min_balances[i] = np.min(all_equity[i])
        max_consecutive_losses[i] = max_consec
    
    return {
        "final_balances": final_balances,
        "max_drawdowns": max_drawdowns,
        "max_daily_dds": max_daily_dds,
        "peak_balances": peak_balances,
        "min_balances": min_balances,
        "max_consecutive_losses": max_consecutive_losses,
        "all_equity": all_equity,
        "n_trades": n_trades,
    }

# ============================================================
# 2. FUNDEDNEXT CHALLENGE MC SIMULATION
# ============================================================
def fn_challenge_mc(trades_df, n_iter=MC_ITERATIONS, starting_bal=STARTING_BALANCE):
    """Simulate FN challenge pass/fail with shuffled trades."""
    r_multiples = (trades_df["pnl"] / trades_df["risk_amount"].replace(0, np.nan)).dropna().values
    
    phase1_passes = 0
    phase2_passes = 0
    full_passes = 0
    breaches = 0
    pass_durations = []
    
    for i in range(n_iter):
        shuffled = np.random.permutation(r_multiples)
        
        # Phase 1
        balance = starting_bal
        peak = starting_bal
        initial = starting_bal
        target = initial * (1 + FN_PHASE1_TARGET)
        p1_passed = False
        p1_trades = 0
        
        for r in shuffled:
            dd_pct = (peak - balance) / initial if peak > 0 else 0
            if dd_pct < DD_THRESHOLDS[1]:
                risk_pct = RISK_TIERS[1]
            elif dd_pct < DD_THRESHOLDS[2]:
                risk_pct = RISK_TIERS[2]
            else:
                risk_pct = RISK_TIERS[3]
            
            risk_amount = balance * risk_pct
            pnl = r * risk_amount
            balance += pnl
            p1_trades += 1
            
            if balance > peak:
                peak = balance
            
            # Check breach (DD from initial for FN Stellar)
            overall_dd = (initial - balance) / initial if initial > 0 else 0
            if overall_dd >= FN_MAX_DD_LIMIT:
                break
            
            if balance >= target:
                p1_passed = True
                break
        
        if not p1_passed:
            breaches += 1
            continue
        
        phase1_passes += 1
        
        # Phase 2
        balance_p2 = starting_bal  # Reset for phase 2
        peak_p2 = starting_bal
        initial_p2 = starting_bal
        target_p2 = initial_p2 * (1 + FN_PHASE2_TARGET)
        p2_passed = False
        p2_trades = 0
        
        shuffled2 = np.random.permutation(r_multiples)
        for r in shuffled2:
            dd_pct = (peak_p2 - balance_p2) / initial_p2 if peak_p2 > 0 else 0
            if dd_pct < DD_THRESHOLDS[1]:
                risk_pct = RISK_TIERS[1]
            elif dd_pct < DD_THRESHOLDS[2]:
                risk_pct = RISK_TIERS[2]
            else:
                risk_pct = RISK_TIERS[3]
            
            risk_amount = balance_p2 * risk_pct
            pnl = r * risk_amount
            balance_p2 += pnl
            p2_trades += 1
            
            if balance_p2 > peak_p2:
                peak_p2 = balance_p2
            
            overall_dd = (initial_p2 - balance_p2) / initial_p2 if initial_p2 > 0 else 0
            if overall_dd >= FN_MAX_DD_LIMIT:
                break
            
            if balance_p2 >= target_p2:
                p2_passed = True
                break
        
        if p2_passed:
            phase2_passes += 1
            full_passes += 1
            pass_durations.append(p1_trades + p2_trades)
        else:
            breaches += 1
    
    return {
        "n_iter": n_iter,
        "phase1_pass_rate": phase1_passes / n_iter * 100,
        "full_pass_rate": full_passes / n_iter * 100,
        "breach_rate": breaches / n_iter * 100,
        "avg_trades_to_pass": np.mean(pass_durations) if pass_durations else 0,
        "median_trades_to_pass": np.median(pass_durations) if pass_durations else 0,
        "p5_trades_to_pass": np.percentile(pass_durations, 5) if pass_durations else 0,
        "p95_trades_to_pass": np.percentile(pass_durations, 95) if pass_durations else 0,
    }

# ============================================================
# 3. COST SENSITIVITY ANALYSIS
# ============================================================
def cost_sensitivity(trades_df, starting_bal=STARTING_BALANCE):
    """What happens if costs increase by 25%, 50%, 100%?"""
    results = {}
    base_pnls = trades_df["pnl"].values
    base_costs = trades_df["costs"].values
    base_pnl_raw = trades_df["pnl_raw"].values  # PnL before costs
    
    for multiplier_label, mult in [("Base (1.0x)", 1.0), ("1.25x costs", 1.25), 
                                     ("1.5x costs", 1.5), ("2.0x costs", 2.0),
                                     ("2.5x costs", 2.5)]:
        adjusted_pnls = base_pnl_raw - (base_costs * mult)
        cumulative = np.cumsum(adjusted_pnls)
        equity = starting_bal + cumulative
        peak = np.maximum.accumulate(equity)
        drawdowns = (peak - equity) / peak
        
        total_pnl = adjusted_pnls.sum()
        wins = adjusted_pnls[adjusted_pnls > 0]
        losses = adjusted_pnls[adjusted_pnls < 0]
        
        results[multiplier_label] = {
            "net_pnl": total_pnl,
            "return_pct": total_pnl / starting_bal * 100,
            "win_rate": len(wins) / len(adjusted_pnls) * 100,
            "profit_factor": abs(wins.sum() / losses.sum()) if losses.sum() != 0 else float('inf'),
            "max_dd_pct": drawdowns.max() * 100,
            "total_costs": (base_costs * mult).sum(),
        }
    
    return results

# ============================================================
# 4. PARAMETER DEGRADATION TEST
# ============================================================
def parameter_degradation(trades_df, n_iter=5000, starting_bal=STARTING_BALANCE):
    """Simulate what happens if strategy edge degrades 10%, 20%, 30%, 50%."""
    r_multiples = (trades_df["pnl"] / trades_df["risk_amount"].replace(0, np.nan)).dropna().values
    
    results = {}
    for degrade_label, degrade_pct in [("0% (baseline)", 0.0), ("10% degradation", 0.10),
                                        ("20% degradation", 0.20), ("30% degradation", 0.30),
                                        ("50% degradation", 0.50)]:
        # Reduce winning R-multiples by degrade_pct
        degraded = r_multiples.copy()
        degraded[degraded > 0] *= (1 - degrade_pct)
        
        finals = []
        max_dds = []
        fn_passes = 0
        
        for i in range(n_iter):
            shuffled = np.random.permutation(degraded)
            balance = starting_bal
            peak = starting_bal
            
            for r in shuffled:
                dd_pct = (peak - balance) / peak if peak > 0 else 0
                if dd_pct < DD_THRESHOLDS[1]:
                    risk_pct = RISK_TIERS[1]
                elif dd_pct < DD_THRESHOLDS[2]:
                    risk_pct = RISK_TIERS[2]
                else:
                    risk_pct = RISK_TIERS[3]
                
                pnl = r * balance * risk_pct
                balance += pnl
                if balance > peak:
                    peak = balance
            
            dd = (peak - balance) / peak if peak > 0 else 0
            finals.append(balance)
            max_dds.append((peak - min(starting_bal, balance)) / peak)
            
            # Quick FN check: did balance ever reach +8% without hitting -10%?
            # Simplified: just check final balance
            if balance >= starting_bal * 1.08:
                fn_passes += 1
        
        results[degrade_label] = {
            "median_final": np.median(finals),
            "p5_final": np.percentile(finals, 5),
            "p25_final": np.percentile(finals, 25),
            "p75_final": np.percentile(finals, 75),
            "p95_final": np.percentile(finals, 95),
            "median_return_pct": (np.median(finals) - starting_bal) / starting_bal * 100,
            "p5_return_pct": (np.percentile(finals, 5) - starting_bal) / starting_bal * 100,
            "profitable_pct": sum(1 for f in finals if f > starting_bal) / n_iter * 100,
            "fn_approx_pass_pct": fn_passes / n_iter * 100,
        }
    
    return results

# ============================================================
# 5. WORST-CASE STREAK ANALYSIS (Ruin probability)
# ============================================================
def ruin_analysis(trades_df, n_iter=MC_ITERATIONS, starting_bal=STARTING_BALANCE):
    """Calculate probability of account ruin (hitting various DD levels)."""
    r_multiples = (trades_df["pnl"] / trades_df["risk_amount"].replace(0, np.nan)).dropna().values
    n_trades = len(r_multiples)
    
    dd_thresholds = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
    hit_counts = {t: 0 for t in dd_thresholds}
    max_dd_dist = []
    
    for i in range(n_iter):
        shuffled = np.random.permutation(r_multiples)
        balance = starting_bal
        peak = starting_bal
        max_dd = 0.0
        
        for r in shuffled:
            dd_pct = (peak - balance) / peak if peak > 0 else 0
            if dd_pct < DD_THRESHOLDS[1]:
                risk_pct = RISK_TIERS[1]
            elif dd_pct < DD_THRESHOLDS[2]:
                risk_pct = RISK_TIERS[2]
            else:
                risk_pct = RISK_TIERS[3]
            
            pnl = r * balance * risk_pct
            balance += pnl
            if balance > peak:
                peak = balance
            
            dd = (peak - balance) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        
        max_dd_dist.append(max_dd)
        for t in dd_thresholds:
            if max_dd >= t:
                hit_counts[t] += 1
    
    return {
        "dd_probabilities": {f"{t*100:.0f}%": hit_counts[t] / n_iter * 100 for t in dd_thresholds},
        "dd_percentiles": {
            "p5": np.percentile(max_dd_dist, 5) * 100,
            "p25": np.percentile(max_dd_dist, 25) * 100,
            "p50": np.percentile(max_dd_dist, 50) * 100,
            "p75": np.percentile(max_dd_dist, 75) * 100,
            "p95": np.percentile(max_dd_dist, 95) * 100,
            "p99": np.percentile(max_dd_dist, 99) * 100,
            "max": np.max(max_dd_dist) * 100,
        },
    }

# ============================================================
# 6. REGIME ANALYSIS (Good months vs Bad months)
# ============================================================
def regime_analysis(trades_df):
    """Analyze performance in different market regimes (monthly bucketing)."""
    df = trades_df.copy()
    df["month"] = df["date"].dt.to_period("M")
    
    monthly = df.groupby("month").agg(
        trades=("pnl", "count"),
        net_pnl=("pnl", "sum"),
        win_rate=("pnl", lambda x: (x > 0).sum() / len(x) * 100),
        avg_pnl=("pnl", "mean"),
        max_win=("pnl", "max"),
        max_loss=("pnl", "min"),
    ).reset_index()
    
    profitable_months = (monthly["net_pnl"] > 0).sum()
    losing_months = (monthly["net_pnl"] <= 0).sum()
    total_months = len(monthly)
    
    best_month = monthly.loc[monthly["net_pnl"].idxmax()]
    worst_month = monthly.loc[monthly["net_pnl"].idxmin()]
    
    # Consecutive losing months
    is_loss = (monthly["net_pnl"] <= 0).values
    max_consec_loss_months = 0
    curr = 0
    for x in is_loss:
        if x:
            curr += 1
            max_consec_loss_months = max(max_consec_loss_months, curr)
        else:
            curr = 0
    
    return {
        "total_months": total_months,
        "profitable_months": profitable_months,
        "losing_months": losing_months,
        "profitable_pct": profitable_months / total_months * 100,
        "best_month": str(best_month["month"]),
        "best_month_pnl": best_month["net_pnl"],
        "worst_month": str(worst_month["month"]),
        "worst_month_pnl": worst_month["net_pnl"],
        "avg_monthly_pnl": monthly["net_pnl"].mean(),
        "median_monthly_pnl": monthly["net_pnl"].median(),
        "monthly_pnl_std": monthly["net_pnl"].std(),
        "max_consecutive_losing_months": max_consec_loss_months,
    }

# ============================================================
# 7. SEQUENTIAL DEPENDENCY (Auto-correlation test)
# ============================================================
def autocorrelation_test(trades_df):
    """Test if trade outcomes are independent (assumption for MC validity)."""
    pnls = trades_df["pnl"].values
    results = (pnls > 0).astype(int)  # 1 = win, 0 = loss
    
    n = len(results)
    
    # Lag-1 autocorrelation
    mean_r = results.mean()
    numerator = sum((results[i] - mean_r) * (results[i+1] - mean_r) for i in range(n-1))
    denominator = sum((results[i] - mean_r)**2 for i in range(n))
    lag1_autocorr = numerator / denominator if denominator != 0 else 0
    
    # Runs test
    runs = 1
    for i in range(1, n):
        if results[i] != results[i-1]:
            runs += 1
    
    n1 = results.sum()  # wins
    n0 = n - n1  # losses
    expected_runs = 1 + 2 * n1 * n0 / n
    std_runs = np.sqrt(2 * n1 * n0 * (2 * n1 * n0 - n) / (n**2 * (n - 1))) if n > 1 else 1
    z_runs = (runs - expected_runs) / std_runs if std_runs > 0 else 0
    
    # Win after win / Win after loss
    ww = sum(1 for i in range(1, n) if results[i] == 1 and results[i-1] == 1)
    wl = sum(1 for i in range(1, n) if results[i] == 1 and results[i-1] == 0)
    
    total_after_w = sum(1 for i in range(1, n) if results[i-1] == 1)
    total_after_l = sum(1 for i in range(1, n) if results[i-1] == 0)
    
    win_after_win = ww / total_after_w * 100 if total_after_w > 0 else 0
    win_after_loss = wl / total_after_l * 100 if total_after_l > 0 else 0
    
    return {
        "lag1_autocorrelation": round(lag1_autocorr, 4),
        "runs_test_z": round(z_runs, 4),
        "runs_test_verdict": "INDEPENDENT (MC valid)" if abs(z_runs) < 1.96 else "DEPENDENT (MC less reliable)",
        "win_after_win_pct": round(win_after_win, 1),
        "win_after_loss_pct": round(win_after_loss, 1),
        "overall_win_rate": round(mean_r * 100, 1),
        "actual_runs": runs,
        "expected_runs": round(expected_runs, 1),
    }

# ============================================================
# MAIN — RUN ALL SIMULATIONS
# ============================================================
def main():
    print("=" * 70)
    print("  MONTE CARLO & STRESS SIMULATION SUITE")
    print("  ICT ORB Clean Breakout — 6-Index Portfolio")
    print("=" * 70)
    
    trades_df = load_trades()
    print(f"\n  Loaded {len(trades_df)} trades from backtest")
    print(f"  Period: {trades_df['date'].min()} to {trades_df['date'].max()}")
    print(f"  Indices: {trades_df['symbol'].unique().tolist()}")
    
    # ---- 0. AUTOCORRELATION (validate MC assumption) ----
    print("\n" + "=" * 70)
    print("  TEST 0: TRADE INDEPENDENCE (Autocorrelation & Runs Test)")
    print("=" * 70)
    ac = autocorrelation_test(trades_df)
    print(f"  Lag-1 Autocorrelation:     {ac['lag1_autocorrelation']:+.4f}  (0 = perfectly independent)")
    print(f"  Runs Test Z-Score:         {ac['runs_test_z']:+.4f}  (|Z| < 1.96 = independent at 95% CI)")
    print(f"  Verdict:                   {ac['runs_test_verdict']}")
    print(f"  Win After Win:             {ac['win_after_win_pct']:.1f}%")
    print(f"  Win After Loss:            {ac['win_after_loss_pct']:.1f}%")
    print(f"  Overall Win Rate:          {ac['overall_win_rate']:.1f}%")
    print(f"  Actual Runs:               {ac['actual_runs']}  (Expected: {ac['expected_runs']})")
    
    # ---- 1. MONTE CARLO EQUITY ----
    print("\n" + "=" * 70)
    print(f"  TEST 1: MONTE CARLO EQUITY SIMULATION ({MC_ITERATIONS:,} iterations)")
    print("=" * 70)
    mc = monte_carlo_equity(trades_df)
    
    finals = mc["final_balances"]
    dds = mc["max_drawdowns"] * 100
    consec = mc["max_consecutive_losses"]
    
    print(f"\n  {'FINAL BALANCE DISTRIBUTION':}")
    print(f"  {'─' * 50}")
    print(f"    Worst Case (P1):         ${np.percentile(finals, 1):,.0f}")
    print(f"    P5 (5th percentile):     ${np.percentile(finals, 5):,.0f}")
    print(f"    P10:                     ${np.percentile(finals, 10):,.0f}")
    print(f"    P25 (conservative):      ${np.percentile(finals, 25):,.0f}")
    print(f"    MEDIAN (P50):            ${np.median(finals):,.0f}")
    print(f"    P75 (optimistic):        ${np.percentile(finals, 75):,.0f}")
    print(f"    P90:                     ${np.percentile(finals, 90):,.0f}")
    print(f"    P95:                     ${np.percentile(finals, 95):,.0f}")
    print(f"    Best Case (P99):         ${np.percentile(finals, 99):,.0f}")
    print(f"    Mean:                    ${np.mean(finals):,.0f}")
    
    print(f"\n  {'RETURN DISTRIBUTION':}")
    print(f"  {'─' * 50}")
    returns = (finals - STARTING_BALANCE) / STARTING_BALANCE * 100
    print(f"    P5 Return:               {np.percentile(returns, 5):+.1f}%")
    print(f"    P25 Return:              {np.percentile(returns, 25):+.1f}%")
    print(f"    Median Return:           {np.median(returns):+.1f}%")
    print(f"    P75 Return:              {np.percentile(returns, 75):+.1f}%")
    print(f"    P95 Return:              {np.percentile(returns, 95):+.1f}%")
    print(f"    Profitable Iterations:   {(finals > STARTING_BALANCE).sum() / MC_ITERATIONS * 100:.1f}%")
    
    print(f"\n  {'MAX DRAWDOWN DISTRIBUTION':}")
    print(f"  {'─' * 50}")
    print(f"    P5 Max DD:               {np.percentile(dds, 5):.1f}%")
    print(f"    P25 Max DD:              {np.percentile(dds, 25):.1f}%")
    print(f"    MEDIAN Max DD:           {np.median(dds):.1f}%")
    print(f"    P75 Max DD:              {np.percentile(dds, 75):.1f}%")
    print(f"    P95 Max DD:              {np.percentile(dds, 95):.1f}%")
    print(f"    P99 Max DD:              {np.percentile(dds, 99):.1f}%")
    print(f"    Worst Max DD:            {np.max(dds):.1f}%")
    
    print(f"\n  {'CONSECUTIVE LOSSES DISTRIBUTION':}")
    print(f"  {'─' * 50}")
    print(f"    Median:                  {np.median(consec):.0f}")
    print(f"    P75:                     {np.percentile(consec, 75):.0f}")
    print(f"    P95:                     {np.percentile(consec, 95):.0f}")
    print(f"    P99:                     {np.percentile(consec, 99):.0f}")
    print(f"    Worst:                   {np.max(consec):.0f}")
    
    # ---- 2. FUNDEDNEXT CHALLENGE MC ----
    print("\n" + "=" * 70)
    print(f"  TEST 2: FUNDEDNEXT CHALLENGE SIMULATION ({MC_ITERATIONS:,} iterations)")
    print("=" * 70)
    fn = fn_challenge_mc(trades_df)
    print(f"  Phase 1 Pass Rate:         {fn['phase1_pass_rate']:.1f}%")
    print(f"  FULL Pass Rate (P1+P2):    {fn['full_pass_rate']:.1f}%")
    print(f"  Breach Rate:               {fn['breach_rate']:.1f}%")
    print(f"  Avg Trades to Pass:        {fn['avg_trades_to_pass']:.0f}")
    print(f"  Median Trades to Pass:     {fn['median_trades_to_pass']:.0f}")
    print(f"  Fast Pass (P5):            {fn['p5_trades_to_pass']:.0f} trades")
    print(f"  Slow Pass (P95):           {fn['p95_trades_to_pass']:.0f} trades")
    
    # ---- 3. RUIN ANALYSIS ----
    print("\n" + "=" * 70)
    print(f"  TEST 3: DRAWDOWN RUIN PROBABILITY ({MC_ITERATIONS:,} iterations)")
    print("=" * 70)
    ruin = ruin_analysis(trades_df)
    print(f"\n  Probability of hitting DD thresholds over {mc['n_trades']} trades:")
    for level, prob in ruin["dd_probabilities"].items():
        marker = " ← FN DAILY LIMIT" if level == "5%" else (" ← FN MAX DD LIMIT" if level == "10%" else "")
        print(f"    DD ≥ {level:>4s}:   {prob:6.2f}%{marker}")
    
    print(f"\n  Max DD Percentiles:")
    for k, v in ruin["dd_percentiles"].items():
        print(f"    {k.upper():>4s}:               {v:.1f}%")
    
    # ---- 4. COST SENSITIVITY ----
    print("\n" + "=" * 70)
    print("  TEST 4: COST SENSITIVITY ANALYSIS")
    print("=" * 70)
    costs = cost_sensitivity(trades_df)
    print(f"\n  {'Scenario':<18s} {'Net PnL':>12s} {'Return':>9s} {'WR':>7s} {'PF':>6s} {'Max DD':>8s} {'Costs':>12s}")
    print(f"  {'─' * 78}")
    for label, data in costs.items():
        print(f"  {label:<18s} ${data['net_pnl']:>10,.0f} {data['return_pct']:>8.1f}% {data['win_rate']:>5.1f}% {data['profit_factor']:>5.2f} {data['max_dd_pct']:>6.1f}% ${data['total_costs']:>10,.0f}")
    
    # ---- 5. PARAMETER DEGRADATION ----
    print("\n" + "=" * 70)
    print(f"  TEST 5: EDGE DEGRADATION STRESS TEST (5,000 iterations each)")
    print("=" * 70)
    degrade = parameter_degradation(trades_df)
    print(f"\n  {'Scenario':<22s} {'Median Final':>14s} {'Median Ret':>12s} {'P5 Ret':>9s} {'Profitable':>12s} {'FN Pass':>9s}")
    print(f"  {'─' * 82}")
    for label, data in degrade.items():
        print(f"  {label:<22s} ${data['median_final']:>12,.0f} {data['median_return_pct']:>10.1f}% {data['p5_return_pct']:>7.1f}% {data['profitable_pct']:>10.1f}% {data['fn_approx_pass_pct']:>7.1f}%")
    
    # ---- 6. REGIME ANALYSIS ----
    print("\n" + "=" * 70)
    print("  TEST 6: REGIME / MONTHLY ANALYSIS")
    print("=" * 70)
    regime = regime_analysis(trades_df)
    print(f"  Total Months:              {regime['total_months']}")
    print(f"  Profitable Months:         {regime['profitable_months']} ({regime['profitable_pct']:.1f}%)")
    print(f"  Losing Months:             {regime['losing_months']} ({100-regime['profitable_pct']:.1f}%)")
    print(f"  Best Month:                {regime['best_month']} (${regime['best_month_pnl']:,.2f})")
    print(f"  Worst Month:               {regime['worst_month']} (${regime['worst_month_pnl']:,.2f})")
    print(f"  Avg Monthly PnL:           ${regime['avg_monthly_pnl']:,.2f}")
    print(f"  Median Monthly PnL:        ${regime['median_monthly_pnl']:,.2f}")
    print(f"  Monthly PnL Std Dev:       ${regime['monthly_pnl_std']:,.2f}")
    print(f"  Max Consec Losing Months:  {regime['max_consecutive_losing_months']}")
    
    # ---- SUMMARY ----
    print("\n" + "=" * 70)
    print("  SIMULATION SUITE SUMMARY")
    print("=" * 70)
    print(f"  ✓ Trade independence validated (Runs test Z={ac['runs_test_z']:+.2f})")
    print(f"  ✓ Monte Carlo: Median return {np.median(returns):+.1f}%, P5 {np.percentile(returns, 5):+.1f}%")
    print(f"  ✓ FN Challenge: {fn['full_pass_rate']:.1f}% full pass rate")
    print(f"  ✓ Ruin: {ruin['dd_probabilities']['10%']:.1f}% chance of 10% DD")
    print(f"  ✓ Cost sensitivity: Profitable up to {[l for l, d in costs.items() if d['net_pnl'] > 0][-1]}")
    print(f"  ✓ Edge degradation: Remains profitable with up to {[l for l, d in degrade.items() if d['profitable_pct'] > 50][-1]}")
    print(f"  ✓ Regime: {regime['profitable_pct']:.0f}% profitable months, max {regime['max_consecutive_losing_months']} consecutive losing")
    print()

if __name__ == "__main__":
    main()
