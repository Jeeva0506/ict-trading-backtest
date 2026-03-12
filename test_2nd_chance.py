"""
A/B Test: 2nd-Chance Trade Rule
================================
Version A (Current) : 1 trade per instrument per day, period.
Version B (2nd Chance): If 1st trade loses → allow 1 more in opposite direction.
                        If 1st trade wins → done for the day.

Uses the 5-index portfolio (SPX500, NDX100, GER30, FRA40, US30).
Each index uses its own default config.

We modify the strategy to optionally return up to 2 signals per day,
and the portfolio engine to enforce the 2nd-chance rule.
"""

import sys
import copy
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict
from datetime import date

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, ADAPTIVE_RISK,
    ACCOUNT_RULES, FUNDEDNEXT_RULES, MAX_OPEN_POSITIONS, MAX_DAILY_LOSSES,
    MAX_DAILY_TRADES, LEVERAGE, load_data, compute_htf_ema, get_htf_trend_for_day,
    get_adaptive_risk, StrategyICTClean,
    get_vol_scaled_slippage, is_high_vol_event_day,
)

DATA_DIR = Path(__file__).parent / "data" / "raw"
EMA_FAST = 20
EMA_SLOW = 50

DATA_FILES = {
    "SPX500": "SPX500_10YR_M1_CLEAN.csv",
    "NDX100": "NDX100_10YR_M1_CLEAN.csv",
    "GER30":  "GER30_10YR_M1_CLEAN.csv",
    "US30":   "US30_10YR_M1_CLEAN.csv",
    "FRA40":  "FRA40_10YR_M1_CLEAN.csv",
}

GER30_CLEAN_PERIODS = {
    "GER30_A": {
        "label": "GER30 Period A (2010-2020)",
        "start": date(2010, 11, 1),
        "end":   date(2020, 6, 12),
    },
    "GER30_B": {
        "label": "GER30 Period B (2024-2026)",
        "start": date(2023, 12, 4),
        "end":   date(2026, 12, 31),
    },
}


# ── Modified Strategy: can return up to 2 signals ──

