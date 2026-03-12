"""Verify risk calculations against actual trade data"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from live.config_fundednext import (
    ADAPTIVE_RISK, FUNDEDNEXT_RULES,
    get_broker_symbol_specs, get_index_config,
    calculate_position_size, calculate_position_size_with_broker_data
)

# Current state
balance = 5836.26
initial = 6000.0
dd_pct = (initial - balance) / initial * 100
print(f"Current DD: {dd_pct:.2f}%")
print(f"Tier thresholds: T2={ADAPTIVE_RISK['tier2_threshold']}%, T3={ADAPTIVE_RISK['tier3_threshold']}%")

if dd_pct < ADAPTIVE_RISK['tier2_threshold']:
    print(f"Current risk tier: TIER 1 ({ADAPTIVE_RISK['default_risk_percent']}%)")
elif dd_pct < ADAPTIVE_RISK['tier3_threshold']:
    print(f"Current risk tier: TIER 2 ({ADAPTIVE_RISK['tier2_risk_percent']}%)")
else:
    print(f"Current risk tier: TIER 3 ({ADAPTIVE_RISK['tier3_risk_percent']}%)")

# Verify each trade's position sizing
trades = [
    {'id': 1, 'sym': 'US2000', 'balance': 6000.0, 'sl_pts': 7.7, 'lots_actual': 1.16,
     'entry': 2625.9, 'sl': 2637.3, 'dir': 'sell', 'pnl': 138.04},
    {'id': 2, 'sym': 'SWI20', 'balance': 6138.04, 'sl_pts': 7.5, 'lots_actual': 0.45,
     'entry': 13981.65, 'sl': 13974.6, 'dir': 'buy', 'pnl': -46.27},
    {'id': 3, 'sym': 'US2000', 'balance': 6091.77, 'sl_pts': 2.4, 'lots_actual': 3.13,
     'entry': 2625.3, 'sl': 2628.4, 'dir': 'sell', 'pnl': -103.29},
    {'id': 4, 'sym': 'US30', 'balance': 5988.48, 'sl_pts': 28.5, 'lots_actual': 0.16,
     'entry': 47759.09, 'sl': 47811.12, 'dir': 'sell', 'pnl': -87.66},
    {'id': 5, 'sym': 'NDX100', 'balance': 5900.82, 'sl_pts': 36.75, 'lots_actual': 0.24,
     'entry': 25119.54, 'sl': 25093.61, 'dir': 'buy', 'pnl': -64.56},
]

# Check DD tier at time of each trade
print("\n" + "=" * 80)
print("DYNAMIC RISK TIER CHECK PER TRADE")
print("=" * 80)
for t in trades:
    dd = (initial - t['balance']) / initial * 100
    dd = max(0, dd)
    if dd < ADAPTIVE_RISK['tier2_threshold']:
        tier = 'TIER 1'
        risk_pct = ADAPTIVE_RISK['default_risk_percent']
    elif dd < ADAPTIVE_RISK['tier3_threshold']:
        tier = 'TIER 2'
        risk_pct = ADAPTIVE_RISK['tier2_risk_percent']
    else:
        tier = 'TIER 3'
        risk_pct = ADAPTIVE_RISK['tier3_risk_percent']
    t['risk_pct'] = risk_pct
    t['dd'] = dd
    t['tier'] = tier
    risk_amt = t['balance'] * risk_pct / 100
    print(f"  Trade #{t['id']} ({t['sym']}): Balance=${t['balance']:.2f}, DD={dd:.2f}% -> {tier} -> {risk_pct}% risk = ${risk_amt:.2f}")

# Position sizing verification
print("\n" + "=" * 80)
print("POSITION SIZING VERIFICATION")
print("=" * 80)
print(f"{'Trade':>6} {'Symbol':>8} {'Balance':>10} {'Risk%':>6} {'RiskAmt':>8} {'SL_pts':>8} {'Lots_exp':>9} {'Lots_act':>9} {'Match':>6}")
print("-" * 80)

all_ok = True
for t in trades:
    specs = get_broker_symbol_specs(t['sym'])
    if specs:
        pv = specs['point_value']
        vol_step = specs['volume_step']
        vol_min = specs['volume_min']
        vol_max = specs['volume_max']
    else:
        config = get_index_config(t['sym'])
        pv = config.get('point_value', 1.0) if config else 1.0
        vol_step = 0.01
        vol_min = 0.01
        vol_max = 40.0
    t['pv'] = pv

    risk_amount = t['balance'] * (t['risk_pct'] / 100)
    risk_per_lot = t['sl_pts'] * pv
    lots_calc = risk_amount / risk_per_lot if risk_per_lot > 0 else vol_min
    lots_calc = int(lots_calc / vol_step) * vol_step
    lots_calc = max(vol_min, min(lots_calc, vol_max))

    match = 'OK' if abs(lots_calc - t['lots_actual']) < 0.02 else 'DIFF'
    if match != 'OK':
        all_ok = False
    print(f"{t['id']:>6} {t['sym']:>8} ${t['balance']:>8.2f} {t['risk_pct']:>5.1f}% ${risk_amount:>7.2f} {t['sl_pts']:>7.2f} {lots_calc:>8.2f} {t['lots_actual']:>8.2f} {match:>6}")
    if match != 'OK':
        print(f"       *** Expected {lots_calc}, got {t['lots_actual']}. pv={pv}, risk_per_lot={risk_per_lot:.2f}")

print(f"\nPosition Sizing: {'ALL CORRECT' if all_ok else 'ISSUES FOUND'}")

# Verify actual risk per trade
print("\n" + "=" * 80)
print("ACTUAL RISK AMOUNT VERIFICATION")
print("=" * 80)
print(f"{'Trade':>6} {'Symbol':>8} {'Lots':>6} {'SL_pts':>8} {'PV':>6} {'RiskCalc':>9} {'PnL_act':>9} {'RR_act':>7} {'Risk_OK':>8}")
print("-" * 80)

for t in trades:
    actual_risk = t['lots_actual'] * t['sl_pts'] * t['pv']
    planned_risk = t['balance'] * t['risk_pct'] / 100
    actual_sl_dist = abs(t['entry'] - t['sl'])
    actual_risk_from_sl = t['lots_actual'] * actual_sl_dist * t['pv']
    actual_rr = t['pnl'] / actual_risk if actual_risk > 0 else 0
    risk_ok = 'OK' if actual_risk <= planned_risk * 1.15 else 'HIGH'  # 15% tolerance
    print(f"{t['id']:>6} {t['sym']:>8} {t['lots_actual']:>5.2f} {t['sl_pts']:>7.2f} {t['pv']:>5.1f} ${actual_risk:>8.2f} ${t['pnl']:>8.2f} {actual_rr:>6.2f}R {risk_ok:>8}")
    if actual_sl_dist != t['sl_pts']:
        diff = actual_sl_dist - t['sl_pts']
        print(f"       Note: Actual SL distance={actual_sl_dist:.2f} vs signal SL={t['sl_pts']:.2f} (diff={diff:+.2f})")

# Point values from broker
print("\n" + "=" * 80)
print("BROKER SYMBOL SPECIFICATIONS")
print("=" * 80)
for sym in ['SPX500', 'NDX100', 'GER30', 'US30', 'US2000']:
    specs = get_broker_symbol_specs(sym)
    if specs:
        print(f"  {sym}: pv={specs['point_value']}, contract={specs['contract_size']}, "
              f"margin/lot=${specs['margin_per_lot']:.2f}, "
              f"vol_min={specs['volume_min']}, vol_step={specs['volume_step']}, vol_max={specs['volume_max']}")
    else:
        config = get_index_config(sym)
        if config:
            print(f"  {sym}: pv={config.get('point_value', 'N/A')} (from config, NO broker specs)")
        else:
            print(f"  {sym}: NOT CONFIGURED")

# Entry/Exit verification
print("\n" + "=" * 80)
print("ENTRY & EXIT LOGIC VERIFICATION")
print("=" * 80)

checks = [
    ("Adaptive risk enabled", ADAPTIVE_RISK.get('enabled'), True),
    ("Tier 1 risk", ADAPTIVE_RISK['default_risk_percent'], 1.5),
    ("Tier 2 risk", ADAPTIVE_RISK['tier2_risk_percent'], 0.75),
    ("Tier 3 risk", ADAPTIVE_RISK['tier3_risk_percent'], 0.5),
    ("Tier 2 threshold", ADAPTIVE_RISK['tier2_threshold'], 3.0),
    ("Tier 3 threshold", ADAPTIVE_RISK['tier3_threshold'], 5.0),
    ("Max drawdown", FUNDEDNEXT_RULES['max_drawdown_percent'], 10.0),
    ("Daily DD stop", FUNDEDNEXT_RULES['daily_drawdown_stop'], 2.0),
    ("Max trades/day", FUNDEDNEXT_RULES['max_trades_per_day'], 2),
]

for name, actual, expected in checks:
    status = "OK" if actual == expected else f"MISMATCH (got {actual})"
    print(f"  {name:30s}: {expected} -> {status}")

# BE/Trail/Exit logic summary
print("\n" + "=" * 80)
print("EXIT MANAGEMENT RULES (from code audit)")
print("=" * 80)
print("  BE trigger:     At +1.0R, SL moves to entry + 0.5*spread")
print("  Trail:          After BE, trails with 1.0R buffer below MFE")
print("  Force close:    15 min before session end")
print("  SL missing:     Re-apply original SL, emergency close if fails")
print("  Post-fill risk: Close if actual SL > 1.5x designed R")
print("  Pre-order drift: Reject if price drifted > 0.5R from signal")
print("  Min R guard:    Reject if R < 5.0 points")
print("  Spread guard:   Reject if spread > 30% of R")
print("  News filter:    Block 5min before / 15min after high-impact news")

# Verify the last trade's actual max risk exposure
print("\n" + "=" * 80)
print("MAX POSSIBLE LOSS PER TRADE (worst case)")
print("=" * 80)
for t in trades:
    max_loss = t['lots_actual'] * t['sl_pts'] * t['pv']
    pct_of_bal = max_loss / t['balance'] * 100
    r_ratio = t['pnl'] / (-max_loss) if max_loss > 0 else 0
    print(f"  Trade #{t['id']} ({t['sym']}): Max loss=${max_loss:.2f} ({pct_of_bal:.2f}% of balance), Actual=${t['pnl']:.2f}, Ratio={r_ratio:.2f}")

# Next trade sizing preview
print("\n" + "=" * 80)
print("NEXT TRADE PREVIEW (current state)")
print("=" * 80)
curr_bal = 5836.26
dd = (initial - curr_bal) / initial * 100
if dd < ADAPTIVE_RISK['tier2_threshold']:
    risk = ADAPTIVE_RISK['default_risk_percent']
    tier = "TIER 1"
elif dd < ADAPTIVE_RISK['tier3_threshold']:
    risk = ADAPTIVE_RISK['tier2_risk_percent']
    tier = "TIER 2"
else:
    risk = ADAPTIVE_RISK['tier3_risk_percent']
    tier = "TIER 3"

risk_amt = curr_bal * risk / 100
print(f"  Balance: ${curr_bal:.2f}, DD: {dd:.2f}%, {tier}")
print(f"  Risk: {risk}% = ${risk_amt:.2f} per trade")
print(f"  FundedNext daily DD limit: {FUNDEDNEXT_RULES['daily_drawdown_percent']}% = ${curr_bal * FUNDEDNEXT_RULES['daily_drawdown_percent']/100:.2f}")
print(f"  FundedNext daily stop: {FUNDEDNEXT_RULES['daily_drawdown_stop']}% = ${curr_bal * FUNDEDNEXT_RULES['daily_drawdown_stop']/100:.2f}")
print(f"  Max trades/day: {FUNDEDNEXT_RULES['max_trades_per_day']}")
print(f"  Max daily exposure: ${risk_amt * FUNDEDNEXT_RULES['max_trades_per_day']:.2f} ({risk * FUNDEDNEXT_RULES['max_trades_per_day']:.1f}%)")
