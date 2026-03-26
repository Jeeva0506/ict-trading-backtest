# FULL OPTIMIZATION ANALYSIS - LOOKBACK WINDOW FOCUS

## PROBLEM IDENTIFIED: Late Entry Risk

You identified that **lookback window may be reducing or causing late entries**.

### Root Cause Analysis

**Current Setting:** `lookback_ranges=10`

**How It Affects Entry Timing:**
```
Lookback Window = Number of previous candles used to calculate 
                  average range for displacement filter

Small (5-8):   Fast response, may catch early moves
Medium (10):   Balanced (current)
Large (12+):   Stable but potentially late detection in fast moves
```

**Scenario: Sudden Breakout in a Consolidating Market**
```
Consolidation Phase (3-5 bars): avg_range = 50 pips

With lookback=5:  
  → Rapid recalculation of avg_range
  → displacement_target = 50 × 1.3 = 65 pips
  → ENTRY TRIGGERS QUICKLY (candle breaks this level)
  → CATCHES EARLY, fewer bars delayed

With lookback=15:
  → Slower recalculation (includes more history)
  → Uses stale avg_range longer
  → displacement_target = 50 × 1.3 = 65 pips (but delayed)
  → Price already moved 100+ pips
  → ENTRY IS LATE, misses initial impulse
```

---

## FULL OPTIMIZATION PLAN

### Phase 1: LOOKBACK WINDOW TESTING (7 Tests)
```
Test lookback_ranges: [5, 6, 8, 10, 12, 15, 20]
Everything else fixed at current baseline
Duration: ~30 minutes

Expected Winners:
  - SPX500/NDX100 (fast movers): lookback = 6-8
  - GER30/FRA40 (medium): lookback = 8-10  
  - UK100 (stable): lookback = 10-12
```

### Phase 2: BREAKOUT & DISPLACEMENT (12 Tests)
```
Fix lookback to Phase 1 winners
Test combinations:
  breakout_margin: [0.02, 0.03, 0.05, 0.07]
  disp_mult: [1.2, 1.3, 1.5]
Duration: ~1 hour

Identify best sensitivity for each index
```

### Phase 3: BODY & CLOSE FILTERS (16 Tests)
```
Fix lookback, margin, disp from Phases 1-2
Test combinations:
  body_min: [0.50, 0.60, 0.70, 0.80]
  close_strength: [0.15, 0.20, 0.25, 0.30]
Duration: ~1 hour

Fine-tune entry quality
```

### Phase 4: BUILD 4 FINAL VARIANTS
```
VARIANT A: AGGRESSIVE (Earliest Entry)
  - lookback=8 (fast)
  - breakout_margin=0.02 (sensitive)
  - disp_mult=1.2 (loose)
  - body_min=0.50, close_strength=0.20
  - RESULT: Most trades, catches early, higher false positives

VARIANT B: BALANCED (Current Optimized)
  - lookback=10 (medium)
  - breakout_margin=0.05
  - disp_mult=1.3
  - body_min=0.60, close_strength=0.25
  - RESULT: Good trade-off, baseline

VARIANT C: CONSERVATIVE (Quality Focus)
  - lookback=12 (stable)
  - breakout_margin=0.07 (difficult)
  - disp_mult=1.5 (tight)
  - body_min=0.75, close_strength=0.30
  - RESULT: Fewest trades, highest quality, slight entry delay OK

VARIANT D: EARLY_SCALP (Fastest Detection)
  - lookback=6 (very fast)
  - breakout_margin=0.03
  - disp_mult=1.2
  - body_min=0.55, close_strength=0.20
  - RESULT: Fastest response, best for volatile periods
```

### Phase 5: WALK-FORWARD VALIDATION (10-Year)
```
Test each final variant on full 10-year history
Split: Train 2016-2020, Test 2020-2023, Validate 2023-2026

Check:
  - Does OOS performance exceed 50% of IS performance?
  - Is variant robust across time periods?
  - Consistent across indices?
```