class StrategyICTClean2ndChance(StrategyICTClean):
    """
    Modified strategy that can generate up to 2 signals per day.
    - 1st signal: normal breakout
    - 2nd signal: opposite direction only, after the 1st has completed
    """

    def generate_signals_multi(self, df, htf_trend=None, daily_atr=None, max_signals=2):
        """
        Like generate_signals but allows up to max_signals per day.
        Removes the `break` after first signal.
        """
        self.debug_rows = []
        out = []

        if df is None or len(df) < 30:
            return out

        cols = {c.lower(): c for c in df.columns}
        for need in ["open", "high", "low", "close"]:
            if need not in cols:
                raise ValueError(f"Missing '{need}' column in df.")

        w = df.copy()
        session_start = w.index[0]
        orb_end = session_start + pd.Timedelta(minutes=self.orb_minutes)
        entry_cutoff = session_start + pd.Timedelta(hours=self.entry_hours)
        breakout_deadline = orb_end + pd.Timedelta(minutes=self.breakout_window_minutes)
        cutoff_close = w.index[-1] - pd.Timedelta(minutes=15)

        orb = w[(w.index >= session_start) & (w.index < orb_end)]
        if len(orb) < max(3, self.orb_minutes - 2):
            return out

        orbH = float(orb["high"].max())
        orbL = float(orb["low"].min())
        orb_range = orbH - orbL
        if orb_range <= 0:
            return out

        # Volatility contraction filter
        orb_candle_ranges = orb["high"] - orb["low"]
        orb_avg_candle_range = float(orb_candle_ranges.mean())
        expected_range = orb_avg_candle_range * len(orb) * 0.40

        if self.orb_filter_mode == "current":
            orb_ok = not (orb_range > expected_range and expected_range > 0)
        elif self.orb_filter_mode == "atr":
            if daily_atr and daily_atr > 0:
                orb_ok = (orb_range <= self.atr_pct * daily_atr)
            else:
                orb_ok = not (orb_range > expected_range and expected_range > 0)
        elif self.orb_filter_mode == "hybrid":
            current_pass = not (orb_range > expected_range and expected_range > 0)
            atr_pass = (orb_range <= self.atr_pct * daily_atr) if (daily_atr and daily_atr > 0) else True
            orb_ok = current_pass and atr_pass
        else:
            orb_ok = not (orb_range > expected_range and expected_range > 0)

        if not orb_ok:
            return out

        # ORB bias
        orb_open = float(orb.iloc[0]["open"])
        orb_close = float(orb.iloc[-1]["close"])
        orb_bias = "bullish" if orb_close > orb_open else "bearish"

        # For 2nd-chance mode, we DON'T require HTF+ORB agreement for the 2nd trade
        # (since it's in opposite direction). For 1st trade, we do require alignment.
        if htf_trend is not None and htf_trend != orb_bias:
            # HTF and ORB disagree → no trades at all
            return out

        trend_filter = orb_bias

        scan = w[(w.index >= orb_end) & (w.index < entry_cutoff)]
        if len(scan) < 10:
            return out

        scan = scan.copy().reset_index()
        idx_col = [c for c in scan.columns if c not in ["open", "high", "low", "close", "volume"]][0]
        scan = scan.rename(columns={idx_col: "ts"})
        scan["range"] = scan["high"] - scan["low"]
        scan["avg_range"] = scan["range"].rolling(self.lookback_ranges, min_periods=3).mean()

        start_i = max(3, self.lookback_ranges)
        used_directions = set()

        for i in range(start_i, len(scan)):
            if len(out) >= max_signals:
                break

            ts = scan.at[i, "ts"]
            if ts >= cutoff_close or ts > breakout_deadline:
                break

            o = float(scan.at[i, "open"])
            h = float(scan.at[i, "high"])
            l = float(scan.at[i, "low"])
            c = float(scan.at[i, "close"])
            candle_range = h - l

            direction = None
            margin = self.breakout_margin * orb_range
            if c > orbH + margin:
                direction = "bullish"
            elif c < orbL - margin:
                direction = "bearish"
            else:
                continue

            # Already traded this direction
            if direction in used_directions:
                continue

            # 1st trade must align with trend; 2nd trade is opposite
            if len(out) == 0:
                if direction != trend_filter:
                    continue
            else:
                # 2nd trade: must be OPPOSITE direction of 1st
                if direction == out[0].direction:
                    continue

            # Displacement filter
            avg_r = float(scan.at[i, "avg_range"]) if pd.notna(scan.at[i, "avg_range"]) else None
            if avg_r is None or avg_r <= 0:
                continue
            if candle_range < self.disp_mult * avg_r:
                continue

            # Body % filter
            body_pct = abs(c - o) / candle_range if candle_range > 0 else 0.0
            if body_pct < self.body_min:
                continue

            # Close strength filter
            if candle_range > 0:
                if direction == "bullish":
                    close_pos = (c - l) / candle_range
                    if close_pos < (1.0 - self.close_strength):
                        continue
                else:
                    close_pos = (h - c) / candle_range
                    if close_pos < (1.0 - self.close_strength):
                        continue

            # Pullback retest entry
            df_idx = w.index.get_indexer([ts])[0]
            retest_found = False
            entry_price = sl = next_idx = None
            retest_limit = min(df_idx + 1 + 20, len(w))

            for look in range(df_idx + 1, retest_limit):
                look_bar = w.iloc[look]
                look_ts = w.index[look]
                if look_ts >= cutoff_close:
                    break
                if direction == "bullish":
                    if float(look_bar["low"]) <= orbH + orb_range * 0.10:
                        if float(look_bar["close"]) >= orbH:
                            if look + 1 < len(w):
                                next_idx = look + 1
                                entry_price = float(w.iloc[next_idx]["open"])
                                sl = float(look_bar["low"]) - self.sl_buffer
                                retest_found = True
                            break
                else:
                    if float(look_bar["high"]) >= orbL - orb_range * 0.10:
                        if float(look_bar["close"]) <= orbL:
                            if look + 1 < len(w):
                                next_idx = look + 1
                                entry_price = float(w.iloc[next_idx]["open"])
                                sl = float(look_bar["high"]) + self.sl_buffer
                                retest_found = True
                            break

            if not retest_found:
                next_idx = df_idx + 1
                if next_idx >= len(w):
                    continue
                entry_price = float(w.iloc[next_idx]["open"])
                if direction == "bullish":
                    sl = l - self.sl_buffer
                else:
                    sl = h + self.sl_buffer

            if next_idx is None or entry_price is None or sl is None:
                continue

            if direction == "bullish":
                R = entry_price - sl
            else:
                R = sl - entry_price

            if R <= 0 or R < self.min_sl or R > self.max_sl:
                continue

            if retest_found:
                signal_close = float(w.iloc[next_idx - 1]["close"])
            else:
                signal_close = c

            trade = self._simulate_trade(
                df=w, start_idx=next_idx + 1, direction=direction,
                entry=entry_price, sl_init=sl, R=R, cutoff_time=cutoff_close,
            )
            trade.signal_price = signal_close
            trade.breakout_time = ts
            out.append(trade)
            used_directions.add(direction)

            # After 1st trade, shift scan start point past this trade's exit
            if len(out) == 1 and trade.exit_time is not None:
                exit_ts = pd.Timestamp(trade.exit_time)
                # Find where to resume scanning
                for j in range(i + 1, len(scan)):
                    if scan.at[j, "ts"] >= exit_ts:
                        start_i = j
                        break

        return out


