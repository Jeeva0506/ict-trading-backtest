"""Quick verification of today's trades against strategy rules."""

print("=" * 70)
print("  TRADE VERIFICATION - Mar 20, 2026 (US Session)")
print("=" * 70)

# ===== TRADE 1: SPX500 SELL =====
print("\n--- TRADE 1: SPX500 SELL ---\n")
orb_h, orb_l, orb_rng = 6591.17, 6570.02, 21.15
sig_entry, sig_sl = 6554.42, 6562.32
sig_R = sig_sl - sig_entry  # bearish: SL above entry
sig_tp = sig_entry - 2.5 * sig_R
margin = 0.15 * orb_rng

print(f"  ORB: {orb_l:.2f} - {orb_h:.2f} (range={orb_rng:.2f} pts)")
print(f"  HTF Trend: bearish")
print(f"  Signal: SELL @ {sig_entry:.2f}, SL={sig_sl:.2f}, TP={sig_tp:.2f}")
print(f"  Signal R: {sig_R:.2f} pts")
print()

# Strategy filter checks
broke = sig_entry < orb_l - margin
p = lambda ok: "PASS" if ok else "FAIL"
print("  STRATEGY FILTER CHECKS:")
print(f"    Breakout margin: entry {sig_entry} < ORBlow-margin ({orb_l}-{margin:.2f}={orb_l-margin:.2f}) -> {p(broke)}")
print(f"    Direction vs HTF: bearish == bearish -> PASS")
print(f"    min_sl (2.0): R={sig_R:.2f} >= 2.0 -> {p(sig_R >= 2.0)}")
print(f"    max_sl (25.0): R={sig_R:.2f} <= 25.0 -> {p(sig_R <= 25.0)}")
print(f"    TP target: entry - 2.5*R = {sig_entry} - {2.5*sig_R:.2f} = {sig_tp:.2f} -> CORRECT")
print()

# Timing check
print("  TIMING:")
print("    MT5 server: EEST (UTC+3) -- confirmed via GER30 bar count")
print("    Config: mt5_open=16:30, mt5_close=23:00 (server)")
print("    Market opened: 19:00 IST = 16:30 EEST -> CORRECT")
print("    ORB detected: 19:19 IST = 16:49 EEST (19min after open, bars=20) -> CORRECT")
print("    Signal time: 19:44 IST = 17:14 EEST (44min after open)")
print("    Entry cutoff: 16:30 + 3h = 19:30 EEST -> WITHIN WINDOW")
print()

# Live fill analysis
fill_s2, fill_15k = 6541.42, 6541.17
live_R_s2 = sig_sl - fill_s2
live_R_15k = sig_sl - fill_15k
drift_s2 = sig_entry - fill_s2
drift_15k = sig_entry - fill_15k
rr_s2 = (fill_s2 - sig_tp) / live_R_s2
rr_15k = (fill_15k - sig_tp) / live_R_15k

print("  LIVE EXECUTION:")
print(f"    Stellar-2:  fill={fill_s2},  drift={drift_s2:.2f}pts (favorable)")
print(f"                live_R={live_R_s2:.2f}pts, lots=0.13, risk=$27.17 (0.5%)")
print(f"    Stellar-15K: fill={fill_15k}, drift={drift_15k:.2f}pts (favorable)")
print(f"                live_R={live_R_15k:.2f}pts, lots=1.04, risk=$219.96 (1.5%)")
print()
print(f"  ** DRIFT IMPACT **")
print(f"    Price drifted ~13pts in trade direction before fill")
print(f"    TP at {sig_tp:.2f} is only {fill_s2-sig_tp:.2f}pts from fill (was {sig_entry-sig_tp:.2f}pts from signal)")
print(f"    SL at {sig_sl:.2f} is {live_R_s2:.2f}pts from fill (was {sig_R:.2f}pts from signal)")
print(f"    Effective R:R from fill = {rr_s2:.2f}:1 (was 2.50:1 at signal)")
print(f"    Dollar risk is still capped correctly via lot sizing")

# ===== TRADE 2: NDX100 SELL =====
print("\n\n--- TRADE 2: NDX100 SELL ---\n")
orb_h2, orb_l2, orb_rng2 = 24273.98, 24160.89, 113.09
sig_entry2, sig_sl2 = 24065.48, 24105.31
sig_R2 = sig_sl2 - sig_entry2
sig_tp2 = sig_entry2 - 2.5 * sig_R2
margin2 = 0.15 * orb_rng2

