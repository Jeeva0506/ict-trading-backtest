"""Trade quality assessment"""

trades = [
    {'id': 1, 'sym': 'US2000', 'dir': 'sell', 'date': 'Feb 23', 'pnl': 138.04, 'rr': 1.55,
     'mfe_r': 1.55, 'mae_r': 0.04, 'r_pts': 7.7, 'orb': 16.9, 'time': '49m',
     'slip_pct': 48.1},
    {'id': 2, 'sym': 'SWI20', 'dir': 'buy', 'date': 'Feb 26', 'pnl': -46.27, 'rr': -0.50,
     'mfe_r': 0.62, 'mae_r': 0.91, 'r_pts': 7.5, 'orb': 39.3, 'time': '13m',
     'slip_pct': 6.0},
    {'id': 3, 'sym': 'US2000', 'dir': 'sell', 'date': 'Feb 27', 'pnl': -103.29, 'rr': -1.13,
     'mfe_r': 0.0, 'mae_r': 0.92, 'r_pts': 2.4, 'orb': 20.8, 'time': '4m',
     'slip_pct': 29.2},
    {'id': 4, 'sym': 'US30', 'dir': 'sell', 'date': 'Mar 3', 'pnl': -87.66, 'rr': -1.92,
     'mfe_r': 0.40, 'mae_r': 1.64, 'r_pts': 28.5, 'orb': 311.45, 'time': '2m',
     'slip_pct': 82.6},
    {'id': 5, 'sym': 'NDX100', 'dir': 'buy', 'date': 'Mar 5', 'pnl': -64.56, 'rr': -1.04,
     'mfe_r': 0.75, 'mae_r': 0.58, 'r_pts': 36.75, 'orb': 139.53, 'time': '4m',
     'slip_pct': 29.4},
]

print("=" * 60)
print("TRADE-BY-TRADE QUALITY ASSESSMENT")
print("=" * 60)

for t in trades:
    print(f"\nTrade #{t['id']}: {t['sym']} {t['dir']} ({t['date']})")
    print(f"  P&L: ${t['pnl']:.2f} ({t['rr']:.2f}R)")
    print(f"  MFE: {t['mfe_r']:.2f}R | MAE: {t['mae_r']:.2f}R | Time: {t['time']}")
    print(f"  R size: {t['r_pts']}pts | ORB: {t['orb']}pts | Slip/R: {t['slip_pct']:.1f}%")

    if t['id'] == 1:
        print("  Quality: WIN. Clean trade. Emergency close at +1.55R (fill_price=0 bug).")
        print("           Would have hit TP at +2.5R. Very clean execution.")
        print("  VERDICT: GOOD TRADE")
    elif t['id'] == 2:
        print("  Quality: SWI20 since REMOVED from portfolio. Only -0.5R partial loss.")
        print("           MFE 0.62R = had favorable move before reversal.")
        print("  VERDICT: PREVENTABLE - SWI20 dropped from lineup")
    elif t['id'] == 3:
        print("  Quality: TINY R = 2.4pts! Now blocked by MIN R GUARD (5pts).")
        print("           Never moved in favor (MFE=0). Lost 1.13R due to tiny R inflating loss.")
        print("           SL hit in just 4 minutes.")
        print("  VERDICT: PREVENTABLE - Min R guard now blocks this")
    elif t['id'] == 4:
        print("  Quality: 23.5pt negative slippage = 82.6% of R!")
        print("           Entry drifted 23pts from signal. SL hit in 2 minutes.")
        print("           R/ORB only 9.2% = very wide ORB, tight R relative to volatility.")
        print("  VERDICT: PREVENTABLE - Drift guard now tightened to 0.5R")
    elif t['id'] == 5:
        print("  Quality: Clean -1.04R SL hit. MFE 0.75R = went 75% toward TP.")
        print("           Price moved in favor, then reversed. Legitimate market behavior.")
        print("           Healthy R=36.75pts, reasonable ORB, normal execution.")
        print("  VERDICT: LEGITIMATE LOSS - Normal variance, strategy worked correctly")

print("\n" + "=" * 60)
print("WHAT IF FIXES WERE IN PLACE FROM DAY 1?")
print("=" * 60)
print("  Trade #1: US2000 +$138.04  (still taken, WIN)")
print("  Trade #2: SWI20  SKIPPED   (symbol removed, save $46.27)")
print("  Trade #3: US2000 BLOCKED   (min R guard, save $103.29)")
print("  Trade #4: US30   BLOCKED   (drift guard, save $87.66)")  
print("  Trade #5: NDX100 -$64.56   (still taken, clean SL)")
print()
print("  Hypothetical: 2 trades, 1W/1L = +$73.48")
print("  Hypothetical Balance: $6,073.48 (+1.2%)")
print()
print("  Actual: 5 trades, 1W/4L = -$163.74")
print("  Actual Balance:  $5,836.26 (-2.73%)")
print()
print("  Difference: $237.22 lost to execution/filter issues")
print("  All 3 preventable issues now have deployed fixes")

print("\n" + "=" * 60)
print("STATISTICAL PERSPECTIVE")
print("=" * 60)
print("  Backtest WR: 59% over 459 trades")
print("  Live WR: 20% over 5 trades (1W/4L)")
print()
print("  4-loss streak in backtest: happened 6 times in 459 trades")
print("  Max backtest streak: 5 consecutive losses")
print("  P(1 or fewer wins in first 5 trades): 9.5% = ~1 in 10")
print()
print("  5 trades is FAR too small for any statistical conclusion.")
print("  Need 30+ trades minimum to evaluate live edge.")

print("\n" + "=" * 60)
print("KEY FINDINGS")
print("=" * 60)
print("  1. Only 1 of 4 losses is a TRUE strategy loss (NDX100 -1.04R)")
print("  2. Three losses were from execution/filter gaps - ALL NOW FIXED:")
print("     - SWI20 removed from portfolio")
print("     - Min R guard (5pts) blocks tiny R trades like US2000")
print("     - Drift guard (0.5R) blocks drift entries like US30")
print("  3. Drawdown is only 2.73% - well within normal range")
print("  4. Deal history bug fixed - no more wrong PnL recording")
print("  5. Strategy is NOT broken - you're in early variance territory")
print()
print("  RECOMMENDATION: Keep trading. The fixes address the root causes.")
print("  Watch for the next 20-30 trades to get meaningful statistics.")
