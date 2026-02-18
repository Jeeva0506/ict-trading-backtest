"""
Test Candidate #12 edge on FRA40, UK100, HK50, JP225
Uses same #12 params: disp=1.3, body=0.60, close=0.25, margin=0.05
Walk-forward: IS 2023-2024, OOS 2025
"""
import sys, time
from pathlib import Path
from datetime import date
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent))
from optimize_timeframes import (
    run_backtest, compute_metrics, load_h1_with_ema, get_htf_trend,
    STARTING_CAPITAL, RISK_PCT
)
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, load_data
)
from strategy.strategy_ict_clean import StrategyICTClean

# Indices to test
NEW_SYMBOLS = ["FRA40", "UK100", "HK50", "JP225"]
ALL_ORIG = ["SPX500", "NDX100", "GER30", "US30"]

# #12 params
C12 = dict(disp_mult=1.3, body_min=0.60, close_strength=0.25, breakout_margin=0.05)
BASELINE_PARAMS = dict(disp_mult=1.7, body_min=0.65, close_strength=0.30, breakout_margin=0.10)

IS_START = date(2023, 1, 1)
IS_END   = date(2024, 12, 31)
OOS_START = date(2025, 1, 1)
OOS_END   = date(2025, 12, 31)


def run_single_index(symbol, date_start, date_end, params):
    """Run backtest for a single index with given params."""
    cfg = LIVE_INDEX_CONFIG[symbol]
    data_file = f"{symbol}_M1_CLEAN.csv"
    h1_file = f"{symbol}_H1_EMA.csv"

    try:
        df = load_data(data_file)
    except Exception as e:
        print(f"    ERROR loading {data_file}: {e}")
        return []

    try:
        htf_df = load_h1_with_ema(symbol, 20, 50)
    except Exception:
        # Try loading H1 EMA manually
        data_dir = Path(__file__).parent / "data" / "raw"
        fpath = data_dir / h1_file
        if not fpath.exists():
            print(f"    WARNING: {h1_file} not found, running without HTF filter")
            htf_df = None
        else:
            h1 = pd.read_csv(fpath, sep="\t")
            time_col = [c for c in h1.columns if c.lower() == "time"][0]
            h1[time_col] = pd.to_datetime(h1[time_col], format="mixed", dayfirst=False)
            h1 = h1.set_index(time_col).sort_index()
            h1.columns = [c.lower() for c in h1.columns]
            h1["ema20"] = h1["close"].ewm(span=20, adjust=False).mean()
            h1["ema50"] = h1["close"].ewm(span=50, adjust=False).mean()
            h1["htf_trend"] = "bullish"
            h1.loc[h1["ema20"] < h1["ema50"], "htf_trend"] = "bearish"
            htf_df = h1[["htf_trend", "ema20", "ema50"]]

    point_value = BROKER_POINT_VALUES[symbol]
    trading_days = sorted(set(df.index.date))
    trading_days = [d for d in trading_days if date_start <= d <= date_end]

    strategy = StrategyICTClean(
        sl_buffer=cfg["sl_buffer"],
        min_sl_points=cfg.get("min_sl_points", 2.0),
        max_sl_points=cfg.get("max_sl_points", 100.0),
        orb_minutes=15,
        entry_hours=2,
        breakout_window_minutes=60,
        disp_mult=params["disp_mult"],
        body_min=params["body_min"],
        close_strength=params["close_strength"],
        breakout_margin=params["breakout_margin"],
        orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
        tp3_r=2.5,
    )

    all_trades = []
    for day in trading_days:
        session_start = pd.Timestamp(day).replace(
            hour=cfg["session_start_hour"], minute=cfg["session_start_minute"]
        )
        session_end = pd.Timestamp(day).replace(
            hour=cfg["session_end_hour"], minute=cfg["session_end_minute"]
        )
        market_df = df[(df.index >= session_start) & (df.index <= session_end)].copy()
        if len(market_df) < 80:
            continue

        htf_trend = get_htf_trend(htf_df, session_start) if htf_df is not None else None
        signals = strategy.generate_signals(market_df, htf_trend=htf_trend)

        for s in signals:
            risk_amount = STARTING_CAPITAL * (RISK_PCT / 100.0)
            risk_points = float(s.risk_points)
            if risk_points <= 0:
                continue
            lots = risk_amount / (risk_points * point_value)
            lots = max(0.01, min(40.0, lots))
            lots = int(lots * 100) / 100

            pnl_points = float(getattr(s, "pnl_points", 0.0))
            exit_reason = getattr(s, "exit_reason", None)

            slippage = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
            spread = TRADING_COSTS["spread_points"].get(symbol, 1.0)
            hs = spread / 2.0
            entry_cost = slippage + hs
            exit_cost = hs if exit_reason == "TP" else (slippage + hs)
            total_cost_pts = entry_cost + exit_cost

            adjusted = pnl_points - total_cost_pts
            if adjusted > 0:
                result = "win" if exit_reason == "TP" else "partial_win"
            elif adjusted == 0:
                result = "breakeven"
            else:
                result = "loss"

            pnl_raw = pnl_points * lots * point_value
            costs = total_cost_pts * lots * point_value
            pnl = pnl_raw - costs

            all_trades.append({
                "symbol": symbol,
                "date": pd.Timestamp(s.entry_time),
                "direction": s.direction,
                "result": result,
                "pnl_points": pnl_points,
                "pnl": pnl,
                "exit_reason": exit_reason,
                "risk_points": risk_points,
                "costs": costs,
            })

    return all_trades


