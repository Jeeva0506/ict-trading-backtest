"""
10-Year Dukascopy Data Assembler & Backtest Runner
====================================================
1. Reads downloaded parquet chunks from data/dukascopy_chunks/
2. Normalizes prices (auto-detects and fixes divisor issues)
3. Converts UTC → EET/EEST (MT5 server timezone)
4. Saves M1 + H1 CSV files matching existing backtest format
5. Runs 10-year portfolio backtest

Usage:
  python run_10year_backtest.py             # Assemble + backtest
  python run_10year_backtest.py --assemble  # Assemble data only
"""

import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import Counter, defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from backtest_live_setup import (
    LIVE_INDEX_CONFIG, BROKER_POINT_VALUES, TRADING_COSTS, ADAPTIVE_RISK,
    ACCOUNT_RULES, FUNDEDNEXT_RULES, MAX_OPEN_POSITIONS, MAX_DAILY_LOSSES,
    MAX_DAILY_TRADES, LEVERAGE, load_data, compute_htf_ema, get_htf_trend_for_day,
    get_adaptive_risk, StrategyICTClean,
    get_vol_scaled_slippage, is_high_vol_event_day,
)

DATA_DIR = Path(__file__).parent / "data" / "raw"
CHUNKS_DIR = Path(__file__).parent / "data" / "dukascopy_chunks"

EMA_FAST = 20
EMA_SLOW = 50

ALL_SYMBOLS = ["SPX500", "NDX100", "GER30", "US30", "US2000"]

# Expected price ranges for each index (used for auto-correction)
# These are approximate ranges covering 2016-2026
EXPECTED_PRICE_RANGES = {
    "SPX500": (1500, 7000),
    "NDX100": (3000, 25000),
    "US30":   (14000, 50000),
    "GER30":  (7000, 25000),
    "US2000": (800, 3000),
}

# 10-year data file names
TEN_YEAR_FILES = {
    "SPX500": "SPX500_10YR_M1_CLEAN.csv",
    "NDX100": "NDX100_10YR_M1_CLEAN.csv",
    "GER30":  "GER30_10YR_M1_CLEAN.csv",
    "US30":   "US30_10YR_M1_CLEAN.csv",
    "US2000": "US2000_10YR_M1_CLEAN.csv",
}


