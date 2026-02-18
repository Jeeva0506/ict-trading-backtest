"""
Timeframe Combination Optimizer
================================
In-Sample:  2023-2024 (build)
Out-of-Sample: 2025 (validation)

Tests different combinations of:
- ORB period: 5, 10, 15, 30 minutes
- HTF EMA: 20/50, 50/200 on H1; 20/50 on H4; None
- Entry window: 1h, 2h, 3h after ORB
- Breakout window: 30, 60, 90 min

Goal: find combo that gives ~3 trading days/week while maintaining PF >= 1.5, WR >= 50%
"""
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, date
from itertools import product
import sys
import time
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent))

from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS,
    load_data,
)
from strategy.strategy_ict_clean import StrategyICTClean

# ── Data ──
ALL_SYMBOLS = ["SPX500", "NDX100", "GER30", "US30"]
CLEAN_M1 = {s: f"{s}_M1_CLEAN.csv" for s in ALL_SYMBOLS}
H1_EMA = {s: f"{s}_H1_EMA.csv" for s in ALL_SYMBOLS}

STARTING_CAPITAL = 6000.0
RISK_PCT = 1.5

# ── Date splits ──
IS_START = date(2023, 1, 1)
IS_END = date(2024, 12, 31)
OOS_START = date(2025, 1, 1)
OOS_END = date(2025, 12, 31)


def load_h1_with_ema(symbol: str, fast: int = 20, slow: int = 50) -> pd.DataFrame:
    """Load H1 data and compute EMA trend."""
    data_dir = Path(__file__).parent / "data" / "raw"
    fpath = data_dir / H1_EMA[symbol]
    h1 = pd.read_csv(fpath, sep="\t")
    time_col = [c for c in h1.columns if c.lower() == "time"][0]
    h1[time_col] = pd.to_datetime(h1[time_col], format="mixed", dayfirst=False)
    h1 = h1.set_index(time_col).sort_index()
    h1.columns = [c.lower() for c in h1.columns]
    h1[f"ema{fast}"] = h1["close"].ewm(span=fast, adjust=False).mean()
    h1[f"ema{slow}"] = h1["close"].ewm(span=slow, adjust=False).mean()
    h1["htf_trend"] = "bullish"
    h1.loc[h1[f"ema{fast}"] < h1[f"ema{slow}"], "htf_trend"] = "bearish"
    return h1[["htf_trend", f"ema{fast}", f"ema{slow}"]]


def load_h4_with_ema(symbol: str, fast: int = 20, slow: int = 50) -> pd.DataFrame:
    """Resample H1 to H4 and compute EMA trend."""
    data_dir = Path(__file__).parent / "data" / "raw"
    fpath = data_dir / H1_EMA[symbol]
    h1 = pd.read_csv(fpath, sep="\t")
    time_col = [c for c in h1.columns if c.lower() == "time"][0]
    h1[time_col] = pd.to_datetime(h1[time_col], format="mixed", dayfirst=False)
    h1 = h1.set_index(time_col).sort_index()
    h1.columns = [c.lower() for c in h1.columns]
    h4 = h1.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    h4[f"ema{fast}"] = h4["close"].ewm(span=fast, adjust=False).mean()
    h4[f"ema{slow}"] = h4["close"].ewm(span=slow, adjust=False).mean()
    h4["htf_trend"] = "bullish"
    h4.loc[h4[f"ema{fast}"] < h4[f"ema{slow}"], "htf_trend"] = "bearish"
    return h4[["htf_trend", f"ema{fast}", f"ema{slow}"]]


def get_htf_trend(htf_df: pd.DataFrame, session_start: pd.Timestamp):
    """Get HTF trend before session start."""
    prior = htf_df[htf_df.index < session_start]
    if len(prior) == 0:
        return None
    return str(prior.iloc[-1]["htf_trend"])


