"""
Quick check: Would we have gotten ORB signals on Feb 19, 2026?
Uses MT5 to fetch historical M1 data and runs the strategy.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / 'live'))

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
import pytz

from config_fundednext import PROFITABLE_INDICES, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
from strategy.strategy_ict_clean import StrategyICTClean


def check_signals_for_date(target_date_str: str = "2026-02-19"):
    """Fetch M1 data for the target date and check for ORB signals."""
    
    # Connect to MT5
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        return
    
    info = mt5.account_info()
    if not info or info.login != MT5_LOGIN:
        ok = mt5.login(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if not ok:
            print(f"MT5 login failed: {mt5.last_error()}")
            return
    
    print(f"Connected to MT5 - Account: {mt5.account_info().login}")
    print(f"\n{'='*60}")
    print(f"  Checking ORB signals for {target_date_str}")
    print(f"{'='*60}\n")
    
    for symbol, cfg in PROFITABLE_INDICES.items():
        print(f"\n--- {symbol} ({cfg['description']}) ---")
        
        # Create strategy instance with same params as live
        strategy = StrategyICTClean(
            sl_buffer=cfg.get("sl_buffer", 1.0),
            orb_minutes=cfg.get("orb_minutes", 15),
            entry_hours=cfg.get("entry_hours", 2),
            breakout_window_minutes=cfg.get("breakout_window_minutes", 60),
            disp_mult=cfg.get("disp_mult", 1.3),
            body_min=cfg.get("body_min", 0.60),
            close_strength=cfg.get("close_strength", 0.25),
            breakout_margin=cfg.get("breakout_margin", 0.05),
            orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
            min_sl_points=cfg.get("min_sl_points", 2.0),
            max_sl_points=cfg.get("max_sl_points", 200.0),
            tp3_r=2.5,
            tp1_r=1.0,
        )
        
        # Compute session boundaries in MT5 server time
        local_tz = pytz.timezone(cfg.get('timezone', 'America/New_York'))
        mt5_tz = pytz.timezone("Europe/Helsinki")
        
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        
        local_open = local_tz.localize(datetime(
            target_date.year, target_date.month, target_date.day,
            cfg["market_open_hour"], cfg["market_open_minute"]))
        local_close = local_tz.localize(datetime(
            target_date.year, target_date.month, target_date.day,
            cfg["market_close_hour"], cfg["market_close_minute"]))
        
        srv_open = local_open.astimezone(mt5_tz).replace(tzinfo=None)
        srv_close = local_close.astimezone(mt5_tz).replace(tzinfo=None)
        
        print(f"  Session: {local_open.strftime('%H:%M %Z')} - {local_close.strftime('%H:%M %Z')}")
        print(f"  MT5 srv: {srv_open.strftime('%Y-%m-%d %H:%M')} - {srv_close.strftime('%H:%M')}")
        
        # Fetch M1 data for that date range
        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, srv_open, srv_close)
        if rates is None or len(rates) == 0:
            print(f"  No data available for {symbol} on {target_date_str}")
            continue
        
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        df.columns = ["open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
        
        print(f"  Bars: {len(df)} (from {df.index[0]} to {df.index[-1]})")
        
        # Get HTF trend (H1 EMA)
        h1_rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H1,
                                         srv_open - timedelta(days=30), srv_close)
        htf_trend = None
        if h1_rates is not None and len(h1_rates) > 60:
            h1 = pd.DataFrame(h1_rates)
            h1["close_price"] = h1["close"]
            ema20 = h1["close_price"].ewm(span=20, adjust=False).mean()
            ema50 = h1["close_price"].ewm(span=50, adjust=False).mean()
            htf_trend = "bullish" if ema20.iloc[-1] > ema50.iloc[-1] else "bearish"
            print(f"  HTF Trend: {htf_trend} (EMA20 {'>' if htf_trend=='bullish' else '<'} EMA50)")
        
        # Run strategy
        signals = strategy.generate_signals(df, htf_trend=htf_trend)
        
        if signals:
            for sig in signals:
                print(f"\n  ** SIGNAL FOUND **")
                print(f"     Direction: {sig.direction}")
                print(f"     Entry Time: {sig.entry_time}")
                print(f"     Entry Price: {sig.entry_price:.2f}")
                print(f"     Stop Loss: {sig.stop_loss:.2f}")
                print(f"     TP (2.5R): {sig.tp3:.2f}")
                print(f"     Risk: {sig.risk_points:.2f} points")
                if sig.exit_price:
                    pnl = sig.pnl_points
                    print(f"     Exit: {sig.exit_reason} @ {sig.exit_price:.2f} ({pnl:+.2f} pts)")
        else:
            print(f"  No valid ORB breakout signal for {symbol}")
    
    mt5.shutdown()
    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    check_signals_for_date("2026-02-19")
