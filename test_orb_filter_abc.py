"""
ORB Filter A/B/C Test
=====================
Compares three ORB filter variants on the 16-year HistData backtest:

  Version A (Current): ORB <= Avg(M1 range) * num_candles * 0.40
  Version B (ATR):     ORB <= 35% of Daily ATR(14)
  Version C (Hybrid):  ORB must pass BOTH filters

No parameter optimization -- uses standard 35% ATR threshold.
Runs on all 3 indices (SPX500, NDX100, GER30).

Usage:
  python test_orb_filter_abc.py
"""

import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent))

from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, ACCOUNT_RULES,
    compute_htf_ema, get_htf_trend_for_day, StrategyICTClean,
)

from run_histdata_backtest import (
    run_portfolio, compute_metrics, DATA_FILES, GER30_CLEAN_PERIODS,
    load_and_filter, EMA_FAST, EMA_SLOW,
)

# ── Config ──────────────────────────────────────────────────────────
VARIANTS = {
    "A_current": {"orb_filter_mode": "current", "atr_pct": 0.35},
    "B_atr":     {"orb_filter_mode": "atr",     "atr_pct": 0.35},
    "C_hybrid":  {"orb_filter_mode": "hybrid",  "atr_pct": 0.35},
}

REGIME_PERIODS = {
    "Pre-COVID (2011-2019)":  (date(2011, 1, 1), date(2019, 12, 31)),
    "COVID Crash (2020)":     (date(2020, 1, 1), date(2020, 12, 31)),
    "Rate Hikes (2023-2026)": (date(2023, 1, 1), date(2026, 12, 31)),
}


