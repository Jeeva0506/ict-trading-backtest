# backtest_live_setup.py
import pandas as pd
from pathlib import Path
from datetime import timedelta

from strategy.strategy_ict_clean import StrategyICTClean

BROKER_POINT_VALUES = {"SPX500": 10.0, "US30": 10.0}

LIVE_INDEX_CONFIG = {
    "SPX500": {
        "local_file": "SPX500_1YEAR_M1.csv",
        "session_start_hour": 16,
        "session_start_minute": 30,
        "session_end_hour": 22,
        "session_end_minute": 45,
        "sl_buffer": 1.0,
        "name": "S&P 500",
    },
    "US30": {
        "local_file": "US30_1YEAR_M1.csv",
        "session_start_hour": 16,
        "session_start_minute": 30,
        "session_end_hour": 22,
        "session_end_minute": 45,
        "sl_buffer": 5.0,
        "name": "Dow Jones 30",
    }
}

ADAPTIVE_RISK = {
    "enabled": True,
    "default_risk_percent": 1.5,
    "reduced_risk_percent": 1.0,
    "daily_dd_reduce_threshold": 1.0,
    "daily_dd_stop_threshold": 2.0,
}

ACCOUNT_RULES = {"starting_balance": 6000.0, "max_drawdown_pct": 8.0}

TRADING_COSTS = {
    "slippage_points": {"SPX500": 0.3, "US30": 1.0},
    "commission_per_lot": {"SPX500": 0.0, "US30": 0.0},
}


def load_data(filename: str) -> pd.DataFrame:
    data_path = Path(__file__).parent / "data" / "raw" / filename
    if not data_path.exists():
        raise FileNotFoundError(f"Missing data file: {data_path}")

    # Your SPX file is tab-separated
    df = pd.read_csv(data_path, sep="\t", parse_dates=["Time"])
    df.set_index("Time", inplace=True)
    df.columns = [c.lower() for c in df.columns]
    df = df.sort_index()
    return df


def get_adaptive_risk(current_balance: float, day_peak_balance: float):
    if not ADAPTIVE_RISK["enabled"]:
        return ADAPTIVE_RISK["default_risk_percent"], False

    daily_dd_pct = ((day_peak_balance - current_balance) / day_peak_balance) * 100.0

    if daily_dd_pct >= ADAPTIVE_RISK["daily_dd_stop_threshold"]:
        return ADAPTIVE_RISK["reduced_risk_percent"], True
    if daily_dd_pct >= ADAPTIVE_RISK["daily_dd_reduce_threshold"]:
        return ADAPTIVE_RISK["reduced_risk_percent"], False
    return ADAPTIVE_RISK["default_risk_percent"], False


def check_account_breach(current_balance: float, starting_balance: float) -> bool:
    max_dd = ((starting_balance - current_balance) / starting_balance) * 100.0
    return max_dd >= ACCOUNT_RULES["max_drawdown_pct"]


