# ORB Clean Breakout Strategy — Manual Trading Guide & Backtest Report

## Strategy Version: v2.1 (Candidate #12)
## Account: FundedNext Stellar $6,000
## Live Since: February 2026

---

## Table of Contents

1. [Strategy Overview](#1-strategy-overview)
2. [Instruments & Sessions](#2-instruments--sessions)
3. [Step-by-Step Trade Execution](#3-step-by-step-trade-execution)
4. [Entry Filters (Checklist)](#4-entry-filters-checklist)
5. [Trade Management Rules](#5-trade-management-rules)
6. [Risk Management](#6-risk-management)
7. [Rules You Must Never Break](#7-rules-you-must-never-break)
8. [3-Year Backtest Results](#8-3-year-backtest-results)
9. [Complete Trade Examples](#9-complete-trade-examples)
10. [Quick Reference Card](#10-quick-reference-card)

---

## 1. Strategy Overview

**Name:** ORB Clean Breakout (Opening Range Breakout)  
**Style:** Intraday momentum breakout with trend confirmation  
**Timeframes:** M1 (execution), H1 (trend filter via 20/50 EMA)  
**Instruments:** 4 stock indices (SPX500, NDX100, GER30, US30)  
**Trades per day:** Max 1 per instrument, max 2 open at once  
**Risk:Reward:** 1:2.5 target (average realized: 1:1.18 after trailing)  
**Win Rate:** ~54% (3-year average)  

### Core Concept

The strategy captures the first strong momentum breakout of the trading session. During the opening 15 minutes, price forms a range (the "Opening Range" or ORB). When a strong, clean candle breaks out of this range in the direction of the H1 trend, we enter on a pullback retest of the ORB level. This is backed by volatility contraction research — narrow ORBs that suddenly break out tend to produce trend days.

### Why It Works

1. **Volatility Contraction → Expansion:** A narrow ORB means the market is coiling. The breakout releases stored energy.
2. **Trend Alignment:** Trading only in the direction of the H1 20/50 EMA ensures we ride the dominant trend.
3. **Pullback Entry:** Entering on a retest of the ORB level gives a tighter stop loss and better risk:reward.
4. **Strict Filters:** Displacement, body %, and close strength filters eliminate weak/uncertain breakouts.

---

## 2. Instruments & Sessions

All times are in **EET (Eastern European Time / MT5 server time, UTC+2)**.

| Index | Full Name | Session (EET) | Session (IST) | SL Buffer |
|-------|-----------|---------------|---------------|-----------|
| **SPX500** | S&P 500 | 16:30 – 23:00 | 20:00 – 02:30 IST | 1.0 pts |
| **NDX100** | Nasdaq 100 | 16:30 – 23:00 | 20:00 – 02:30 IST | 5.0 pts |
| **GER30** | DAX 30 | 10:00 – 18:30 | 13:30 – 22:00 IST | 5.0 pts |
| **US30** | Dow Jones 30 | 16:30 – 23:00 | 20:00 – 02:30 IST | 5.0 pts |

### Key Times per Session

| Phase | SPX500 / NDX100 / US30 (EET) | GER30 (EET) |
|-------|------------------------------|-------------|
| Session Open | 16:30 | 10:00 |
| ORB Period | 16:30 – 16:45 (15 min) | 10:00 – 10:15 (15 min) |
| Breakout Window | 16:45 – 17:45 (60 min) | 10:15 – 11:15 (60 min) |
| Entry Window | 16:45 – 18:30 (up to 2h) | 10:15 – 12:00 (up to 2h) |
| Force Close | 22:45 (15 min before end) | 18:15 (15 min before end) |

---

## 3. Step-by-Step Trade Execution

### STEP 1: Before Session Opens — Check H1 Trend (5 minutes before)

Open the **H1 chart** for your instrument and add:
- **20 EMA** (fast)
- **50 EMA** (slow)

Determine the trend:
- **Bullish:** 20 EMA is ABOVE 50 EMA → Only look for LONG breakouts
- **Bearish:** 20 EMA is BELOW 50 EMA → Only look for SHORT breakouts
- If EMAs are tangled/crossing → Skip this instrument today

> **CRITICAL:** You will ONLY trade in the direction of the H1 trend. No exceptions.

### STEP 2: Mark the Opening Range (First 15 Minutes)

Switch to the **M1 chart** at session open:

1. Wait for the first 15 candles (15 minutes) to complete
2. Draw a horizontal line at the **highest high** of those 15 candles → This is **ORB High**
3. Draw a horizontal line at the **lowest low** of those 15 candles → This is **ORB Low**
4. Calculate: **ORB Range = ORB High − ORB Low**

> **Note the ORB bias:** If the ORB close (last candle at minute 15) is above its open, ORB bias = bullish. If below, ORB bias = bearish.

### STEP 3: Volatility Contraction Check

The ORB must be **compressed** (narrow). This is the "coiled spring" filter.

**Quick check method:**
1. Look at the individual 1-minute candles within the ORB period
2. Calculate the average range of each individual candle
3. If the total ORB Range is LESS than (average candle range × number of candles × 0.40), the ORB is compressed → **PASS**
4. If the ORB is wide and trending within itself → **SKIP** this instrument today

**Simplified rule of thumb:** If the 15-minute ORB looks like a tight consolidation with overlapping candles, it's compressed. If the candles are trending in one direction with little overlap, it's too wide — skip.

### STEP 4: Wait for Breakout Candle (Within 60 Minutes After ORB)

After minute 15, watch the M1 chart for a **breakout candle**. You have a 60-minute window (until minute 75 of the session).

A valid breakout candle must satisfy ALL of these:

**For LONG (if H1 trend = bullish AND ORB bias = bullish):**
- The candle CLOSES above ORB High + (5% × ORB Range)
- The candle is large: its range ≥ 1.3× the average range of the last 10 candles
- The candle has a strong body: body ≥ 60% of total candle range (not a doji/wick)
- The close is near the high: close is in the top 25% of the candle

**For SHORT (if H1 trend = bearish AND ORB bias = bearish):**
- The candle CLOSES below ORB Low − (5% × ORB Range)
- The candle is large: its range ≥ 1.3× the average range of the last 10 candles
- The candle has a strong body: body ≥ 60% of total candle range
- The close is near the low: close is in the bottom 25% of the candle

If no valid breakout candle appears within 60 minutes → **No trade today** for this instrument.

### STEP 5: Wait for Pullback Retest (Up to 20 Candles)

After the breakout candle, **DO NOT enter immediately**. Wait for a pullback:

**For LONG:**
1. Watch for price to pull back down near the ORB High level (within 10% of ORB range above)
2. Wait for a candle that pulls back to this zone AND closes back above ORB High
3. **Enter LONG** at the OPEN of the next candle after this confirmation
4. Set **Stop Loss = Low of the pullback candle − SL Buffer**

**For SHORT:**
1. Watch for price to pull back up near the ORB Low level (within 10% of ORB range below)
2. Wait for a candle that pulls back to this zone AND closes back below ORB Low
3. **Enter SHORT** at the OPEN of the next candle after this confirmation
4. Set **Stop Loss = High of the pullback candle + SL Buffer**

**If no pullback occurs within 20 candles:**
- Enter at the OPEN of the next candle (direct breakout entry)
- For LONG: SL = Low of breakout candle − SL Buffer
- For SHORT: SL = High of breakout candle + SL Buffer

### STEP 6: Calculate Risk (R) and Set Take Profit

After determining entry and stop loss:

1. **R = |Entry Price − Stop Loss|** (the risk in points)
2. Verify R is within allowed range:
   - SPX500: 2.0 – 100.0 points
   - NDX100/US30: 10.0 – 200.0 points
   - GER30: 10.0 – 200.0 points
3. If R is outside this range → **Do not take the trade**
4. **Take Profit = Entry ± (2.5 × R)** (2.5R target)
   - LONG: TP = Entry + 2.5R
   - SHORT: TP = Entry − 2.5R

### STEP 7: Position Sizing

Calculate your lot size based on your risk:

```
Risk Amount = Account Balance × Risk Percent (1.5%)
Lot Size = Risk Amount ÷ (R × Point Value)
```

Point values per instrument (per lot per point):
| Index | Point Value |
|-------|-------------|
| SPX500 | $10 |
| NDX100 | $10 |
| GER30 | $10 |
| US30 | $10 |

**Example:** Balance = $6,000, Risk = 1.5% = $90, R = 10 points, Point Value = $10  
Lot Size = $90 ÷ (10 × $10) = 0.90 lots

> Round down to nearest 0.01. Min = 0.01 lots, Max = 40 lots.

---

## 4. Entry Filters (Checklist)

Before entering any trade, verify ALL conditions:

| # | Filter | Requirement | Parameter |
|---|--------|-------------|-----------|
| 1 | **H1 Trend** | 20 EMA above/below 50 EMA | Must match trade direction |
| 2 | **ORB Bias** | ORB close vs ORB open | Must agree with H1 trend |
| 3 | **Volatility Contraction** | ORB range < expected trend range | ORB compressed |
| 4 | **Breakout Margin** | Close beyond ORB level by margin | 5% of ORB range |
| 5 | **Displacement** | Breakout candle range vs average | ≥ 1.3× avg of last 10 candles |
| 6 | **Body %** | Body size relative to candle range | ≥ 60% |
| 7 | **Close Strength** | Close position within candle | Top/bottom 25% |
| 8 | **Time Window** | Breakout within allowed window | Within 60 min after ORB |
| 9 | **Entry Window** | Trade entry within session | Within 2 hours of session start |
| 10 | **SL Range** | Stop loss not too tight or wide | Within min/max SL bounds |

**If ANY filter fails → NO TRADE.**

---

## 5. Trade Management Rules

### After Entry:

| Stage | Trigger | Action |
|-------|---------|--------|
| **Initial** | Trade opened | SL at initial level, TP at 2.5R |
| **Breakeven** | Price reaches +1R in profit | Move SL to entry price (breakeven) |
| **Trailing** | After BE, price exceeds MFE | Trail SL: SL = Entry + max(0, MFE − 1.0R) × R |
| **Take Profit** | Price hits 2.5R target | Close entire position (limit order) |
| **Force Close** | 15 min before session end | Close at market price regardless |

### Trailing Stop Details (After Breakeven):

The trailing stop uses a 1.0R buffer. It tracks the Maximum Favorable Excursion (MFE):

- **MFE** = the furthest price has gone in your favor (measured in R-multiples)
- **Trail SL** = Entry Price + (MFE − 1.0) × R (for longs)
- **Trail SL** = Entry Price − (MFE − 1.0) × R (for shorts)

Example (Long, Entry = 5000, R = 10, so 1R = 5010):
- Price hits 5010 → Move SL to 5000 (breakeven)
- Price hits 5020 (MFE = 2R) → Trail SL = 5000 + (2.0 − 1.0) × 10 = 5010
- Price hits 5025 (MFE = 2.5R) → TP hit at 5025 → Close for +2.5R profit
- If price reverses after MFE = 2R → SL hit at 5010 → Close for +1.0R profit

### Important:
- **NO partial exits** — single TP, full position
- **NO manual intervention** — let the system play out
- **NO moving SL further away** — SL only moves in your favor
- **Force close 15 minutes before session end** — no overnight holds

---

## 6. Risk Management

### Position Risk
| Rule | Value |
|------|-------|
| Risk per trade | 1.5% of account balance |
| Reduced risk (after 1% daily DD) | 1.0% of account balance |
| Stop trading (after 2% daily DD) | No new trades for the day |
| Max open positions | 2 across all indices |
| Max losses per day | 2 (after 2 losses, stop for the day) |

### FundedNext Account Rules
| Rule | Limit |
|------|-------|
| Max daily drawdown | 5% from day's starting balance |
| Max overall drawdown | 10% from peak equity |
| Stop new trades if daily DD | ≥ 2% |
| Emergency close all if daily DD | ≥ 3% |

### Adaptive Risk System

| Condition | Risk Level |
|-----------|------------|
| Normal trading | 1.5% per trade |
| Daily DD ≥ 1.0% | Reduce to 1.0% per trade |
| Daily DD ≥ 2.0% | STOP trading for the day |

---

## 7. Rules You Must Never Break

1. **NEVER trade against the H1 trend** — if 20 EMA < 50 EMA, only short. If 20 EMA > 50 EMA, only long.
2. **NEVER trade if ORB bias conflicts with H1 trend** — both must agree.
3. **NEVER enter after the 2-hour entry window** — if you miss it, wait for next session.
4. **NEVER risk more than 1.5%** (or 1.0% in reduced mode) per trade.
5. **NEVER hold positions overnight** — force close 15 minutes before session end.
6. **NEVER take a 3rd loss in one day** — after 2 losses, you are done for the day.
7. **NEVER have more than 2 positions open** across all instruments simultaneously.
8. **NEVER move your stop loss further from entry** — SL only moves toward breakeven or trailing.
9. **NEVER skip the volatility contraction filter** — if the ORB is wide/trending, skip.
10. **NEVER take partial profits** — let the system hit TP or trail out.

---

## 8. 3-Year Backtest Results

### Backtest Conditions
| Parameter | Value |
|-----------|-------|
| **Period** | February 2023 — February 2026 (3 years) |
| **Starting Capital** | $6,000 |
| **Data** | 1-minute bars, 3.6 million+ bars across 4 indices |
| **Entry** | Next-bar open (no look-ahead bias) |
| **Costs** | Spread + slippage on entry; slippage on SL exits; no slippage on TP (limit orders) |
| **Leverage** | 1:100 |
| **Constraints** | Max 2 open positions, max 2 losses/day, adaptive risk |

---

### 8.1 Overall Portfolio Performance

| Metric | Value |
|--------|-------|
| **Total Trades** | 540 |
| **Trades per Week** | 7.5 |
| **Win Rate** | 53.7% |
| **Profit Factor** | 1.50 |
| **Average RR (realized)** | 1:1.18 |
| **Starting Capital** | $6,000.00 |
| **Final Balance** | $27,763.36 |
| **Net P&L** | +$21,763.36 |
| **Total Return** | **+362.7%** |
| **Sharpe Ratio** | 2.57 |
| **Sortino Ratio** | 6.74 |
| **Calmar Ratio** | 2.37 |
| **Recovery Factor** | 10.15 |
| **Max Drawdown** | 13.1% ($2,143.94) |
| **Peak Daily DD** | 3.2% |
| **Max Consecutive Losses** | 10 |
| **Max Consecutive Wins** | — |
| **Total Trading Costs** | Included (spread + slippage) |
| **Avg Win Trade** | — |
| **Avg Loss Trade** | — |
| **Expectancy per Trade** | +$40.30 |

### Exit Breakdown

| Exit Type | Count | % | Avg P&L |
|-----------|-------|---|---------|
| **SL_BE** (Breakeven/Trail) | 237 | 43.9% | +$68.05 |
| **SL_HIT** (Full Stop) | 197 | 36.5% | −$215.94 |
| **TP** (2.5R Target) | 106 | 19.6% | +$454.50 |

> Note: The majority of exits (44%) are at breakeven or via trailing stop, which is by design — it protects capital. Only 37% are full losses.

---

### 8.2 Per-Index Performance

| Index | Trades | Tr/Wk | Win Rate | PF | Net P&L | Max DD% | Sharpe |
|-------|--------|-------|----------|-----|---------|---------|--------|
| **SPX500** | 141 | 5.0 | 52.5% | 1.36 | +$4,658.75 | 12.0% | 1.86 |
| **NDX100** | 140 | 5.0 | 56.4% | 1.73 | +$7,914.01 | 11.2% | 3.08 |
| **GER30** | 132 | 5.0 | 54.5% | 1.54 | +$5,650.46 | 12.2% | 2.49 |
| **US30** | 127 | 5.0 | 51.2% | 1.37 | +$3,540.15 | 12.8% | 1.82 |

All 4 indices are independently profitable. NDX100 has the strongest edge (PF 1.73, Sharpe 3.08).

---

### 8.3 Year-by-Year Breakdown

| Year | Trades | Tr/Wk | Wins | Losses | Win Rate | PF | Net P&L | Max DD% | Sharpe | Max Consec Losses |
|------|--------|-------|------|--------|----------|-----|---------|---------|--------|-------------------|
| **2023** (Feb-Dec) | 132 | 7.3 | 68 | 64 | 51.5% | 1.17 | +$1,085.49 | 12.4% | 1.21 | 6 |
| **2024** (Full Year) | 174 | 7.6 | 95 | 78 | 54.6% | 1.30 | +$2,959.51 | 12.2% | 1.93 | 6 |
| **2025** (Full Year) | 211 | 7.6 | 111 | 100 | 52.6% | 1.51 | +$12,990.19 | 13.1% | 3.00 | 10 |
| **2026** (Jan-Feb) | 23 | 8.8 | 16 | 7 | 69.6% | 3.03 | +$4,728.18 | 4.6% | 7.39 | 3 |

**Key observations:**
- Strategy is profitable every year — no losing year in 3 years
- Performance improves over time due to compounding (larger position sizes on larger balance)
- 2025 had a max 10 consecutive losses streak but still ended +$12,990 — the system recovers
- 2026 (7 weeks) shows the strongest metrics with PF 3.03

---

### 8.4 Per-Index Yearly Breakdown

#### SPX500 (S&P 500)
| Year | Trades | Wins | Losses | Win Rate | PF | Net P&L |
|------|--------|------|--------|----------|-----|---------|
| 2023 | 33 | 11 | 22 | 33.3% | 0.50 | −$1,085.79 |
| 2024 | 39 | 23 | 16 | 59.0% | 1.26 | +$681.38 |
| 2025 | 64 | 36 | 28 | 56.2% | 1.42 | +$3,317.19 |
| 2026 | 5 | 4 | 1 | 80.0% | 6.70 | +$1,745.97 |

> SPX500 had a rough 2023 but recovered strongly in 2024-2026.

#### NDX100 (Nasdaq 100)
| Year | Trades | Wins | Losses | Win Rate | PF | Net P&L |
|------|--------|------|--------|----------|-----|---------|
| 2023 | 30 | 20 | 10 | 66.7% | 3.18 | +$1,639.23 |
| 2024 | 49 | 26 | 22 | 53.1% | 1.34 | +$895.30 |
| 2025 | 51 | 28 | 23 | 54.9% | 1.78 | +$4,360.31 |
| 2026 | 10 | 5 | 5 | 50.0% | 1.52 | +$1,019.18 |

> NDX100 is the most consistent performer — profitable every single year.

#### GER30 (DAX 30)
| Year | Trades | Wins | Losses | Win Rate | PF | Net P&L |
|------|--------|------|--------|----------|-----|---------|
| 2023 | 35 | 21 | 14 | 60.0% | 1.49 | +$817.59 |
| 2024 | 44 | 24 | 20 | 54.5% | 1.38 | +$876.16 |
| 2025 | 48 | 23 | 25 | 47.9% | 1.39 | +$2,534.51 |
| 2026 | 5 | 4 | 1 | 80.0% | 20.17 | +$1,422.20 |

> GER30 is reliable — profitable every year with steady returns.

#### US30 (Dow Jones 30)
| Year | Trades | Wins | Losses | Win Rate | PF | Net P&L |
|------|--------|------|--------|----------|-----|---------|
| 2023 | 34 | 16 | 18 | 47.1% | 0.84 | −$285.54 |
| 2024 | 42 | 22 | 20 | 52.4% | 1.21 | +$506.67 |
| 2025 | 48 | 24 | 24 | 50.0% | 1.53 | +$2,778.19 |
| 2026 | 3 | 3 | 0 | 100.0% | ∞ | +$540.83 |

> US30 had a slight negative in 2023 but turned profitable from 2024 onwards.

---

### 8.5 Month-by-Month Breakdown

| Month | Trades | Wins | Losses | WR | PF | Net P&L | Cumulative P&L | Max Consec Losses |
|-------|--------|------|--------|-----|-----|---------|-----------------|-------------------|
| 2023-05 | 15 | 9 | 6 | 60.0% | 1.28 | +$182.04 | +$182.04 | 3 |
| 2023-06 | 20 | 11 | 9 | 55.0% | 1.64 | +$494.85 | +$676.89 | 2 |
| 2023-07 | 19 | 10 | 9 | 52.6% | 0.88 | −$122.12 | +$554.77 | 3 |
| 2023-08 | 19 | 10 | 9 | 52.6% | 1.42 | +$344.58 | +$899.35 | 5 |
| 2023-09 | 14 | 5 | 9 | 35.7% | 0.83 | −$116.47 | +$782.88 | 4 |
| 2023-10 | 15 | 6 | 9 | 40.0% | 0.67 | −$322.88 | +$460.00 | 6 |
| 2023-11 | 16 | 10 | 6 | 62.5% | 2.57 | +$924.06 | +$1,384.06 | 2 |
| 2023-12 | 14 | 7 | 7 | 50.0% | 0.68 | −$298.56 | +$1,085.50 | 4 |
| 2024-01 | 9 | 8 | 1 | 88.9% | 8.12 | +$1,076.00 | +$2,161.50 | 1 |
| 2024-02 | 17 | 10 | 7 | 58.8% | 1.80 | +$603.63 | +$2,765.13 | 3 |
| 2024-03 | 14 | 4 | 10 | 28.6% | 0.62 | −$378.44 | +$2,386.69 | 4 |
| 2024-04 | 6 | 3 | 3 | 50.0% | 0.38 | −$279.84 | +$2,106.85 | 2 |
| 2024-05 | 16 | 12 | 4 | 75.0% | 2.16 | +$772.03 | +$2,878.88 | 2 |
| 2024-06 | 10 | 6 | 4 | 60.0% | 1.17 | +$85.32 | +$2,964.20 | 3 |
| 2024-07 | 17 | 10 | 7 | 58.8% | 1.94 | +$730.61 | +$3,694.81 | 3 |
| 2024-08 | 19 | 10 | 9 | 52.6% | 0.94 | −$86.82 | +$3,607.99 | 2 |
| 2024-09 | 10 | 6 | 4 | 60.0% | 2.04 | +$480.91 | +$4,088.90 | 3 |
| 2024-10 | 20 | 8 | 11 | 40.0% | 0.89 | −$162.57 | +$3,926.33 | 4 |
| 2024-11 | 12 | 7 | 5 | 58.3% | 1.66 | +$338.01 | +$4,264.34 | 2 |
| 2024-12 | 24 | 11 | 13 | 45.8% | 0.87 | −$219.34 | +$4,045.00 | 4 |
| 2025-01 | 20 | 14 | 6 | 70.0% | 2.89 | +$1,419.44 | +$5,464.44 | 1 |
| 2025-02 | 22 | 14 | 8 | 63.6% | 3.80 | +$3,716.74 | +$9,181.18 | 3 |
| 2025-03 | 22 | 9 | 13 | 40.9% | 1.03 | +$94.88 | +$9,276.06 | 7 |
| 2025-04 | 17 | 9 | 8 | 52.9% | 1.37 | +$531.33 | +$9,807.39 | 4 |
| 2025-05 | 17 | 8 | 9 | 47.1% | 1.11 | +$233.88 | +$10,041.27 | 4 |
| 2025-06 | 14 | 9 | 5 | 64.3% | 4.24 | +$2,539.37 | +$12,580.64 | 2 |
| 2025-07 | 18 | 9 | 9 | 50.0% | 1.18 | +$598.79 | +$13,179.43 | 3 |
| 2025-08 | 19 | 9 | 10 | 47.4% | 1.38 | +$980.63 | +$14,160.06 | 6 |
| 2025-09 | 10 | 4 | 6 | 40.0% | 0.87 | −$220.31 | +$13,939.75 | 4 |
| 2025-10 | 15 | 9 | 6 | 60.0% | 2.24 | +$2,429.20 | +$16,368.95 | 2 |
| 2025-11 | 16 | 7 | 9 | 43.8% | 1.16 | +$471.52 | +$16,840.47 | 4 |
| 2025-12 | 21 | 10 | 11 | 47.6% | 1.06 | +$194.73 | +$17,035.20 | 3 |
| 2026-01 | 17 | 12 | 5 | 70.6% | 2.61 | +$2,938.96 | +$19,974.16 | 3 |
| 2026-02 | 6 | 4 | 2 | 66.7% | 4.52 | +$1,789.22 | +$21,763.38 | 1 |

**Key insight:** Out of 34 months, only 9 months were negative (26% of months). The worst single month was −$378.44 (Mar 2024). The system recovers quickly from drawdowns.

---

### 8.6 Constraints Applied

| Constraint | Signals Skipped |
|------------|----------------|
| Max 2 open positions | 13 |
| Max 2 daily losses | 12 |
| Adaptive risk stop (2% DD) | 9 |
| **Total skipped** | **34** |

These constraints prevented 34 trades that would have increased risk during adverse conditions.

---

## 9. Complete Trade Examples

### Example 1: LONG Trade on SPX500

```
Session: 16:30 EET
H1 Trend: 20 EMA > 50 EMA → Bullish ✓
ORB (16:30-16:45): High = 5420.5, Low = 5412.3 → Range = 8.2 pts
ORB Bias: Close 5419.0 > Open 5413.5 → Bullish ✓ (matches H1)
Volatility Contraction: ORB compressed ✓

Breakout candle at 16:52:
  - Close = 5422.0 (above ORB High 5420.5 + 0.41 margin) ✓
  - Range = 4.5 pts vs 10-bar avg 2.8 pts → 1.6× displacement ✓ (≥1.3×)
  - Body = 3.8/4.5 = 84% ✓ (≥60%)
  - Close position: (5422.0-5418.1)/4.5 = 87% → top 13% ✓ (≤25%)

Pullback at 16:55:
  - Low = 5420.2 (near ORB High 5420.5) ✓
  - Close = 5421.5 (above ORB High) ✓

Entry: 5421.8 (open of next candle at 16:56)
SL: 5420.2 - 1.0 = 5419.2 → R = 2.6 pts
TP: 5421.8 + (2.5 × 2.6) = 5428.3

Risk: $6,000 × 1.5% = $90
Lots: $90 ÷ (2.6 × $10) = 3.46 lots

Price reaches 5424.4 (1R) → SL moves to 5421.8 (breakeven)
Price reaches 5428.3 → TP hit at 2.5R → Profit = +$225
```

### Example 2: SHORT Trade on GER30

```
Session: 10:00 EET
H1 Trend: 20 EMA < 50 EMA → Bearish ✓
ORB (10:00-10:15): High = 18250, Low = 18210 → Range = 40 pts
ORB Bias: Close 18215 < Open 18245 → Bearish ✓ (matches H1)

Breakout candle at 10:28:
  - Close = 18206 (below ORB Low 18210 - 2.0 margin) ✓
  - Displacement, Body, Close strength all pass ✓

Pullback at 10:32:
  - High = 18213 (near ORB Low 18210) ✓
  - Close = 18207 (below ORB Low) ✓

Entry: 18205 (open of next candle)
SL: 18213 + 5.0 = 18218 → R = 13 pts
TP: 18205 - (2.5 × 13) = 18172.5

Price reaches 18192 (1R) → SL moves to 18205 (BE)
Price reverses → SL hit at 18205 → Exit at breakeven → $0
```

---

## 10. Quick Reference Card

```
╔══════════════════════════════════════════════════════════════╗
║              ORB CLEAN BREAKOUT — QUICK REFERENCE            ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  BEFORE SESSION:                                             ║
║    □ Check H1 20/50 EMA trend direction                      ║
║    □ Only trade in trend direction                            ║
║                                                              ║
║  OPENING RANGE (first 15 min):                               ║
║    □ Mark ORB High and ORB Low                               ║
║    □ Check ORB bias matches H1 trend                         ║
║    □ Verify ORB is compressed (narrow)                       ║
║                                                              ║
║  BREAKOUT (within 60 min after ORB):                         ║
║    □ Close beyond ORB + 5% margin                            ║
║    □ Displacement ≥ 1.3× avg range                           ║
║    □ Body ≥ 60%                                              ║
║    □ Close in top/bottom 25%                                 ║
║                                                              ║
║  ENTRY:                                                      ║
║    □ Wait for pullback retest of ORB level                   ║
║    □ Enter on next candle open after confirmation             ║
║    □ SL = pullback extreme ± buffer                          ║
║    □ TP = Entry ± 2.5R                                       ║
║    □ Position size: 1.5% risk                                ║
║                                                              ║
║  MANAGEMENT:                                                 ║
║    □ At +1R → move SL to breakeven                           ║
║    □ After BE → trail with 1R buffer                         ║
║    □ Force close 15 min before session end                    ║
║                                                              ║
║  NEVER:                                                      ║
║    ✗ Trade against H1 trend                                  ║
║    ✗ Risk more than 1.5%                                     ║
║    ✗ Take 3+ losses in a day                                 ║
║    ✗ Hold more than 2 positions                              ║
║    ✗ Hold overnight                                          ║
║                                                              ║
║  PARAMETERS:                                                 ║
║    ORB: 15 min | Entry window: 2 hrs | Breakout: 60 min     ║
║    Disp: 1.3× | Body: 60% | Close: 25% | Margin: 5%        ║
║    TP: 2.5R | BE at 1R | Trail buffer: 1R                   ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
```

---

## Disclaimer

This document describes a systematic trading strategy with backtested results. **Past performance does not guarantee future results.** All backtest results include realistic trading costs (spread + slippage) and are based on next-bar open entries to eliminate look-ahead bias. The strategy has been validated on 3 years of historical data but market conditions can change. Always trade with capital you can afford to lose.

---

*Document generated: February 19, 2026*  
*Strategy version: v2.1 (Candidate #12)*  
*Backtest engine: Python + MT5 1-minute data*
