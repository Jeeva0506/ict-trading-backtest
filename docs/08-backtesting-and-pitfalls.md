# 8. Backtesting, Validation & Pitfalls

[← Back to README](../README.md)

This repo has been through many backtest → live cycles. Most "great" backtest results didn't survive. These are the lessons, so you don't have to pay for them twice.

## Honest status of the strategies here

| Strategy | Status |
|---|---|
| Forex ORB (`StrategyICTClean`, M1 / ORB15 with retest entry) | The edge in early backtests was a **lookahead artifact** (see below). With the bias removed it was negative over 15 years. Live results agreed with the corrected backtest, not the original. |
| M15 first-hour range breakout (`orb15_trader`, not included) | Positive after causality checks, but it's a generic "first-hour volatility" effect, not a special opening-range effect |
| Indian ORB Sweep / straddle | Only validated by backtest + paper trading, and option prices in the backtest are **synthetic**. Unproven with real money. |

**Treat everything as a starting point for your own research, not as a money machine.**

## Pitfall 1: Lookahead bias (the big one)

The original ORB entry logic scanned **up to 20 future bars** for a retest. If no retest came, it "entered" back at the breakout bar, a price that was already in the past by then. In a backtest that's free information. Live, it's impossible.

You can spot it:
- The live bot misses most of the signals the backtest takes (here, 57–59% of signals died at the price-drift guard).
- Live entries are much worse than the backtest's (live drift was 0.44R vs 0.03R in the backtest).

**Causality test (do this for every strategy):** run the signal function on data **truncated at each bar**, and check that the signals it produces on the truncated data match the ones from the full dataset. If adding future bars changes a past signal, you have lookahead.

```python
full = set(s.entry_time for s in strat.generate_signals(day_df))
for i in range(len(day_df)):
    part = strat.generate_signals(day_df.iloc[:i + 1])
    for s in part:
        assert s.entry_time in full, f"signal at {s.entry_time} depends on future data"
```

## Pitfall 2: Fill realism
- Fill at the **next bar's open** after the signal bar closes, not at the signal close.
- If SL and TP are both inside one bar, assume **SL first**.
- Model spread on entry and exit, plus slippage on stop exits. Then rerun at **3× the slippage**. An edge that disappears there isn't real.
- For options, synthetic Black-Scholes premiums ignore the bid-ask spread and IV skew, so expect real fills to be worse.

## Pitfall 3: Overfitting
- 7 different "improvement" levers were tested on the Forex ORB, and **every one of them looked better in-sample and failed out of sample**.
- Rule: tune on one period, **freeze the parameters**, and test on a later period you've never looked at. Report both.
- Check **per-year** results. "Profitable overall" with most of the profit in one year (e.g. a 2019-only edge) isn't an edge.
- Run a **random-direction** or **inverted-signal** control. If flipping the direction is also profitable, your filter is picking volatility, not direction.

## Pitfall 4: Silent parameter fallbacks
One comparison script silently used old default parameters when a key was missing, and its "PF 1.09" result justified a live config change. Make your config loaders **fail loudly** on missing keys, like `_req()` in `mt5_connector.py`.

## Pitfall 5: Two config copies
See [doc 1](01-overview-and-structure.md). Backtests and live can load different `config_fundednext.py` files with different risk values. Run `config_guard.py`'s checks.

## Pitfall 6: Trusting the per-trade journal
When trades overlap, per-trade P&L worked out from balance changes gets misattributed. Use **account-level** totals and broker statements.

## Validation checklist before going live

- [ ] Causality / truncation test passes
- [ ] Next-bar fills, SL-first on ambiguous bars, spread + slippage modelled
- [ ] Still positive at 3× slippage
- [ ] Parameters frozen, then out-of-sample test passes
- [ ] No single year carries the result; most years positive
- [ ] Inverted / random-direction control is clearly worse
- [ ] Max consecutive losses × risk% fits inside your daily and max DD limits
- [ ] At least 1–3 months of demo / paper trading, with results compared trade by trade against the backtest over the same period
- [ ] Start live at the **smallest** size
