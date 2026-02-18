"""
Backtest ORB Strategy on ALL new FundedNext indices.
Runs each index individually using the same proven strategy params.
Reports per-index results for comparison.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).parent))

from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS,
    load_data, compute_htf_ema, get_htf_trend_for_day,
)
from strategy.strategy_ict_clean import StrategyICTClean

# Data files for new indices (fetched from MT5)
DATA_FILES = {
    "AUS200":  ("AUS200_M1_CLEAN.csv",  "AUS200_H1_EMA.csv"),
    "EUSTX50": ("EUSTX50_M1_CLEAN.csv", "EUSTX50_H1_EMA.csv"),
    "FRA40":   ("FRA40_M1_CLEAN.csv",   "FRA40_H1_EMA.csv"),
    "HK50":    ("HK50_M1_CLEAN.csv",    "HK50_H1_EMA.csv"),
    "JP225":   ("JP225_M1_CLEAN.csv",   "JP225_H1_EMA.csv"),
    "NTH25":   ("NTH25_M1_CLEAN.csv",   "NTH25_H1_EMA.csv"),
    "SWI20":   ("SWI20_M1_CLEAN.csv",   "SWI20_H1_EMA.csv"),
    "UK100":   ("UK100_M1_CLEAN.csv",   "UK100_H1_EMA.csv"),
    "US2000":  ("US2000_M1_CLEAN.csv",  "US2000_H1_EMA.csv"),
}

STARTING_CAPITAL = 6000.0
RISK_PCT = 1.5


def load_h1_ema(filename: str) -> pd.DataFrame:
    """Load native H1 data and compute 20/50 EMA for trend filter."""
    data_path = Path(__file__).parent / "data" / "raw" / filename
    h1 = pd.read_csv(data_path, sep="\t")
    time_col = [c for c in h1.columns if c.lower() == "time"][0]
    h1[time_col] = pd.to_datetime(h1[time_col], format="mixed", dayfirst=False)
    h1 = h1.set_index(time_col).sort_index()
    h1.columns = [c.lower() for c in h1.columns]
    h1["ema20"] = h1["close"].ewm(span=20, adjust=False).mean()
    h1["ema50"] = h1["close"].ewm(span=50, adjust=False).mean()
    h1["htf_trend"] = "bullish"
    h1.loc[h1["ema20"] < h1["ema50"], "htf_trend"] = "bearish"
    return h1[["htf_trend", "ema20", "ema50"]]


def run_single_index_backtest(symbol: str):
    """Run ORB backtest on a single index with costs."""
    cfg = LIVE_INDEX_CONFIG[symbol]
    m1_file, h1_file = DATA_FILES[symbol]
    point_value = BROKER_POINT_VALUES.get(symbol, 10.0)

    # Load data
    df = load_data(m1_file)
    htf_ema = load_h1_ema(h1_file)
    
    trading_days = sorted(set(df.index.date))
    
    strategy = StrategyICTClean(
        sl_buffer=cfg["sl_buffer"],
        min_sl_points=cfg.get("min_sl_points", 2.0),
        max_sl_points=cfg.get("max_sl_points", 100.0),
        orb_minutes=cfg.get("orb_minutes", 15),
        entry_hours=cfg.get("entry_hours", 2),
        breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
        disp_mult=cfg.get("disp_mult", 1.7),
        body_min=cfg.get("body_min", 0.65),
        close_strength=cfg.get("close_strength", 0.30),
        breakout_margin=cfg.get("breakout_margin", 0.10),
        orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
        tp3_r=2.5,
    )

    all_trades = []
    balance = STARTING_CAPITAL

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

        htf_trend = get_htf_trend_for_day(htf_ema, session_start)
        signals = strategy.generate_signals(market_df, htf_trend=htf_trend)

        for s in signals:
            risk_amount = balance * (RISK_PCT / 100.0)
            risk_points = float(s.risk_points)
            if risk_points <= 0:
                continue

            lots = risk_amount / (risk_points * point_value)
            lots = max(0.01, min(40.0, lots))
            lots = int(lots * 100) / 100

            pnl_points = float(getattr(s, "pnl_points", 0.0))
            exit_reason = getattr(s, "exit_reason", None)

            # Cost model
            slippage_pts = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
            spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
            half_spread = spread_pts / 2.0
            entry_cost = slippage_pts + half_spread
            exit_cost = half_spread if exit_reason == "TP" else (slippage_pts + half_spread)
            total_cost_pts = entry_cost + exit_cost

            adjusted_pnl_pts = pnl_points - total_cost_pts
            if adjusted_pnl_pts > 0:
                result = "win" if exit_reason == "TP" else "partial_win"
            elif adjusted_pnl_pts == 0:
                result = "breakeven"
            else:
                result = "loss"

            pnl_raw = pnl_points * lots * point_value
            costs = total_cost_pts * lots * point_value
            pnl = pnl_raw - costs
            balance += pnl

            all_trades.append({
                "symbol": symbol, "date": s.entry_time, "direction": s.direction,
                "result": result, "pnl_points": pnl_points, "pnl": pnl,
                "balance": balance, "lots": lots, "exit_reason": exit_reason,
                "risk_points": risk_points, "costs": costs,
            })

    return all_trades, trading_days


def print_index_summary(symbol: str, trades: list, trading_days: list):
    """Print summary for a single index."""
    cfg = LIVE_INDEX_CONFIG[symbol]
    if len(trades) == 0:
        print(f"  {symbol:<10} ({cfg['name']:<20}): 0 trades generated")
        return {}

    df = pd.DataFrame(trades)
    total = len(df)
    wins = len(df[df["result"] == "win"])
    partial_wins = len(df[df["result"] == "partial_win"])
    losses = len(df[df["result"] == "loss"])
    profitable = wins + partial_wins
    wr = (profitable / total * 100) if total > 0 else 0
    net_pnl = df["pnl"].sum()
    gross_profit = df[df["pnl"] > 0]["pnl"].sum()
    gross_loss = df[df["pnl"] < 0]["pnl"].sum()
    pf = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")
    total_costs = df["costs"].sum()

    # Drawdown
    equity = df["balance"].values
    peak_eq = np.maximum.accumulate(equity)
    dd = (peak_eq - equity) / peak_eq * 100
    max_dd = dd.max()

    # Sharpe
    df["date_dt"] = pd.to_datetime(df["date"])
    df["trade_date"] = df["date_dt"].dt.date
    daily_pnl = df.groupby("trade_date")["pnl"].sum()
    sharpe = (daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)) if daily_pnl.std() > 0 else 0

    # Max consecutive losses
    max_cl = 0
    cl = 0
    for r in df["result"].tolist():
        if r == "loss":
            cl += 1
            max_cl = max(max_cl, cl)
        else:
            cl = 0

    # Avg win/loss ratio
    avg_win_pts = df[df["pnl_points"] > 0]["pnl_points"].mean() if profitable > 0 else 0
    avg_loss_pts = abs(df[df["pnl_points"] < 0]["pnl_points"].mean()) if losses > 0 else 0
    rr = avg_win_pts / avg_loss_pts if avg_loss_pts > 0 else 0

    weeks = max(1, len(trading_days) / 5)
    tpw = total / weeks

    final_bal = df["balance"].iloc[-1]
    ret_pct = ((final_bal - STARTING_CAPITAL) / STARTING_CAPITAL * 100)

    first_date = df["date_dt"].min().strftime("%Y-%m-%d")
    last_date = df["date_dt"].max().strftime("%Y-%m-%d")

    return {
        "symbol": symbol,
        "name": cfg["name"],
        "period": f"{first_date} to {last_date}",
        "trades": total,
        "trades_per_week": round(tpw, 1),
        "wins": wins,
        "partial_wins": partial_wins,
        "losses": losses,
        "win_rate": round(wr, 1),
        "rr": round(rr, 2),
        "profit_factor": round(pf, 2),
        "net_pnl": round(net_pnl, 2),
        "return_pct": round(ret_pct, 1),
        "max_dd_pct": round(max_dd, 1),
        "sharpe": round(sharpe, 2),
        "max_consec_loss": max_cl,
        "total_costs": round(total_costs, 2),
        "final_balance": round(final_bal, 2),
    }


def main():
    symbols_to_test = list(DATA_FILES.keys())

    print("=" * 80)
    print("ORB STRATEGY BACKTEST — ALL FUNDEDNEXT INDICES")
    print(f"Capital: ${STARTING_CAPITAL:,.0f}  |  Risk: {RISK_PCT}%  |  TP: 2.5R")
    print("=" * 80)

    results = []
    for sym in symbols_to_test:
        print(f"\nRunning {sym} ({LIVE_INDEX_CONFIG[sym]['name']})...", flush=True)
        t0 = time.time()
        trades, tdays = run_single_index_backtest(sym)
        elapsed = time.time() - t0
        print(f"  {len(trades)} trades in {elapsed:.1f}s")
        summary = print_index_summary(sym, trades, tdays)
        if summary:
            results.append(summary)

    # Print comparison table
    print("\n" + "=" * 120)
    print("COMPARISON TABLE — ALL INDICES")
    print("=" * 120)
    header = f"{'Symbol':<10} {'Name':<20} {'Trades':>6} {'Tr/Wk':>6} {'WR%':>6} {'PF':>6} {'RR':>6} {'Net PnL':>10} {'Ret%':>6} {'MaxDD%':>7} {'Sharpe':>7} {'MCL':>4} {'Costs':>8}"
    print(header)
    print("-" * 120)

    for r in sorted(results, key=lambda x: x["net_pnl"], reverse=True):
        line = (
            f"{r['symbol']:<10} {r['name']:<20} {r['trades']:>6} {r['trades_per_week']:>5.1f} "
            f"{r['win_rate']:>5.1f}% {r['profit_factor']:>5.2f} {r['rr']:>5.2f} "
            f"${r['net_pnl']:>9,.2f} {r['return_pct']:>5.1f}% {r['max_dd_pct']:>6.1f}% "
            f"{r['sharpe']:>6.2f} {r['max_consec_loss']:>4} ${r['total_costs']:>7,.2f}"
        )
        # Color-code: mark profitable ones
        if r["profit_factor"] >= 1.5 and r["win_rate"] >= 45:
            line += "  ★"
        elif r["profit_factor"] >= 1.0:
            line += "  ✓"
        else:
            line += "  ✗"
        print(line)

    # Summary
    profitable_indices = [r for r in results if r["profit_factor"] >= 1.2 and r["win_rate"] >= 45]
    print(f"\n{'=' * 120}")
    print(f"PROFITABLE INDICES (PF >= 1.2, WR >= 45%):")
    if profitable_indices:
        for r in sorted(profitable_indices, key=lambda x: x["profit_factor"], reverse=True):
            print(f"  ★ {r['symbol']:<10} PF={r['profit_factor']:.2f}  WR={r['win_rate']}%  Sharpe={r['sharpe']:.2f}  Return={r['return_pct']}%  MaxDD={r['max_dd_pct']}%")
    else:
        print("  None found")

    # Save results
    results_df = pd.DataFrame(results)
    output_path = Path(__file__).parent / "reports" / "new_indices_backtest.csv"
    results_df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()