def load_and_filter(symbol, start_date=None, end_date=None):
    fname = DATA_FILES.get(symbol)
    if fname is None:
        return None
    try:
        df = load_data(fname)
    except FileNotFoundError:
        return None
    if start_date:
        df = df[df.index.date >= start_date]
    if end_date:
        df = df[df.index.date <= end_date]
    return df if len(df) > 0 else None


def generate_signals(symbol, df, label=None, mode="single"):
    """Generate signals. mode='single' for Version A, 'multi' for Version B."""
    if label is None:
        label = symbol
    cfg = LIVE_INDEX_CONFIG.get(symbol)
    if cfg is None:
        return []

    htf_ema = compute_htf_ema(df, fast=EMA_FAST, slow=EMA_SLOW)
    trading_days = sorted(set(df.index.date))
    point_value = BROKER_POINT_VALUES[symbol]

    # Use the extended strategy class
    strategy = StrategyICTClean2ndChance(
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
    )

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

        if mode == "single":
            day_signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
        else:
            day_signals = strategy.generate_signals_multi(
                market_df, htf_trend=htf_trend, max_signals=2)

        for s in day_signals:
            signals.append({
                "symbol": symbol, "segment": label, "signal": s,
                "point_value": point_value,
                "entry_time": pd.Timestamp(s.entry_time),
                "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
            })

    return signals


def generate_all_signals(mode="single"):
    all_signals = []

    # SPX500
    df = load_and_filter("SPX500")
    if df is not None:
        all_signals.extend(generate_signals("SPX500", df, mode=mode))

    # NDX100
    df = load_and_filter("NDX100")
    if df is not None:
        all_signals.extend(generate_signals("NDX100", df, mode=mode))

    # GER30 (split)
    for pkey, pcfg in GER30_CLEAN_PERIODS.items():
        df = load_and_filter("GER30", start_date=pcfg["start"], end_date=pcfg["end"])
        if df is not None:
            all_signals.extend(generate_signals("GER30", df, pcfg["label"], mode=mode))

    # FRA40
    df = load_and_filter("FRA40")
    if df is not None:
        all_signals.extend(generate_signals("FRA40", df, mode=mode))

    # US30
    df = load_and_filter("US30")
    if df is not None:
        all_signals.extend(generate_signals("US30", df, "US30 (Dukascopy)", mode=mode))

    all_signals.sort(key=lambda x: x["entry_time"])
    return all_signals