def run_backtest(symbols, date_start, date_end, orb_minutes, entry_hours,
                 breakout_window, htf_mode, disp_mult=1.7, body_min=0.65,
                 close_strength=0.30, breakout_margin=0.10):
    """Run backtest with given params on date range. Returns trades list."""
    all_trades = []

    for symbol in symbols:
        cfg = LIVE_INDEX_CONFIG[symbol]
        df = load_data(CLEAN_M1[symbol])
        point_value = BROKER_POINT_VALUES[symbol]

        # Load HTF trend based on mode
        if htf_mode == "h1_20_50":
            htf_df = load_h1_with_ema(symbol, 20, 50)
        elif htf_mode == "h1_50_200":
            htf_df = load_h1_with_ema(symbol, 50, 200)
        elif htf_mode == "h4_20_50":
            htf_df = load_h4_with_ema(symbol, 20, 50)
        elif htf_mode == "none":
            htf_df = None
        else:
            htf_df = load_h1_with_ema(symbol, 20, 50)

        trading_days = sorted(set(df.index.date))
        trading_days = [d for d in trading_days if date_start <= d <= date_end]

        strategy = StrategyICTClean(
            sl_buffer=cfg["sl_buffer"],
            min_sl_points=cfg.get("min_sl_points", 2.0),
            max_sl_points=cfg.get("max_sl_points", 100.0),
            orb_minutes=orb_minutes,
            entry_hours=entry_hours,
            breakout_window_minutes=breakout_window,
            disp_mult=disp_mult,
            body_min=body_min,
            close_strength=close_strength,
            breakout_margin=breakout_margin,
            orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
            tp3_r=2.5,
        )

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

                # Costs
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


def compute_metrics(trades, date_start, date_end):
    """Compute key metrics from trade list."""
    if not trades:
        return None

    df = pd.DataFrame(trades)
    df["date"] = pd.to_datetime(df["date"])
    total = len(df)

    wins = len(df[df["result"] == "win"])
    partial_wins = len(df[df["result"] == "partial_win"])
    losses = len(df[df["result"] == "loss"])
    profitable = wins + partial_wins
    wr = (profitable / total * 100) if total > 0 else 0

    net_pnl = df["pnl"].sum()
    gp = df[df["pnl"] > 0]["pnl"].sum()
    gl = df[df["pnl"] < 0]["pnl"].sum()
    pf = abs(gp / gl) if gl != 0 else float("inf")

    # Trading days/week
    trading_dates = set(df["date"].dt.date)
    total_calendar_days = (date_end - date_start).days
    total_weeks = max(1, total_calendar_days / 7)
    trading_days_per_week = len(trading_dates) / total_weeks

    # Sharpe
    df["trade_date"] = df["date"].dt.date
    daily_pnl = df.groupby("trade_date")["pnl"].sum()
    sharpe = (daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)) if daily_pnl.std() > 0 else 0

    # Max DD
    equity = np.array([STARTING_CAPITAL])
    for p in df["pnl"].values:
        equity = np.append(equity, equity[-1] + p)
    peak = np.maximum.accumulate(equity)
    dd_pct = ((peak - equity) / peak * 100)
    max_dd = dd_pct.max()

    # Max consecutive losses
    max_cl = 0
    cl = 0
    for r in df["result"].tolist():
        if r == "loss":
            cl += 1
            max_cl = max(max_cl, cl)
        else:
            cl = 0

    return {
        "trades": total,
        "wins": wins,
        "partial_wins": partial_wins,
        "losses": losses,
        "win_rate": round(wr, 1),
        "pf": round(pf, 2),
        "net_pnl": round(net_pnl, 2),
        "return_pct": round(net_pnl / STARTING_CAPITAL * 100, 1),
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 1),
        "max_consec_loss": max_cl,
        "td_per_week": round(trading_days_per_week, 1),
        "trades_per_week": round(total / total_weeks, 1),
    }


