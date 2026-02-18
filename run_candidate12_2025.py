"""
Detailed 2025 analysis for Candidate #12: 15m ORB, loose disp 1.3
Params: disp_mult=1.3, body_min=0.60, close_strength=0.25, breakout_margin=0.05
Fixed:  15m ORB | H1 20/50 EMA | 2h entry | 60m breakout window
"""
import sys
from pathlib import Path
from datetime import date
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent))
from optimize_timeframes import (
    run_backtest, compute_metrics, ALL_SYMBOLS,
    STARTING_CAPITAL, RISK_PCT
)

# ── #12 parameters ──
PARAMS = dict(orb_minutes=15, entry_hours=2, breakout_window=60, htf_mode="h1_20_50",
              disp_mult=1.3, body_min=0.60, close_strength=0.25, breakout_margin=0.05)

OOS_START = date(2025, 1, 1)
OOS_END   = date(2025, 12, 31)

def main():
    print("=" * 130)
    print("CANDIDATE #12 — DETAILED 2025 OUT-OF-SAMPLE ANALYSIS")
    print("Params: 15m ORB | H1 20/50 | 2h entry | 60m bkout | disp=1.3 | body=0.60 | close=0.25 | margin=0.05")
    print(f"Period: {OOS_START} to {OOS_END}  |  Capital: ${STARTING_CAPITAL:,.0f}  |  Risk: {RISK_PCT}%")
    print("=" * 130)

    # Run backtest
    trades = run_backtest(ALL_SYMBOLS, OOS_START, OOS_END, **PARAMS)
    metrics = compute_metrics(trades, OOS_START, OOS_END)

    if not metrics:
        print("No trades found!")
        return

    df = pd.DataFrame(trades)
    df["date"] = pd.to_datetime(df["date"])
    df["day"] = df["date"].dt.date
    df["month"] = df["date"].dt.to_period("M")
    df["week"] = df["date"].dt.isocalendar().week.astype(int)
    df["year_week"] = df["date"].dt.strftime("%Y-W%U")

    # ════════════════════════════════════════════
    # 1. OVERALL METRICS
    # ════════════════════════════════════════════
    m = metrics
    print(f"\n{'─'*60}")
    print(f"  OVERALL METRICS (2025)")
    print(f"{'─'*60}")
    print(f"  Total Trades:        {m['trades']}")
    print(f"  Wins / Losses:       {m['wins']} W / {m['losses']} L")
    print(f"  Win Rate:            {m['win_rate']:.1f}%")
    print(f"  Profit Factor:       {m['pf']:.2f}")
    print(f"  Sharpe Ratio:        {m['sharpe']:.2f}")
    print(f"  Net PnL:             ${m['net_pnl']:,.2f}")
    print(f"  Return:              {m['return_pct']:.1f}%")
    print(f"  Max Drawdown:        {m['max_dd']:.1f}%")
    print(f"  Max Consec Losses:   {m['max_consec_loss']}")
    print(f"  Trading Days/Week:   {m['td_per_week']:.1f}")
    print(f"  Trades/Week:         {m['trades_per_week']:.1f}")

    # ════════════════════════════════════════════
    # 2. PER-INDEX BREAKDOWN
    # ════════════════════════════════════════════
    print(f"\n{'─'*60}")
    print(f"  PER-INDEX BREAKDOWN (2025)")
    print(f"{'─'*60}")
    print(f"  {'Symbol':<10} {'Trades':>6} {'Wins':>5} {'WR%':>6} {'PF':>6} {'Net PnL':>10} {'Avg PnL':>8}")
    print(f"  {'-'*55}")

    for sym in ALL_SYMBOLS:
        sub = df[df["symbol"] == sym]
        if len(sub) == 0:
            print(f"  {sym:<10}      0")
            continue
        wins = len(sub[sub["result"] == "win"])
        wr = wins / len(sub) * 100
        gp = sub[sub["pnl"] > 0]["pnl"].sum()
        gl = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gp / gl if gl > 0 else 99.0
        net = sub["pnl"].sum()
        avg = sub["pnl"].mean()
        print(f"  {sym:<10} {len(sub):>6} {wins:>5} {wr:>5.1f}% {pf:>5.2f} ${net:>9,.0f} ${avg:>7,.0f}")

    # ════════════════════════════════════════════
    # 3. MONTHLY BREAKDOWN
    # ════════════════════════════════════════════
    print(f"\n{'─'*60}")
    print(f"  MONTHLY BREAKDOWN (2025)")
    print(f"{'─'*60}")
    print(f"  {'Month':<10} {'Trades':>6} {'TDs':>4} {'TD/Wk':>6} {'WR%':>6} {'PF':>6} {'Net PnL':>10} {'CumPnL':>10}")
    print(f"  {'-'*60}")

    cum_pnl = 0
    profitable_months = 0
    for period in sorted(df["month"].unique()):
        sub = df[df["month"] == period]
        wins = len(sub[sub["result"] == "win"])
        wr = wins / len(sub) * 100 if len(sub) > 0 else 0
        gp = sub[sub["pnl"] > 0]["pnl"].sum()
        gl = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gp / gl if gl > 0 else 99.0
        net = sub["pnl"].sum()
        cum_pnl += net
        td = sub["day"].nunique()
        # Approximate weeks in month
        weeks_in_month = 4.3
        td_wk = td / weeks_in_month
        status = "✅" if net > 0 else "❌"
        if net > 0:
            profitable_months += 1
        print(f"  {str(period):<10} {len(sub):>6} {td:>4} {td_wk:>5.1f} {wr:>5.1f}% {pf:>5.2f} ${net:>9,.0f} ${cum_pnl:>9,.0f} {status}")

    total_months = len(df["month"].unique())
    print(f"\n  Profitable months: {profitable_months}/{total_months} ({profitable_months/total_months*100:.0f}%)")

    # ════════════════════════════════════════════
    # 4. WEEKLY TRADING DAYS DISTRIBUTION
    # ════════════════════════════════════════════
    print(f"\n{'─'*60}")
    print(f"  WEEKLY TRADING DAYS DISTRIBUTION (2025)")
    print(f"{'─'*60}")

    weekly = df.groupby("year_week").agg(
        trades=("pnl", "count"),
        trading_days=("day", "nunique"),
        pnl=("pnl", "sum")
    ).reset_index()

    td_dist = weekly["trading_days"].value_counts().sort_index()
    total_weeks = len(weekly)
    print(f"  Total trading weeks: {total_weeks}")
    for td_count, freq in td_dist.items():
        pct = freq / total_weeks * 100
        bar = "█" * freq
        print(f"    {td_count} TD/wk: {freq:>3} weeks ({pct:>5.1f}%) {bar}")

    print(f"\n  Average: {weekly['trading_days'].mean():.1f} TD/wk")
    print(f"  Median:  {weekly['trading_days'].median():.0f} TD/wk")
    print(f"  Weeks with 2+ TDs: {(weekly['trading_days'] >= 2).sum()} ({(weekly['trading_days'] >= 2).sum()/total_weeks*100:.0f}%)")
    print(f"  Weeks with 3+ TDs: {(weekly['trading_days'] >= 3).sum()} ({(weekly['trading_days'] >= 3).sum()/total_weeks*100:.0f}%)")

    # ════════════════════════════════════════════
    # 5. EQUITY CURVE & DRAWDOWN
    # ════════════════════════════════════════════
    print(f"\n{'─'*60}")
    print(f"  EQUITY CURVE & DRAWDOWN (2025)")
    print(f"{'─'*60}")

    df_sorted = df.sort_values("date").reset_index(drop=True)
    df_sorted["cum_pnl"] = df_sorted["pnl"].cumsum()
    df_sorted["equity"] = STARTING_CAPITAL + df_sorted["cum_pnl"]
    df_sorted["peak"] = df_sorted["equity"].cummax()
    df_sorted["dd_pct"] = (df_sorted["peak"] - df_sorted["equity"]) / df_sorted["peak"] * 100

    max_dd_idx = df_sorted["dd_pct"].idxmax()
    max_dd_date = df_sorted.loc[max_dd_idx, "date"]
    max_dd_val = df_sorted.loc[max_dd_idx, "dd_pct"]
    max_dd_equity = df_sorted.loc[max_dd_idx, "equity"]

    print(f"  Starting equity:   ${STARTING_CAPITAL:,.0f}")
    print(f"  Ending equity:     ${df_sorted['equity'].iloc[-1]:,.0f}")
    print(f"  Peak equity:       ${df_sorted['peak'].max():,.0f}")
    print(f"  Max drawdown:      {max_dd_val:.1f}% on {max_dd_date.strftime('%Y-%m-%d')}")
    print(f"  Equity at max DD:  ${max_dd_equity:,.0f}")

    # Show 5 worst drawdown periods
    print(f"\n  Top 5 drawdown events:")
    dd_events = df_sorted[df_sorted["dd_pct"] > 2.0].copy()
    if len(dd_events) > 0:
        dd_events["dd_group"] = (dd_events["dd_pct"].diff() < 0).cumsum()
        worst_dd = dd_events.groupby("dd_group").agg(
            max_dd=("dd_pct", "max"),
            start=("date", "first"),
            end=("date", "last"),
            trades=("pnl", "count")
        ).sort_values("max_dd", ascending=False).head(5)
        for _, row in worst_dd.iterrows():
            print(f"    {row['max_dd']:.1f}% DD | {row['start'].strftime('%Y-%m-%d')} to {row['end'].strftime('%Y-%m-%d')} | {row['trades']} trades")

    # ════════════════════════════════════════════
    # 6. DAILY PnL & RISK CHECK
    # ════════════════════════════════════════════
    print(f"\n{'─'*60}")
    print(f"  DAILY PnL & FUNDEDNEXT RISK CHECK (2025)")
    print(f"{'─'*60}")

    daily_pnl = df.groupby("day")["pnl"].sum()
    daily_trades = df.groupby("day").size()

    worst_day = daily_pnl.min()
    best_day = daily_pnl.max()
    daily_limit = STARTING_CAPITAL * 0.05  # 5% daily loss limit

    print(f"  Worst day:    ${worst_day:,.0f} ({worst_day/STARTING_CAPITAL*100:.1f}%)")
    print(f"  Best day:     ${best_day:,.0f} ({best_day/STARTING_CAPITAL*100:.1f}%)")
    print(f"  Avg daily:    ${daily_pnl.mean():,.0f}")
    print(f"  Daily limit:  -${daily_limit:,.0f} (5%)")
    violations = daily_pnl[daily_pnl < -daily_limit]
    print(f"  Violations:   {len(violations)} days")
    if len(violations) > 0:
        for d, pnl in violations.sort_values().items():
            print(f"    {d}: ${pnl:,.0f} ({pnl/STARTING_CAPITAL*100:.1f}%)")

    print(f"\n  Trades per day: Max={daily_trades.max()}, Avg={daily_trades.mean():.1f}")
    print(f"  Days with 3+ trades: {(daily_trades >= 3).sum()}")
    print(f"  Days with 4+ trades: {(daily_trades >= 4).sum()}")

    # ════════════════════════════════════════════
    # 7. WIN/LOSS STREAKS
    # ════════════════════════════════════════════
    print(f"\n{'─'*60}")
    print(f"  WIN/LOSS STREAKS (2025)")
    print(f"{'─'*60}")

    df_sorted["is_win"] = df_sorted["result"] == "win"
    streaks = []
    current_type = None
    current_len = 0
    for _, row in df_sorted.iterrows():
        if row["is_win"] != current_type:
            if current_type is not None:
                streaks.append(("W" if current_type else "L", current_len))
            current_type = row["is_win"]
            current_len = 1
        else:
            current_len += 1
    if current_type is not None:
        streaks.append(("W" if current_type else "L", current_len))

    win_streaks = [s[1] for s in streaks if s[0] == "W"]
    loss_streaks = [s[1] for s in streaks if s[0] == "L"]

    if win_streaks:
        print(f"  Longest win streak:  {max(win_streaks)}")
        print(f"  Avg win streak:      {np.mean(win_streaks):.1f}")
    if loss_streaks:
        print(f"  Longest loss streak: {max(loss_streaks)}")
        print(f"  Avg loss streak:     {np.mean(loss_streaks):.1f}")

    # ════════════════════════════════════════════
    # 8. DIRECTION BREAKDOWN
    # ════════════════════════════════════════════
    print(f"\n{'─'*60}")
    print(f"  DIRECTION BREAKDOWN (2025)")
    print(f"{'─'*60}")

    for direction in ["long", "short"]:
        sub = df[df["direction"] == direction]
        if len(sub) == 0:
            continue
        wins = len(sub[sub["result"] == "win"])
        wr = wins / len(sub) * 100
        gp = sub[sub["pnl"] > 0]["pnl"].sum()
        gl = abs(sub[sub["pnl"] < 0]["pnl"].sum())
        pf = gp / gl if gl > 0 else 99.0
        print(f"  {direction.upper():<6}: {len(sub):>3} trades | WR {wr:.1f}% | PF {pf:.2f} | ${sub['pnl'].sum():,.0f}")

    # ════════════════════════════════════════════
    # 9. EXIT REASON BREAKDOWN
    # ════════════════════════════════════════════
    print(f"\n{'─'*60}")
    print(f"  EXIT REASON BREAKDOWN (2025)")
    print(f"{'─'*60}")

    for reason in df["exit_reason"].unique():
        sub = df[df["exit_reason"] == reason]
        net = sub["pnl"].sum()
        print(f"  {str(reason):<20}: {len(sub):>4} trades | ${net:>9,.0f}")

    # ════════════════════════════════════════════
    # 10. TRADE LOG (first 30 and last 10)
    # ════════════════════════════════════════════
    print(f"\n{'─'*60}")
    print(f"  TRADE LOG (first 20 trades of 2025)")
    print(f"{'─'*60}")
    print(f"  {'#':>3} {'Date':<20} {'Symbol':<8} {'Dir':<6} {'Result':<8} {'PnL Pts':>8} {'PnL $':>9} {'Exit':>8}")
    print(f"  {'-'*80}")

    for i, (_, row) in enumerate(df_sorted.head(20).iterrows(), 1):
        print(f"  {i:>3} {row['date'].strftime('%Y-%m-%d %H:%M'):<20} {row['symbol']:<8} {row['direction']:<6} "
              f"{row['result']:<8} {row['pnl_points']:>8.1f} ${row['pnl']:>8,.0f} {str(row['exit_reason']):>8}")

    # ════════════════════════════════════════════
    # COMPARISON: BASELINE vs #12
    # ════════════════════════════════════════════
    print(f"\n{'═'*60}")
    print(f"  BASELINE vs #12 (2025)")
    print(f"{'═'*60}")

    baseline_trades = run_backtest(ALL_SYMBOLS, OOS_START, OOS_END,
                                   orb_minutes=15, entry_hours=2, breakout_window=60,
                                   htf_mode="h1_20_50", disp_mult=1.7, body_min=0.65,
                                   close_strength=0.30, breakout_margin=0.10)
    baseline_m = compute_metrics(baseline_trades, OOS_START, OOS_END)

    if baseline_m:
        bm = baseline_m
        print(f"\n  {'Metric':<20} {'BASELINE':>12} {'#12 Loose':>12} {'Delta':>12}")
        print(f"  {'-'*56}")
        print(f"  {'Trades':<20} {bm['trades']:>12} {m['trades']:>12} {m['trades']-bm['trades']:>+12}")
        print(f"  {'TD/Week':<20} {bm['td_per_week']:>12.1f} {m['td_per_week']:>12.1f} {m['td_per_week']-bm['td_per_week']:>+12.1f}")
        print(f"  {'Win Rate %':<20} {bm['win_rate']:>12.1f} {m['win_rate']:>12.1f} {m['win_rate']-bm['win_rate']:>+12.1f}")
        print(f"  {'Profit Factor':<20} {bm['pf']:>12.2f} {m['pf']:>12.2f} {m['pf']-bm['pf']:>+12.2f}")
        print(f"  {'Sharpe':<20} {bm['sharpe']:>12.2f} {m['sharpe']:>12.2f} {m['sharpe']-bm['sharpe']:>+12.2f}")
        print(f"  {'Net PnL':<20} ${bm['net_pnl']:>11,.0f} ${m['net_pnl']:>11,.0f} ${m['net_pnl']-bm['net_pnl']:>+11,.0f}")
        print(f"  {'Return %':<20} {bm['return_pct']:>12.1f} {m['return_pct']:>12.1f} {m['return_pct']-bm['return_pct']:>+12.1f}")
        print(f"  {'Max DD %':<20} {bm['max_dd']:>12.1f} {m['max_dd']:>12.1f} {m['max_dd']-bm['max_dd']:>+12.1f}")
        print(f"  {'Max Consec Loss':<20} {bm['max_consec_loss']:>12} {m['max_consec_loss']:>12} {m['max_consec_loss']-bm['max_consec_loss']:>+12}")

    print(f"\n{'═'*60}")
    print("  DONE")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()