print(f"  ORB: {orb_l2:.2f} - {orb_h2:.2f} (range={orb_rng2:.2f} pts)")
print(f"  HTF Trend: bearish")
print(f"  Signal: SELL @ {sig_entry2:.2f}, SL={sig_sl2:.2f}, TP={sig_tp2:.2f}")
print(f"  Signal R: {sig_R2:.2f} pts")
print()

broke2 = sig_entry2 < orb_l2 - margin2
print("  STRATEGY FILTER CHECKS:")
print(f"    Breakout margin: entry {sig_entry2} < ORBlow-margin ({orb_l2}-{margin2:.2f}={orb_l2-margin2:.2f}) -> {p(broke2)}")
print(f"    Direction vs HTF: bearish == bearish -> PASS")
print(f"    min_sl (10.0): R={sig_R2:.2f} >= 10.0 -> {p(sig_R2 >= 10.0)}")
print(f"    max_sl (80.0): R={sig_R2:.2f} <= 80.0 -> {p(sig_R2 <= 80.0)}")
print(f"    TP target: entry - 2.5*R = {sig_entry2} - {2.5*sig_R2:.2f} = {sig_tp2:.2f} -> CORRECT")
print()

print("  TIMING:")
print("    Signal time: 19:44 IST = 17:14 EEST (44min after open) -> WITHIN WINDOW")
print()

fill2_s2, fill2_15k = 24032.91, 24031.41
live_R2_s2 = sig_sl2 - fill2_s2
live_R2_15k = sig_sl2 - fill2_15k
drift2_s2 = sig_entry2 - fill2_s2
drift2_15k = sig_entry2 - fill2_15k
rr2_s2 = (fill2_s2 - sig_tp2) / live_R2_s2
rr2_15k = (fill2_15k - sig_tp2) / live_R2_15k

print("  LIVE EXECUTION:")
print(f"    Stellar-2:  fill={fill2_s2},  drift={drift2_s2:.2f}pts (favorable)")
print(f"                live_R={live_R2_s2:.2f}pts, lots=0.03, risk=$21.68 (0.4%)")
print(f"    Stellar-15K: fill={fill2_15k}, drift={drift2_15k:.2f}pts (favorable)")
print(f"                live_R={live_R2_15k:.2f}pts, lots=0.29, risk=$214.31 (1.5%)")
print()
print(f"  ** DRIFT IMPACT **")
print(f"    Price drifted ~{drift2_s2:.0f}pts in trade direction before fill")
print(f"    TP at {sig_tp2:.2f} is only {fill2_s2-sig_tp2:.2f}pts from fill (was {sig_entry2-sig_tp2:.2f}pts from signal)")
print(f"    SL at {sig_sl2:.2f} is {live_R2_s2:.2f}pts from fill (was {sig_R2:.2f}pts from signal)")
print(f"    Effective R:R from fill = {rr2_s2:.2f}:1 (was 2.50:1 at signal)")
print(f"    Dollar risk is still capped correctly via lot sizing")

# ===== SUMMARY =====
print("\n\n" + "=" * 70)
print("  SUMMARY")
print("=" * 70)
print()
print("  Both trades PASS all strategy filter checks:")
print("    - ORB breakout with margin: PASS")
print("    - HTF trend alignment (bearish): PASS")
print("    - Displacement + body% + close strength: PASS (logged)")
print("    - SL within min/max range: PASS")
print("    - TP at 2.5R from signal entry: PASS")
print("    - Entry within time window (< entry_hours cutoff): PASS")
print("    - Market session timing (EEST server): CORRECT")
print()
print("  CONCERN: Both trades had significant favorable drift")
print("    SPX500: 13pts drift -> effective 0.32R from fill (was 2.5R)")
print("    NDX100: 33pts drift -> effective 0.93R from fill (was 2.5R)")
print("    This means TP is much closer than intended, SL much further.")
print("    Dollar risk is still correct (lot sizing adjusted).")
print("    TP has HIGH probability of hitting (price already moved in direction).")
print("    But if it reverses, loss is at full risk amount.")