def metrics_from_trades(trades, start, end):
    """Compute metrics from trade list."""
    if not trades:
        return None
    df = pd.DataFrame(trades)
    df["date"] = pd.to_datetime(df["date"])
    total = len(df)
    wins = len(df[df["result"] == "win"])
    partial = len(df[df["result"] == "partial_win"])
    losses = len(df[df["result"] == "loss"])
    wr = (wins + partial) / total * 100 if total > 0 else 0

    gross_profit = df[df["pnl"] > 0]["pnl"].sum()
    gross_loss = abs(df[df["pnl"] < 0]["pnl"].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else 99.0
    net_pnl = df["pnl"].sum()

    # Sharpe
    daily_pnl = df.groupby(df["date"].dt.date)["pnl"].sum()
    sharpe = (daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)) if daily_pnl.std() > 0 else 0

    # Max DD
    equity = STARTING_CAPITAL + df["pnl"].cumsum()
    peak = equity.cummax()
    dd = (peak - equity) / peak * 100
    max_dd = dd.max()

    # Trading frequency
    trading_days_count = df["date"].dt.date.nunique()
    total_cal_days = (end - start).days
    total_weeks = max(1, total_cal_days / 7)
    td_per_week = trading_days_count / total_weeks

    # MCL
    mcl = 0
    cur = 0
    for _, row in df.sort_values("date").iterrows():
        if row["result"] == "loss":
            cur += 1
            mcl = max(mcl, cur)
        else:
            cur = 0

    return {
        "trades": total, "wins": wins, "losses": losses,
        "win_rate": round(wr, 1), "pf": round(pf, 2),
        "net_pnl": round(net_pnl, 2), "return_pct": round(net_pnl / STARTING_CAPITAL * 100, 1),
        "sharpe": round(sharpe, 2), "max_dd": round(max_dd, 1),
        "td_per_week": round(td_per_week, 1),
        "trades_per_week": round(total / total_weeks, 1),
        "max_consec_loss": mcl,
    }


def section(title):
    print(f"\n{'═'*120}")
    print(f"  {title}")
    print(f"{'═'*120}")