### Phase 6: FINAL RECOMMENDATION
```
Compare all variants in matrix:
  - Trade count vs baseline
  - Win rate changes
  - Expectancy per trade
  - Max drawdown
  - Entry timing (bars until fill)
  - Walk-forward consistency
  
Recommend top 3 for live implementation
```

---

## 4 VARIANTS SUMMARY

| Metric | AGGRESSIVE | BALANCED | CONSERVATIVE | EARLY_SCALP |
|--------|-----------|----------|--------------|------------|
| **Lookback** | 8 | 10 | 12 | 6 |
| **Entry Speed** | Fast | Medium | Slow | Fastest |
| **Expected Trades** | +40% | Baseline | -30% | +50% |
| **Expected WR** | ~57% | 61% | ~65% | ~56% |
| **Expected DD** | ~14% | 12% | ~10% | ~15% |
| **Best For** | Choppy/Scalp | Normal | Quality | Fast Sessions |
| **Late Entry Risk** | Low | Medium | High | Very Low |

---

## EXPECTED IMPROVEMENTS

**If lookback is the bottleneck:**

- **AGGRESSIVE variant** could catch moves 1-3 bars earlier
- **EARLY_SCALP** could catch volatile session spikes immediately
- **CONSERVATIVE** trades slower but avoids false breakouts

**Expected outcomes after optimization:**
- ✓ Faster entry detection (0.5-2 bar improvement)
- ✓ Identification of best lookback per index
- ✓ Improved trade timing on fast-moving markets
- ✓ Maintained quality / reduced chop through proper filters

---

## EXECUTION TIMELINE

| Step | Duration | Task |
|------|----------|------|
| 1 | 30 min | Lookback window testing (7 combos) |
| 2 | 1 hour | Breakout & displacement (12 combos) |
| 3 | 1 hour | Body & close filters (16 combos) |
| 4 | 30 min | Build final variants |
| 5 | 1.5 hours | Walk-forward validation (10-year) |
| 6 | 30 min | Generate report |
| **TOTAL** | **~4.5 hours** | *Full optimization* |

---

## KEY FINDINGS PREVIEW

**From existing data analysis:**
- 482 trades on current baseline (61% WR, 2.33x PF)
- SPX500 strongest: 65.8% WR, 3.63x PF (312 bars avg)
- US2000 weakest: 51.1% WR, 1.51x PF (should exclude)
- Displacement filter (disp_mult) = highest impact parameter

**Late entry risk factors:**
1. Lookback too large → slow response
2. Breakout margin too tight → miss early moves
3. ORB box size (orb_minutes) affects initial range quality

---

## APPROVAL CHECKLIST

Ready to proceed with full optimization if you approve:

- [ ] Test lookback_ranges [5, 6, 8, 10, 12, 15, 20] to find optimal?
- [ ] Build 4 optimized variants (AGGRESSIVE, BALANCED, CONSERVATIVE, EARLY_SCALP)?
- [ ] Run 10-year walk-forward validation?
- [ ] Accept that parameters may change from current?
- [ ] Ready to test winners on live trading for 2-4 weeks?

---

## LIVE IMPLEMENTATION PLAN (After Approval)

**Week 1-2:**
- Run BALANCED variant on STELLAR-2 (6K account)
- Keep STELLAR-15K on current to compare
- Track: Trade count, WR, entry quality

**If BALANCED wins:**
- Apply to STELLAR-15K
- Monitor drawdown vs expectations
- Adjust if needed

**Ongoing:**
- Every 50 trades, compare actual vs backtest
- If divergence > 5%, investigate root cause
- Quarterly reoptimization recommended

---

## QUESTIONS TO CONFIRM

1. **Early vs Quality?** Do you prefer earlier entries (AGGRESSIVE) or higher quality (CONSERVATIVE)?
2. **Trade Count?** Would you prefer ±30% more/fewer trades than current?
3. **Timeline?** Run full optimization now, or test lookback window first (30 min) to see impact?
4. **Risk Level?** Can we test variants simultaneously on both accounts, or apply incrementally?

---

**Status: READY TO EXECUTE**
All configurations generated and ready to run.
Awaiting your approval to proceed with full optimization.
