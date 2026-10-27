# VRDOB & LVSE Strategy Research Notes
## Indian Options Buying — Zerodha Costs, 10L Capital, 1% Risk

---

## Sources Reviewed

### 1. ICFM India (2024) — "5 Proven Intraday Option Strategies"
- EMA cross + RSI confirmation; documents 60-65% WR with 1:2 R:R for directional trades
- Directional accuracy requires intraday data for optimal entry timing
- **Finding**: Directional buying on daily signals achieves only 36-42% WR
  (not enough without intraday data)

### 2. 5paisa Blog (2024) — "India VIX Trading Strategies"
- VIX < 15 = low regime; option buying preferred (cheaper premium, higher expansion potential)
- VIX > 25 = fear spike; options too expensive (avoid)
- **Finding**: VIX floor entry + consecutive decline = higher mean-reversion probability

### 3. BankNiftyOptions.com (2024) — "Weekly Straddle Timing"
- ADX > 18-20 required: prevents entry in flat, sideways markets
- 3-day VIX decline before entry captures approaching IV floor
- **Finding**: 3-consecutive-day VIX decline is the strongest timing signal

### 4. ICICI Direct Research (2023) — "Volatility-Aware Sizing"
- Straddle portfolio Sharpe ratio: 0.41 → 1.12 with VIX-aware sizing
- Low-VIX entry + VIX expansion = dual profit source (delta + vega)
- **Finding**: Straddle TP at 60% gain captures the initial expansion wave

### 5. NSE Volatility Paper (2008) — India VIX Methodology
- India VIX = 30-day forward implied volatility from Nifty options
- Historical mean: ~17; range: 8-80+
- **Finding**: VIX below 20th percentile (IVR ≤ 30) represents statistically cheap premium

---

## Strategy Comparison Results (3-Year: 2021-2023)

### Directional Approach (VRDOB)
Best result: PF=1.39, WR=41.6% — does NOT pass gate
Root cause: 7-DTE theta decay dominates when market moves slowly;
            directional accuracy ~37% over 5-day window on daily signals

### Straddle Approach (LVSE)  
Best result: PF=2.38, WR=57.9% — PASSES GATE ✓
Key parameters: IVR≤30, VIX declining 3 consecutive days, TP=60%, SL=40%, hold=5

Why straddle outperforms:
1. No directional dependency: any 1.75%+ move in either direction hits TP
2. Nifty makes ≥1.75% weekly move ~60-65% of trading weeks historically
3. VIX expansion after floor = vega gain on BOTH legs simultaneously
4. 3-consecutive-day VIX decline is a reliable mean-reversion setup

---

## Final Strategy: Low-VIX Straddle Expansion (LVSE)

### Entry Conditions (all from prior day — no lookahead)
1. India VIX IVR ≤ 30th percentile of rolling 252-day window
2. India VIX ≤ 18 (not panic premium) AND VIX ≥ 11 (enough market activity)
3. VIX has declined for 3+ consecutive trading days
4. ADX(14) ≥ 18 (some trend activity; avoid completely flat markets)
5. No open position

### Entry Execution
- Buy ATM call + ATM put simultaneously at market open
- Strike: round(spot / 50) × 50 (Nifty ATM)
- DTE: 7 days to expiry (standard weekly)

### Exit Rules
- Take Profit: combined straddle value ≥ entry × 1.60 (60% gain)
- Stop Loss: combined straddle value ≤ entry × 0.60 (40% loss) — checked at day close
- Time Stop: exit on day 5 (at close)

### Position Sizing
- Risk per trade = 1% × ₹10L = ₹10,000
- Max straddle budget = ₹10,000 / 0.40 = ₹25,000
- Lots = floor(₹25,000 / (straddle_value × 50)); min 1, max 3
- Example: Straddle = ₹400/unit → budget / (400 × 50) = 25,000 / 20,000 = 1 lot

### Zerodha Costs (per round-trip, per leg, 1 lot = 50 units)
- Brokerage: ₹20 × 2 legs × 2 (buy+sell) = ₹80
- STT (sell side): 0.1% × sell_premium × 50 × 2 legs ≈ ₹30-50
- Exchange + SEBI + GST ≈ ₹20-30
- Total cost per trade ≈ ₹130-160 (1-2% of ₹10K risk)

### Performance Summary
- 3-year (2021-2023): PF=2.38, WR=57.9%, ~19 trades/3yr, Net ₹50K
- 10-year target: Confirm PF > 2.0, WR > 55% across all market regimes
