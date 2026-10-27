"""
Full adaptive-risk backtest using real Zerodha data (Nifty + BankNifty).

Prerequisites:
  1. python data/zerodha_auth.py          <- authenticate (once per day)
  2. python data/zerodha_data.py          <- download / cache all data
  3. python run_zerodha_backtest.py       <- run this

Strategy: Adaptive-risk ORB
  A+ signal (body_strength + vix_percentile + atr): 2% risk = Rs20,000
  Regular signal (baseline fvg+sweep+bar_quality+ema_slope): 1% risk = Rs10,000
  Exit: Hard SL = 30 spot-pts × |delta| | BEP slide at +25% | Trail 20% below peak from +50% | No fixed TP
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np

from data.zerodha_data import (
    load_zerodha_1m, load_zerodha_daily, load_zerodha_vix,
    CACHE_NIFTY, CACHE_FINNIFTY, CACHE_NIFTY_DAY, CACHE_VIX_DAY,
)
from data.download_intraday import resample_from_1m
from data.download_data import load_all
from strategies.orb_structural_strategy import generate_structural_signals
from strategies.orb_enhanced_strategy import generate_enhanced_signals, ENHANCED_PARAMS
from backtest.option_pricer import (bs_price, atm_strike, price_option,
                                    next_expiry, dte_to_expiry, skew_adjusted_iv,
                                    select_strike, is_expiry_day,
                                    next_expiry_fn, is_expiry_day_fn)
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics

CAPITAL = 1_000_000

# ── Filter sets ───────────────────────────────────────────────────────────────
BASELINE_FILTERS = {"fvg", "sweep", "bar_quality", "ema_slope"}
APLUS_EXTRA      = {"body_strength", "vix_percentile", "atr"}
APLUS_FILTERS    = BASELINE_FILTERS | APLUS_EXTRA

# ── Risk tiers ────────────────────────────────────────────────────────────────
RISK_APLUS   = 20_000   # 2%
RISK_REGULAR = 10_000   # 1%
LOTS_MAX_APLUS   = 15
LOTS_MAX_REGULAR = 8

# ── Exit parameters ───────────────────────────────────────────────────────────
FLAT_SL_RS     = 30         # flat option SL in Rs per unit (30 spot-pts = 30 Rs/unit)

# ── VIX regime gates ──────────────────────────────────────────────────────────
VIX_BUY_MIN    = 12.0       # skip ORB buying when VIX < this (premium too cheap)
VIX_SELL_MIN   = 13.0       # short straddle lower VIX bound
VIX_SELL_MAX   = 20.0       # short straddle upper VIX bound (above = gamma risk too high)

# ── Short straddle parameters ─────────────────────────────────────────────────
SS_TARGET_PCT  = 0.50       # close short straddle when premium decays to 50 %
SS_STOP_PCT    = 0.50       # cut loss when straddle expands by 50 % above entry
SS_ENTRY_HOUR  = 9
SS_ENTRY_MIN   = 30         # sell straddle at 9:30 am bar
SS_RISK        = 10_000     # Rs 10 k risk per short-straddle trade (1 %)
SS_LOTS_MAX    = 4
BEP_ACTIVATE   = 0.25   # slide SL to BEP when premium up 25%
BEP_LOCK       = 0.01   # BEP = entry * (1 + BEP_LOCK)
TRAIL_ACTIVATE = 0.50   # start trailing at +50%
TRAIL_PCT      = 0.20   # trail 20% below running peak

# ── Retest entry (wait for ORB pullback before entering) ─────────────────────
RETEST_ENTRY     = True   # True = skip breakout bar, wait for price to retest ORB
MAX_RETEST_BARS  = 12     # max 5m bars to watch for retest (60 min window)
RETEST_TOLERANCE = 10     # pts: bar low/high must come within N pts of ORB level

# ── Monthly drawdown circuit breaker ─────────────────────────────────────────
MONTHLY_DD_LIMIT   = -0.05   # pause month if running loss > 5% of capital (Rs 50k)
WEEKLY_TRADE_LIMIT = None    # max trades per week COMBINED across all strategies (None = off)
RUN_SHORT_STRADDLE = False   # short straddle is disabled — BS pricer can't model intraday theta
RUN_ABLATION       = True    # run filter ablation study (adds ~3 min to total run time)

# ── Option selling parameters ─────────────────────────────────────────────────
# Strategy: sell the OPPOSITE ATM option on each ORB signal
#   call signal → sell ATM put  (bullish: collect put premium, profit if spot stays up)
#   put  signal → sell ATM call (bearish: collect call premium, profit if spot stays down)
#
# Slippage model (tiered by speed of adverse move):
#   ≤2 bars to SL : very fast / gap move → 30% extra above SL trigger
#   3-5 bars      : fast move            → 20% extra
#   >5 bars       : slow grind           → 10% extra
SELL_ENTRY_SLIP = 0.02   # entry: sell at 98% of BS mid (bid vs mid spread)
SELL_SL_SLIP_FAST = 0.30 # SL exit slippage: ≤2 bars to trigger (gap / very fast move)
SELL_SL_SLIP_MED  = 0.20 # SL exit slippage: 3-5 bars to trigger
SELL_SL_SLIP_SLOW = 0.10 # SL exit slippage: >5 bars to trigger (slow grind)
SELL_TP_FRAC    = 0.25   # take profit: close when option worth ≤25% of entry (lock 75% premium)

# ── IS / OOS dates ────────────────────────────────────────────────────────────
IS_END    = "2018-12-31"
OOS_START = "2019-01-01"
OOS_END   = "2024-03-27"

# ── Instrument configs ────────────────────────────────────────────────────────
INSTRUMENTS = {
    "nifty": {
        "lot_size"   : 50,
        "strike_step": 50,
        "rfr"        : 0.065,
        "entry_dte"  : 7,
        "risk_per_trade": None,   # set dynamically
        "max_lots"   : None,
    },
    "finnifty": {
        "lot_size"   : 60,
        "strike_step": 50,
        "rfr"        : 0.065,
        "entry_dte"  : 7,
        "risk_per_trade": None,
        "max_lots"   : None,
    },
}


# ── Signal classifiers ───────────────────────────────────────────────────────
def classify_signals(df_5m: pd.DataFrame, df_daily: pd.DataFrame,
                     df_vix: pd.DataFrame) -> pd.DataFrame:
    """
    Returns baseline signals with a 'quality' column: 'A+' or 'regular'.
    A+ = date also fires in the stricter APLUS_FILTERS generator.

    Bug fix: must filter to rows where signal != None before extracting dates,
    otherwise every calendar date appears in both sets and everything is A+.
    """
    base_sigs  = generate_structural_signals(df_5m, df_daily, df_vix,
                                              active_filters=BASELINE_FILTERS)
    aplus_sigs = generate_structural_signals(df_5m, df_daily, df_vix,
                                              active_filters=APLUS_FILTERS)

    # Only dates where the A+ generator fired an actual signal (call or put)
    aplus_signal_dates = set(
        aplus_sigs[aplus_sigs["signal"].notna()].index.normalize()
    )

    base_sigs = base_sigs.copy()
    # Default quality to None; only assign for actual signal rows
    base_sigs["quality"] = None
    signal_mask = base_sigs["signal"].notna()
    base_sigs.loc[signal_mask, "quality"] = base_sigs.loc[signal_mask].index.normalize().map(
        lambda d: "A+" if d in aplus_signal_dates else "regular"
    )
    return base_sigs


def classify_signals_finnifty(df_5m: pd.DataFrame, df_daily: pd.DataFrame,
                               df_vix: pd.DataFrame,
                               nifty_signals_by_date: dict = None) -> pd.DataFrame:
    """
    FinNifty uses Baseline 4 filters only (ablation-proven; extra 3 filters hurt PF).

    A+ (2% risk) = FinNifty AND Nifty both fire the SAME direction on the same day.
                   Both indices aligned -> correlated trend day -> higher conviction.
    Regular (1%) = FinNifty signal but Nifty did not signal same direction.

    nifty_signals_by_date : {date: "call"/"put"} — pass from classify_signals output.
                             If None, all signals default to 'regular'.
    """
    sigs = generate_structural_signals(df_5m, df_daily, df_vix,
                                       active_filters=BASELINE_FILTERS)
    sigs = sigs.copy()
    signal_rows = sigs["signal"].isin(["call", "put"])
    sigs["quality"] = None

    if nifty_signals_by_date:
        # Map each bar's date to the Nifty signal direction on that day (or None)
        nifty_dir = pd.Series(
            [nifty_signals_by_date.get(d) for d in sigs.index.date],
            index=sigs.index,
        )
        # A+ where FinNifty direction matches Nifty direction
        aligned = signal_rows & (sigs["signal"] == nifty_dir)
        sigs.loc[aligned,  "quality"] = "A+"
        sigs.loc[signal_rows & ~aligned, "quality"] = "regular"
    else:
        sigs.loc[signal_rows, "quality"] = "regular"

    return sigs


# ── 3-phase exit backtest engine ─────────────────────────────────────────────
def _run_adaptive(df_5m: pd.DataFrame, df_daily: pd.DataFrame,
                  df_vix: pd.DataFrame, sigs: pd.DataFrame,
                  inst_cfg: dict, start: str = None, end: str = None,
                  itm_strikes: int = 0,
                  next_exp_fn=None, is_exp_fn=None,
                  weekly_counts: dict = None) -> list:
    """
    itm_strikes  : 0 = ATM, 1 = 1 strike ITM
    next_exp_fn  : expiry date calculator (default: Nifty Thursday calendar)
    is_exp_fn    : expiry-day checker (default: Nifty Thursday calendar)

    Damage-control features (controlled by module constants):
      RETEST_ENTRY     : skip breakout bar, wait for price to pull back to ORB
                         level and bounce — improves WR, reduces false breakouts.
      MONTHLY_DD_LIMIT : pause new entries for rest of month when cumulative
                         monthly loss exceeds MONTHLY_DD_LIMIT * CAPITAL.
    """
    next_exp_fn = next_exp_fn or next_expiry
    is_exp_fn   = is_exp_fn   or is_expiry_day
    cfg = inst_cfg
    if start:
        sigs = sigs[sigs.index >= pd.Timestamp(start)]
    if end:
        sigs = sigs[sigs.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    if sigs.empty:
        return []

    df = df_5m.loc[sigs.index].copy()
    df["signal"]   = sigs["signal"]
    df["vix_val"]  = sigs["vix_val"]
    df["ep_close"] = sigs["entry_price"]
    df["quality"]  = sigs["quality"]
    df["orb_h"]    = sigs["orb_h"]
    df["orb_l"]    = sigs["orb_l"]

    trades       = []
    active_date  = None
    monthly_pnl  = {}    # (year, month) -> cumulative net P&L this calendar month
    month_paused = set() # months where monthly DD limit was hit

    i = 0
    while i < len(df):
        row      = df.iloc[i]
        bar_date = df.index[i].date()

        # Skip rest of day once a trade has been taken (one trade per day)
        if bar_date == active_date:
            i += 1; continue

        # Skip non-signal / missing VIX bars
        if row["signal"] not in ("call", "put") or pd.isna(row["vix_val"]):
            i += 1; continue

        # VIX gate: skip when options are too cheap to have edge
        if row["vix_val"] < VIX_BUY_MIN:
            i += 1; continue

        # Monthly circuit breaker: pause new entries if month is down >5%
        month_key = (bar_date.year, bar_date.month)
        if month_key in month_paused:
            active_date = bar_date   # mark day used to fast-skip remaining bars
            i += 1; continue

        # Weekly trade limit: shared across strategies via mutable weekly_counts dict
        iso      = bar_date.isocalendar()
        week_key = (iso[0], iso[1])   # (ISO year, ISO week number)
        if (weekly_counts is not None and WEEKLY_TRADE_LIMIT is not None and
                weekly_counts.get(week_key, 0) >= WEEKLY_TRADE_LIMIT):
            active_date = bar_date
            i += 1; continue

        # No new entries after 13:00 on expiry day (pin-risk avoidance)
        bar_time = df.index[i].time()
        if is_exp_fn(bar_date) and (bar_time.hour > 13 or
                                    (bar_time.hour == 13 and bar_time.minute > 0)):
            i += 1; continue

        quality  = row["quality"]
        risk_amt = RISK_APLUS    if quality == "A+" else RISK_REGULAR
        max_lots = LOTS_MAX_APLUS if quality == "A+" else LOTS_MAX_REGULAR

        opt_type = row["signal"]
        vix      = row["vix_val"]
        lot_size = cfg["lot_size"]
        step     = cfg["strike_step"]

        # ── Entry price: breakout bar close OR retest bounce ───────────────
        if RETEST_ENTRY:
            # Wait for price to pull back to the ORB level, then enter on bounce.
            # Call: wait for Low to touch within RETEST_TOLERANCE pts of ORB_H,
            #       then enter only if bar closes ABOVE ORB_H (support held).
            # Put:  wait for High to touch within RETEST_TOLERANCE pts of ORB_L,
            #       then enter only if bar closes BELOW ORB_L (resistance held).
            orb_level  = row["orb_h"] if opt_type == "call" else row["orb_l"]
            entry_spot = None
            next_i     = i + 1   # default advance when no retest found

            max_j = min(i + 1 + MAX_RETEST_BARS, len(df))
            for j in range(i + 1, max_j):
                if df.index[j].date() != bar_date:
                    break
                t_j = df.index[j].time()
                if t_j.hour > 15 or (t_j.hour == 15 and t_j.minute >= 5):
                    break
                nrow_j = df.iloc[j]

                if opt_type == "call":
                    if nrow_j["Low"] <= orb_level + RETEST_TOLERANCE:
                        if nrow_j["Close"] > orb_level:   # bounced off support
                            entry_spot = nrow_j["Close"]
                            next_i = j + 1
                        break   # touched ORB — either bounced or broke, stop searching
                else:
                    if nrow_j["High"] >= orb_level - RETEST_TOLERANCE:
                        if nrow_j["Close"] < orb_level:   # rejected at resistance
                            entry_spot = nrow_j["Close"]
                            next_i = j + 1
                        break

            if entry_spot is None:
                active_date = bar_date   # signal seen but no clean retest
                i += 1
                continue

            i = next_i   # start tracking from bar after retest bar

        else:
            entry_spot = row["ep_close"]   # enter on breakout bar close
            i += 1

        # ── Price the option at entry ──────────────────────────────────────
        K   = select_strike(entry_spot, opt_type, step, itm_strikes)
        dte = (next_exp_fn(bar_date) - bar_date).days
        T   = dte / 365.0
        iv  = skew_adjusted_iv(vix, opt_type, itm_strikes) / 100.0
        ep  = bs_price(entry_spot, K, T, cfg["rfr"], iv, opt_type)

        if ep < 1.0:
            active_date = bar_date
            continue   # i already advanced correctly above

        # ── Flat SL sizing ─────────────────────────────────────────────────
        option_sl_pts = max(FLAT_SL_RS, ep * 0.05)
        isl           = ep - option_sl_pts
        lots          = max(1, min(int(risk_amt / (option_sl_pts * lot_size)), max_lots))
        qty           = lots * lot_size
        bep_trig   = ep * (1 + BEP_ACTIVATE)
        bep_sl     = ep * (1 + BEP_LOCK)
        trail_trig = ep * (1 + TRAIL_ACTIVATE)

        peak         = ep
        bep_active   = False
        trail_active = False
        active_date  = bar_date
        exit_prem    = None
        exit_str     = None

        # ── Bar-by-bar tracking loop ───────────────────────────────────────
        while i < len(df):
            nrow = df.iloc[i]
            nts  = df.index[i]
            if nts.date() != bar_date:
                break
            t = nts.time()
            if t.hour > 15 or (t.hour == 15 and t.minute >= 5):
                exit_prem = bs_price(nrow["Close"], K, T, cfg["rfr"], iv, opt_type)
                exit_str  = "3:05"
                i += 1; break

            if opt_type == "call":
                opt_h = bs_price(nrow["High"], K, T, cfg["rfr"], iv, "call")
                opt_l = bs_price(nrow["Low"],  K, T, cfg["rfr"], iv, "call")
            else:
                opt_h = bs_price(nrow["Low"],  K, T, cfg["rfr"], iv, "put")
                opt_l = bs_price(nrow["High"], K, T, cfg["rfr"], iv, "put")

            if opt_h > peak:
                peak = opt_h
            if peak >= bep_trig:
                bep_active = True
            if peak >= trail_trig:
                trail_active = True

            if trail_active:
                eff_sl = max(bep_sl if bep_active else isl, peak * (1 - TRAIL_PCT))
            elif bep_active:
                eff_sl = bep_sl
            else:
                eff_sl = isl

            if opt_l <= eff_sl:
                exit_prem = eff_sl
                if trail_active and eff_sl > isl:
                    exit_str = "Trail SL"
                elif bep_active:
                    exit_str = "BEP"
                else:
                    exit_str = "SL"
                i += 1; break
            i += 1

        if exit_str is None:
            last = df[df.index.date == bar_date].iloc[-1]
            exit_prem = bs_price(last["Close"], K, T, cfg["rfr"], iv, opt_type)
            exit_str  = "3:05"

        sell    = max(exit_prem, 0.05)
        gross   = (sell - ep) * qty
        cost    = round_trip_cost(ep, sell, qty)
        pnl_net = round(gross - cost, 2)

        # Update monthly circuit breaker
        monthly_pnl[month_key] = monthly_pnl.get(month_key, 0) + pnl_net
        if monthly_pnl[month_key] < CAPITAL * MONTHLY_DD_LIMIT:
            month_paused.add(month_key)

        # Update weekly trade counter (shared across strategies)
        if weekly_counts is not None:
            weekly_counts[week_key] = weekly_counts.get(week_key, 0) + 1

        trades.append({
            "entry_date" : str(bar_date),
            "exit_date"  : str(bar_date),
            "direction"  : opt_type,
            "quality"    : quality,
            "strike"     : K,
            "dte"        : dte,
            "iv_used"    : round(iv * 100, 2),
            "entry_prem" : round(ep, 2),
            "exit_prem"  : round(sell, 2),
            "peak_prem"  : round(peak, 2),
            "lots"       : lots,
            "qty"        : qty,
            "exit_type"  : exit_str,
            "pnl_net"    : pnl_net,
            "vix"        : round(vix, 2),
            "option_sl"  : round(option_sl_pts, 2),
        })

    return trades


# ── Option selling backtest engine ────────────────────────────────────────────
def _run_sell_orb(df_5m: pd.DataFrame, df_daily: pd.DataFrame,
                  df_vix: pd.DataFrame, sigs: pd.DataFrame,
                  inst_cfg: dict, start: str = None, end: str = None,
                  itm_strikes: int = 0,
                  next_exp_fn=None, is_exp_fn=None,
                  weekly_counts: dict = None,
                  sl_slip_fast: float = None,
                  sl_slip_med:  float = None,
                  sl_slip_slow: float = None) -> list:
    """
    Sell the OPPOSITE ATM option on each ORB signal:
      call signal → sell ATM put  (bullish; collected put premium decays if spot holds)
      put  signal → sell ATM call (bearish; collected call premium decays if spot falls)

    Entry  : sell at SELL_ENTRY_SLIP below BS mid (98% of mid-market price).
    SL     : when option value rises by FLAT_SL_RS above entry → buy back.
             Actual fill = sl_trigger × (1 + slip), slip tiered by speed:
               ≤2 bars → SELL_SL_SLIP_FAST | 3-5 bars → MED | >5 → SLOW
    TP     : when option decays to SELL_TP_FRAC of entry (lock 75% premium).
    EOD    : close at 3:05 pm at current BS price.
    """
    _slip_fast = sl_slip_fast if sl_slip_fast is not None else SELL_SL_SLIP_FAST
    _slip_med  = sl_slip_med  if sl_slip_med  is not None else SELL_SL_SLIP_MED
    _slip_slow = sl_slip_slow if sl_slip_slow is not None else SELL_SL_SLIP_SLOW

    next_exp_fn = next_exp_fn or next_expiry
    is_exp_fn   = is_exp_fn   or is_expiry_day
    cfg = inst_cfg
    if start:
        sigs = sigs[sigs.index >= pd.Timestamp(start)]
    if end:
        sigs = sigs[sigs.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    if sigs.empty:
        return []

    df = df_5m.loc[sigs.index].copy()
    df["signal"]   = sigs["signal"]
    df["vix_val"]  = sigs["vix_val"]
    df["ep_close"] = sigs["entry_price"]
    df["quality"]  = sigs["quality"]
    df["orb_h"]    = sigs["orb_h"]
    df["orb_l"]    = sigs["orb_l"]

    trades       = []
    active_date  = None
    monthly_pnl  = {}
    month_paused = set()

    i = 0
    while i < len(df):
        row      = df.iloc[i]
        bar_date = df.index[i].date()

        if bar_date == active_date:
            i += 1; continue

        if row["signal"] not in ("call", "put") or pd.isna(row["vix_val"]):
            i += 1; continue

        if row["vix_val"] < VIX_BUY_MIN:
            i += 1; continue

        month_key = (bar_date.year, bar_date.month)
        if month_key in month_paused:
            active_date = bar_date
            i += 1; continue

        iso      = bar_date.isocalendar()
        week_key = (iso[0], iso[1])
        if (weekly_counts is not None and WEEKLY_TRADE_LIMIT is not None and
                weekly_counts.get(week_key, 0) >= WEEKLY_TRADE_LIMIT):
            active_date = bar_date
            i += 1; continue

        bar_time = df.index[i].time()
        if is_exp_fn(bar_date) and (bar_time.hour > 13 or
                                    (bar_time.hour == 13 and bar_time.minute > 0)):
            i += 1; continue

        quality  = row["quality"]
        risk_amt = RISK_APLUS    if quality == "A+" else RISK_REGULAR
        max_lots = LOTS_MAX_APLUS if quality == "A+" else LOTS_MAX_REGULAR

        sig_type  = row["signal"]
        sold_type = "put" if sig_type == "call" else "call"   # sell the opposite leg

        vix      = row["vix_val"]
        lot_size = cfg["lot_size"]
        step     = cfg["strike_step"]

        # ── Entry: same retest logic as buying ────────────────────────────────
        if RETEST_ENTRY:
            orb_level  = row["orb_h"] if sig_type == "call" else row["orb_l"]
            entry_spot = None
            next_i     = i + 1

            max_j = min(i + 1 + MAX_RETEST_BARS, len(df))
            for j in range(i + 1, max_j):
                if df.index[j].date() != bar_date:
                    break
                t_j = df.index[j].time()
                if t_j.hour > 15 or (t_j.hour == 15 and t_j.minute >= 5):
                    break
                nrow_j = df.iloc[j]
                if sig_type == "call":
                    if nrow_j["Low"] <= orb_level + RETEST_TOLERANCE:
                        if nrow_j["Close"] > orb_level:
                            entry_spot = nrow_j["Close"]
                            next_i = j + 1
                        break
                else:
                    if nrow_j["High"] >= orb_level - RETEST_TOLERANCE:
                        if nrow_j["Close"] < orb_level:
                            entry_spot = nrow_j["Close"]
                            next_i = j + 1
                        break

            if entry_spot is None:
                active_date = bar_date
                i += 1
                continue
            i = next_i
        else:
            entry_spot = row["ep_close"]
            i += 1

        # ── Price the sold option (always ATM, ATM IV = VIX/100) ─────────────
        K   = atm_strike(entry_spot, step)
        dte = (next_exp_fn(bar_date) - bar_date).days
        T   = dte / 365.0
        iv  = vix / 100.0
        mid          = bs_price(entry_spot, K, T, cfg["rfr"], iv, sold_type)
        ep_collected = mid * (1 - SELL_ENTRY_SLIP)     # sell at 98% of BS mid

        if ep_collected < 1.0:
            active_date = bar_date
            continue

        # ── Sizing: same FLAT_SL_RS risk cap as buying ────────────────────────
        option_sl_pts = max(FLAT_SL_RS, mid * 0.05)
        lots  = max(1, min(int(risk_amt / (option_sl_pts * lot_size)), max_lots))
        qty   = lots * lot_size

        sl_trigger = ep_collected + option_sl_pts   # option value at which we stop out
        tp_trigger = ep_collected * SELL_TP_FRAC    # option value at which we lock profit

        active_date = bar_date
        exit_cost   = None
        exit_str    = None
        bars_in     = 0

        # ── Bar-by-bar tracking ───────────────────────────────────────────────
        while i < len(df):
            nrow = df.iloc[i]
            nts  = df.index[i]
            if nts.date() != bar_date:
                break
            t = nts.time()
            if t.hour > 15 or (t.hour == 15 and t.minute >= 5):
                exit_cost = bs_price(nrow["Close"], K, T, cfg["rfr"], iv, sold_type)
                exit_str  = "3:05"
                i += 1; break

            bars_in += 1

            # Extremes within this bar for the sold option:
            # sold put : worst (most expensive) when spot=Low; best (cheapest) when spot=High
            # sold call: worst (most expensive) when spot=High; best (cheapest) when spot=Low
            if sold_type == "put":
                opt_worst = bs_price(nrow["Low"],  K, T, cfg["rfr"], iv, "put")
                opt_best  = bs_price(nrow["High"], K, T, cfg["rfr"], iv, "put")
            else:
                opt_worst = bs_price(nrow["High"], K, T, cfg["rfr"], iv, "call")
                opt_best  = bs_price(nrow["Low"],  K, T, cfg["rfr"], iv, "call")

            # SL: option rose to sl_trigger — apply tiered slippage on buy-back fill
            if opt_worst >= sl_trigger:
                slip = (_slip_fast if bars_in <= 2 else
                        _slip_med  if bars_in <= 5 else
                        _slip_slow)
                exit_cost = sl_trigger * (1 + slip)
                exit_str  = "SL"
                i += 1; break

            # TP: option decayed to tp_trigger — buy back cheaply
            if opt_best <= tp_trigger:
                exit_cost = tp_trigger
                exit_str  = "TP"
                i += 1; break

            i += 1

        if exit_str is None:
            last = df[df.index.date == bar_date].iloc[-1]
            exit_cost = bs_price(last["Close"], K, T, cfg["rfr"], iv, sold_type)
            exit_str  = "3:05"

        # We SOLD at ep_collected and BOUGHT BACK at exit_cost
        gross   = (ep_collected - exit_cost) * qty
        cost    = round_trip_cost(exit_cost, ep_collected, qty)   # buy=exit, sell=entry
        pnl_net = round(gross - cost, 2)

        monthly_pnl[month_key] = monthly_pnl.get(month_key, 0) + pnl_net
        if monthly_pnl[month_key] < CAPITAL * MONTHLY_DD_LIMIT:
            month_paused.add(month_key)

        if weekly_counts is not None:
            weekly_counts[week_key] = weekly_counts.get(week_key, 0) + 1

        trades.append({
            "entry_date" : str(bar_date),
            "exit_date"  : str(bar_date),
            "direction"  : sig_type,
            "sold_type"  : sold_type,
            "quality"    : quality,
            "strike"     : K,
            "dte"        : dte,
            "iv_used"    : round(iv * 100, 2),
            "entry_prem" : round(ep_collected, 2),
            "exit_prem"  : round(exit_cost, 2),
            "lots"       : lots,
            "qty"        : qty,
            "exit_type"  : exit_str,
            "bars_in"    : bars_in,
            "pnl_net"    : pnl_net,
            "vix"        : round(vix, 2),
            "option_sl"  : round(option_sl_pts, 2),
        })

    return trades


# ── Short straddle on non-signal days ─────────────────────────────────────────
def _run_short_straddle(df_5m: pd.DataFrame, df_vix: pd.DataFrame,
                        signal_dates: set, inst_cfg: dict,
                        start: str = None, end: str = None) -> list:
    """
    Sell ATM straddle at 9:30 am on days when ORB fires NO signal,
    provided VIX is in the sweet-spot (VIX_SELL_MIN … VIX_SELL_MAX).

    Exit rules:
      TP  : straddle decays to 50 % of entry (premium captured)
      SL  : straddle expands to 150 % of entry (buyer-side reversal)
      EOD : close at 3 pm regardless
    """
    cfg = inst_cfg
    lot_size = cfg["lot_size"]
    step     = cfg["strike_step"]
    rfr      = cfg["rfr"]

    df = df_5m.copy()
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end) + pd.Timedelta(days=1)]

    # Build a quick VIX lookup: forward-fill to daily
    vix_daily = df_vix["Close"].resample("D").last().ffill()

    trades = []

    for day, day_df in df.groupby(df.index.date):
        if day in signal_dates:          # ORB was traded today — skip
            continue

        # VIX gate
        day_ts  = pd.Timestamp(day)
        day_vix = vix_daily.get(day_ts)
        if day_vix is None or pd.isna(day_vix):
            continue
        if not (VIX_SELL_MIN <= day_vix <= VIX_SELL_MAX):
            continue

        # Find 9:30 am entry bar
        entry_bars = day_df[(day_df.index.hour == SS_ENTRY_HOUR) &
                            (day_df.index.minute == SS_ENTRY_MIN)]
        if entry_bars.empty:
            continue

        entry_idx  = day_df.index.get_loc(entry_bars.index[0])
        entry_spot = entry_bars.iloc[0]["Close"]

        dte = (next_expiry(day) - day).days
        if dte < 2:
            continue
        T  = dte / 365.0
        K  = atm_strike(entry_spot, step)
        iv = day_vix / 100.0

        ep_call = bs_price(entry_spot, K, T, rfr, iv, "call")
        ep_put  = bs_price(entry_spot, K, T, rfr, iv, "put")
        ep      = ep_call + ep_put         # total premium collected

        if ep < 50:                        # straddle too cheap to bother
            continue

        # Risk sizing: stop = straddle expands SS_STOP_PCT above entry
        sl_per_unit = ep * SS_STOP_PCT
        lots = max(1, min(int(SS_RISK / (sl_per_unit * lot_size)), SS_LOTS_MAX))
        qty  = lots * lot_size

        target_val = ep * (1 - SS_TARGET_PCT)   # buy back at 50 % of entry
        stop_val   = ep * (1 + SS_STOP_PCT)     # exit loss at 150 % of entry

        exit_straddle = None
        exit_str      = None

        for j in range(entry_idx + 1, len(day_df)):
            nrow = day_df.iloc[j]
            t    = day_df.index[j].time()
            H, L, C = nrow["High"], nrow["Low"], nrow["Close"]

            # EOD
            if t.hour >= 15:
                exit_straddle = (bs_price(C, K, T, rfr, iv, "call") +
                                 bs_price(C, K, T, rfr, iv, "put"))
                exit_str = "EOD"
                break

            sv_H = bs_price(H, K, T, rfr, iv, "call") + bs_price(H, K, T, rfr, iv, "put")
            sv_L = bs_price(L, K, T, rfr, iv, "call") + bs_price(L, K, T, rfr, iv, "put")
            # Straddle minimum is at K when K is within bar range
            sv_min = min(sv_H, sv_L,
                         bs_price(K, K, T, rfr, iv, "call") +
                         bs_price(K, K, T, rfr, iv, "put")) if L <= K <= H \
                     else min(sv_H, sv_L)
            sv_max = max(sv_H, sv_L)

            if sv_max >= stop_val:         # straddle blew up
                exit_straddle = stop_val
                exit_str = "SL"
                break
            if sv_min <= target_val:       # decay target hit
                exit_straddle = target_val
                exit_str = "TP"
                break

        if exit_straddle is None:
            last = day_df.iloc[-1]
            exit_straddle = (bs_price(last["Close"], K, T, rfr, iv, "call") +
                             bs_price(last["Close"], K, T, rfr, iv, "put"))
            exit_str = "EOD"

        # Short straddle P&L: positive when straddle falls (seller wins)
        gross = (ep - exit_straddle) * qty
        # Cost: sell both legs at entry, buy back at exit → 2 legs
        cost  = round_trip_cost(exit_straddle / 2, ep / 2, qty) * 2

        trades.append({
            "entry_date" : str(day),
            "exit_date"  : str(day),
            "direction"  : "short_straddle",
            "quality"    : "regular",
            "strike"     : K,
            "dte"        : dte,
            "iv_used"    : round(iv * 100, 2),
            "entry_prem" : round(ep, 2),
            "exit_prem"  : round(exit_straddle, 2),
            "peak_prem"  : round(ep, 2),
            "lots"       : lots,
            "qty"        : qty,
            "exit_type"  : exit_str,
            "pnl_net"    : round(gross - cost, 2),
            "vix"        : round(day_vix, 2),
            "option_sl"  : round(sl_per_unit, 2),
        })

    return trades


# ── Reporting helpers ─────────────────────────────────────────────────────────
def _metrics(trades, label, yrs=None):
    if not trades:
        print(f"  {label:<50} no trades")
        return
    m  = compute_metrics(trades, CAPITAL)
    if "error" in m:
        print(f"  {label:<50} error"); return
    eb  = m.get("exit_breakdown", {})
    n   = len(trades)
    if yrs is None:
        dates = [t["entry_date"] for t in trades]
        yr_min = int(min(dates)[:4]); yr_max = int(max(dates)[:4])
        yrs = max(yr_max - yr_min + 1, 1)
    print(f"  {label:<50} n={n:>5} ({n/yrs:.0f}/yr)  "
          f"WR={m['win_rate_pct']:>5.1f}%  PF={m['profit_factor']:>5.3f}  "
          f"Net=Rs{m['net_pnl']:>10,.0f} ({m['net_pnl']/CAPITAL/yrs*100:.1f}%/yr)  "
          f"SL={eb.get('SL',0):>3} BEP={eb.get('BEP',0):>3} "
          f"TSL={eb.get('Trail SL',0):>3} EOD={eb.get('3:05',0):>3}")


def _yearly(trades, label):
    if not trades:
        return
    df = pd.DataFrame(trades)
    df["year"] = pd.to_datetime(df["entry_date"]).dt.year
    df_ap  = df[df["quality"] == "A+"]
    df_reg = df[df["quality"] == "regular"]

    print(f"\n  {label}")
    print(f"  {'Year':>5}  {'n':>4}  {'A+':>4}  {'Reg':>4}  "
          f"{'WR':>6}  {'PF':>6}  {'A+ P&L':>12}  {'Reg P&L':>12}  "
          f"{'Total P&L':>12}  {'Return%':>8}  Gate")
    print("  " + "-"*110)

    for yr in sorted(df["year"].unique()):
        yt  = df[df["year"] == yr]
        yap = df_ap[df_ap["year"] == yr]
        yrg = df_reg[df_reg["year"] == yr]
        n   = len(yt)
        wr  = (yt["pnl_net"] > 0).mean() * 100
        pf_t = yt[yt["pnl_net"] > 0]["pnl_net"].sum()
        pf_b = abs(yt[yt["pnl_net"] < 0]["pnl_net"].sum())
        pf   = pf_t / pf_b if pf_b > 0 else float("inf")
        net_ap  = yap["pnl_net"].sum()
        net_reg = yrg["pnl_net"].sum()
        net_tot = yt["pnl_net"].sum()
        ret     = net_tot / CAPITAL * 100
        gates   = []
        if n >= 100: gates.append("100t")
        if ret >= 50: gates.append("50%")
        gate_str = " ".join(gates) if gates else ""
        print(f"  {yr:>5}  {n:>4}  {len(yap):>4}  {len(yrg):>4}  "
              f"{wr:>5.1f}%  {pf:>5.3f}  "
              f"Rs{net_ap:>10,.0f}  Rs{net_reg:>10,.0f}  "
              f"Rs{net_tot:>10,.0f}  {ret:>7.1f}%   {gate_str}")

    # averages
    avg_n   = len(df) / df["year"].nunique()
    avg_ret = df.groupby("year")["pnl_net"].sum().mean() / CAPITAL * 100
    print(f"  {'AVG':>5}  {avg_n:>4.0f}                                              "
          f"         Rs{df.groupby('year')['pnl_net'].sum().mean():>10,.0f}  "
          f"{avg_ret:>7.1f}%")
    yr_pass_100 = sum(1 for y in df["year"].unique()
                      if len(df[df["year"]==y]) >= 100)
    yr_pass_50  = sum(1 for y in df["year"].unique()
                      if df[df["year"]==y]["pnl_net"].sum()/CAPITAL*100 >= 50)
    print(f"\n  Years with 100+ trades : {yr_pass_100}/{df['year'].nunique()}")
    print(f"  Years with 50%+ return : {yr_pass_50}/{df['year'].nunique()}")


def _yearly_total(trades_list: list, label: str):
    """Simple yearly breakdown across multiple trade lists (no A+/Reg split)."""
    all_trades = [t for tl in trades_list for t in tl]
    if not all_trades:
        return
    df = pd.DataFrame(all_trades)
    df["year"] = pd.to_datetime(df["entry_date"]).dt.year
    print(f"\n  {label}")
    print(f"  {'Year':>5}  {'n':>5}  {'WR':>6}  {'PF':>6}  {'Net P&L':>13}  {'Return%':>8}  Gate")
    print("  " + "-"*72)
    for yr in sorted(df["year"].unique()):
        yt  = df[df["year"] == yr]
        n   = len(yt)
        wr  = (yt["pnl_net"] > 0).mean() * 100
        w   = yt[yt["pnl_net"] > 0]["pnl_net"].sum()
        l   = abs(yt[yt["pnl_net"] < 0]["pnl_net"].sum())
        pf  = w / l if l > 0 else float("inf")
        net = yt["pnl_net"].sum()
        ret = net / CAPITAL * 100
        gates = []
        if n >= 100: gates.append("100t")
        if ret >= 50: gates.append("50%")
        print(f"  {yr:>5}  {n:>5}  {wr:>5.1f}%  {pf:>5.3f}  Rs{net:>11,.0f}  {ret:>7.1f}%   {' '.join(gates)}")
    avg_net = df.groupby("year")["pnl_net"].sum().mean()
    avg_n   = df.groupby("year").size().mean()
    avg_ret = avg_net / CAPITAL * 100
    yr100 = sum(1 for y in df["year"].unique() if len(df[df["year"]==y]) >= 100)
    yr50  = sum(1 for y in df["year"].unique()
                if df[df["year"]==y]["pnl_net"].sum()/CAPITAL*100 >= 50)
    print(f"  {'AVG':>5}  {avg_n:>5.0f}              Rs{avg_net:>11,.0f}  {avg_ret:>7.1f}%")
    print(f"\n  Years >=100 trades: {yr100}/{df['year'].nunique()}  |  "
          f"Years >=50% return: {yr50}/{df['year'].nunique()}")


def _yearly_buy_vs_sell(buy_lists: list, sell_lists: list, label: str):
    """Year-by-year P&L comparison: buying vs selling the same ORB signals."""
    buy_all  = pd.DataFrame([t for tl in buy_lists  for t in tl])
    sell_all = pd.DataFrame([t for tl in sell_lists for t in tl])
    if buy_all.empty and sell_all.empty:
        return

    buy_all["year"]  = pd.to_datetime(buy_all["entry_date"]).dt.year
    sell_all["year"] = pd.to_datetime(sell_all["entry_date"]).dt.year
    all_years = sorted(set(buy_all["year"].unique()) | set(sell_all["year"].unique()))

    print(f"\n  {label}")
    print(f"  {'Year':>5}  {'Buy n':>5}  {'BuyWR':>6}  {'Buy P&L':>13}  "
          f"{'Sell n':>6}  {'SelWR':>6}  {'Sell P&L':>13}  Win")
    print("  " + "-"*88)

    for yr in all_years:
        bt = buy_all[buy_all["year"] == yr]
        st = sell_all[sell_all["year"] == yr]
        bn,   bwr  = len(bt), (bt["pnl_net"] > 0).mean() * 100 if len(bt) > 0 else 0
        sn,   swr  = len(st), (st["pnl_net"] > 0).mean() * 100 if len(st) > 0 else 0
        bnet = bt["pnl_net"].sum()
        snet = st["pnl_net"].sum()
        bret = bnet / CAPITAL * 100
        sret = snet / CAPITAL * 100
        win  = "BUY " if bnet >= snet else "SELL"
        print(f"  {yr:>5}  {bn:>5}  {bwr:>5.1f}%  Rs{bnet:>10,.0f}({bret:>6.1f}%)  "
              f"{sn:>6}  {swr:>5.1f}%  Rs{snet:>10,.0f}({sret:>6.1f}%)  {win}")

    bavg = buy_all.groupby("year")["pnl_net"].sum().mean() / CAPITAL * 100
    savg = sell_all.groupby("year")["pnl_net"].sum().mean() / CAPITAL * 100
    bmn  = buy_all.groupby("year")["pnl_net"].sum().mean()
    smn  = sell_all.groupby("year")["pnl_net"].sum().mean()
    print(f"  {'AVG':>5}                Rs{bmn:>10,.0f}({bavg:>6.1f}%)  "
          f"              Rs{smn:>10,.0f}({savg:>6.1f}%)")
    print(f"\n  Avg return/yr -> BUY={bavg:.1f}%  SELL={savg:.1f}%  "
          f"Edge: {'BUY' if bavg >= savg else 'SELL'} by {abs(bavg-savg):.1f}%/yr")


def _tier_summary(trades):
    df = pd.DataFrame(trades)
    for q in ["A+", "regular"]:
        qt = df[df["quality"] == q]
        if qt.empty:
            continue
        risk_lbl = "2%" if q == "A+" else "1%"
        _metrics(qt.to_dict("records"), f"{q} ({risk_lbl} risk)")


# ── Filter ablation study ────────────────────────────────────────────────────
def _filter_ablation(df_5m: pd.DataFrame, df_daily: pd.DataFrame,
                     df_vix: pd.DataFrame, inst_cfg: dict,
                     label: str = "Nifty", itm_strikes: int = 0,
                     next_exp_fn=None, is_exp_fn=None) -> None:
    """
    Ablation study: remove one filter at a time and show the effect.
    All signals treated as 'regular' quality (1% risk) to isolate filter impact.
    Highlights 2022, 2023, 2024 individually because those are the target years.
    """
    ALL_FILTERS = {"fvg", "sweep", "bar_quality", "ema_slope",
                   "body_strength", "vix_percentile", "atr"}

    configs = [
        ("All 7 filters (current)",    ALL_FILTERS),
        ("Baseline 4 only",            {"fvg", "sweep", "bar_quality", "ema_slope"}),
        ("No filters (raw ORB)",       set()),
        ("Remove fvg",                 ALL_FILTERS - {"fvg"}),
        ("Remove sweep",               ALL_FILTERS - {"sweep"}),
        ("Remove bar_quality",         ALL_FILTERS - {"bar_quality"}),
        ("Remove ema_slope",           ALL_FILTERS - {"ema_slope"}),
        ("Remove body_strength",       ALL_FILTERS - {"body_strength"}),
        ("Remove vix_percentile",      ALL_FILTERS - {"vix_percentile"}),
        ("Remove atr",                 ALL_FILTERS - {"atr"}),
    ]

    print(f"\n  Filter Ablation — {label} (all @ 1% risk, VIX>={VIX_BUY_MIN}, retest={RETEST_ENTRY})")
    print(f"  {'Config':<30}  {'n':>4}  {'WR':>6}  {'PF':>6}  "
          f"{'Total%':>7}  {'2022':>8}  {'2023':>8}  {'2024':>8}")
    print("  " + "-"*90)

    for lbl, filters in configs:
        raw_sigs = generate_structural_signals(df_5m, df_daily, df_vix,
                                               active_filters=filters)
        sigs = raw_sigs.copy()
        # Force all signals to 'regular' quality so risk is constant across configs
        sigs["quality"] = sigs["signal"].apply(
            lambda x: "regular" if (pd.notna(x) and x in ("call", "put")) else None
        )
        trades = _run_adaptive(df_5m, df_daily, df_vix, sigs, inst_cfg,
                               itm_strikes=itm_strikes,
                               next_exp_fn=next_exp_fn, is_exp_fn=is_exp_fn)

        if not trades:
            print(f"  {lbl:<30}  no trades")
            continue

        df_t = pd.DataFrame(trades)
        df_t["year"] = pd.to_datetime(df_t["entry_date"]).dt.year
        n   = len(trades)
        wr  = (df_t["pnl_net"] > 0).mean() * 100
        w   = df_t[df_t["pnl_net"] > 0]["pnl_net"].sum()
        lo  = abs(df_t[df_t["pnl_net"] < 0]["pnl_net"].sum())
        pf  = w / lo if lo > 0 else float("inf")
        tot = df_t["pnl_net"].sum() / CAPITAL * 100

        def _yr(y):
            v = df_t[df_t["year"] == y]["pnl_net"].sum() / CAPITAL * 100
            return f"{v:>6.1f}%"

        print(f"  {lbl:<30}  {n:>4}  {wr:>5.1f}%  {pf:>5.3f}  "
              f"{tot:>6.1f}%  {_yr(2022):>8}  {_yr(2023):>8}  {_yr(2024):>8}")


# ─── daily resample helper (BankNifty has no separate day cache) ──────────────
def _to_daily(df_1m: pd.DataFrame) -> pd.DataFrame:
    return (df_1m.resample("D")
            .agg(Open=("Open", "first"), High=("High", "max"),
                 Low=("Low", "min"), Close=("Close", "last"),
                 Volume=("Volume", "sum"))
            .dropna(subset=["Open"]))


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    retest_lbl = f"Retest entry (tol={RETEST_TOLERANCE}pt, max {MAX_RETEST_BARS} bars)" if RETEST_ENTRY else "Breakout entry"
    print("="*80)
    print("  ZERODHA ADAPTIVE-RISK ORB  —  Nifty + FinNifty")
    print(f"  ORB: A+=2% risk | Regular=1% | VIX gate >={VIX_BUY_MIN} | Flat SL Rs{FLAT_SL_RS}")
    print(f"  {retest_lbl} | Monthly DD cap {abs(MONTHLY_DD_LIMIT)*100:.0f}% of capital")
    print(f"  Capital Rs{CAPITAL:,}")
    print("="*80)

    def _net(tl): return sum(t["pnl_net"] for t in tl)

    # ── [1] Load data ─────────────────────────────────────────────────────────
    print("\n[1] Loading data ...")

    if CACHE_NIFTY.exists():
        df_nifty_1m = load_zerodha_1m("nifty")
    else:
        print("  Zerodha cache missing — using local CSV")
        from data.download_intraday import load_nifty1m_historical
        df_nifty_1m = load_nifty1m_historical()

    if df_nifty_1m.empty:
        print("ERROR: No Nifty 1m data. Run: python data/zerodha_auth.py && python data/zerodha_data.py")
        sys.exit(1)

    df_nifty_day = load_zerodha_daily("nifty") if CACHE_NIFTY_DAY.exists() else load_all()["nifty"]
    df_vix       = load_zerodha_vix()           if CACHE_VIX_DAY.exists()  else load_all()["indiavix"]

    df_nifty_5m = resample_from_1m(df_nifty_1m, "5m")
    DATA_YRS    = (df_nifty_1m.index[-1] - df_nifty_1m.index[0]).days / 365
    print(f"  Nifty    1m: {len(df_nifty_1m):,} bars  "
          f"{df_nifty_1m.index[0].date()} to {df_nifty_1m.index[-1].date()}  ({DATA_YRS:.1f} yrs)")
    print(f"  Nifty    5m: {len(df_nifty_5m):,} bars")

    # FinNifty
    if not CACHE_FINNIFTY.exists():
        print("ERROR: FinNifty 1m cache missing. Run: python -c \"from data.zerodha_data import download_finnifty_1m; download_finnifty_1m()\"")
        sys.exit(1)
    df_fn_1m  = load_zerodha_1m("finnifty")
    df_fn_day = _to_daily(df_fn_1m)
    df_fn_5m  = resample_from_1m(df_fn_1m, "5m")
    FN_YRS    = (df_fn_1m.index[-1] - df_fn_1m.index[0]).days / 365
    print(f"  FinNifty  1m: {len(df_fn_1m):,} bars  "
          f"{df_fn_1m.index[0].date()} to {df_fn_1m.index[-1].date()}  ({FN_YRS:.1f} yrs)")
    print(f"  FinNifty  5m: {len(df_fn_5m):,} bars")

    # ── [2] Classify signals ──────────────────────────────────────────────────
    print("\n[2] Classifying signals ...")
    nifty_sigs = classify_signals(df_nifty_5m, df_nifty_day, df_vix)

    # Build {date: direction} map for FinNifty A+ alignment check
    _nifty_by_date = {
        ts.date(): sig
        for ts, sig in nifty_sigs["signal"].dropna().items()
        if sig in ("call", "put")
    }
    # FinNifty: Baseline 4 filters, all regular (1% risk) — avoids A+ loss amplification in choppy years
    fn_sigs = classify_signals_finnifty(df_fn_5m, df_fn_day, df_vix)

    def _sig_counts(sigs, yrs, label):
        sr    = sigs[sigs["quality"].notna()]
        n_ap  = (sr["quality"] == "A+").sum()
        n_reg = (sr["quality"] == "regular").sum()
        print(f"  {label:<10} A+={n_ap:>3} ({n_ap/yrs:.0f}/yr)  "
              f"Reg={n_reg:>3} ({n_reg/yrs:.0f}/yr)  Total={n_ap+n_reg} ({(n_ap+n_reg)/yrs:.0f}/yr)")
    _sig_counts(nifty_sigs, DATA_YRS, "Nifty")
    _sig_counts(fn_sigs,    FN_YRS,   "FinNifty")

    # ── [3] Nifty ORB — ATM + VIX gate ──────────────────────────────────────
    print("\n[3] Nifty ORB backtest (ATM, VIX gate >= 12) ...")
    nifty_cfg    = INSTRUMENTS["nifty"]
    _shared_weekly = {}   # shared weekly counter — passed to both Nifty and FinNifty
    nifty_trades = _run_adaptive(df_nifty_5m, df_nifty_day, df_vix,
                                  nifty_sigs, nifty_cfg, itm_strikes=0,
                                  weekly_counts=_shared_weekly)
    print(f"  {len(nifty_trades)} trades")

    # ── [4] Nifty signal date set (used by IS/OOS section) ───────────────────
    nifty_signal_dates = set(
        nifty_sigs[nifty_sigs["quality"].notna()].index.normalize().date
    )
    ss_nifty = []   # short straddle disabled — BS pricer can't model intraday theta
    if RUN_SHORT_STRADDLE:
        print("\n[4] Nifty short straddle (non-signal days, VIX 13-20) ...")
        ss_nifty = _run_short_straddle(df_nifty_5m, df_vix,
                                        nifty_signal_dates, nifty_cfg)
        ss_wins = sum(1 for t in ss_nifty if t["pnl_net"] > 0)
        print(f"  {len(ss_nifty)} trades  WR={ss_wins/max(len(ss_nifty),1)*100:.1f}%")

    # ── [5] FinNifty ORB — 1-ITM + VIX gate (shares weekly counter with Nifty) ──
    print("\n[5] FinNifty ORB backtest (1-ITM, VIX gate >= 12) ...")
    fn_cfg    = INSTRUMENTS["finnifty"]
    fn_trades = _run_adaptive(df_fn_5m, df_fn_day, df_vix,
                               fn_sigs, fn_cfg, itm_strikes=1,
                               next_exp_fn=next_expiry_fn, is_exp_fn=is_expiry_day_fn,
                               weekly_counts=_shared_weekly)
    print(f"  {len(fn_trades)} trades")

    # ── [6] Summary ──────────────────────────────────────────────────────────
    print("\n[6] Results summary ...")
    print(f"\n  {'Strategy':<52} {'n':>5}  {'WR':>6}  {'PF':>6}  {'Net P&L':>13}  Exits")
    print("  " + "-"*108)
    _metrics(nifty_trades, "Nifty ORB ATM (VIX>=12)",      DATA_YRS)
    _metrics(fn_trades,    "FinNifty ORB 1-ITM (VIX>=12)", FN_YRS)
    print()
    _metrics([t for t in nifty_trades if t["quality"] == "A+"],
             "  Nifty A+ (2%)",   DATA_YRS)
    _metrics([t for t in nifty_trades if t["quality"] == "regular"],
             "  Nifty Reg (1%)",  DATA_YRS)
    print()
    _metrics(nifty_trades + fn_trades,
             "  GRAND TOTAL (Nifty + FinNifty)",     DATA_YRS)

    # ── [7] Yearly breakdown ──────────────────────────────────────────────────
    print("\n[7] Yearly breakdown ...")
    _yearly(nifty_trades, "Nifty ORB ATM")
    _yearly(fn_trades,    "FinNifty ORB 1-ITM")
    _yearly_total([nifty_trades, fn_trades], "GRAND TOTAL — Nifty + FinNifty")

    # ── [8] Filter ablation study ─────────────────────────────────────────────
    if RUN_ABLATION:
        print("\n[8] Filter ablation study ...")
        _filter_ablation(df_nifty_5m, df_nifty_day, df_vix,
                         INSTRUMENTS["nifty"], label="Nifty ATM", itm_strikes=0)
        _filter_ablation(df_fn_5m, df_fn_day, df_vix,
                         INSTRUMENTS["finnifty"], label="FinNifty 1-ITM", itm_strikes=1,
                         next_exp_fn=next_expiry_fn, is_exp_fn=is_expiry_day_fn)

    # ── [9] IS / OOS ─────────────────────────────────────────────────────────
    print("\n[9] IS / OOS validation ...")
    print(f"\n  {'Period':<12}  {'Strategy':<30}  {'n':>5}  "
          f"{'WR':>6}  {'PF':>6}  {'Net P&L':>13}  {'Ret/yr':>7}")
    print("  " + "-"*100)

    for period, s, e, yrs_p in [
        ("IS 2015-18",  "2015-01-01", IS_END,   4),
        ("OOS 2019-24", OOS_START,   OOS_END,   6),
    ]:
        _wc   = {}   # fresh shared weekly counter for each IS/OOS period
        n_orb = _run_adaptive(df_nifty_5m, df_nifty_day, df_vix, nifty_sigs,
                               nifty_cfg, s, e, itm_strikes=0, weekly_counts=_wc)
        n_ss  = (_run_short_straddle(df_nifty_5m, df_vix,
                                      nifty_signal_dates, nifty_cfg, s, e)
                 if RUN_SHORT_STRADDLE else [])
        f_orb = _run_adaptive(df_fn_5m, df_fn_day, df_vix, fn_sigs,
                               fn_cfg, s, e, itm_strikes=1,
                               next_exp_fn=next_expiry_fn, is_exp_fn=is_expiry_day_fn,
                               weekly_counts=_wc)
        comb  = n_orb + n_ss + f_orb

        for seg, tl in [("Nifty ORB",   n_orb),
                        ("FinNifty ORB", f_orb),
                        ("COMBINED",     comb)]:
            if not tl: continue
            m = compute_metrics(tl, CAPITAL)
            if "error" in m: continue
            print(f"  {period:<12}  {seg:<30}  {len(tl):>5}  "
                  f"{m['win_rate_pct']:>5.1f}%  {m['profit_factor']:>5.3f}  "
                  f"Rs{m['net_pnl']:>11,.0f}  {m['net_pnl']/CAPITAL/yrs_p*100:>6.1f}%")
        print()

    # ── [11] Option selling comparison ───────────────────────────────────────
    print("\n[11] Option selling comparison ...")
    print("     Strategy: sell the OPPOSITE ATM leg on each ORB signal")
    print("     call signal -> sell ATM put  |  put signal -> sell ATM call")
    print(f"    SL: entry + Rs{FLAT_SL_RS}/unit | slippage: "
          f"<=2bars={SELL_SL_SLIP_FAST*100:.0f}%, 3-5bars={SELL_SL_SLIP_MED*100:.0f}%, "
          f">5bars={SELL_SL_SLIP_SLOW*100:.0f}% | TP: option decays to {SELL_TP_FRAC*100:.0f}% of entry")

    _sw = {}
    nifty_sell = _run_sell_orb(df_nifty_5m, df_nifty_day, df_vix,
                                nifty_sigs, nifty_cfg, itm_strikes=0,
                                weekly_counts=_sw)
    fn_sell    = _run_sell_orb(df_fn_5m, df_fn_day, df_vix,
                                fn_sigs, fn_cfg, itm_strikes=0,
                                next_exp_fn=next_expiry_fn, is_exp_fn=is_expiry_day_fn,
                                weekly_counts=_sw)
    print(f"  Nifty sell: {len(nifty_sell)} trades  |  FinNifty sell: {len(fn_sell)} trades")

    print(f"\n  {'Strategy':<52} {'n':>5}  {'WR':>6}  {'PF':>6}  {'Net P&L':>13}  Exits")
    print("  " + "-"*108)
    _metrics(nifty_trades, "Nifty  BUY  ATM  (buy breakout leg)", DATA_YRS)
    _metrics(nifty_sell,   "Nifty  SELL ATM  (sell opposite leg)", DATA_YRS)
    print()
    _metrics(fn_trades, "FinNifty  BUY  1-ITM (buy breakout leg)", FN_YRS)
    _metrics(fn_sell,   "FinNifty  SELL ATM   (sell opposite leg)", FN_YRS)
    print()
    _metrics(nifty_trades + fn_trades, "COMBINED BUY  (Nifty + FinNifty)", DATA_YRS)
    _metrics(nifty_sell   + fn_sell,   "COMBINED SELL (Nifty + FinNifty)", DATA_YRS)

    _yearly_buy_vs_sell([nifty_trades, fn_trades], [nifty_sell, fn_sell],
                        "Year-by-year: BUY vs SELL  (Nifty + FinNifty combined)")

    # ── [12] SL slippage sensitivity ──────────────────────────────────────────
    print("\n[12] SL slippage sensitivity (sell strategy, Nifty + FinNifty combined) ...")
    print("     Shows how much the SL buy-back slippage damages the sell strategy.")
    print("     Each SL exit: fill = sl_trigger × (1 + slip%)  [worse than trigger]\n")

    slip_scenarios = [
        ("Zero (0/0/0%)",        0.00, 0.00, 0.00),
        ("Current (30/20/10%)",  0.30, 0.20, 0.10),
        ("Severe  (50/35/20%)",  0.50, 0.35, 0.20),
    ]

    # Collect yearly P&L for each scenario
    scen_results = []
    for scen_name, fast, med, slow in slip_scenarios:
        _sw2 = {}
        ns = _run_sell_orb(df_nifty_5m, df_nifty_day, df_vix, nifty_sigs, nifty_cfg,
                            itm_strikes=0, weekly_counts=_sw2,
                            sl_slip_fast=fast, sl_slip_med=med, sl_slip_slow=slow)
        fs = _run_sell_orb(df_fn_5m, df_fn_day, df_vix, fn_sigs, fn_cfg,
                            itm_strikes=0, next_exp_fn=next_expiry_fn,
                            is_exp_fn=is_expiry_day_fn, weekly_counts=_sw2,
                            sl_slip_fast=fast, sl_slip_med=med, sl_slip_slow=slow)
        scen_results.append((scen_name, ns + fs))

    # Header: scenario names across columns
    print(f"  {'Year':>5}", end="")
    for scen_name, _ in scen_results:
        print(f"  {scen_name:>21}", end="")
    print(f"  {'BUY (baseline)':>21}")
    print("  " + "-"*100)

    all_years_sell = sorted(set(
        int(t["entry_date"][:4]) for _, tl in scen_results for t in tl
    ))
    buy_by_year = (pd.DataFrame(nifty_trades + fn_trades)
                   .assign(year=lambda d: pd.to_datetime(d["entry_date"]).dt.year)
                   .groupby("year")["pnl_net"].sum())

    for yr in all_years_sell:
        print(f"  {yr:>5}", end="")
        for _, tl in scen_results:
            yr_net = sum(t["pnl_net"] for t in tl if t["entry_date"][:4] == str(yr))
            ret    = yr_net / CAPITAL * 100
            print(f"  {ret:>20.1f}%", end="")
        buy_ret = buy_by_year.get(yr, 0) / CAPITAL * 100
        print(f"  {buy_ret:>20.1f}%")

    print(f"\n  {'AVG/yr':>5}", end="")
    for _, tl in scen_results:
        df_s = pd.DataFrame(tl)
        if df_s.empty:
            print(f"  {'N/A':>21}", end=""); continue
        df_s["year"] = pd.to_datetime(df_s["entry_date"]).dt.year
        avg = df_s.groupby("year")["pnl_net"].sum().mean() / CAPITAL * 100
        print(f"  {avg:>20.1f}%", end="")
    buy_avg = buy_by_year.mean() / CAPITAL * 100
    print(f"  {buy_avg:>20.1f}%")

    # Exit breakdown per scenario
    print(f"\n  {'Scenario':<25}  {'n':>5}  {'WR':>6}  {'PF':>6}  "
          f"{'SL%':>5}  {'TP%':>5}  {'EOD%':>6}  "
          f"{'Avg SL loss':>12}  {'Avg TP win':>11}  {'2023 ret':>9}")
    print("  " + "-"*110)
    for scen_name, tl in scen_results:
        if not tl:
            continue
        df_s = pd.DataFrame(tl)
        df_s["year"] = pd.to_datetime(df_s["entry_date"]).dt.year
        n   = len(df_s)
        wr  = (df_s["pnl_net"] > 0).mean() * 100
        w   = df_s[df_s["pnl_net"] > 0]["pnl_net"].sum()
        lo  = abs(df_s[df_s["pnl_net"] < 0]["pnl_net"].sum())
        pf  = w / lo if lo > 0 else float("inf")
        sl_df  = df_s[df_s["exit_type"] == "SL"]
        tp_df  = df_s[df_s["exit_type"] == "TP"]
        eod_df = df_s[df_s["exit_type"] == "3:05"]
        avg_sl = sl_df["pnl_net"].mean() if not sl_df.empty else 0
        avg_tp = tp_df["pnl_net"].mean() if not tp_df.empty else 0
        pct_sl  = len(sl_df)  / n * 100
        pct_tp  = len(tp_df)  / n * 100
        pct_eod = len(eod_df) / n * 100
        yr2023  = df_s[df_s["year"] == 2023]["pnl_net"].sum() / CAPITAL * 100
        print(f"  {scen_name:<25}  {n:>5}  {wr:>5.1f}%  {pf:>5.3f}  "
              f"{pct_sl:>4.0f}%  {pct_tp:>4.0f}%  {pct_eod:>5.0f}%  "
              f"Rs{avg_sl:>10,.0f}  Rs{avg_tp:>9,.0f}  {yr2023:>8.1f}%")

    # ── [13] Save ─────────────────────────────────────────────────────────────
    os.makedirs("results", exist_ok=True)
    pd.DataFrame(nifty_trades).to_csv("results/zerodha_nifty_trades.csv",    index=False)
    pd.DataFrame(fn_trades).to_csv(   "results/zerodha_finnifty_trades.csv", index=False)
    all_trades = nifty_trades + fn_trades
    pd.DataFrame(all_trades).to_csv(  "results/zerodha_combined_trades.csv", index=False)
    pd.DataFrame(nifty_sell + fn_sell).to_csv("results/zerodha_sell_trades.csv", index=False)
    print("  Saved: results/zerodha_nifty_trades.csv")
    print("  Saved: results/zerodha_finnifty_trades.csv")
    print("  Saved: results/zerodha_combined_trades.csv")
    print("  Saved: results/zerodha_sell_trades.csv")

    print("\n" + "="*80)
    print("  DONE")
    print("="*80)