def run_portfolio(all_signals, version="A", max_daily_trades_override=None):
    """
    Version A: Standard — 1 trade per instrument per day, MAX_DAILY_TRADES global cap
    Version B: 2nd Chance — if 1st trade on an instrument loses, allow opposite direction
               MAX_DAILY_TRADES raised to 3 to accommodate 2nd chances
    """
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    MAX_DD_PCT = ACCOUNT_RULES.get("max_drawdown_pct", 10.0)
    max_dt = max_daily_trades_override or MAX_DAILY_TRADES

    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL
    open_positions = []
    daily_losses = {}
    daily_trade_counts = {}
    # Track per-instrument per-day results for 2nd-chance rule
    daily_instrument_trades = {}  # {(date, symbol): [list of (direction, result, pnl)]}
    all_trades = []
    skipped_reasons = defaultdict(int)
    account_terminated = False
    account_resets = 0

    _r_by_sym = defaultdict(list)
    for sig in all_signals:
        _r_by_sym[sig["symbol"]].append(float(sig["signal"].risk_points))
    median_R = {sym: float(np.median(rs)) for sym, rs in _r_by_sym.items()}

    current_day = None
    day_peak_balance = STARTING_CAPITAL
    day_start_balance = STARTING_CAPITAL

    for sig in all_signals:
        if account_terminated:
            account_resets += 1
            account_terminated = False
            current_balance = STARTING_CAPITAL
            peak_balance = STARTING_CAPITAL
            day_peak_balance = STARTING_CAPITAL
            day_start_balance = STARTING_CAPITAL
            daily_losses.clear()
            daily_trade_counts.clear()
            daily_instrument_trades.clear()
            open_positions.clear()

        s = sig["signal"]
        symbol = sig["symbol"]
        segment = sig.get("segment", symbol)
        point_value = sig["point_value"]
        entry_time = sig["entry_time"]
        exit_time = sig["exit_time"]
        trade_day = entry_time.date()
        risk_points = float(s.risk_points)

        if trade_day != current_day:
            current_day = trade_day
            day_peak_balance = current_balance
            day_start_balance = current_balance

        day_peak_balance = max(day_peak_balance, current_balance)

        # Daily loss limit
        if daily_losses.get(trade_day, 0) >= MAX_DAILY_LOSSES:
            skipped_reasons["daily_loss_limit"] += 1
            continue

        # Daily trade cap
        if daily_trade_counts.get(trade_day, 0) >= max_dt:
            skipped_reasons["max_daily_trades"] += 1
            continue

        # 2nd-chance rule for Version B
        day_sym_key = (trade_day, symbol)
        prev_trades = daily_instrument_trades.get(day_sym_key, [])

        if version == "A":
            # Version A: strict 1 trade per instrument per day
            if len(prev_trades) >= 1:
                skipped_reasons["1_per_instrument"] += 1
                continue
        elif version == "B":
            if len(prev_trades) >= 1:
                last_dir, last_result, last_pnl = prev_trades[-1]
                if last_pnl > 0:
                    # Won → done for this instrument today
                    skipped_reasons["won_done"] += 1
                    continue
                if len(prev_trades) >= 2:
                    # Already took 2nd chance, no more
                    skipped_reasons["2nd_chance_done"] += 1
                    continue
                # Lost → allow opposite direction only
                if s.direction == last_dir:
                    skipped_reasons["same_dir_block"] += 1
                    continue
                # OK: 2nd chance, opposite direction

        # Position limit
        open_positions = [p for p in open_positions if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            skipped_reasons["max_positions"] += 1
            continue

        # Min R filter
        MIN_R_BY_SYMBOL = {"SPX500": 3.0, "US2000": 3.0}
        MIN_R_POINTS = MIN_R_BY_SYMBOL.get(symbol, 5.0)
        if risk_points < MIN_R_POINTS:
            skipped_reasons["min_R"] += 1
            continue

        # Spread guard
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        if risk_points > 0 and (spread_pts / risk_points) > 0.30:
            skipped_reasons["spread_guard"] += 1
            continue

        # Stale signal check
        breakout_time = getattr(s, 'breakout_time', None)
        if breakout_time is not None:
            bars_delay = int((pd.Timestamp(s.entry_time) -
                              pd.Timestamp(breakout_time)).total_seconds() / 60)
            if bars_delay > 15:
                skipped_reasons["stale_signal"] += 1
                continue

        # Price drift check
        signal_price = getattr(s, 'signal_price', None)
        if signal_price is not None and risk_points > 0:
            drift = abs(float(s.entry_price) - float(signal_price))
            if drift > 0.5 * risk_points:
                skipped_reasons["price_drift"] += 1
                continue

        # Adaptive risk
        risk_pct, should_stop = get_adaptive_risk(
            current_balance, day_peak_balance, starting_balance=STARTING_CAPITAL)
        if should_stop:
            skipped_reasons["adaptive_stop"] += 1
            continue

        risk_amount = current_balance * (risk_pct / 100.0)
        if risk_points <= 0:
            continue
        lots = risk_amount / (risk_points * point_value)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        exit_reason = getattr(s, "exit_reason", None)
        pnl_points = float(getattr(s, "pnl_points", 0.0))

        # Costs
        base_slippage = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
        high_vol = is_high_vol_event_day(entry_time, symbol)
        slippage_pts = get_vol_scaled_slippage(
            base_slippage, risk_points,
            median_R.get(symbol, risk_points), high_vol)
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        half_spread = spread_pts / 2.0
        entry_cost_pts = slippage_pts + half_spread
        exit_cost_pts = half_spread if exit_reason == "TP" else slippage_pts + half_spread
        total_cost_pts = entry_cost_pts + exit_cost_pts
        commission_cost = TRADING_COSTS["commission_per_lot"].get(symbol, 0.0) * lots

        adjusted_pnl_points = pnl_points - total_cost_pts
        if adjusted_pnl_points > 0:
            result = "win" if exit_reason == "TP" else "partial_win"
        elif adjusted_pnl_points == 0:
            result = "breakeven"
        else:
            result = "loss"

        pnl_raw = pnl_points * lots * point_value
        total_costs = total_cost_pts * lots * point_value + commission_cost
        pnl = pnl_raw - total_costs

        current_balance += pnl
        if pnl > 0:
            day_peak_balance = max(day_peak_balance, current_balance)
        peak_balance = max(peak_balance, current_balance)

        overall_dd_pct = ((STARTING_CAPITAL - current_balance) / STARTING_CAPITAL) * 100
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True

        daily_ref_post = max(day_start_balance, day_peak_balance)
        daily_dd_pct = ((daily_ref_post - current_balance) / daily_ref_post * 100) if daily_ref_post > 0 else 0
        trailing_dd_pct = ((peak_balance - current_balance) / peak_balance * 100) if peak_balance > 0 else 0

        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1
        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1
        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})

        # Track per-instrument-day for 2nd chance rule
        daily_instrument_trades.setdefault(day_sym_key, []).append(
            (s.direction, result, pnl))

        is_2nd_chance = len(prev_trades) >= 1

        all_trades.append({
            "symbol": symbol, "segment": segment, "date": entry_time,
            "direction": s.direction, "result": result,
            "risk_percent": risk_pct, "risk_amount": risk_amount,
            "risk_points": risk_points, "pnl_points": pnl_points,
            "adjusted_pnl_points": adjusted_pnl_points,
            "pnl_raw": pnl_raw, "costs": total_costs, "pnl": pnl,
            "balance": current_balance, "peak_balance": peak_balance,
            "daily_dd_pct": round(daily_dd_pct, 2),
            "trailing_dd_pct": round(trailing_dd_pct, 2),
            "lots": lots, "exit_reason": exit_reason,
            "is_2nd_chance": is_2nd_chance,
        })

    return all_trades, dict(skipped_reasons), current_balance, peak_balance, account_resets