def normalize_chunk_prices(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Auto-detect and fix price scaling issues in a chunk.
    
    Dukascopy stores raw integer prices with different divisors per instrument.
    Some chunks may have correct prices (from findatapy cache), while others
    have raw undivided prices.
    """
    if len(df) == 0:
        return df
    
    lo, hi = EXPECTED_PRICE_RANGES[symbol]
    price_cols = ["Open", "High", "Low", "Close"]
    sample_price = df["Close"].median()
    
    # Already in range — no correction needed
    if lo * 0.5 <= sample_price <= hi * 1.5:
        return df
    
    # Try divisors: 10, 100, 1000, 10000
    for divisor in [10.0, 100.0, 1000.0, 10000.0]:
        corrected = sample_price / divisor
        if lo * 0.5 <= corrected <= hi * 1.5:
            df = df.copy()
            for col in price_cols:
                df[col] = df[col] / divisor
            return df
    
    # Try multipliers (for under-scaled data like US30 at 1/10th)
    for multiplier in [10.0, 100.0]:
        corrected = sample_price * multiplier
        if lo * 0.5 <= corrected <= hi * 1.5:
            df = df.copy()
            for col in price_cols:
                df[col] = df[col] * multiplier
            return df
    
    # If no correction works, return as-is with warning
    print(f"  WARNING: {symbol} chunk has unexpected price {sample_price:.2f}, "
          f"expected {lo}-{hi}")
    return df


def convert_utc_to_eet(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert UTC timestamps to EET/EEST (MT5 server timezone).
    
    EET = UTC+2 (winter), EEST = UTC+3 (summer)
    Uses Europe/Bucharest which follows the same DST rules.
    """
    if df.index.tz is not None:
        # Already has timezone — convert
        df.index = df.index.tz_convert("Europe/Bucharest").tz_localize(None)
    else:
        # Naive UTC — localize then convert
        df.index = (
            df.index
            .tz_localize("UTC")
            .tz_convert("Europe/Bucharest")
            .tz_localize(None)
        )
    return df


def fill_session_gaps(m1: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Forward-fill missing M1 bars within each day's session window only.
    
    Dukascopy tick data is sparse — many minutes have zero ticks, so no
    M1 bar exists. MT5 broker data has continuous minute bars, which the
    ORB strategy depends on (ORB range = first 15 *minutes*, breakout
    windows counted in *minutes*, displacement vs recent candles, etc).
    
    For each trading day:
      1. Build a complete minute grid for the session window
      2. Reindex the M1 data to this grid
      3. Forward-fill: open=high=low=close = previous close, volume = 0
      4. Never fill across session boundaries or weekends
    
    This preserves time continuity without inventing price movement.
    """
    cfg = LIVE_INDEX_CONFIG.get(symbol)
    if cfg is None:
        print(f"    WARNING: No session config for {symbol}, skipping gap fill")
        return m1
    
    s_h = cfg["session_start_hour"]
    s_m = cfg["session_start_minute"]
    e_h = cfg["session_end_hour"]
    e_m = cfg["session_end_minute"]
    
    # Get all unique trading dates present in the data
    trading_dates = sorted(set(m1.index.date))
    
    filled_parts = []
    total_filled = 0
    
    for day in trading_dates:
        # Skip weekends
        if pd.Timestamp(day).weekday() >= 5:
            continue
        
        session_start = pd.Timestamp(day).replace(hour=s_h, minute=s_m)
        session_end = pd.Timestamp(day).replace(hour=e_h, minute=e_m)
        
        # Extract this day's session bars
        day_bars = m1[(m1.index >= session_start) & (m1.index <= session_end)]
        
        if len(day_bars) == 0:
            continue
        
        # Build complete minute grid for this session
        minute_grid = pd.date_range(start=session_start, end=session_end, freq="1min")
        
        # Reindex to the complete grid
        day_filled = day_bars.reindex(minute_grid)
        
        # Count how many bars we're filling
        n_missing = day_filled["Close"].isna().sum()
        
        if n_missing > 0:
            # Forward-fill Close first (carry last known price)
            day_filled["Close"] = day_filled["Close"].ffill()
            
            # For still-NaN rows (gaps at session start before first tick),
            # backfill from the first available price
            day_filled["Close"] = day_filled["Close"].bfill()
            
            # Fill OHLC for synthetic bars: flat candle at carried price
            missing_mask = day_filled["Open"].isna()
            day_filled.loc[missing_mask, "Open"] = day_filled.loc[missing_mask, "Close"]
            day_filled.loc[missing_mask, "High"] = day_filled.loc[missing_mask, "Close"]
            day_filled.loc[missing_mask, "Low"] = day_filled.loc[missing_mask, "Close"]
            day_filled.loc[missing_mask, "Volume"] = 0
            
            total_filled += n_missing
        
        filled_parts.append(day_filled)
    
    if not filled_parts:
        return m1
    
    # Also keep bars outside session windows (needed for H1 EMA computation)
    all_session_mask = pd.Series(False, index=m1.index)
    for day in trading_dates:
        if pd.Timestamp(day).weekday() >= 5:
            continue
        session_start = pd.Timestamp(day).replace(hour=s_h, minute=s_m)
        session_end = pd.Timestamp(day).replace(hour=e_h, minute=e_m)
        all_session_mask |= (m1.index >= session_start) & (m1.index <= session_end)
    
    outside_session = m1[~all_session_mask]
    
    # Combine filled session bars + outside-session bars
    result = pd.concat(filled_parts + [outside_session])
    result = result.sort_index()
    result = result[~result.index.duplicated(keep="first")]
    
    pct_synthetic = (total_filled / len(result) * 100) if len(result) > 0 else 0
    print(f"    Session gap fill: {total_filled:,} synthetic bars added ({pct_synthetic:.1f}% of total)")
    
    return result


def assemble_index_data(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """
    Read all available chunks for an index, normalize prices,
    convert timezone, fill session gaps, and build M1 + H1 DataFrames.
    """
    chunk_dir = CHUNKS_DIR / symbol
    if not chunk_dir.exists():
        print(f"  {symbol}: No chunk directory found")
        return None
    
    chunks = sorted(chunk_dir.glob("m1_*.parquet"))
    if not chunks:
        print(f"  {symbol}: No chunks found")
        return None
    
    print(f"  {symbol}: Loading {len(chunks)} chunks...")
    
    parts = []
    skipped = 0
    for chunk_path in chunks:
        try:
            df = pd.read_parquet(chunk_path)
            if len(df) == 0:
                continue
            
            # Normalize prices
            df = normalize_chunk_prices(df, symbol)
            parts.append(df)
        except Exception as e:
            print(f"    WARNING: Failed to read {chunk_path.name}: {e}")
            skipped += 1
    
    if not parts:
        print(f"  {symbol}: No valid data after loading chunks")
        return None
    
    if skipped:
        print(f"    ({skipped} chunks skipped due to errors)")
    
    # Combine
    m1 = pd.concat(parts)
    m1 = m1.sort_index()
    m1 = m1[~m1.index.duplicated(keep="first")]
    
    # Ensure timezone is stripped before conversion
    if m1.index.tz is not None:
        m1.index = m1.index.tz_localize(None)
    
    # Convert UTC → EET/EEST
    m1 = convert_utc_to_eet(m1)
    
    # Verify prices one more time
    lo, hi = EXPECTED_PRICE_RANGES[symbol]
    median_price = m1["Close"].median()
    if not (lo * 0.5 <= median_price <= hi * 1.5):
        print(f"  WARNING: {symbol} final median price {median_price:.2f} outside expected range!")
    
    bars_before = len(m1)
    
    # Fill missing M1 bars within session windows (critical for ORB strategy)
    m1 = fill_session_gaps(m1, symbol)
    
    bars_after = len(m1)
    print(f"    Bars: {bars_before:,} raw → {bars_after:,} after session fill")
    
    # Build H1 from all bars (including outside session, for EMA continuity)
    h1 = pd.DataFrame()
    h1["Open"] = m1["Open"].resample("1h").first()
    h1["High"] = m1["High"].resample("1h").max()
    h1["Low"] = m1["Low"].resample("1h").min()
    h1["Close"] = m1["Close"].resample("1h").last()
    h1["Volume"] = m1["Volume"].resample("1h").sum()
    h1 = h1.dropna(subset=["Open"])
    h1.index.name = "Time"
    m1.index.name = "Time"
    
    # Compute session bars/day for verification
    cfg = LIVE_INDEX_CONFIG.get(symbol, {})
    s_h = cfg.get("session_start_hour", 0)
    s_m = cfg.get("session_start_minute", 0)
    e_h = cfg.get("session_end_hour", 23)
    e_m = cfg.get("session_end_minute", 59)
    session_mask = (
        ((m1.index.hour > s_h) | ((m1.index.hour == s_h) & (m1.index.minute >= s_m))) &
        ((m1.index.hour < e_h) | ((m1.index.hour == e_h) & (m1.index.minute <= e_m)))
    )
    session_bars = m1[session_mask]
    session_days = len(set(session_bars.index.date))
    bars_per_day = len(session_bars) / max(1, session_days)
    
    print(f"    M1: {len(m1):,} bars | {m1.index[0]} → {m1.index[-1]}")
    print(f"    H1: {len(h1):,} bars")
    print(f"    Session: {bars_per_day:.0f} bars/session/day ({session_days} days)")
    print(f"    Price range: {m1['Close'].min():.2f} – {m1['Close'].max():.2f}")
    
    return m1, h1


def save_data(symbol: str, m1: pd.DataFrame, h1: pd.DataFrame):
    """Save M1 and H1 as tab-separated CSVs matching existing format."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    m1_file = DATA_DIR / TEN_YEAR_FILES[symbol]
    h1_file = DATA_DIR / f"{symbol}_10YR_H1_EMA.csv"
    
    m1.to_csv(m1_file, sep="\t", float_format="%.2f")
    h1.to_csv(h1_file, sep="\t", float_format="%.2f")
    
    print(f"    Saved: {m1_file.name} ({m1_file.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"    Saved: {h1_file.name}")


def assemble_all():
    """Assemble all available index data."""
    print("=" * 70)
    print("  ASSEMBLING 10-YEAR DATA FROM DUKASCOPY CHUNKS")
    print("=" * 70)
    
    available = {}
    for symbol in ALL_SYMBOLS:
        print(f"\n─── {symbol} ───")
        result = assemble_index_data(symbol)
        if result is not None:
            m1, h1 = result
            save_data(symbol, m1, h1)
            available[symbol] = True
        else:
            available[symbol] = False
    
    print(f"\n  Available for backtest: {[s for s, ok in available.items() if ok]}")
    return available


# ═══════════════════════════════════════════════════════════════
# BACKTEST ENGINE (adapted from run_3year_backtest.py)
# ═══════════════════════════════════════════════════════════════

def generate_all_signals(symbols: list[str]):
    """Generate signals for all available indices using 10-year data."""
    all_signals = []

    for sym in symbols:
        cfg = LIVE_INDEX_CONFIG.get(sym)
        if cfg is None:
            print(f"  {sym}: No config in LIVE_INDEX_CONFIG, skipping")
            continue
        
        data_file = TEN_YEAR_FILES[sym]
        print(f"  Loading {sym}: {data_file} ...", end=" ")
        
        try:
            df = load_data(data_file)
        except FileNotFoundError:
            print("NOT FOUND, skipping")
            continue
        
        print(f"{len(df):,} M1 bars")

        # Compute H1 EMA from M1 data
        htf_ema = compute_htf_ema(df, fast=EMA_FAST, slow=EMA_SLOW)
        print(f"    HTF EMA: {len(htf_ema)} H1 bars")

        trading_days = sorted(set(df.index.date))
        print(f"    Trading days: {len(trading_days)} ({trading_days[0]} → {trading_days[-1]})")

        point_value = BROKER_POINT_VALUES[sym]
        count = 0

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
                orb_minutes=cfg.get("orb_minutes_opt", 10),  # OPTIMIZED: 10 (from 15)
                entry_hours=cfg.get("entry_hours", 2),
                breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
                lookback_ranges=cfg.get("lookback_ranges_opt", 6),  # OPTIMIZED: 6 (new parameter)
                disp_mult=cfg.get("disp_mult_opt", 1.2),  # OPTIMIZED: 1.2 (from 1.3)
                body_min=cfg.get("body_min_opt", 0.55),  # OPTIMIZED: 0.55 (from 0.60)
                close_strength=cfg.get("close_strength_opt", 0.20),  # OPTIMIZED: 0.20 (from 0.25)
                breakout_margin=cfg.get("breakout_margin_opt", 0.03),  # OPTIMIZED: 0.03 (from 0.05)
                orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
                tp3_r=2.5,
            )
            signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
            for s in signals:
                all_signals.append({
                    "symbol": sym,
                    "signal": s,
                    "point_value": point_value,
                    "entry_time": pd.Timestamp(s.entry_time),
                    "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
                })
                count += 1

        print(f"    {sym}: {count} signals generated")

    all_signals.sort(key=lambda x: x["entry_time"])
    print(f"\n  Total raw signals: {len(all_signals)}")
    return all_signals


def run_portfolio(all_signals):
    """Execute portfolio backtest with full constraints."""
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    MAX_DD_PCT = ACCOUNT_RULES.get("max_drawdown_pct", 10.0)
    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL

    open_positions = []
    daily_losses = {}
    daily_trade_counts = {}
    all_trades = []
    skipped_reasons = {"max_positions": 0, "daily_loss_limit": 0, "adaptive_stop": 0,
                       "max_daily_trades": 0, "max_dd_breach": 0,
                       "spread_guard": 0, "price_drift": 0, "stale_signal": 0,
                       "min_R": 0, "news_window": 0}
    account_terminated = False
    account_resets = 0

    # Pre-compute median R per symbol
    _r_by_sym = defaultdict(list)
    for sig in all_signals:
        _r_by_sym[sig["symbol"]].append(float(sig["signal"].risk_points))
    median_R = {sym: float(np.median(rs)) for sym, rs in _r_by_sym.items()}

    current_day = None
    day_peak_balance = STARTING_CAPITAL
    day_start_balance = STARTING_CAPITAL

    for sig in all_signals:
        # For 10-year: reset account if terminated (simulate "new account")
        if account_terminated:
            account_resets += 1
            account_terminated = False
            current_balance = STARTING_CAPITAL
            peak_balance = STARTING_CAPITAL
            day_peak_balance = STARTING_CAPITAL
            day_start_balance = STARTING_CAPITAL
            daily_losses.clear()
            daily_trade_counts.clear()
            open_positions.clear()

        s = sig["signal"]
        symbol = sig["symbol"]
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

        if daily_losses.get(trade_day, 0) >= MAX_DAILY_LOSSES:
            skipped_reasons["daily_loss_limit"] += 1
            continue

        if daily_trade_counts.get(trade_day, 0) >= MAX_DAILY_TRADES:
            skipped_reasons["max_daily_trades"] += 1
            continue

        open_positions = [p for p in open_positions if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            skipped_reasons["max_positions"] += 1
            continue

        # Min R guard
        MIN_R_BY_SYMBOL = {"SPX500": 3.0, "US2000": 3.0}
        MIN_R_POINTS = MIN_R_BY_SYMBOL.get(symbol, 5.0)
        if risk_points < MIN_R_POINTS:
            skipped_reasons["min_R"] += 1
            continue

        # Spread-to-R guard
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        if risk_points > 0 and (spread_pts / risk_points) > 0.30:
            skipped_reasons["spread_guard"] += 1
            continue

        # Stale signal guard
        breakout_time = getattr(s, 'breakout_time', None)
        if breakout_time is not None:
            bars_delay = int((pd.Timestamp(s.entry_time) - pd.Timestamp(breakout_time)).total_seconds() / 60)
            if bars_delay > 15:
                skipped_reasons["stale_signal"] += 1
                continue

        # Price drift guard
        signal_price = getattr(s, 'signal_price', None)
        if signal_price is not None and risk_points > 0:
            drift = abs(float(s.entry_price) - float(signal_price))
            if drift > 0.5 * risk_points:
                skipped_reasons["price_drift"] += 1
                continue

        # Adaptive risk
        risk_pct, should_stop = get_adaptive_risk(current_balance, day_peak_balance,
                                                   starting_balance=STARTING_CAPITAL)
        if should_stop:
            skipped_reasons["adaptive_stop"] += 1
            continue

        # Position sizing
        risk_amount = current_balance * (risk_pct / 100.0)
        if risk_points <= 0:
            continue

        lots = risk_amount / (risk_points * point_value)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        exit_reason = getattr(s, "exit_reason", None)
        pnl_points = float(getattr(s, "pnl_points", 0.0))

        # Realistic costs
        base_slippage = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
        high_vol = is_high_vol_event_day(entry_time, symbol)
        slippage_pts = get_vol_scaled_slippage(
            base_slippage, risk_points, median_R.get(symbol, risk_points), high_vol)
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        half_spread = spread_pts / 2.0
        entry_cost_pts = slippage_pts + half_spread

        if exit_reason == "TP":
            exit_cost_pts = half_spread
        else:
            exit_cost_pts = slippage_pts + half_spread

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

        # Max DD enforcement
        overall_dd_pct = ((STARTING_CAPITAL - current_balance) / STARTING_CAPITAL) * 100 if STARTING_CAPITAL > 0 else 0
        if overall_dd_pct >= MAX_DD_PCT:
            account_terminated = True
            print(f"    *** ACCOUNT RESET #{account_resets+1}: DD={overall_dd_pct:.1f}% on {entry_time.date()} ***")

        daily_ref_post = max(day_start_balance, day_peak_balance)
        daily_dd_pct = ((daily_ref_post - current_balance) / daily_ref_post * 100) if daily_ref_post > 0 else 0
        trailing_dd_pct = ((peak_balance - current_balance) / peak_balance * 100) if peak_balance > 0 else 0

        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1

        daily_trade_counts[trade_day] = daily_trade_counts.get(trade_day, 0) + 1

        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})

        all_trades.append({
            "symbol": symbol, "date": entry_time, "direction": s.direction,
            "result": result, "risk_percent": risk_pct, "risk_amount": risk_amount,
            "risk_points": risk_points, "pnl_points": pnl_points,
            "adjusted_pnl_points": adjusted_pnl_points,
            "pnl_raw": pnl_raw, "costs": total_costs, "pnl": pnl,
            "balance": current_balance, "peak_balance": peak_balance,
            "day_peak_balance": day_peak_balance,
            "daily_dd_pct": round(daily_dd_pct, 2),
            "trailing_dd_pct": round(trailing_dd_pct, 2),
            "lots": lots, "exit_reason": exit_reason,
        })

    return all_trades, skipped_reasons, current_balance, peak_balance, account_resets


def compute_metrics(trades_df, starting_capital, label="Overall"):
    """Compute comprehensive metrics from a trades DataFrame."""
    total = len(trades_df)
    if total == 0:
        return None

    wins = len(trades_df[trades_df["result"] == "win"])
    partial_wins = len(trades_df[trades_df["result"] == "partial_win"])
    losses = len(trades_df[trades_df["result"] == "loss"])
    profitable = wins + partial_wins
    win_rate = (profitable / total * 100) if total > 0 else 0

    net_pnl = trades_df["pnl"].sum()
    gross_profit = trades_df[trades_df["pnl"] > 0]["pnl"].sum()
    gross_loss = trades_df[trades_df["pnl"] < 0]["pnl"].sum()
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")

    avg_win_pnl = trades_df[trades_df["pnl"] > 0]["pnl"].mean() if profitable > 0 else 0
    avg_loss_pnl = abs(trades_df[trades_df["pnl"] < 0]["pnl"].mean()) if losses > 0 else 0
    avg_win_pts = trades_df[trades_df["pnl_points"] > 0]["pnl_points"].mean() if profitable > 0 else 0
    avg_loss_pts = abs(trades_df[trades_df["pnl_points"] < 0]["pnl_points"].mean()) if losses > 0 else 0
    actual_rr = (avg_win_pts / avg_loss_pts) if avg_loss_pts > 0 else float("inf")

    expectancy = net_pnl / total
    total_costs = trades_df["costs"].sum()

    largest_win = trades_df["pnl"].max()
    largest_loss = trades_df["pnl"].min()

    # Drawdown
    equity = trades_df["balance"].values
    peak_equity = np.maximum.accumulate(equity)
    drawdowns_pct = (peak_equity - equity) / peak_equity * 100
    drawdowns_dollar = peak_equity - equity
    max_dd_pct = drawdowns_pct.max()
    max_dd_idx = drawdowns_pct.argmax()
    max_dd_dollar = drawdowns_dollar[max_dd_idx]

    peak_daily_dd = trades_df["daily_dd_pct"].max()
    peak_trailing_dd = trades_df["trailing_dd_pct"].max()

    # Streaks
    results_list = trades_df["result"].tolist()
    max_consec_loss = max_consec_win = 0
    cl = cw = 0
    for r in results_list:
        if r == "loss":
            cl += 1; cw = 0
            max_consec_loss = max(max_consec_loss, cl)
        elif r in ("win", "partial_win"):
            cw += 1; cl = 0
            max_consec_win = max(max_consec_win, cw)
        else:
            cl = cw = 0

    # Sharpe / Sortino
    trades_df = trades_df.copy()
    trades_df["trade_date"] = trades_df["date"].dt.date
    daily_pnl = trades_df.groupby("trade_date")["pnl"].sum()
    trading_days = len(daily_pnl)
    trading_weeks = max(1, trading_days / 5)
    trades_per_week = total / trading_weeks

    avg_dp = daily_pnl.mean()
    std_dp = daily_pnl.std()
    down_std = daily_pnl[daily_pnl < 0].std() if len(daily_pnl[daily_pnl < 0]) > 1 else std_dp
    sharpe = (avg_dp / std_dp * np.sqrt(252)) if std_dp > 0 else 0
    sortino = (avg_dp / down_std * np.sqrt(252)) if down_std > 0 else 0

    final_balance = trades_df["balance"].iloc[-1]
    total_return_pct = ((final_balance - starting_capital) / starting_capital * 100)

    annual_return = net_pnl / max(1, trading_days / 252)
    calmar = (annual_return / max_dd_dollar) if max_dd_dollar > 0 else 0
    recovery_factor = (net_pnl / max_dd_dollar) if max_dd_dollar > 0 else 0

    return {
        "label": label,
        "total_trades": total,
        "trades_per_week": round(trades_per_week, 1),
        "wins": wins,
        "partial_wins": partial_wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "actual_rr": round(actual_rr, 2),
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(avg_win_pnl, 2),
        "avg_loss": round(avg_loss_pnl, 2),
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
        "expectancy": round(expectancy, 2),
        "total_costs": round(total_costs, 2),
        "final_balance": round(final_balance, 2),
        "total_return_pct": round(total_return_pct, 1),
        "max_dd_pct": round(max_dd_pct, 1),
        "max_dd_dollar": round(max_dd_dollar, 2),
        "peak_daily_dd": round(peak_daily_dd, 1),
        "peak_trailing_dd": round(peak_trailing_dd, 1),
        "max_consec_wins": max_consec_win,
        "max_consec_losses": max_consec_loss,
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "recovery_factor": round(recovery_factor, 2),
        "starting_capital": starting_capital,
        "trading_days": trading_days,
    }


def print_report(results, skipped, account_resets):
    """Print comprehensive backtest report."""
    o = results["overall"]
    
    print(f"""
  ┌─────────────────────────────────────────────────────────────┐
  │              10-YEAR PORTFOLIO BACKTEST                     │
  ├─────────────────────────────────────────────────────────────┤
  │  Total Trades      : {o['total_trades']:<8} ({o['trades_per_week']}/wk)                  │
  │  Win Rate          : {o['win_rate']}%                                   │
  │  Profit Factor     : {o['profit_factor']:<8}                              │
  │  Avg RR            : 1:{o['actual_rr']:<8}                            │
  │  Sharpe Ratio      : {o['sharpe']:<8}                              │
  │  Sortino Ratio     : {o['sortino']:<8}                              │
  ├─────────────────────────────────────────────────────────────┤
  │  Starting Capital  : ${o['starting_capital']:>10,.2f}                        │
  │  Final Balance     : ${o['final_balance']:>10,.2f}                        │
  │  Net P&L           : ${o['net_pnl']:>10,.2f}                        │
  │  Total Return      : {o['total_return_pct']:>8.1f}%                           │
  │  Account Resets    : {account_resets:<8} (10% DD terminations)         │
  ├─────────────────────────────────────────────────────────────┤
  │  Max Drawdown      : {o['max_dd_pct']}% (${o['max_dd_dollar']:,.2f})                  │
  │  Peak Daily DD     : {o['peak_daily_dd']}%                                  │
  │  Max Consec Losses : {o['max_consec_losses']:<8}                              │
  │  Recovery Factor   : {o['recovery_factor']:<8}                              │
  └─────────────────────────────────────────────────────────────┘""")

    # Per-index summary
    print(f"\n  {'Index':<10} {'Trades':>6} {'Tr/Wk':>6} {'WR':>6} {'PF':>6} {'Net PnL':>10} {'MaxDD%':>7} {'Sharpe':>7}")
    print(f"  {'-'*62}")
    for sym in ALL_SYMBOLS:
        if sym in results["per_index"]:
            m = results["per_index"][sym]
            print(f"  {sym:<10} {m['total_trades']:>6} {m['trades_per_week']:>5.1f} {m['win_rate']:>5.1f}% {m['profit_factor']:>5.2f} ${m['net_pnl']:>9,.2f} {m['max_dd_pct']:>6.1f}% {m['sharpe']:>6.2f}")

    # Yearly breakdown
    print(f"\n  ── YEARLY BREAKDOWN ──")
    print(f"  {'Year':<6} {'Trades':>6} {'Tr/Wk':>6} {'W':>4} {'L':>4} {'WR':>6} {'PF':>6} {'Net PnL':>10} {'MaxDD%':>7} {'Sharpe':>7} {'MaxCL':>5}")
    print(f"  {'-'*75}")
    for yr, m in sorted(results["yearly"].items()):
        print(f"  {yr:<6} {m['total_trades']:>6} {m['trades_per_week']:>5.1f} {m['wins']+m['partial_wins']:>4} {m['losses']:>4} {m['win_rate']:>5.1f}% {m['profit_factor']:>5.2f} ${m['net_pnl']:>9,.2f} {m['max_dd_pct']:>6.1f}% {m['sharpe']:>6.2f} {m['max_consec_losses']:>5}")

    # Per-index yearly
    for sym in ALL_SYMBOLS:
        if sym in results["per_index_yearly"] and results["per_index_yearly"][sym]:
            print(f"\n  ── {sym} YEARLY ──")
            print(f"  {'Year':<6} {'Trades':>6} {'W':>4} {'L':>4} {'WR':>6} {'PF':>6} {'Net PnL':>10}")
            print(f"  {'-'*50}")
            for yr, m in sorted(results["per_index_yearly"][sym].items()):
                w_count = m['wins'] + m['partial_wins']
                print(f"  {yr:<6} {m['total_trades']:>6} {w_count:>4} {m['losses']:>4} {m['win_rate']:>5.1f}% {m['profit_factor']:>5.2f} ${m['net_pnl']:>9,.2f}")

    # Exit reasons
    print(f"\n  ── EXIT REASONS ──")
    for reason, info in results["exit_reasons"].items():
        print(f"  {reason:<15} : {info['count']:>4} ({info['pct']:>5.1f}%)  avg PnL: ${info['avg_pnl']:>8,.2f}")

    print(f"\n  Skipped: {skipped}")


def main():
    assemble_only = "--assemble" in sys.argv
    
    # Phase 1: Assemble data from chunks
    available = assemble_all()
    
    symbols_ready = [s for s, ok in available.items() if ok]
    if not symbols_ready:
        print("\n  No data available for backtest. Wait for download to complete.")
        return
    
    if assemble_only:
        print("\n  Assembly complete. Run without --assemble to backtest.")
        return
    
    # Phase 2: Generate signals
    print("\n" + "=" * 70)
    print(f"  10-YEAR BACKTEST — {', '.join(symbols_ready)}")
    print(f"  Capital: ${ACCOUNT_RULES['starting_balance']:,.0f} | Risk: 1.5% (adaptive)")
    print("=" * 70)
    
    all_signals = generate_all_signals(symbols_ready)
    if not all_signals:
        print("  No signals generated. Exiting.")
        return
    
    # Phase 3: Run portfolio backtest
    print("\n  Running portfolio backtest with constraints...")
    all_trades, skipped, final_balance, peak_balance, account_resets = run_portfolio(all_signals)
    print(f"  Executed trades: {len(all_trades)}")
    
    if not all_trades:
        print("  No trades executed. Exiting.")
        return
    
    trades_df = pd.DataFrame(all_trades)
    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)
    
    # Save trades
    output_dir = Path(__file__).parent / "reports" / "10year_backtest"
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(output_dir / "all_trades.csv", index=False)
    
    # Phase 4: Compute metrics
    results = {}
    starting_cap = ACCOUNT_RULES["starting_balance"]
    
    # Overall
    results["overall"] = compute_metrics(trades_df, starting_cap, "Overall (10 Years)")
    
    # Per-index
    results["per_index"] = {}
    for sym in symbols_ready:
        sdf = trades_df[trades_df["symbol"] == sym].reset_index(drop=True)
        if len(sdf) > 0:
            results["per_index"][sym] = compute_metrics(sdf, starting_cap, sym)
    
    # Yearly
    trades_df["year"] = trades_df["date"].dt.year
    results["yearly"] = {}
    for yr in sorted(trades_df["year"].unique()):
        ydf = trades_df[trades_df["year"] == yr].reset_index(drop=True)
        if len(ydf) > 0:
            yr_start_cap = ydf["balance"].iloc[0] - ydf["pnl"].iloc[0]
            results["yearly"][str(yr)] = compute_metrics(ydf, yr_start_cap, str(yr))
    
    # Per-index yearly
    results["per_index_yearly"] = {}
    for sym in symbols_ready:
        sdf = trades_df[trades_df["symbol"] == sym]
        results["per_index_yearly"][sym] = {}
        for yr in sorted(sdf["year"].unique()):
            sydf = sdf[sdf["year"] == yr].reset_index(drop=True)
            if len(sydf) > 0:
                yr_start = sydf["balance"].iloc[0] - sydf["pnl"].iloc[0]
                results["per_index_yearly"][sym][str(yr)] = compute_metrics(sydf, yr_start, f"{sym} {yr}")
    
    # Exit reasons
    exit_counts = trades_df["exit_reason"].value_counts()
    results["exit_reasons"] = {}
    for reason, count in exit_counts.items():
        pct = count / len(trades_df) * 100
        avg_pnl = trades_df[trades_df["exit_reason"] == reason]["pnl"].mean()
        results["exit_reasons"][reason] = {
            "count": int(count), "pct": round(pct, 1), "avg_pnl": round(avg_pnl, 2)
        }
    
    # Save JSON
    json_path = output_dir / "metrics_10year.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    # Print report
    print("\n" + "=" * 75)
    print("  10-YEAR BACKTEST RESULTS — ORB Clean Breakout (Live Strategy)")
    print("=" * 75)
    print_report(results, skipped, account_resets)
    
    print("\n" + "=" * 75)
    print(f"  Data: 10-year M1 (Dukascopy, {len(symbols_ready)} indices)")
    print(f"  Costs: Spread + Slippage (realistic model)")
    print(f"  Report saved: {json_path}")
    print("=" * 75)


if __name__ == "__main__":
    main()