def main():
    print("=" * 120)
    print("TIMEFRAME COMBINATION OPTIMIZER")
    print(f"In-Sample:  {IS_START} to {IS_END}  (build)")
    print(f"Out-of-Sample: {OOS_START} to {OOS_END}  (validation)")
    print(f"Indices: {', '.join(ALL_SYMBOLS)}  |  Capital: ${STARTING_CAPITAL:,.0f}  |  Risk: {RISK_PCT}%")
    print("=" * 120)

    # ── Combinations to test ──
    combos = [
        # (label, orb_min, entry_hrs, bkout_win, htf_mode, disp_mult, body_min, close_str, bkout_margin)
        # --- BASELINE (current) ---
        ("BASELINE (15m ORB, H1 20/50)", 15, 2, 60, "h1_20_50", 1.7, 0.65, 0.30, 0.10),

        # --- ORB period variations ---
        ("5m ORB, H1 20/50",   5, 2, 60, "h1_20_50", 1.7, 0.65, 0.30, 0.10),
        ("10m ORB, H1 20/50", 10, 2, 60, "h1_20_50", 1.7, 0.65, 0.30, 0.10),
        ("30m ORB, H1 20/50", 30, 2, 60, "h1_20_50", 1.7, 0.65, 0.30, 0.10),

        # --- HTF trend filter variations ---
        ("15m ORB, H1 50/200",  15, 2, 60, "h1_50_200", 1.7, 0.65, 0.30, 0.10),
        ("15m ORB, H4 20/50",   15, 2, 60, "h4_20_50",  1.7, 0.65, 0.30, 0.10),
        ("15m ORB, NO HTF",     15, 2, 60, "none",      1.7, 0.65, 0.30, 0.10),

        # --- Entry window variations ---
        ("15m ORB, 1h entry",  15, 1, 60, "h1_20_50", 1.7, 0.65, 0.30, 0.10),
        ("15m ORB, 3h entry",  15, 3, 60, "h1_20_50", 1.7, 0.65, 0.30, 0.10),

        # --- Breakout window variations ---
        ("15m ORB, 30m bkout", 15, 2, 30, "h1_20_50", 1.7, 0.65, 0.30, 0.10),
        ("15m ORB, 90m bkout", 15, 2, 90, "h1_20_50", 1.7, 0.65, 0.30, 0.10),

        # --- Relaxed filters (more trades) ---
        ("15m ORB, loose disp 1.3", 15, 2, 60, "h1_20_50", 1.3, 0.60, 0.25, 0.05),
        ("15m ORB, loose disp 1.5", 15, 2, 60, "h1_20_50", 1.5, 0.60, 0.25, 0.08),

        # --- Combined: different ORB + wider entry + relaxed ---
        ("5m ORB, 3h entry, loose",   5, 3, 90, "h1_20_50", 1.5, 0.60, 0.25, 0.05),
        ("10m ORB, 3h entry, loose", 10, 3, 90, "h1_20_50", 1.5, 0.60, 0.25, 0.05),
        ("10m ORB, 2h, H4 20/50",   10, 2, 60, "h4_20_50",  1.7, 0.65, 0.30, 0.10),
        ("30m ORB, 3h entry",        30, 3, 90, "h1_20_50", 1.7, 0.65, 0.30, 0.10),

        # --- No HTF + relaxed (max trades) ---
        ("15m ORB, no HTF, loose",   15, 3, 90, "none", 1.5, 0.60, 0.25, 0.05),
        ("10m ORB, no HTF, loose",   10, 3, 90, "none", 1.5, 0.60, 0.25, 0.05),
        ("5m ORB, no HTF, loose",     5, 3, 90, "none", 1.5, 0.60, 0.25, 0.05),

        # --- Sweet spot attempts ---
        ("10m ORB, 2h, H1, loose1.5", 10, 2, 60, "h1_20_50", 1.5, 0.60, 0.30, 0.08),
        ("15m ORB, 3h, H1, loose1.5", 15, 3, 90, "h1_20_50", 1.5, 0.60, 0.30, 0.08),
        ("5m ORB, 2h, H1, std",        5, 2, 60, "h1_20_50", 1.7, 0.65, 0.30, 0.10),
    ]

    # ── Phase 1: In-Sample (2023-2024) ──
    print(f"\nPHASE 1: IN-SAMPLE ({IS_START} to {IS_END})")
    print("-" * 120)
    header = f"{'#':>2} {'Label':<35} {'Trades':>6} {'TD/Wk':>6} {'Tr/Wk':>6} {'WR%':>6} {'PF':>6} {'Sharpe':>7} {'Net PnL':>10} {'Ret%':>6} {'MaxDD':>6} {'MCL':>4}"
    print(header)
    print("-" * 120)

    is_results = []
    for idx, (label, orb, entry_h, bkout, htf, disp, body, close_s, bk_margin) in enumerate(combos):
        t0 = time.time()
        trades = run_backtest(ALL_SYMBOLS, IS_START, IS_END, orb, entry_h, bkout, htf, disp, body, close_s, bk_margin)
        metrics = compute_metrics(trades, IS_START, IS_END)
        elapsed = time.time() - t0

        if metrics:
            m = metrics
            # Flag promising ones
            flag = ""
            if m["pf"] >= 1.5 and m["win_rate"] >= 48 and m["td_per_week"] >= 2.5:
                flag = " ★★"  # great
            elif m["pf"] >= 1.2 and m["win_rate"] >= 45 and m["td_per_week"] >= 2.0:
                flag = " ★"   # good

            line = (f"{idx+1:>2} {label:<35} {m['trades']:>6} {m['td_per_week']:>5.1f} {m['trades_per_week']:>5.1f} "
                    f"{m['win_rate']:>5.1f}% {m['pf']:>5.2f} {m['sharpe']:>6.2f} "
                    f"${m['net_pnl']:>9,.2f} {m['return_pct']:>5.1f}% {m['max_dd']:>5.1f}% {m['max_consec_loss']:>4}{flag}")
            print(line)
            is_results.append({"idx": idx, "label": label, "metrics": m, "combo": combos[idx]})
        else:
            print(f"{idx+1:>2} {label:<35}      0 trades")
            is_results.append({"idx": idx, "label": label, "metrics": None, "combo": combos[idx]})

    # ── Phase 2: Out-of-Sample validation on promising combos ──
    # Filter: PF >= 1.2, WR >= 45%, TD/wk >= 2.0
    promising = [r for r in is_results if r["metrics"] and
                 r["metrics"]["pf"] >= 1.2 and r["metrics"]["win_rate"] >= 45 and r["metrics"]["td_per_week"] >= 1.5]

    print(f"\n{'=' * 120}")
    print(f"PHASE 2: OUT-OF-SAMPLE VALIDATION ({OOS_START} to {OOS_END})")
    print(f"Testing {len(promising)} promising combos from IS phase")
    print("-" * 120)
    print(header)
    print("-" * 120)

    oos_results = []
    for r in promising:
        combo = r["combo"]
        label, orb, entry_h, bkout, htf, disp, body, close_s, bk_margin = combo
        trades = run_backtest(ALL_SYMBOLS, OOS_START, OOS_END, orb, entry_h, bkout, htf, disp, body, close_s, bk_margin)
        m = compute_metrics(trades, OOS_START, OOS_END)

        if m:
            flag = ""
            if m["pf"] >= 1.5 and m["win_rate"] >= 48 and m["td_per_week"] >= 2.5:
                flag = " ★★"
            elif m["pf"] >= 1.2 and m["win_rate"] >= 45 and m["td_per_week"] >= 2.0:
                flag = " ★"

            is_m = r["metrics"]
            line = (f"{r['idx']+1:>2} {label:<35} {m['trades']:>6} {m['td_per_week']:>5.1f} {m['trades_per_week']:>5.1f} "
                    f"{m['win_rate']:>5.1f}% {m['pf']:>5.2f} {m['sharpe']:>6.2f} "
                    f"${m['net_pnl']:>9,.2f} {m['return_pct']:>5.1f}% {m['max_dd']:>5.1f}% {m['max_consec_loss']:>4}{flag}")
            print(line)
            oos_results.append({"idx": r["idx"], "label": label, "is_metrics": is_m, "oos_metrics": m, "combo": combo})
        else:
            print(f"{r['idx']+1:>2} {label:<35}      0 trades on OOS")

    # ── Phase 3: Side-by-side comparison ──
    # Filter OOS: PF >= 1.0 and profitable
    valid_oos = [r for r in oos_results if r["oos_metrics"]["pf"] >= 1.0]

    print(f"\n{'=' * 120}")
    print("SIDE-BY-SIDE: IN-SAMPLE vs OUT-OF-SAMPLE (PF >= 1.0 on OOS)")
    print("-" * 120)
    print(f"{'#':>2} {'Label':<35} {'IS-TD/W':>7} {'IS-WR':>6} {'IS-PF':>6} {'IS-Sh':>6} | {'OOS-TD/W':>8} {'OOS-WR':>7} {'OOS-PF':>7} {'OOS-Sh':>7} {'OOS-Ret':>8}")
    print("-" * 120)

    for r in sorted(valid_oos, key=lambda x: x["oos_metrics"]["td_per_week"], reverse=True):
        i = r["is_metrics"]
        o = r["oos_metrics"]
        flag = ""
        # Best candidate: OOS PF >= 1.3, WR >= 48%, TD/Wk >= 2.5
        if o["pf"] >= 1.3 and o["win_rate"] >= 48 and o["td_per_week"] >= 2.5:
            flag = " ★★★ CANDIDATE"
        elif o["pf"] >= 1.2 and o["win_rate"] >= 45 and o["td_per_week"] >= 2.0:
            flag = " ★★"
        elif o["pf"] >= 1.0:
            flag = " ★"

        print(f"{r['idx']+1:>2} {r['label']:<35} {i['td_per_week']:>6.1f} {i['win_rate']:>5.1f}% {i['pf']:>5.2f} {i['sharpe']:>5.2f}"
              f" | {o['td_per_week']:>7.1f} {o['win_rate']:>6.1f}% {o['pf']:>6.2f} {o['sharpe']:>6.2f} {o['return_pct']:>7.1f}%{flag}")

    print(f"\n{'=' * 120}")


if __name__ == "__main__":
    main()
