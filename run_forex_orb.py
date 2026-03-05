"""
Forex ORB Backtest — London & New York Sessions (1-min bars)
=============================================================
Runs the same ORB Clean Breakout strategy used on indices,
adapted for forex pairs (EURUSD, USDJPY, XAUUSD) with:
  - 1-minute timeframe (native M1 data)
  - London ORB: 10:00 EET (08:00 GMT) — Europe open
  - New York ORB: 16:30 EET (14:30 GMT / 09:30 ET) — US open
  - HTF trend from native H1 data (EMA 20/50)
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent))
from strategy.strategy_ict_clean import StrategyICTClean, TradeSignalICT


# ── Forex pair configs ────────────────────────────────────────────
FOREX_CONFIG = {
    "EURUSD": {
        "m1_file": "EURUSD_3YEAR_M1.csv",
        "h1_file": "EURUSD_3YEAR_H1.csv",
        "name": "EUR/USD",
        "point_value": 100000.0,   # 1 standard lot = 100,000 units; 1 pip = $10/lot
        "pip_size": 0.0001,
        "sl_buffer": 0.00020,      # 2 pips buffer
        "min_sl_points": 0.00030,   # min 3 pips SL
        "max_sl_points": 0.00500,   # max 50 pips SL
        "spread_points": 0.00012,   # 1.2 pips typical
        "slippage_points": 0.00005, # 0.5 pips
    },
    "USDJPY": {
        "m1_file": "USDJPY_3YEAR_M1.csv",
        "h1_file": "USDJPY_3YEAR_H1.csv",
        "name": "USD/JPY",
        "point_value": 666.67,     # ~1000 JPY per pip per lot ÷ ~150 USDJPY
        "pip_size": 0.01,
        "sl_buffer": 0.020,        # 2 pips buffer
        "min_sl_points": 0.030,    # min 3 pips SL
        "max_sl_points": 0.500,    # max 50 pips SL
        "spread_points": 0.015,    # 1.5 pips
        "slippage_points": 0.010,  # 1 pip
    },
    "XAUUSD": {
        "m1_file": "XAUUSD_3YEAR_M1.csv",
        "h1_file": "XAUUSD_3YEAR_H1.csv",
        "name": "Gold (XAU/USD)",
        "point_value": 100.0,      # 1 lot = 100 oz; $1 move = $100/lot
        "pip_size": 0.01,
        "sl_buffer": 0.30,         # $0.30 buffer  
        "min_sl_points": 0.50,     # min $0.50 SL
        "max_sl_points": 15.0,     # max $15 SL
        "spread_points": 0.30,     # $0.30 spread
        "slippage_points": 0.15,   # $0.15 slippage
    },
}

# ── Session configs (all times in EET = broker time) ──────────────
SESSION_CONFIGS = {
    "London_ORB": {
        "name": "London ORB",
        "session_start_hour": 10,   # 08:00 GMT = 10:00 EET
        "session_start_minute": 0,
        "session_end_hour": 14,     # session runs till 14:00 EET (12:00 GMT)
        "session_end_minute": 0,
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
    },
    "NewYork_ORB": {
        "name": "New York ORB",
        "session_start_hour": 16,   # 14:30 GMT ≈ 16:30 EET (use 16:30)
        "session_start_minute": 30,
        "session_end_hour": 21,     # session runs till 21:00 EET (19:00 GMT)
        "session_end_minute": 0,
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
    },
}

# ── Strategy params (universal — same as live indices) ────────────
STRATEGY_PARAMS = {
    "disp_mult": 1.3,
    "body_min": 0.60,
    "close_strength": 0.25,
    "breakout_margin": 0.05,
    "orb_range_min_mult": 0.5,
    "tp3_r": 2.5,
}

# ── Portfolio / account ──────────────────────────────────────────
STARTING_BALANCE = 6000.0
RISK_PERCENT = 1.5
LEVERAGE = 100
MAX_DAILY_LOSSES = 2
MAX_DAILY_TRADES = 3  # 2 sessions + 1 buffer


def load_forex_data(filename: str, data_dir: str = "forexDATA") -> pd.DataFrame:
    """Load comma-separated forex CSV from forexDATA/ folder."""
    fpath = Path(__file__).parent / data_dir / filename
    if not fpath.exists():
        raise FileNotFoundError(f"Missing: {fpath}")
    df = pd.read_csv(fpath, sep=",")
    df["time"] = pd.to_datetime(df["time"], format="mixed", dayfirst=False)
    df.set_index("time", inplace=True)
    df.columns = [c.lower() for c in df.columns]
    df = df.sort_index()
    return df


def compute_htf_ema_forex(df_h1: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.DataFrame:
    """Compute EMA trend from native H1 data."""
    h1 = df_h1.copy()
    h1[f"ema{fast}"] = h1["close"].ewm(span=fast, adjust=False).mean()
    h1[f"ema{slow}"] = h1["close"].ewm(span=slow, adjust=False).mean()
    h1["htf_trend"] = "bullish"
    h1.loc[h1[f"ema{fast}"] < h1[f"ema{slow}"], "htf_trend"] = "bearish"
    return h1[["htf_trend", f"ema{fast}", f"ema{slow}"]]


def get_htf_trend(htf_ema: pd.DataFrame, session_start: pd.Timestamp) -> str | None:
    prior = htf_ema[htf_ema.index < session_start]
    if len(prior) == 0:
        return None
    return str(prior.iloc[-1]["htf_trend"])


def generate_signals_forex():
    """Generate ORB signals for all forex pairs × both sessions using M1 bars."""
    all_signals = []

    for sym, cfg in FOREX_CONFIG.items():
        print(f"\n{'='*60}")
        print(f"Loading {sym} ({cfg['name']})")
        print(f"{'='*60}")

        # Load M1 and H1
        df_m1 = load_forex_data(cfg["m1_file"])
        df_h1 = load_forex_data(cfg["h1_file"])
        print(f"  M1: {len(df_m1):,} bars ({df_m1.index[0]} → {df_m1.index[-1]})")
        print(f"  H1: {len(df_h1):,} bars")

        # HTF trend from native H1
        htf_ema = compute_htf_ema_forex(df_h1)
        print(f"  HTF EMA: {len(htf_ema)} H1 bars")

        trading_days = sorted(set(df_m1.index.date))
        print(f"  Trading days: {len(trading_days)} ({trading_days[0]} → {trading_days[-1]})")

        for session_key, sess_cfg in SESSION_CONFIGS.items():
            count = 0
            for day in trading_days:
                session_start = pd.Timestamp(day).replace(
                    hour=sess_cfg["session_start_hour"],
                    minute=sess_cfg["session_start_minute"])
                session_end = pd.Timestamp(day).replace(
                    hour=sess_cfg["session_end_hour"],
                    minute=sess_cfg["session_end_minute"])

                market_df = df_m1[(df_m1.index >= session_start) & (df_m1.index <= session_end)].copy()
                # With M1 bars, 4-hour session = ~240 bars; need at least 60
                if len(market_df) < 60:
                    continue

                htf_trend = get_htf_trend(htf_ema, session_start)

                strategy = StrategyICTClean(
                    sl_buffer=cfg["sl_buffer"],
                    min_sl_points=cfg["min_sl_points"],
                    max_sl_points=cfg["max_sl_points"],
                    orb_minutes=sess_cfg["orb_minutes"],
                    entry_hours=sess_cfg["entry_hours"],
                    breakout_window_minutes=sess_cfg["breakout_window_minutes"],
                    disp_mult=STRATEGY_PARAMS["disp_mult"],
                    body_min=STRATEGY_PARAMS["body_min"],
                    close_strength=STRATEGY_PARAMS["close_strength"],
                    breakout_margin=STRATEGY_PARAMS["breakout_margin"],
                    orb_range_min_mult=STRATEGY_PARAMS["orb_range_min_mult"],
                    tp3_r=STRATEGY_PARAMS["tp3_r"],
                )
                signals = strategy.generate_signals(market_df, htf_trend=htf_trend)

                for s in signals:
                    all_signals.append({
                        "symbol": sym,
                        "session": session_key,
                        "signal": s,
                        "point_value": cfg["point_value"],
                        "spread": cfg["spread_points"],
                        "slippage": cfg["slippage_points"],
                        "entry_time": pd.Timestamp(s.entry_time),
                        "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                    })
                    count += 1

            print(f"  {sym} × {sess_cfg['name']}: {count} signals")

    all_signals.sort(key=lambda x: x["entry_time"])
    print(f"\nTotal raw signals: {len(all_signals)}")
    return all_signals


def run_portfolio(all_signals):
    """Execute all trades with costs, adaptive risk, and position limits."""
    balance = STARTING_BALANCE
    peak_balance = STARTING_BALANCE
    trades = []
    daily_losses = {}
    daily_count = {}

    for sig in all_signals:
        s = sig["signal"]
        sym = sig["symbol"]
        session = sig["session"]
        pv = sig["point_value"]
        spread = sig["spread"]
        slippage = sig["slippage"]
        entry_time = sig["entry_time"]

        day = entry_time.date()

        # Daily limits
        if daily_losses.get(day, 0) >= MAX_DAILY_LOSSES:
            continue
        if daily_count.get(day, 0) >= MAX_DAILY_TRADES:
            continue

        # Account breach — DISABLED for research (show full 3yr picture)
        # dd_pct = ((peak_balance - balance) / peak_balance) * 100 if peak_balance > 0 else 0
        # if dd_pct >= 10.0:
        #     break

        # Risk sizing
        risk_usd = balance * (RISK_PERCENT / 100.0)
        R = s.risk_points
        if R <= 0:
            continue

        lot_size = risk_usd / (R * pv)
        lot_size = max(0.01, round(lot_size, 2))

        # Apply costs
        total_cost_points = spread + slippage  # adverse entry cost
        exit_cost = 0.0
        if s.exit_reason in ("SL_HIT", "SL_BE"):
            exit_cost = slippage  # SL = market order, slipped

        raw_pnl = s.pnl_points
        net_pnl_points = raw_pnl - total_cost_points - exit_cost
        pnl_usd = net_pnl_points * pv * lot_size

        balance += pnl_usd
        peak_balance = max(peak_balance, balance)

        is_loss = pnl_usd < 0
        if is_loss:
            daily_losses[day] = daily_losses.get(day, 0) + 1
        daily_count[day] = daily_count.get(day, 0) + 1

        trades.append({
            "symbol": sym,
            "session": session,
            "direction": s.direction,
            "entry_time": entry_time,
            "exit_time": s.exit_time,
            "entry_price": s.entry_price,
            "exit_price": s.exit_price,
            "exit_reason": s.exit_reason,
            "risk_points": R,
            "raw_pnl_pts": raw_pnl,
            "net_pnl_pts": net_pnl_points,
            "lot_size": lot_size,
            "pnl_usd": round(pnl_usd, 2),
            "balance": round(balance, 2),
        })

    return trades


def print_report(trades):
    """Print comprehensive results."""
    if not trades:
        print("\n*** NO TRADES GENERATED ***")
        return

    df = pd.DataFrame(trades)
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    df["year"] = df["entry_time"].dt.year

    print("\n" + "=" * 75)
    print("FOREX ORB BACKTEST — COMPREHENSIVE RESULTS")
    print("=" * 75)

    # ── Overall ──
    total = len(df)
    wins = (df["pnl_usd"] > 0).sum()
    losses = (df["pnl_usd"] <= 0).sum()
    wr = wins / total * 100 if total > 0 else 0
    total_pnl = df["pnl_usd"].sum()
    final_bal = STARTING_BALANCE + total_pnl
    gross_profit = df.loc[df["pnl_usd"] > 0, "pnl_usd"].sum()
    gross_loss = abs(df.loc[df["pnl_usd"] <= 0, "pnl_usd"].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_win = df.loc[df["pnl_usd"] > 0, "pnl_usd"].mean() if wins > 0 else 0
    avg_loss = abs(df.loc[df["pnl_usd"] <= 0, "pnl_usd"].mean()) if losses > 0 else 0
    rr = avg_win / avg_loss if avg_loss > 0 else float("inf")

    # Max drawdown
    cum = df["pnl_usd"].cumsum() + STARTING_BALANCE
    peak = cum.cummax()
    dd = (peak - cum) / peak * 100
    max_dd_pct = dd.max()

    # Expectancy
    expectancy = (wr / 100 * avg_win) - ((100 - wr) / 100 * avg_loss)

    print(f"\n  Total Trades:     {total}")
    print(f"  Win Rate:         {wr:.1f}%  ({wins}W / {losses}L)")
    print(f"  Profit Factor:    {pf:.2f}")
    print(f"  Avg Win/Loss RR:  {rr:.2f}")
    print(f"  Expectancy:       ${expectancy:.2f}/trade")
    print(f"  Total P&L:        ${total_pnl:,.2f}")
    print(f"  Final Balance:    ${final_bal:,.2f}")
    print(f"  Return:           {(final_bal/STARTING_BALANCE - 1)*100:.1f}%")
    print(f"  Max Drawdown:     {max_dd_pct:.1f}%")
    days_span = (df["entry_time"].max() - df["entry_time"].min()).days
    print(f"  Period:           {days_span} days (~{days_span/365:.1f} years)")
    print(f"  Trades/Week:      {total / max(1, days_span/7):.1f}")

    # ── By Session ──
    print(f"\n{'─'*75}")
    print("BY SESSION:")
    print(f"{'─'*75}")
    print(f"  {'Session':<18} {'Trades':>7} {'WR%':>7} {'PF':>7} {'P&L':>10} {'MaxDD%':>8} {'Expect':>8}")
    for sess in sorted(df["session"].unique()):
        sub = df[df["session"] == sess]
        n = len(sub)
        w = (sub["pnl_usd"] > 0).sum()
        wr_s = w / n * 100 if n > 0 else 0
        gp = sub.loc[sub["pnl_usd"] > 0, "pnl_usd"].sum()
        gl = abs(sub.loc[sub["pnl_usd"] <= 0, "pnl_usd"].sum())
        pf_s = gp / gl if gl > 0 else float("inf")
        pnl_s = sub["pnl_usd"].sum()
        aw = sub.loc[sub["pnl_usd"] > 0, "pnl_usd"].mean() if w > 0 else 0
        al = abs(sub.loc[sub["pnl_usd"] <= 0, "pnl_usd"].mean()) if (n - w) > 0 else 0
        exp_s = (wr_s / 100 * aw) - ((100 - wr_s) / 100 * al)
        cum_s = sub["pnl_usd"].cumsum() + STARTING_BALANCE
        pk_s = cum_s.cummax()
        dd_s = ((pk_s - cum_s) / pk_s * 100).max()
        print(f"  {sess:<18} {n:>7} {wr_s:>6.1f}% {pf_s:>7.2f} {pnl_s:>+10,.0f} {dd_s:>7.1f}% {exp_s:>+7.1f}")

    # ── By Symbol ──
    print(f"\n{'─'*75}")
    print("BY PAIR:")
    print(f"{'─'*75}")
    print(f"  {'Pair':<12} {'Trades':>7} {'WR%':>7} {'PF':>7} {'P&L':>10} {'MaxDD%':>8} {'Expect':>8}")
    for sym in sorted(df["symbol"].unique()):
        sub = df[df["symbol"] == sym]
        n = len(sub)
        w = (sub["pnl_usd"] > 0).sum()
        wr_s = w / n * 100 if n > 0 else 0
        gp = sub.loc[sub["pnl_usd"] > 0, "pnl_usd"].sum()
        gl = abs(sub.loc[sub["pnl_usd"] <= 0, "pnl_usd"].sum())
        pf_s = gp / gl if gl > 0 else float("inf")
        pnl_s = sub["pnl_usd"].sum()
        aw = sub.loc[sub["pnl_usd"] > 0, "pnl_usd"].mean() if w > 0 else 0
        al = abs(sub.loc[sub["pnl_usd"] <= 0, "pnl_usd"].mean()) if (n - w) > 0 else 0
        exp_s = (wr_s / 100 * aw) - ((100 - wr_s) / 100 * al)
        cum_s = sub["pnl_usd"].cumsum() + STARTING_BALANCE
        pk_s = cum_s.cummax()
        dd_s = ((pk_s - cum_s) / pk_s * 100).max()
        print(f"  {sym:<12} {n:>7} {wr_s:>6.1f}% {pf_s:>7.2f} {pnl_s:>+10,.0f} {dd_s:>7.1f}% {exp_s:>+7.1f}")

    # ── By Symbol × Session ──
    print(f"\n{'─'*75}")
    print("BY PAIR × SESSION:")
    print(f"{'─'*75}")
    print(f"  {'Pair × Session':<28} {'Trades':>7} {'WR%':>7} {'PF':>7} {'P&L':>10} {'Expect':>8}")
    for sym in sorted(df["symbol"].unique()):
        for sess in sorted(df["session"].unique()):
            sub = df[(df["symbol"] == sym) & (df["session"] == sess)]
            n = len(sub)
            if n == 0:
                continue
            w = (sub["pnl_usd"] > 0).sum()
            wr_s = w / n * 100 if n > 0 else 0
            gp = sub.loc[sub["pnl_usd"] > 0, "pnl_usd"].sum()
            gl = abs(sub.loc[sub["pnl_usd"] <= 0, "pnl_usd"].sum())
            pf_s = gp / gl if gl > 0 else float("inf")
            pnl_s = sub["pnl_usd"].sum()
            aw = sub.loc[sub["pnl_usd"] > 0, "pnl_usd"].mean() if w > 0 else 0
            al = abs(sub.loc[sub["pnl_usd"] <= 0, "pnl_usd"].mean()) if (n - w) > 0 else 0
            exp_s = (wr_s / 100 * aw) - ((100 - wr_s) / 100 * al)
            label = f"{sym} × {SESSION_CONFIGS[sess]['name']}"
            print(f"  {label:<28} {n:>7} {wr_s:>6.1f}% {pf_s:>7.2f} {pnl_s:>+10,.0f} {exp_s:>+7.1f}")

    # ── By Year ──
    print(f"\n{'─'*75}")
    print("BY YEAR:")
    print(f"{'─'*75}")
    print(f"  {'Year':<8} {'Trades':>7} {'WR%':>7} {'PF':>7} {'P&L':>10}")
    for yr in sorted(df["year"].unique()):
        sub = df[df["year"] == yr]
        n = len(sub)
        w = (sub["pnl_usd"] > 0).sum()
        wr_s = w / n * 100 if n > 0 else 0
        gp = sub.loc[sub["pnl_usd"] > 0, "pnl_usd"].sum()
        gl = abs(sub.loc[sub["pnl_usd"] <= 0, "pnl_usd"].sum())
        pf_s = gp / gl if gl > 0 else float("inf")
        pnl_s = sub["pnl_usd"].sum()
        print(f"  {yr:<8} {n:>7} {wr_s:>6.1f}% {pf_s:>7.2f} {pnl_s:>+10,.0f}")

    # ── Exit Reasons ──
    print(f"\n{'─'*75}")
    print("EXIT REASONS:")
    print(f"{'─'*75}")
    for reason, count in df["exit_reason"].value_counts().items():
        sub = df[df["exit_reason"] == reason]
        pnl_r = sub["pnl_usd"].sum()
        print(f"  {reason:<18} {count:>5} trades  ${pnl_r:>+10,.0f}")

    # ── Save CSV ──
    out_dir = Path(__file__).parent / "reports" / "forex_orb"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "forex_orb_trades.csv"
    df.to_csv(csv_path, index=False)
    print(f"\n  Trade log saved: {csv_path}")

    return df


if __name__ == "__main__":
    print("=" * 75)
    print("FOREX ORB BACKTEST — 1-min bars, London + New York ORB")
    print(f"Pairs: {', '.join(FOREX_CONFIG.keys())}")
    print(f"Sessions: {', '.join(s['name'] for s in SESSION_CONFIGS.values())}")
    print(f"Account: ${STARTING_BALANCE:,.0f} | Risk: {RISK_PERCENT}% | Leverage: 1:{LEVERAGE}")
    print("=" * 75)

    signals = generate_signals_forex()
    trades = run_portfolio(signals)
    print_report(trades)