def main():
    print("█" * 120)
    print("█  CANDIDATE #12 EDGE TEST — FRA40, UK100, HK50, JP225")
    print("█  Params: 15m ORB | H1 20/50 | disp=1.3 | body=0.60 | close=0.25 | margin=0.05")
    print("█" * 120)

    # ═══════════════════════════════════════
    # 1. DATA CHECK
    # ═══════════════════════════════════════
    section("1. DATA AVAILABILITY")
    for sym in NEW_SYMBOLS:
        data_dir = Path(__file__).parent / "data" / "raw"
        m1_file = data_dir / f"{sym}_M1_CLEAN.csv"
        h1_file = data_dir / f"{sym}_H1_EMA.csv"
        if m1_file.exists():
            df = load_data(f"{sym}_M1_CLEAN.csv")
            dates = sorted(set(df.index.date))
            print(f"  {sym}: {len(df):,} M1 bars | {len(dates)} days | {dates[0]} → {dates[-1]}")
        else:
            print(f"  {sym}: ❌ M1 data missing")

    # ═══════════════════════════════════════
    # 2. IS + OOS RESULTS PER INDEX
    # ═══════════════════════════════════════
    section("2. PER-INDEX RESULTS: #12 PARAMS (IS 2023-24 vs OOS 2025)")

    print(f"\n  {'Symbol':<10} {'Period':<8} {'Trades':>6} {'TD/Wk':>6} {'WR%':>6} {'PF':>6} {'Sharpe':>7} {'Net PnL':>10} {'Ret%':>6} {'DD%':>5} {'MCL':>4}")
    print(f"  {'-'*90}")

    results = {}
    for sym in NEW_SYMBOLS:
        for period_name, start, end in [("IS", IS_START, IS_END), ("OOS", OOS_START, OOS_END)]:
            trades = run_single_index(sym, start, end, C12)
            m = metrics_from_trades(trades, start, end)
            key = f"{sym}_{period_name}"
            results[key] = {"trades": trades, "metrics": m}

            if m:
                print(f"  {sym:<10} {period_name:<8} {m['trades']:>6} {m['td_per_week']:>5.1f} {m['win_rate']:>5.1f}% "
                      f"{m['pf']:>5.2f} {m['sharpe']:>6.2f} ${m['net_pnl']:>9,.0f} {m['return_pct']:>5.1f}% "
                      f"{m['max_dd']:>4.1f}% {m['max_consec_loss']:>4}")
            else:
                print(f"  {sym:<10} {period_name:<8}      0 trades")

        print()  # space between indices

    # ═══════════════════════════════════════
    # 3. BASELINE COMPARISON
    # ═══════════════════════════════════════
    section("3. BASELINE (old params) vs #12 ON EACH INDEX (OOS 2025)")

    print(f"\n  {'Symbol':<10} {'Config':<12} {'Trades':>6} {'TD/Wk':>6} {'WR%':>6} {'PF':>6} {'Sharpe':>7} {'PnL':>10} {'DD%':>5}")
    print(f"  {'-'*75}")

    for sym in NEW_SYMBOLS:
        for label, params in [("BASELINE", BASELINE_PARAMS), ("#12", C12)]:
            trades = run_single_index(sym, OOS_START, OOS_END, params)
            m = metrics_from_trades(trades, OOS_START, OOS_END)
            if m:
                print(f"  {sym:<10} {label:<12} {m['trades']:>6} {m['td_per_week']:>5.1f} {m['win_rate']:>5.1f}% "
                      f"{m['pf']:>5.2f} {m['sharpe']:>6.2f} ${m['net_pnl']:>9,.0f} {m['max_dd']:>4.1f}%")
            else:
                print(f"  {sym:<10} {label:<12}      0 trades")
        print()

    # ═══════════════════════════════════════
    # 4. MONTHLY CONSISTENCY (OOS 2025) FOR PROFITABLE INDICES
    # ═══════════════════════════════════════
    section("4. MONTHLY CONSISTENCY (OOS 2025) — #12 PARAMS")

    for sym in NEW_SYMBOLS:
        key = f"{sym}_OOS"
        if results[key]["metrics"] is None or results[key]["metrics"]["pf"] < 1.0:
            print(f"\n  {sym}: Skipped (not profitable in OOS)")
            continue

        trades = results[key]["trades"]
        df = pd.DataFrame(trades)
        df["date"] = pd.to_datetime(df["date"])
        df["month"] = df["date"].dt.to_period("M")

        print(f"\n  {sym} ({LIVE_INDEX_CONFIG[sym]['name']}):")
        print(f"  {'Month':<10} {'Trades':>6} {'WR%':>6} {'PF':>6} {'PnL':>10}")
        print(f"  {'-'*42}")

        profitable_m = 0
        total_m = 0
        for period in sorted(df["month"].unique()):
            sub = df[df["month"] == period]
            wins = len(sub[sub["result"] == "win"])
            wr = wins / len(sub) * 100 if len(sub) > 0 else 0
            gp = sub[sub["pnl"] > 0]["pnl"].sum()
            gl = abs(sub[sub["pnl"] < 0]["pnl"].sum())
            pf = gp / gl if gl > 0 else 99.0
            net = sub["pnl"].sum()
            total_m += 1
            if net > 0:
                profitable_m += 1
            flag = "✅" if net > 0 else "❌"
            print(f"  {str(period):<10} {len(sub):>6} {wr:>5.1f}% {pf:>5.2f} ${net:>9,.0f} {flag}")

        print(f"  Profitable months: {profitable_m}/{total_m} ({profitable_m/total_m*100:.0f}%)")

    # ═══════════════════════════════════════
    # 5. PORTFOLIO IMPACT — ADD TO ORIGINAL 4
    # ═══════════════════════════════════════
    section("5. PORTFOLIO IMPACT — ORIGINAL 4 + NEW ADDITIONS (OOS 2025)")

    # Original 4 with #12 params
    orig_trades = run_backtest(ALL_ORIG, OOS_START, OOS_END,
                                orb_minutes=15, entry_hours=2, breakout_window=60,
                                htf_mode="h1_20_50", **C12)
    orig_m = metrics_from_trades(orig_trades, OOS_START, OOS_END)

    print(f"\n  {'Portfolio':<35} {'Trades':>6} {'TD/Wk':>6} {'WR%':>6} {'PF':>6} {'Sharpe':>7} {'PnL':>10} {'Ret%':>6} {'DD%':>5}")
    print(f"  {'-'*95}")

    if orig_m:
        print(f"  {'Original 4 (SPX/NDX/GER/US30)':<35} {orig_m['trades']:>6} {orig_m['td_per_week']:>5.1f} "
              f"{orig_m['win_rate']:>5.1f}% {orig_m['pf']:>5.2f} {orig_m['sharpe']:>6.2f} "
              f"${orig_m['net_pnl']:>9,.0f} {orig_m['return_pct']:>5.1f}% {orig_m['max_dd']:>4.1f}%")

    # Test adding each new index individually
    for sym in NEW_SYMBOLS:
        new_trades = run_single_index(sym, OOS_START, OOS_END, C12)
        combined_trades = orig_trades + new_trades
        combo_m = metrics_from_trades(combined_trades, OOS_START, OOS_END)
        if combo_m:
            pf_delta = combo_m["pf"] - orig_m["pf"]
            td_delta = combo_m["td_per_week"] - orig_m["td_per_week"]
            label = f"+ {sym}"
            flag = "✅" if combo_m["pf"] >= orig_m["pf"] * 0.95 and combo_m["td_per_week"] > orig_m["td_per_week"] else "⚠️"
            print(f"  {label:<35} {combo_m['trades']:>6} {combo_m['td_per_week']:>5.1f} "
                  f"{combo_m['win_rate']:>5.1f}% {combo_m['pf']:>5.2f} {combo_m['sharpe']:>6.2f} "
                  f"${combo_m['net_pnl']:>9,.0f} {combo_m['return_pct']:>5.1f}% {combo_m['max_dd']:>4.1f}% "
                  f"(PF {pf_delta:+.2f}, TD {td_delta:+.1f}) {flag}")

    # All 4 new combined
    all_new_trades = []
    for sym in NEW_SYMBOLS:
        all_new_trades.extend(run_single_index(sym, OOS_START, OOS_END, C12))
    combined_all = orig_trades + all_new_trades
    combo_all_m = metrics_from_trades(combined_all, OOS_START, OOS_END)
    if combo_all_m:
        pf_delta = combo_all_m["pf"] - orig_m["pf"]
        td_delta = combo_all_m["td_per_week"] - orig_m["td_per_week"]
        print(f"\n  {'+ ALL 4 NEW':<35} {combo_all_m['trades']:>6} {combo_all_m['td_per_week']:>5.1f} "
              f"{combo_all_m['win_rate']:>5.1f}% {combo_all_m['pf']:>5.2f} {combo_all_m['sharpe']:>6.2f} "
              f"${combo_all_m['net_pnl']:>9,.0f} {combo_all_m['return_pct']:>5.1f}% {combo_all_m['max_dd']:>4.1f}% "
              f"(PF {pf_delta:+.2f}, TD {td_delta:+.1f})")

    # Only add profitable new indices
    profitable_new = []
    for sym in NEW_SYMBOLS:
        key = f"{sym}_OOS"
        if results[key]["metrics"] and results[key]["metrics"]["pf"] >= 1.1:
            profitable_new.append(sym)

    if profitable_new:
        prof_trades = []
        for sym in profitable_new:
            prof_trades.extend(run_single_index(sym, OOS_START, OOS_END, C12))
        combined_prof = orig_trades + prof_trades
        combo_prof_m = metrics_from_trades(combined_prof, OOS_START, OOS_END)
        if combo_prof_m:
            pf_delta = combo_prof_m["pf"] - orig_m["pf"]
            td_delta = combo_prof_m["td_per_week"] - orig_m["td_per_week"]
            label = f"+ PROFITABLE ONLY ({','.join(profitable_new)})"
            print(f"  {label:<35} {combo_prof_m['trades']:>6} {combo_prof_m['td_per_week']:>5.1f} "
                  f"{combo_prof_m['win_rate']:>5.1f}% {combo_prof_m['pf']:>5.2f} {combo_prof_m['sharpe']:>6.2f} "
                  f"${combo_prof_m['net_pnl']:>9,.0f} {combo_prof_m['return_pct']:>5.1f}% {combo_prof_m['max_dd']:>4.1f}% "
                  f"(PF {pf_delta:+.2f}, TD {td_delta:+.1f})")

    # ═══════════════════════════════════════
    # 6. FINAL VERDICT
    # ═══════════════════════════════════════
    section("6. FINAL VERDICT — NEW INDICES")

    for sym in NEW_SYMBOLS:
        is_key = f"{sym}_IS"
        oos_key = f"{sym}_OOS"
        is_m = results[is_key]["metrics"]
        oos_m = results[oos_key]["metrics"]

        print(f"\n  {sym} ({LIVE_INDEX_CONFIG[sym]['name']}):")

        if not is_m or not oos_m:
            print(f"    ❌ REJECT — insufficient trades")
            continue

        checks = []
        # PF >= 1.0 in both IS and OOS
        checks.append(("IS PF >= 1.0", is_m["pf"] >= 1.0))
        checks.append(("OOS PF >= 1.0", oos_m["pf"] >= 1.0))
        # Walk-forward: OOS not drastically worse
        if is_m["pf"] > 0:
            pf_degradation = (oos_m["pf"] - is_m["pf"]) / is_m["pf"] * 100
            checks.append(("PF degradation < 30%", pf_degradation > -30))
        # OOS DD < 15%
        checks.append(("OOS Max DD < 15%", oos_m["max_dd"] < 15))
        # OOS positive return
        checks.append(("OOS positive return", oos_m["net_pnl"] > 0))
        # Minimum trades
        checks.append(("OOS 20+ trades", oos_m["trades"] >= 20))

        passed = sum(1 for _, ok in checks if ok)
        for name, ok in checks:
            print(f"    {'✅' if ok else '❌'} {name}: {'PASS' if ok else 'FAIL'}")

        if passed == len(checks):
            print(f"    → ✅ ADD TO PORTFOLIO")
        elif passed >= len(checks) - 1:
            print(f"    → ⚠️ MARGINAL — monitor closely")
        else:
            print(f"    → ❌ REJECT — edge not confirmed")

    print(f"\n{'█'*120}")


if __name__ == "__main__":
    main()