def compute_metrics(trades_df, label=""):
    total = len(trades_df)
    if total == 0:
        return None

    wins = len(trades_df[trades_df["result"].isin(["win", "partial_win"])])
    losses = len(trades_df[trades_df["result"] == "loss"])
    win_rate = wins / total * 100

    net_pnl = trades_df["pnl"].sum()
    gross_profit = trades_df[trades_df["pnl"] > 0]["pnl"].sum()
    gross_loss = trades_df[trades_df["pnl"] < 0]["pnl"].sum()
    pf = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")

    expectancy = net_pnl / total
    total_costs = trades_df["costs"].sum()

    equity = trades_df["balance"].values
    peak_eq = np.maximum.accumulate(equity)
    dd_pct = (peak_eq - equity) / peak_eq * 100
    max_dd = dd_pct.max()

    tdf = trades_df.copy()
    tdf["trade_date"] = tdf["date"].dt.date
    daily_pnl = tdf.groupby("trade_date")["pnl"].sum()
    avg_dp = daily_pnl.mean()
    std_dp = daily_pnl.std()
    sharpe = (avg_dp / std_dp * np.sqrt(252)) if std_dp > 0 else 0

    first_date = trades_df["date"].min()
    last_date = trades_df["date"].max()
    calendar_days = max(1, (last_date - first_date).days)
    calendar_weeks = max(1, calendar_days / 7)
    trades_per_week = total / calendar_weeks

    max_cl = cl = 0
    for r in trades_df["result"].tolist():
        if r == "loss":
            cl += 1
            max_cl = max(max_cl, cl)
        else:
            cl = 0

    return {
        "label": label, "trades": total, "tpw": round(trades_per_week, 1),
        "wins": wins, "losses": losses,
        "wr": round(win_rate, 1), "pf": round(pf, 2),
        "net_pnl": round(net_pnl, 2), "expectancy": round(expectancy, 2),
        "costs": round(total_costs, 2),
        "max_dd": round(max_dd, 1), "sharpe": round(sharpe, 2),
        "mcl": max_cl,
    }


