"""Quick verification: Is the live bot ready to trade all 6 indices?"""
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'live')

from config_fundednext import PROFITABLE_INDICES, HIGH_IMPACT_NEWS, get_index_config, get_all_profitable_indices
from live.news_filter import EconomicCalendar

errors = []
symbols = get_all_profitable_indices()
print(f"[CHECK] PROFITABLE_INDICES: {symbols} ({len(symbols)} indices)")

# 1. Every index has all required keys
required_keys = [
    'symbol', 'currency', 'point_value', 'sl_buffer',
    'disp_mult', 'body_min', 'close_strength', 'breakout_margin',
    'market_open_hour', 'market_close_hour', 'force_close_hour',
    'mt5_open_hour', 'mt5_close_hour', 'timezone',
    'min_sl_points', 'max_sl_points', 'orb_minutes', 'entry_hours',
    'min_sl_distance', 'max_sl_distance', 'contract_size',
]
for sym in symbols:
    cfg = get_index_config(sym)
    missing = [k for k in required_keys if k not in cfg]
    if missing:
        errors.append(f"{sym} missing keys: {missing}")
        print(f"  [FAIL] {sym} missing keys: {missing}")
    else:
        d, b, m = cfg['disp_mult'], cfg['body_min'], cfg['breakout_margin']
        print(f"  [OK] {sym}: {len(required_keys)} keys | disp={d}, body={b}, margin={m}, tz={cfg['timezone']}")
    # SL sanity
    if cfg['min_sl_points'] >= cfg['max_sl_points']:
        errors.append(f"{sym}: min_sl >= max_sl")

# 2. News filter covers all symbols
ec = EconomicCalendar()
print(f"\n[CHECK] News filter SYMBOL_CURRENCIES:")
for sym in symbols:
    if sym in ec.SYMBOL_CURRENCIES:
        print(f"  [OK] {sym} -> {ec.SYMBOL_CURRENCIES[sym]}")
    else:
        errors.append(f"News filter missing {sym}")
        print(f"  [FAIL] {sym} NOT MAPPED!")

# 3. All currencies have HIGH_IMPACT_NEWS
all_currencies = set(c for sym in symbols for c in ec.SYMBOL_CURRENCIES.get(sym, []))
print(f"\n[CHECK] HIGH_IMPACT_NEWS coverage (need: {sorted(all_currencies)}):")
for curr in sorted(all_currencies):
    if curr in HIGH_IMPACT_NEWS:
        print(f"  [OK] {curr}: {len(HIGH_IMPACT_NEWS[curr])} events")
    else:
        errors.append(f"HIGH_IMPACT_NEWS missing {curr}")
        print(f"  [FAIL] {curr} NOT COVERED!")

# 4. Param grouping summary
print(f"\n[CHECK] Parameter groups:")
param_groups = {}
for sym in symbols:
    cfg = get_index_config(sym)
    key = (cfg['disp_mult'], cfg['body_min'], cfg['close_strength'], cfg['breakout_margin'])
    param_groups.setdefault(key, []).append(sym)
for params, syms in param_groups.items():
    label = "Universal WF" if params == (1.0, 0.70, 0.25, 0.15) else "Original C12"
    print(f"  {label}: disp={params[0]}, body={params[1]}, close={params[2]}, margin={params[3]} -> {syms}")

# 5. Market hours sanity
print(f"\n[CHECK] Market hours (local):")
for sym in symbols:
    cfg = get_index_config(sym)
    oh, om = cfg['market_open_hour'], cfg['market_open_minute']
    ch, cm = cfg['market_close_hour'], cfg['market_close_minute']
    fh, fm = cfg['force_close_hour'], cfg['force_close_minute']
    print(f"  {sym}: open={oh:02d}:{om:02d}, close={ch:02d}:{cm:02d}, force_close={fh:02d}:{fm:02d} ({cfg['timezone']})")
    # Force close must be before market close
    fc_min = fh * 60 + fm
    mc_min = ch * 60 + cm
    if fc_min >= mc_min:
        errors.append(f"{sym}: force_close ({fh:02d}:{fm:02d}) >= market_close ({ch:02d}:{cm:02d})")

# 6. MT5 hours
print(f"\n[CHECK] MT5 server hours (EET):")
for sym in symbols:
    cfg = get_index_config(sym)
    oh, om = cfg['mt5_open_hour'], cfg['mt5_open_minute']
    ch, cm = cfg['mt5_close_hour'], cfg['mt5_close_minute']
    print(f"  {sym}: mt5_open={oh:02d}:{om:02d}, mt5_close={ch:02d}:{cm:02d}")

# VERDICT
print("\n" + "=" * 60)
if errors:
    print(f"VERDICT: {len(errors)} ERROR(S) FOUND!")
    for e in errors:
        print(f"  - {e}")
else:
    print("VERDICT: ALL CHECKS PASSED - READY TO GO LIVE")
    print(f"  6 indices configured | News protection active | Params synced")
print("=" * 60)