def compute_daily_atr(df_m1, period=14):
    """
    Compute ATR(14) from M1 data by resampling to daily OHLC.
    Returns dict: {date -> ATR value} using previous day's ATR for each signal day.
    """
    daily = df_m1.resample("1D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()

    if len(daily) < period + 1:
        return {}

    daily["prev_close"] = daily["close"].shift(1)
    daily["tr"] = np.maximum(
        daily["high"] - daily["low"],
        np.maximum(
            abs(daily["high"] - daily["prev_close"]),
            abs(daily["low"] - daily["prev_close"])
        )
    )
    daily["tr"] = daily["tr"].fillna(daily["high"] - daily["low"])
    daily["atr"] = daily["tr"].ewm(span=period, adjust=False).mean()

    # Use yesterday's ATR for today's signal
    atr_dict = {}
    atr_vals = daily["atr"].values
    dates = daily.index.date
    for i in range(1, len(daily)):
        atr_dict[dates[i]] = float(atr_vals[i - 1])

    return atr_dict


def generate_signals_variant(symbol, df, label, variant_cfg, atr_dict):
    """Generate signals for one symbol with a specific ORB filter variant."""
    cfg = LIVE_INDEX_CONFIG.get(symbol)
    if cfg is None:
        return []

    htf_ema = compute_htf_ema(df, fast=EMA_FAST, slow=EMA_SLOW)
    trading_days = sorted(set(df.index.date))
    point_value = BROKER_POINT_VALUES[symbol]

    signals = []
    for day in trading_days:
        session_start = pd.Timestamp(day).replace(
            hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
        session_end = pd.Timestamp(day).replace(
            hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
        market_df = df[(df.index >= session_start) & (df.index <= session_end)].copy()

        if len(market_df) < 80:
            continue

        htf_trend = get_htf_trend_for_day(htf_ema, session_start)

        strategy = StrategyICTClean(
            sl_buffer=cfg["sl_buffer"],
            min_sl_points=cfg.get("min_sl_points", 2.0),
            max_sl_points=cfg.get("max_sl_points", 25.0),
            orb_minutes=cfg.get("orb_minutes", 15),
            entry_hours=cfg.get("entry_hours", 2),
            breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
            disp_mult=cfg.get("disp_mult", 1.3),
            body_min=cfg.get("body_min", 0.60),
            close_strength=cfg.get("close_strength", 0.25),
            breakout_margin=cfg.get("breakout_margin", 0.05),
            orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
            tp3_r=2.5,
            orb_filter_mode=variant_cfg["orb_filter_mode"],
            atr_pct=variant_cfg["atr_pct"],
        )

        day_atr = atr_dict.get(day)
        day_signals = strategy.generate_signals(
            market_df, htf_trend=htf_trend, daily_atr=day_atr
        )

        for s in day_signals:
            signals.append({
                "symbol": symbol,
                "segment": label,
                "signal": s,
                "point_value": point_value,
                "entry_time": pd.Timestamp(s.entry_time),
                "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
            })

    return signals


def run_variant(variant_name, variant_cfg, datasets):
    """Run one complete backtest variant across all indices."""
    all_signals = []
    for sym, entries in datasets.items():
        for entry in entries:
            sigs = generate_signals_variant(
                sym, entry["df"], entry["label"], variant_cfg, entry["atr_dict"]
            )
            all_signals.extend(sigs)

    all_signals.sort(key=lambda x: x["entry_time"])
    if not all_signals:
        return None, None, 0, 0, 0

    trades, skipped, final_bal, peak_bal, resets = run_portfolio(all_signals)
    return trades, skipped, final_bal, peak_bal, resets


def regime_metrics(trades_df, starting_cap):
    """Compute metrics for each market regime period."""
    results = {}
    for regime_name, (start_d, end_d) in REGIME_PERIODS.items():
        mask = ((trades_df["date"].dt.date >= start_d) &
                (trades_df["date"].dt.date <= end_d))
        rdf = trades_df[mask].reset_index(drop=True)
        if len(rdf) > 0:
            results[regime_name] = compute_metrics(rdf, starting_cap, regime_name)
        else:
            results[regime_name] = None
    return results


def worst_year(trades_df):
    """Find the worst year by net PnL."""
    tdf = trades_df.copy()
    tdf["year"] = tdf["date"].dt.year
    worst_yr = None
    worst_pnl = float("inf")
    for yr in tdf["year"].unique():
        pnl = tdf[tdf["year"] == yr]["pnl"].sum()
        if pnl < worst_pnl:
            worst_pnl = pnl
            worst_yr = yr
    return worst_yr, worst_pnl


def main():
    print("=" * 72)
    print("  ORB FILTER A/B/C TEST -- 16-Year HistData")
    print("  A = Current (M1 micro)  |  B = ATR(14)  |  C = Hybrid (both)")
    print("=" * 72)

    # ── Load all data once ──────────────────────────────────────────
    print("\n  Loading data & computing ATR(14)...")
    datasets = {}

    # SPX500
    df = load_and_filter("SPX500")
    if df is not None:
        atr = compute_daily_atr(df)
        datasets["SPX500"] = [{"df": df, "label": "SPX500", "atr_dict": atr}]
        print(f"    SPX500: {len(df):,} bars, ATR for {len(atr)} days")

    # NDX100
    df = load_and_filter("NDX100")
    if df is not None:
        atr = compute_daily_atr(df)
        datasets["NDX100"] = [{"df": df, "label": "NDX100", "atr_dict": atr}]
        print(f"    NDX100: {len(df):,} bars, ATR for {len(atr)} days")

    # GER30 (split periods)
    ger_entries = []
    for period_key, period_cfg in GER30_CLEAN_PERIODS.items():
        df = load_and_filter(
            "GER30", start_date=period_cfg["start"], end_date=period_cfg["end"]
        )
        if df is not None:
            atr = compute_daily_atr(df)
            ger_entries.append({
                "df": df, "label": period_cfg["label"], "atr_dict": atr
            })
            print(f"    {period_cfg['label']}: {len(df):,} bars, "
                  f"ATR for {len(atr)} days")
    if ger_entries:
        datasets["GER30"] = ger_entries

    if not datasets:
        print("  No data loaded. Exiting.")
        return

    # ── Run each variant ────────────────────────────────────────────
    results = {}
    starting_cap = ACCOUNT_RULES["starting_balance"]

    for vname, vcfg in VARIANTS.items():
        print(f"\n  Running variant {vname} "
              f"(mode={vcfg['orb_filter_mode']})...")
        trades, skipped, final_bal, peak_bal, resets = run_variant(
            vname, vcfg, datasets
        )

        if trades is None or len(trades) == 0:
            print(f"    {vname}: No trades generated!")
            results[vname] = None
            continue

        tdf = pd.DataFrame(trades)
        tdf["date"] = pd.to_datetime(tdf["date"])
        tdf = tdf.sort_values("date").reset_index(drop=True)

        overall = compute_metrics(tdf, starting_cap, vname)
        regimes = regime_metrics(tdf, starting_cap)
        w_yr, w_pnl = worst_year(tdf)

        per_index = {}
        for sym in sorted(tdf["symbol"].unique()):
            sdf = tdf[tdf["symbol"] == sym].reset_index(drop=True)
            if len(sdf) > 0:
                per_index[sym] = compute_metrics(sdf, starting_cap, sym)

        results[vname] = {
            "overall": overall,
            "regimes": regimes,
            "per_index": per_index,
            "worst_year": w_yr,
            "worst_year_pnl": w_pnl,
            "resets": resets,
            "skipped": skipped,
            "trades_df": tdf,
        }
        print(f"    {vname}: {overall['total_trades']} trades, "
              f"PF={overall['profit_factor']}, "
              f"WR={overall['win_rate']}%, "
              f"Sharpe={overall['sharpe']}, "
              f"Net=${overall['net_pnl']:,.0f}")

    # ── Print comparison ────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  COMPARISON RESULTS")
    print("=" * 72)

    labels = [v for v in VARIANTS if results.get(v)]
    if not labels:
        print("  No results to compare.")
        return

    # Summary table
    print(f"\n  {'Metric':<22}", end="")
    for v in labels:
        print(f"  {v:>14}", end="")
    print()
    print(f"  {'-' * 22}", end="")
    for _ in labels:
        print(f"  {'-' * 14}", end="")
    print()

    metric_rows = [
        ("Trades",          "total_trades",     "{}", False),
        ("Trades/Week",     "trades_per_week",  "{:.1f}", False),
        ("Win Rate %",      "win_rate",         "{:.1f}%", False),
        ("Profit Factor",   "profit_factor",    "{:.2f}", False),
        ("Avg RR",          "actual_rr",        "1:{:.2f}", False),
        ("Sharpe",          "sharpe",           "{:.2f}", False),
        ("Sortino",         "sortino",          "{:.2f}", False),
        ("Net PnL",         "net_pnl",          "${:,.0f}", False),
        ("Max DD %",        "max_dd_pct",       "{:.1f}%", False),
        ("Peak Daily DD %", "peak_daily_dd",    "{:.1f}%", False),
        ("Max Consec Loss", "max_consec_losses", "{}", False),
        ("Recovery Factor", "recovery_factor",  "{:.2f}", False),
        ("Total Costs",     "total_costs",      "${:,.0f}", False),
    ]

    for row_label, key, fmt, _ in metric_rows:
        print(f"  {row_label:<22}", end="")
        for v_ in labels:
            o = results[v_]["overall"]
            val = o.get(key, "N/A")
            try:
                print(f"  {fmt.format(val):>14}", end="")
            except Exception:
                print(f"  {'N/A':>14}", end="")
        print()

    # Account resets
    print(f"  {'Account Resets':<22}", end="")
    for v_ in labels:
        print(f"  {results[v_]['resets']:>14}", end="")
    print()

    # Worst year
    print(f"\n  {'Worst Year':<22}", end="")
    for v_ in labels:
        r = results[v_]
        txt = f"{r['worst_year']} (${r['worst_year_pnl']:,.0f})"
        print(f"  {txt:>14}", end="")
    print()

    # ── Per-Index Breakdown ─────────────────────────────────────────
    print(f"\n  -- PER-INDEX BREAKDOWN --")
    all_syms = sorted(
        set().union(*[results[v_]["per_index"].keys() for v_ in labels])
    )

    for sym in all_syms:
        print(f"\n  {sym}:")
        hdr = (f"    {'Variant':<14} {'Trades':>7} {'WR':>7} "
               f"{'PF':>7} {'Net PnL':>12} {'Sharpe':>8}")
        print(hdr)
        print(f"    {'-' * 56}")
        for v_ in labels:
            m = results[v_]["per_index"].get(sym)
            if m:
                print(f"    {v_:<14} {m['total_trades']:>7} "
                      f"{m['win_rate']:>6.1f}% "
                      f"{m['profit_factor']:>6.2f} "
                      f"${m['net_pnl']:>11,.2f} "
                      f"{m['sharpe']:>7.2f}")
            else:
                print(f"    {v_:<14}    (no trades)")

    # ── Regime Stability ────────────────────────────────────────────
    print(f"\n  -- REGIME STABILITY --")
    for regime_name in REGIME_PERIODS:
        print(f"\n  {regime_name}:")
        hdr = (f"    {'Variant':<14} {'Trades':>7} {'WR':>7} "
               f"{'PF':>7} {'Net PnL':>12} {'Sharpe':>8}")
        print(hdr)
        print(f"    {'-' * 56}")
        for v_ in labels:
            m = results[v_]["regimes"].get(regime_name)
            if m:
                print(f"    {v_:<14} {m['total_trades']:>7} "
                      f"{m['win_rate']:>6.1f}% "
                      f"{m['profit_factor']:>6.2f} "
                      f"${m['net_pnl']:>11,.2f} "
                      f"{m['sharpe']:>7.2f}")
            else:
                print(f"    {v_:<14}    (no trades in this period)")

    # ── Trade count change ──────────────────────────────────────────
    if len(labels) >= 2:
        base = results[labels[0]]["overall"]["total_trades"]
        print(f"\n  -- TRADE COUNT CHANGE vs {labels[0]} --")
        for v_ in labels[1:]:
            delta = results[v_]["overall"]["total_trades"] - base
            pct = (delta / base * 100) if base > 0 else 0
            print(f"    {v_}: {delta:+d} trades ({pct:+.1f}%)")

    # ── Monthly payout impact ───────────────────────────────────────
    print(f"\n  -- MONTHLY PAYOUT IMPACT --")
    for v_ in labels:
        tdf = results[v_]["trades_df"].copy()
        tdf["month"] = tdf["date"].dt.to_period("M")
        monthly = tdf.groupby("month").agg(
            trades=("pnl", "count"),
            pnl=("pnl", "sum"),
        )
        payout_months = len(monthly[monthly["pnl"] > 0])
        total_months = len(monthly)
        avg_monthly_pnl = monthly["pnl"].mean()
        avg_trades_mo = monthly["trades"].mean()
        pct_payout = (payout_months / total_months * 100
                      if total_months > 0 else 0)
        print(f"    {v_}: {avg_trades_mo:.1f} trades/mo, "
              f"{payout_months}/{total_months} payout months "
              f"({pct_payout:.0f}%), "
              f"avg month PnL: ${avg_monthly_pnl:,.0f}")

    # ── Save results ────────────────────────────────────────────────
    output_dir = Path(__file__).parent / "reports" / "histdata_backtest"
    output_dir.mkdir(parents=True, exist_ok=True)

    save_data = {}
    for v_ in labels:
        r = results[v_]
        save_data[v_] = {
            "overall": r["overall"],
            "regimes": {k: v for k, v in r["regimes"].items()
                        if v is not None},
            "per_index": r["per_index"],
            "worst_year": r["worst_year"],
            "worst_year_pnl": r["worst_year_pnl"],
            "resets": r["resets"],
        }

    json_path = output_dir / "orb_filter_abc_results.json"
    with open(json_path, "w") as f:
        json.dump(save_data, f, indent=2, default=str)

    print(f"\n  Results saved: {json_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
