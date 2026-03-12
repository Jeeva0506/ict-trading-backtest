"""Final cross-check with real MT5 point values"""

# Real point values from MT5 broker
PV = {'US2000': 10.0, 'SWI20': 10.0, 'US30': 10.0, 'NDX100': 10.0, 'SPX500': 10.0, 'GER30': 11.58}

trades = [
    {'id': 1, 'sym': 'US2000', 'bal': 6000.0, 'sl_pts': 7.7, 'lots_act': 1.16, 'entry': 2625.9, 'sl': 2637.3, 'exit': 2614.0, 'pnl': 138.04, 'dir': 'sell'},
    {'id': 2, 'sym': 'SWI20', 'bal': 6138.04, 'sl_pts': 7.5, 'lots_act': 0.45, 'entry': 13981.65, 'sl': 13974.6, 'exit': 13973.7, 'pnl': -46.27, 'dir': 'buy'},
    {'id': 3, 'sym': 'US2000', 'bal': 6091.77, 'sl_pts': 2.4, 'lots_act': 3.13, 'entry': 2625.3, 'sl': 2628.4, 'exit': 2628.6, 'pnl': -103.29, 'dir': 'sell'},
    {'id': 4, 'sym': 'US30', 'bal': 5988.48, 'sl_pts': 28.5, 'lots_act': 0.16, 'entry': 47759.09, 'sl': 47811.12, 'exit': 47811.12, 'pnl': -87.66, 'dir': 'sell'},
    {'id': 5, 'sym': 'NDX100', 'bal': 5900.82, 'sl_pts': 36.75, 'lots_act': 0.24, 'entry': 25119.54, 'sl': 25093.61, 'exit': 25092.49, 'pnl': -64.56, 'dir': 'buy'},
]

risk_pct = 1.5

print("=" * 95)
print(f"  #  Symbol    Balance   Risk$   SL_pts  PV  Lots_exp Lots_act MATCH  MaxLoss$    PnL$     RR")
print("=" * 95)

all_sizing_ok = True
for t in trades:
    pv = PV.get(t['sym'], 10.0)
    risk_amt = t['bal'] * risk_pct / 100
    risk_per_lot = t['sl_pts'] * pv
    lots_exp = risk_amt / risk_per_lot
    lots_exp = int(lots_exp / 0.01) * 0.01
    lots_exp = max(0.01, min(lots_exp, 40.0))

    max_loss = t['lots_act'] * t['sl_pts'] * pv
    max_loss_pct = max_loss / t['bal'] * 100

    rr = t['pnl'] / (t['lots_act'] * t['sl_pts'] * pv) if t['lots_act'] * t['sl_pts'] * pv else 0

    match = 'OK' if abs(lots_exp - t['lots_act']) < 0.02 else 'DIFF'
    if match != 'OK':
        all_sizing_ok = False

    print(f"  {t['id']}  {t['sym']:>7} ${t['bal']:>7.0f}  ${risk_amt:>6.2f}  {t['sl_pts']:>6.2f}  {pv:>4.0f}  {lots_exp:>7.2f}  {t['lots_act']:>7.2f}  {match:>4}  ${max_loss:>7.2f}  ${t['pnl']:>7.2f}  {rr:>5.2f}R")

    if match != 'OK':
        print(f"     *** Lots mismatch: expected={lots_exp}, actual={t['lots_act']}")

print(f"\nPosition Sizing: {'ALL CORRECT' if all_sizing_ok else 'ISSUES FOUND'}")

# Risk per trade check
print("\n--- Risk Per Trade vs Planned ---")
for t in trades:
    pv = PV.get(t['sym'], 10.0)
    actual_risk = t['lots_act'] * t['sl_pts'] * pv
    planned_risk = t['bal'] * risk_pct / 100
    ratio = actual_risk / planned_risk * 100
    status = 'OK' if ratio <= 105 else 'OVER-RISKED!'
    print(f"  T{t['id']} ({t['sym']:<7}): Planned=${planned_risk:.2f}, Actual=${actual_risk:.2f} ({ratio:.1f}%) -> {status}")

# PnL verification
print("\n--- PnL Verification (entry-exit calc vs recorded) ---")
for t in trades:
    pv = PV.get(t['sym'], 10.0)
    if t['dir'] == 'sell':
        calc_pnl = t['lots_act'] * (t['entry'] - t['exit']) * pv
    else:
        calc_pnl = t['lots_act'] * (t['exit'] - t['entry']) * pv
    diff = abs(calc_pnl - t['pnl'])
    status = 'OK' if diff < 2.0 else f'DIFF=${diff:.2f}'
    print(f"  T{t['id']} ({t['sym']:<7}): Calc=${calc_pnl:>8.2f}, Recorded=${t['pnl']:>8.2f} -> {status}")

# Dynamic risk tier at each trade point
print("\n--- Dynamic Risk Tier Progression ---")
initial = 6000.0
for t in trades:
    dd = max(0, (initial - t['bal']) / initial * 100)
    if dd >= 5.0:
        tier, rpct = 'TIER 3', 0.5
    elif dd >= 3.0:
        tier, rpct = 'TIER 2', 0.75
    else:
        tier, rpct = 'TIER 1', 1.5
    print(f"  T{t['id']}: DD={dd:.2f}% -> {tier} ({rpct}%)")

# Next trade preview
print("\n--- Next Trade: What Will Happen ---")
curr = 5836.26
dd_now = max(0, (initial - curr) / initial * 100)
if dd_now >= 5.0:
    tier_now, risk_now = 'TIER 3', 0.5
elif dd_now >= 3.0:
    tier_now, risk_now = 'TIER 2', 0.75
else:
    tier_now, risk_now = 'TIER 1', 1.5
next_risk_amt = curr * risk_now / 100
print(f"  DD={dd_now:.2f}% -> {tier_now} -> Risk={risk_now}% = ${next_risk_amt:.2f}")
print(f"  Only {3.0 - dd_now:.2f}% more DD before dropping to TIER 2 (0.75%)")
print(f"  If next trade is -1R: new balance = ${curr - next_risk_amt:.2f}, DD = {(initial - (curr - next_risk_amt))/initial*100:.2f}%")
tier_after_loss = 'TIER 2' if (initial - (curr - next_risk_amt))/initial*100 >= 3.0 else 'TIER 1'
print(f"  After -1R loss: will switch to {tier_after_loss}")

# Edge degraded check
print("\n--- Edge Guard Status ---")
print(f"  is_edge_degraded: True (PF=0.61 < 1.2)")
print(f"  This means edge guard WILL block new trades unless overridden with /start")
print(f"  Edge guard uses the seed trades (25 simulated wins) + 4 real trades")
print(f"  Seed baseline is washing out as more real trades come in")
