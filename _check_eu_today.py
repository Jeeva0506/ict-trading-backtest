"""Quick diagnostic: why no EU trades today"""
import MetaTrader5 as mt5, os, sys, pandas as pd, numpy as np
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
mt5.initialize(os.getenv('MT5_PATH'))
mt5.login(int(os.getenv('MT5_LOGIN')), os.getenv('MT5_PASSWORD'), os.getenv('MT5_SERVER'))

# Config matching live
CFG = {
    'GER30': {'mt5_open': (10, 0), 'mt5_close': (18, 30), 'max_sl': 80, 'sl_buf': 5,
              'disp_mult': 1.0, 'body_min': 0.70, 'close_str': 0.25, 'bk_margin': 0.15, 'entry_hrs': 3},
    'FRA40': {'mt5_open': (10, 0), 'mt5_close': (18, 30), 'max_sl': 30, 'sl_buf': 1,
              'disp_mult': 1.5, 'body_min': 0.60, 'close_str': 0.30, 'bk_margin': 0.10, 'entry_hrs': 2},
}

today = datetime(2026, 3, 20).date()

for sym, cfg in CFG.items():
    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 600)
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    oh, om = cfg['mt5_open']
    ch, cm = cfg['mt5_close']
    sess_start = pd.Timestamp(f'{today} {oh:02d}:{om:02d}:00')
    sess_end = pd.Timestamp(f'{today} {ch:02d}:{cm:02d}:00')
    entry_cutoff = sess_start + pd.Timedelta(hours=cfg['entry_hrs'])
    
    sess = df[(df['time'] >= sess_start) & (df['time'] <= sess_end)].copy()
    if len(sess) == 0:
        print(f"{sym}: No session data"); continue
    
    # ORB
    orb_end = sess_start + pd.Timedelta(minutes=15)
    orb = sess[sess['time'] < orb_end]
    orbH = orb['high'].max()
    orbL = orb['low'].min()
    orb_rng = orbH - orbL
    orb_bias = "bullish" if orb.iloc[-1]['close'] > (orbH + orbL) / 2 else "bearish"
    
    # Volatility contraction filter
    num_orb_candles = len(orb)
    orb_candle_ranges = orb['high'] - orb['low']
    avg_candle_range = orb_candle_ranges.mean()
    expected_range = avg_candle_range * num_orb_candles * 0.40
    wide_orb = orb_rng > expected_range
    
    print(f"\n{'='*60}")
    print(f"{sym}: ORB = {orbL:.2f} - {orbH:.2f}  range = {orb_rng:.2f}")
    print(f"  ORB bias: {orb_bias} | Wide ORB: {wide_orb} (rng={orb_rng:.2f} vs expected={expected_range:.2f})")
    print(f"  Entry cutoff: {entry_cutoff} | max_sl: {cfg['max_sl']}")
    
    # Scan for breakout candles
    scan = sess[(sess['time'] >= orb_end) & (sess['time'] <= entry_cutoff)]
    margin = cfg['bk_margin'] * orb_rng
    
    # Rolling avg range for displacement
    scan = scan.copy()
    scan['range'] = scan['high'] - scan['low']
    all_sess = sess.copy()
    all_sess['range'] = all_sess['high'] - all_sess['low']
    avg_ranges = all_sess['range'].rolling(20, min_periods=3).mean()
    
    bull_breaks = scan[scan['close'] > orbH + margin]
    bear_breaks = scan[scan['close'] < orbL - margin]
    
    print(f"  Scan window: {orb_end} to {entry_cutoff} ({len(scan)} bars)")
    print(f"  Bars closing above ORB+margin ({orbH + margin:.2f}): {len(bull_breaks)}")
    print(f"  Bars closing below ORB-margin ({orbL - margin:.2f}): {len(bear_breaks)}")
    
    for label, breaks in [("BULLISH", bull_breaks), ("BEARISH", bear_breaks)]:
        if len(breaks) == 0:
            continue
        for idx, (_, row) in enumerate(breaks.head(3).iterrows()):
            t = row['time']
            o, h, l, c = row['open'], row['high'], row['low'], row['close']
            rng = h - l
            body_pct = abs(c - o) / rng if rng > 0 else 0
            
            # Get avg range at this point
            pos = all_sess[all_sess['time'] <= t].index
            if len(pos) >= 3:
                recent = all_sess.loc[pos[-20:]]['range']
                avg_r = recent.mean()
            else:
                avg_r = rng
            
            disp_ok = rng >= cfg['disp_mult'] * avg_r
            body_ok = body_pct >= cfg['body_min']
            
            if rng > 0:
                if label == "BULLISH":
                    close_pos = (c - l) / rng
                    cs_ok = close_pos >= (1.0 - cfg['close_str'])
                else:
                    close_pos = (h - c) / rng
                    cs_ok = close_pos >= (1.0 - cfg['close_str'])
            else:
                cs_ok = False
                close_pos = 0
            
            # SL estimate
            sl_buf = cfg['sl_buf']
            if label == "BULLISH":
                sl = l - sl_buf
                R = (orbH + margin) - sl  # approximate entry near ORB
            else:
                sl = h + sl_buf
                R = sl - (orbL - margin)
            sl_ok = R <= cfg['max_sl'] and R > 0
            
            all_ok = disp_ok and body_ok and cs_ok and sl_ok and not wide_orb
            
            print(f"\n  {label} break #{idx+1} @ {t}")
            print(f"    O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f} range={rng:.2f}")
            print(f"    disp: {rng:.2f} vs {cfg['disp_mult']}x{avg_r:.2f}={cfg['disp_mult']*avg_r:.2f} -> {'PASS' if disp_ok else 'FAIL'}")
            print(f"    body%: {body_pct:.2f} vs {cfg['body_min']} -> {'PASS' if body_ok else 'FAIL'}")
            print(f"    close_str: {close_pos:.2f} vs {1-cfg['close_str']:.2f} -> {'PASS' if cs_ok else 'FAIL'}")
            print(f"    SL: R={R:.2f} vs max_sl={cfg['max_sl']} -> {'PASS' if sl_ok else 'FAIL'}")
            print(f"    wide_orb: {'REJECT' if wide_orb else 'PASS'}")
            print(f"    OVERALL: {'WOULD TRADE' if all_ok else 'FILTERED'}")
    
    if len(bull_breaks) == 0 and len(bear_breaks) == 0:
        # Show price extremes during scan window
        if len(scan) > 0:
            max_high = scan['high'].max()
            min_low = scan['low'].min()
            print(f"\n  No breakout candle closed beyond ORB+margin")
            print(f"  Scan high: {max_high:.2f} (ORB high+margin: {orbH + margin:.2f})")
            print(f"  Scan low:  {min_low:.2f} (ORB low-margin: {orbL - margin:.2f})")
            if max_high > orbH:
                print(f"  -> Price pierced ORB high but no candle CLOSED above margin level")
            if min_low < orbL:
                print(f"  -> Price pierced ORB low but no candle CLOSED below margin level")

mt5.shutdown()