def run_live_simulation(run_days: int | None = 15, debug_strategy: bool = False, symbol: str = "SPX500"):
    cfg = LIVE_INDEX_CONFIG[symbol]
    df = load_data(cfg["local_file"])

    trading_days = sorted(set(df.index.date))
    if run_days is not None:
        trading_days = trading_days[:run_days]

    print(f"\n📊 {symbol} Backtest | {len(trading_days)} trading days")
    print(
        f"   Session window: {cfg['session_start_hour']:02d}:{cfg['session_start_minute']:02d} "
        f"→ {cfg['session_end_hour']:02d}:{cfg['session_end_minute']:02d} (server time)"
    )

    all_signals = []
    point_value = BROKER_POINT_VALUES[symbol]

    log_path = Path(__file__).parent / "detection_log.csv"
    if debug_strategy and log_path.exists():
        log_path.unlink()

    for day in trading_days:
        session_start = pd.Timestamp(day).replace(hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
        session_end = pd.Timestamp(day).replace(hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])

        market_df = df[(df.index >= session_start) & (df.index <= session_end)].copy()

        if day == trading_days[0]:
            day_data = df[df.index.date == day]
            print(f"\n   Sample day: {day}")
            print(f"   Full day rows: {len(day_data)} | {day_data.index.min()} → {day_data.index.max()}")
            print(f"   Session rows: {len(market_df)} | {market_df.index.min()} → {market_df.index.max()}")

        if len(market_df) < 80:
            continue

        strategy = StrategyICTClean(
            sl_buffer=cfg["sl_buffer"],
            bos_required=2,
            swing_lookback=5,
            pullback_min_opposite_candles=3,
            ob2_retracement=0.65,
            min_sl_points=2.0 if symbol == "SPX500" else 10.0,
            max_sl_points=25.0 if symbol == "SPX500" else 200.0,
            debug=debug_strategy,
        )

        signals = strategy.generate_signals(market_df)

        if debug_strategy and getattr(strategy, "debug_rows", None):
            pd.DataFrame(strategy.debug_rows).to_csv(
                log_path,
                mode="a",
                header=not log_path.exists(),
                index=False,
            )

        for s in signals:
            all_signals.append(
                {
                    "symbol": symbol,
                    "signal": s,
                    "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                }
            )

    all_signals.sort(key=lambda x: x["entry_time"])

    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    current_balance = STARTING_CAPITAL
    all_trades = []

    current_day = None
    day_peak_balance = STARTING_CAPITAL

    for sig in all_signals:
        s = sig["signal"]
        point_value = sig["point_value"]

        if check_account_breach(current_balance, STARTING_CAPITAL):
            print("\n⛔ Account breach hit. Stopping.")
            break

        trade_day = pd.Timestamp(s.entry_time).date()
        if trade_day != current_day:
            current_day = trade_day
            day_peak_balance = current_balance

        day_peak_balance = max(day_peak_balance, current_balance)

        risk_pct, should_stop = get_adaptive_risk(current_balance, day_peak_balance)
        if should_stop:
            continue

        risk_amount = current_balance * (risk_pct / 100.0)
        risk_points = float(s.risk_points)
        if risk_points <= 0:
            continue

        lots = risk_amount / (risk_points * point_value)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        exit_reason = getattr(s, "exit_reason", None)
        pnl_points = float(getattr(s, "pnl_points", 0.0))

        # your mapping
        if exit_reason == "TP3":
            result = "win"
        elif exit_reason == "SL_BE_TP2":
            result = "partial_win"
        elif exit_reason == "SL_BE":
            result = "breakeven"
        elif exit_reason == "SL_HIT":
            result = "loss"
        elif exit_reason == "SESSION_CLOSE":
            result = "win" if pnl_points > 0 else ("breakeven" if pnl_points == 0 else "loss")
        else:
            result = "win" if pnl_points > 0 else ("breakeven" if pnl_points == 0 else "loss")

        pnl_raw = pnl_points * lots * point_value
        slippage_cost = TRADING_COSTS["slippage_points"].get(symbol, 0.3) * lots * point_value
        commission_cost = TRADING_COSTS["commission_per_lot"].get(symbol, 0.0) * lots
        total_costs = slippage_cost + commission_cost
        pnl = pnl_raw - total_costs

        current_balance += pnl
        if pnl > 0:
            day_peak_balance = max(day_peak_balance, current_balance)

        all_trades.append(
            {
                "symbol": symbol,
                "date": pd.Timestamp(s.entry_time),
                "direction": s.direction,
                "result": result,
                "risk_percent": risk_pct,
                "risk_amount": risk_amount,
                "risk_points": risk_points,
                "pnl_points": pnl_points,
                "pnl_raw": pnl_raw,
                "costs": total_costs,
                "pnl": pnl,
                "balance": current_balance,
                "day_peak_balance": day_peak_balance,
                "lots": lots,
                "exit_reason": exit_reason,
            }
        )

    expected_cols = [
        "symbol","date","direction","result","risk_percent","risk_amount",
        "risk_points","pnl_points","pnl_raw","costs","pnl",
        "balance","day_peak_balance","lots","exit_reason"
    ]

    output_dir = Path(__file__).parent / "reports" / "live_simulation"
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / "trades.csv"

    if len(all_trades) == 0:
        print("\n⚠️ 0 trades generated for this run window.")
        pd.DataFrame(columns=expected_cols).to_csv(trades_path, index=False)
        print(f"Empty trades file saved to {trades_path}")
        return

    trades_df = pd.DataFrame(all_trades)
    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)
    trades_df.to_csv(trades_path, index=False)

    print(f"\n✅ Trades: {len(trades_df)} saved to {trades_path}")
    print(trades_df.head(10).to_string(index=False))

    # Summary stats
    wins = len(trades_df[trades_df['result'] == 'win'])
    partial_wins = len(trades_df[trades_df['result'] == 'partial_win'])
    breakevens = len(trades_df[trades_df['result'] == 'breakeven'])
    losses = len(trades_df[trades_df['result'] == 'loss'])
    total = len(trades_df)
    win_rate = ((wins + partial_wins) / total * 100) if total > 0 else 0
    net_pnl = trades_df['pnl'].sum()
    gross_profit = trades_df[trades_df['pnl'] > 0]['pnl'].sum()
    gross_loss = trades_df[trades_df['pnl'] < 0]['pnl'].sum()
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float('inf')

    print(f"\n{'='*50}")
    print(f"📈 SUMMARY")
    print(f"{'='*50}")
    print(f"  Total Trades : {total}")
    print(f"  Wins (TP3)   : {wins}")
    print(f"  Partial Wins : {partial_wins}  (TP2 hit, then SL at BE)")
    print(f"  Breakevens   : {breakevens}  (TP1 hit, SL at BE)")
    print(f"  Losses (SL)  : {losses}")
    print(f"  Win Rate     : {win_rate:.1f}%  (wins + partial wins)")
    print(f"  Net PnL      : ${net_pnl:,.2f}")
    print(f"  Gross Profit : ${gross_profit:,.2f}")
    print(f"  Gross Loss   : ${gross_loss:,.2f}")
    print(f"  Profit Factor: {profit_factor:.2f}")
    print(f"  Final Balance: ${current_balance:,.2f}  (started ${STARTING_CAPITAL:,.2f})")
    print(f"{'='*50}")

    if debug_strategy:
        print(f"\n🧾 Detection log saved to: {log_path}")


if __name__ == "__main__":
    # full year run
    run_live_simulation(run_days=15, debug_strategy=True, symbol="SPX500")