def main():
    print("=" * 80)
    print("  A/B TEST: 2ND-CHANCE TRADE RULE")
    print("  Version A: 1 trade per instrument per day (current)")
    print("  Version B: If 1st trade loses → allow opposite direction 2nd trade")
    print("=" * 80)

    # Generate signals with both modes
    print("\n  Generating signals (Version A: single mode)...")
    signals_a = generate_all_signals(mode="single")
    print(f"    Total signals A: {len(signals_a)}")

    print("\n  Generating signals (Version B: multi mode)...")
    signals_b = generate_all_signals(mode="multi")
    print(f"    Total signals B: {len(signals_b)}")

    # Run Version A
    print("\n  Running Version A portfolio...")
    trades_a, skip_a, bal_a, peak_a, resets_a = run_portfolio(
        signals_a, version="A", max_daily_trades_override=2)
    df_a = pd.DataFrame(trades_a)
    df_a["date"] = pd.to_datetime(df_a["date"])
    m_a = compute_metrics(df_a, "Version A")

    # Run Version B
    print("  Running Version B portfolio...")
    trades_b, skip_b, bal_b, peak_b, resets_b = run_portfolio(
        signals_b, version="B", max_daily_trades_override=3)
    df_b = pd.DataFrame(trades_b)
    df_b["date"] = pd.to_datetime(df_b["date"])
    m_b = compute_metrics(df_b, "Version B")

    # 2nd-chance specific stats
    second_chance_trades = df_b[df_b["is_2nd_chance"] == True] if "is_2nd_chance" in df_b.columns else pd.DataFrame()
    first_trades = df_b[df_b["is_2nd_chance"] == False] if "is_2nd_chance" in df_b.columns else df_b

    # Print comparison
    print(f"""
{'='*80}
  RESULTS COMPARISON
{'='*80}

  {'Metric':<25} {'Version A':>15} {'Version B':>15} {'Delta':>12}
  {'-'*70}
  {'Total Trades':<25} {m_a['trades']:>15,} {m_b['trades']:>15,} {m_b['trades']-m_a['trades']:>+12,}
  {'Trades/Week':<25} {m_a['tpw']:>15.1f} {m_b['tpw']:>15.1f} {m_b['tpw']-m_a['tpw']:>+12.1f}
  {'Win Rate':<25} {m_a['wr']:>14.1f}% {m_b['wr']:>14.1f}% {m_b['wr']-m_a['wr']:>+11.1f}%
  {'Profit Factor':<25} {m_a['pf']:>15.2f} {m_b['pf']:>15.2f} {m_b['pf']-m_a['pf']:>+12.2f}
  {'Net P&L':<25} ${m_a['net_pnl']:>14,.2f} ${m_b['net_pnl']:>14,.2f} ${m_b['net_pnl']-m_a['net_pnl']:>+11,.2f}
  {'Expectancy/Trade':<25} ${m_a['expectancy']:>14,.2f} ${m_b['expectancy']:>14,.2f} ${m_b['expectancy']-m_a['expectancy']:>+11,.2f}
  {'Sharpe Ratio':<25} {m_a['sharpe']:>15.2f} {m_b['sharpe']:>15.2f} {m_b['sharpe']-m_a['sharpe']:>+12.2f}
  {'Max Drawdown':<25} {m_a['max_dd']:>14.1f}% {m_b['max_dd']:>14.1f}% {m_b['max_dd']-m_a['max_dd']:>+11.1f}%
  {'Max Consec Losses':<25} {m_a['mcl']:>15} {m_b['mcl']:>15} {m_b['mcl']-m_a['mcl']:>+12}
  {'Total Costs':<25} ${m_a['costs']:>14,.2f} ${m_b['costs']:>14,.2f} ${m_b['costs']-m_a['costs']:>+11,.2f}
  {'Account Resets':<25} {resets_a:>15} {resets_b:>15} {resets_b-resets_a:>+12}""")

    # 2nd-chance trade analysis
    if len(second_chance_trades) > 0:
        sc_wins = len(second_chance_trades[second_chance_trades["pnl"] > 0])
        sc_total = len(second_chance_trades)
        sc_wr = sc_wins / sc_total * 100
        sc_net = second_chance_trades["pnl"].sum()
        sc_avg = second_chance_trades["pnl"].mean()
        print(f"""
  -- 2ND-CHANCE TRADE ANALYSIS --
  2nd-chance trades    : {sc_total:>5}
  2nd-chance win rate  : {sc_wr:>5.1f}%
  2nd-chance net PnL   : ${sc_net:>12,.2f}
  2nd-chance avg PnL   : ${sc_avg:>12,.2f}

  1st trades only      : {len(first_trades):>5}
  1st trades net PnL   : ${first_trades['pnl'].sum():>12,.2f}""")

        # Per-index 2nd-chance breakdown
        print(f"\n  -- 2ND-CHANCE BY INDEX --")
        print(f"  {'Index':<10} {'2nd Trades':>10} {'WR':>7} {'Net PnL':>12} {'Avg PnL':>10}")
        print(f"  {'-'*53}")
        for sym in sorted(second_chance_trades["symbol"].unique()):
            sc_sym = second_chance_trades[second_chance_trades["symbol"] == sym]
            sc_w = len(sc_sym[sc_sym["pnl"] > 0])
            print(f"  {sym:<10} {len(sc_sym):>10} {sc_w/len(sc_sym)*100:>6.1f}% "
                  f"${sc_sym['pnl'].sum():>11,.2f} ${sc_sym['pnl'].mean():>9,.2f}")
    else:
        print("\n  No 2nd-chance trades were taken.")

    # Skipped reasons comparison
    print(f"\n  -- SKIPPED SIGNALS --")
    all_reasons = set(list(skip_a.keys()) + list(skip_b.keys()))
    print(f"  {'Reason':<25} {'Version A':>10} {'Version B':>10}")
    print(f"  {'-'*47}")
    for r in sorted(all_reasons):
        a_val = skip_a.get(r, 0)
        b_val = skip_b.get(r, 0)
        if a_val > 0 or b_val > 0:
            print(f"  {r:<25} {a_val:>10} {b_val:>10}")

    # Verdict
    print(f"\n  -- VERDICT --")
    if m_b["pf"] > m_a["pf"] and m_b["net_pnl"] > m_a["net_pnl"]:
        print(f"  Version B WINS: Higher PF ({m_b['pf']} vs {m_a['pf']}) "
              f"and more profit (${m_b['net_pnl']:,.0f} vs ${m_a['net_pnl']:,.0f})")
    elif m_b["net_pnl"] > m_a["net_pnl"]:
        print(f"  Version B has more profit but lower PF — marginal improvement")
    else:
        print(f"  Version A WINS: Keep current rule (1 trade per instrument per day)")

    print("=" * 80)


if __name__ == "__main__":
    main()
