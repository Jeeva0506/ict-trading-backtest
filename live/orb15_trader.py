"""STELLAR-15K first-hour range trader -- a SUBCLASS of LiveTrader.

Same pattern as smc_trader.py: mt5_connector.py is NOT edited, so the other
live bots are provably unaffected. Only _generate_signal() is overridden.

WHAT THIS REPLACES AND WHY
    The current live config (M1 detection, ORB15, retest entry) has NO causal
    edge: over 15 years `direct` entry measures -0.0787R and `retest` -0.1838R.
    Its reported +0.0748R comes entirely from choosing between those two paths
    with hindsight -- the strategy cannot do that live, which is why 57-59% of
    its signals die at the drift guard.

    This uses a TRUE 15-minute range on M15 bars with a resting stop entry:
        15yr  +0.1738R  t=+14.03  WR 59.8%  MCL 10   1 losing year in 17
        TEST 2019-2026  +0.2885R  t=+15.49  WR 63.5%  0 losing years in 8
    Validated: causality gate PASSED (954 closed trades compared under
    truncation), daily cap NOT binding, robust to 3x slippage (+0.052, t=4.29),
    79.1% of signals survive the EXISTING live SL guards unchanged.

HONEST LABEL
    The random-anchor control FAILED: a fake "open" 60 minutes into the session
    scores 106% of the real open (+0.1839 vs +0.1738), decaying only after
    (+120m 70%, +180m 41%, +240m 24%). So this is a FIRST-HOUR
    volatility-expansion effect, not an opening-range effect. The edge is real
    and survives costs, but it is a more generic -- and so more crowded --
    claim than "the open is special". Sized accordingly.

GUARDS
    Unchanged from config_fundednext.py. Its 11-106pt stops sit naturally
    inside the existing caps, which is why this needs no config edit (SMC,
    with 21-159pt stops, did).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta as _td, timezone as _tz
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz

import MetaTrader5 as mt5

from mt5_connector import LiveTrader, logger, get_index_config

_ORB_V2 = Path(__file__).resolve().parent.parent.parent / "orb_v2"
if str(_ORB_V2) not in sys.path:
    sys.path.insert(0, str(_ORB_V2))
import strategy_v3                                     # noqa: E402
from strategy_v3 import ParamsV3                       # noqa: E402

# Resolved at import from the env var run_live.py sets from --account BEFORE
# any config import. Defined here because the symbol book below depends on it.
ACCOUNT_PROFILE = os.environ.get("ACCOUNT_PROFILE", "").strip().lower()

# The original six, all passing the EXISTING live guards (x27):
#   NDX100 62% kept t6.45 | FRA40 79% t6.59 | US30 71% t5.63
#   GER30  84% kept t5.70 | SPX500 89% t4.34 | UK100 88% t3.10
# NOTE what x27 measured: what fraction of SIGNALS SURVIVE THE GUARDS. It says
# nothing about whether the symbol makes money, which is why all six were here.
ORB15_SYMBOLS_ALL = ["NDX100", "SPX500", "US30", "GER30", "FRA40", "UK100"]

# ---------------------------------------------------------------------------
# 2026-10-12: GER30 / FRA40 / UK100 REMOVED FROM STELLAR-2.
#
# Measured on M1 with next-M1-open fills and MEASURED spreads, 2019-2026
# (orb_v2/x76_other4_buckets.py). Unfiltered 7-year totals at 1% risk per
# trade -- i.e. what the account was actually doing:
#       NDX100  +78.9%      GER30  -119.9%
#       SPX500   -7.6%      FRA40  -134.8%
#       US30    +24.4%      UK100  -147.7%   -> -402% combined
#
# NO FILTER RESCUES THEM. Every displacement bucket is negative on all three,
# and the U-shape structure that works on US indices is absent -- on UK100 the
# "explosive" bucket is the WORST (-0.149%) where on NDX100 it is the best
# (+0.114%). So this is a symbol-selection problem, not a filter problem.
#
# The spreads are now MEASURED, not assumed (orb_v2/x75_spread_sampler.py, 562
# samples per symbol across a full session covering both opens):
#       GER30 2.00 (1.00x stored, min=max=2.00)   FRA40 2.02 (1.01x)
#       UK100 1.82 (0.91x)                        US30  2.16 (1.09x)
# GER30 was the only borderline case -- breakeven at 0.5x spread -- and its
# true spread is 1.00x, so its negative verdict stands. FRA40 and UK100 are
# negative even at 0.5x. Their median cost/R is 0.064-0.074, at or past the
# 0.06 failure line.
#
# US30 ADDED TO THE u_shape FILTER at the same time: u_shape +0.062%/trade,
# its quiet bucket +0.106%, the best cost/R in the book at 0.015, still
# positive at 3x spread, and TEST (+0.064) above TRAIN (+0.052). Its t is only
# 1.26 because US30 has 3 years of M1 (US30_3YEAR_M1) against the others' 7.
#
# STELLAR-15K IS DELIBERATELY LEFT ON ALL SIX. The user's standing instruction
# is to leave 15K running its pass_only validation (started 2026-10-07
# 12:03:35). Removing symbols would change its trade population mid-test. It
# is therefore STILL TRADING THE THREE LOSING SYMBOLS -- a known, flagged
# state awaiting a decision, not an oversight.
ORB15_SYMBOLS_BY_ACCOUNT = {
    "stellar2": ("NDX100", "SPX500", "US30"),
    "stellar3": ("NDX100", "SPX500", "US30"),   # same book as STELLAR-2, so
                                                # the only difference between
                                                # the two accounts is the EXIT
    "stellar15k": tuple(ORB15_SYMBOLS_ALL),     # unchanged pending decision
}
ORB15_SYMBOLS = list(ORB15_SYMBOLS_BY_ACCOUNT.get(ACCOUNT_PROFILE,
                                                  ORB15_SYMBOLS_ALL))

# PER-SESSION daily cap. The parent caps trades GLOBALLY (mt5_connector:1781),
# which with a two-session book lets Europe (opens 10:00 broker) consume the
# whole budget 6.5 hours before the US symbols (16:30) ever fire. That exact
# artefact made an earlier analysis conclude SMC "does not trade US indices"
# when ungated it produced 919 NDX100 trades. Each session gets its own cap.
SESSION_GROUP = {"GER30": "EU", "FRA40": "EU", "UK100": "EU",
                 "NDX100": "US", "SPX500": "US", "US30": "US"}
MAX_TRADES_PER_SESSION = 2

BAR_MINUTES = 15
RANGE_MINUTES = 15           # a TRUE 15-min range = 1 bar at M15
ENTRY_WINDOW_MINUTES = 240


# ---------------------------------------------------------------------------
# MOMENTUM FILTER ON THE CONFIRMING BAR   (added 2026-10-07, STELLAR-15K only)
#
# WHAT IT DOES
#     Rejects a breakout whose confirming bar is not a decisive bar:
#         body        = |close-open| / (high-low)   must be >= 0.50
#         displacement= (high-low) / mean(high-low of the prior 14 bars)
#                                                   must be >= 1.20
#     Both are read from the bar that CLOSED beyond the opening range -- the
#     same bar strategy_v3 stamps as signal_time -- and the displacement
#     baseline uses only bars BEFORE it. Nothing from the future is touched.
#
# WHY THESE NUMBERS AND WHY THIS ACCOUNT
#     Measured in orb_v2/x67_momentum_filter.py on M15 index data with
#     next-bar-open fills and MEASURED spreads, then attacked in
#     orb_v2/x69c_momentum_falsify.py. On the US symbols:
#         baseline   TRAIN +0.0253R   TEST +0.0135R
#         filtered   TRAIN +0.0729R   TEST +0.0596R     ~15.5 trades/month
#     It is the ELEVENTH filter tried and the FIRST to survive the full
#     battery: random-direction control ~0, inversion clearly negative,
#     majority of years positive, no single symbol carrying it. The ten that
#     failed are listed in the x67 docstring.
#
# SCOPE -- deliberately narrow, and narrower than it may look
#     accounts: STELLAR-15K only. STELLAR-2 runs this SAME module
#         (--strategy orb15), so an ungated edit would have changed it too.
#     symbols:  NDX100/SPX500/US30 only. The falsification ran on these three
#         because their spreads are measured; GER30/FRA40/UK100 spreads are
#         proxy values, so the filter is NOT validated there and is NOT
#         applied -- those symbols keep their existing behaviour exactly.
#     STELLAR-3 is a different module (strategy_ict_clean) and is untouched.
#         It already runs stricter versions of both thresholds (disp 1.50,
#         body 0.60) and still measures -0.16R causally, so these looser
#         values are not transferable to it.
#
# EXPECTED LIVE EFFECT: fewer US signals, higher win rate per signal. If the
# US trade count does not drop, the gate is not running -- check the
# ORB15 momentum filter line that initialize() logs.
# ---------------------------------------------------------------------------
# 2026-10-07 (second change): STELLAR-2 gets a DIFFERENT rule -- "u_shape".
#
# WHY TWO RULES RATHER THAN NEW THRESHOLDS
#     Displacement turned out to be U-SHAPED, not monotone. Measured on M1,
#     next-M1-open fills, measured spreads, NDX100+SPX500, 2019-2026, n=3638
#     (orb_v2/x72_momentum_plus_meanrev.py), by EXCLUSIVE bucket:
#         quiet  disp < 0.8                 n=1094  WR 55.7%  +0.089%  t=+3.13
#         mushy  0.8 <= disp < 1.2          n=1366  WR 49.6%  -0.040%  t=-1.54
#         pass   disp >= 1.2 & body >= 0.5  n= 981  WR 54.3%  +0.049%  t=+1.56
#     So "pass_only" -- the rule deployed to STELLAR-15K earlier today -- keeps
#     the WEAKER of the two positive groups and discards its best one. x67's
#     "monotone dose-response" was an artefact of testing CUMULATIVE disp>=
#     cuts, which can look monotone over non-monotone buckets.
#
#     Combined, one trade per index per day:
#         every breakout  n=3637  WR 52.6%  +0.020%  t=1.23  PF 1.05  R/DD 1.04
#         pass_only       n= 980  WR 54.3%  +0.049%  t=1.56  PF 1.12  R/DD 2.22
#         u_shape         n=2074  WR 55.0%  +0.070%  t=3.32  PF 1.18  R/DD 5.95
#     2.1x the trades and 2.7x the risk-adjusted return of pass_only.
#
# FALSIFICATION (orb_v2/x73_quiet_falsify.py) -- the quiet arm passed all of:
#     random-direction control  mean -0.0360% over 5 seeds vs real +0.0886%
#                               (negative, so not a volatility artefact; trade
#                               count asserted == 1094 on every seed, which
#                               excludes the x69 silent-skip bug)
#     inversion                 -0.1396%, t=-5.00; and real + inverted
#                               = -0.0509 against -2x median cost/R = -0.054,
#                               i.e. direction plus friction, nothing residual
#     both symbols              t=2.25 / 2.18, neither carries it
#     train -> test             t=2.24 -> 2.38 (test stronger)
#     year by year              6 of 7 positive
#     threshold sensitivity     positive at every cut 0.6-1.0, smooth decay
#
# WHY STELLAR-15K IS DELIBERATELY LEFT ON pass_only
#     It began a 15-trade live validation of pass_only at 2026-10-07 12:03:35.
#     Switching it now would reset that count. Running the two accounts on
#     different rules over the SAME market days is a better test than choosing
#     between two backtests, so this is intentional, not an oversight.
#     => any edit here MUST leave pass_only behaviour bit-identical.
#
# WHAT WAS TESTED AND REJECTED, so it is not retried:
#     * mean reversion / fading, naive form (x72): -0.42..-0.55%/trade at every
#       target, both periods. Cause: a fade stop at the confirming bar's
#       extreme gives median R 4.5 pts and cost/R 0.208 vs 0.027 with-break.
#     * mean reversion, CANONICAL failed-ORB form (x74): enter on the close
#       back inside the range, stop at the excursion extreme, structural
#       targets. Fixed the cost problem (R 24.8 pts, cost/R 0.037) and still
#       lost -0.064%, t=-2.58, because median R:R is 0.69 -- the stop ends up
#       further than the target. 55.7% WR at PF 0.84.
#     * Crabel session-level contraction (x74): works BACKWARDS here.
#       rel_or<0.6 -0.057% vs rel_or>=1.0 +0.030%. Do NOT enable
#       strategy_v3's rel_range_min/max expecting a contraction effect.
#
# SYMBOL SCOPE IS NARROWER FOR u_shape, deliberately: the U-shape was measured
# on NDX100 and SPX500 only, because US30's M1 history in data/raw stops in
# 2019. US30 therefore stays UNFILTERED on STELLAR-2 rather than inheriting a
# rule that was never tested on it.
MOMENTUM_RULES = {
    "stellar15k": "pass_only",      # validation in progress -- do not change
    "stellar2": "u_shape",
    "stellar3": "u_shape",          # 2026-10-13: converted off legacy ORB
}
MOMENTUM_RULE_SYMBOLS = {
    "pass_only": ("NDX100", "SPX500", "US30"),
    # US30 added 2026-10-12: u_shape +0.062%/trade, quiet bucket +0.106%,
    # cost/R 0.015 (best in the book), still positive at 3x spread, TEST above
    # TRAIN. Measured on US30_3YEAR_M1 (2023-02..2026-02), so 3 years not 7.
    "u_shape": ("NDX100", "SPX500", "US30"),
}
MOMENTUM_BODY_MIN = 0.50
MOMENTUM_DISP_MIN = 1.20
MOMENTUM_QUIET_MAX = 0.80       # u_shape only: below this, trade it regardless
MOMENTUM_DISP_LOOKBACK = 14

# ACCOUNT_PROFILE is defined near the top of the module (the symbol book
# depends on it). The active rule is logged at startup so it is never a silent
# default.
MOMENTUM_RULE = MOMENTUM_RULES.get(ACCOUNT_PROFILE)
MOMENTUM_FILTER_ACTIVE = MOMENTUM_RULE is not None
MOMENTUM_FILTER_SYMBOLS = (MOMENTUM_RULE_SYMBOLS[MOMENTUM_RULE]
                           if MOMENTUM_FILTER_ACTIVE else ())


def _bar_is_closed(ts, last_m1):
    """Has the M15 bucket labelled `ts` finished forming?

    _generate_signal resamples session M1 into M15, so the LAST bucket is the
    bar currently forming. A bucket starting at ts ends at ts+15min.

    We require an M1 bar stamped at or after ts+15min to exist -- i.e. the
    NEXT bucket has begun. That is one bar stricter than "coverage reaches
    ts+15min", deliberately: the final M1 bar of a bucket may itself still be
    forming, which would have the filter judge displacement on ~14.5 minutes
    of a 15-minute bar. It is also EXACTLY the backtest's condition, since the
    backtest enters on the M1 bar stamped ts+15min -- so live and backtest
    judge the same completed bar.
    """
    if last_m1 is None:
        return False
    return (pd.Timestamp(last_m1)
            >= pd.Timestamp(ts) + pd.Timedelta(minutes=BAR_MINUTES))


def _momentum_ok(df, ts, symbol, last_m1=None):
    """True if the confirming bar at `ts` is decisive enough to trade.

    Returns (ok, detail) so a rejection can be logged with its numbers.
    Raises nothing: a bar that cannot be measured (zero range, or no prior
    bars to form a baseline) is REJECTED rather than passed, so a measurement
    failure can never become a silent trade.
    """
    if not MOMENTUM_FILTER_ACTIVE or symbol not in MOMENTUM_FILTER_SYMBOLS:
        return True, "filter not applicable"
    try:
        i = df.index.get_loc(pd.Timestamp(ts))
    except Exception:
        return False, f"confirming bar {ts} not found in frame"
    if not isinstance(i, int):
        return False, f"ambiguous index for {ts}"

    # CLOSED-BAR REQUIREMENT -- u_shape only, and the reason is specific.
    #
    # A forming M15 bar has a tiny range, so its displacement is near zero
    # (measured: disp ~0.08 one minute in). u_shape reads anything under 0.80
    # as "quiet" and would therefore TAKE the very first tick beyond the
    # opening range, every session -- an intrabar touch entry, not the
    # closed-bar signal that was validated. That is a live/backtest parity
    # break, not a tuning question, so the filter refuses to judge a bar that
    # has not finished forming.
    #
    # pass_only is deliberately NOT gated here. It is immune in practice (a
    # forming bar cannot reach disp>=1.20 until it has grown), and STELLAR-15K
    # is mid-validation on it from 2026-10-07 12:03:35 -- adding a gate would
    # change its behaviour. Its residual exposure is that an explosive move
    # can satisfy pass_only mid-bar; that is a known, smaller parity gap,
    # recorded rather than silently altered.
    if MOMENTUM_RULE == "u_shape" and last_m1 is None:
        # A caller that omits last_m1 would make u_shape reject EVERY signal
        # and quietly stop the account trading. That is a programming error,
        # not a market condition, so it fails loudly instead.
        raise ValueError("u_shape requires last_m1 (the last M1 bar time) to "
                         "verify the confirming bar has closed")
    if MOMENTUM_RULE == "u_shape" and not _bar_is_closed(ts, last_m1):
        return False, (f"confirming bar {ts} has not closed yet "
                       f"(last M1 {last_m1}) -- u_shape needs a completed bar")

    bar = df.iloc[i]
    rng = float(bar["high"]) - float(bar["low"])
    if rng <= 0:
        return False, "zero-range confirming bar"
    body = abs(float(bar["close"]) - float(bar["open"])) / rng

    prev = df.iloc[max(0, i - MOMENTUM_DISP_LOOKBACK):i]
    if len(prev) == 0:
        return False, "no prior bars for displacement baseline"
    avg = float((prev["high"] - prev["low"]).mean())
    if not (avg > 0):
        return False, "degenerate displacement baseline"
    disp = rng / avg

    explosive = (body >= MOMENTUM_BODY_MIN and disp >= MOMENTUM_DISP_MIN)
    if MOMENTUM_RULE == "pass_only":
        ok = explosive
    elif MOMENTUM_RULE == "u_shape":
        # take the quiet tail OR the explosive tail; reject the mushy middle
        ok = (disp < MOMENTUM_QUIET_MAX) or explosive
    else:
        # An unrecognised rule must never fall through to "trade it". Fail
        # loudly: a behaviour parameter with no defined meaning is a config
        # error, not a default.
        raise ValueError("unknown MOMENTUM_RULE %r for account %r"
                         % (MOMENTUM_RULE, ACCOUNT_PROFILE))

    detail = (f"rule={MOMENTUM_RULE} body={body:.2f} disp={disp:.2f}x "
              f"(quiet<{MOMENTUM_QUIET_MAX:.2f} | explosive: body>="
              f"{MOMENTUM_BODY_MIN:.2f} & disp>={MOMENTUM_DISP_MIN:.2f}) "
              f"over {len(prev)} prior bars")
    return ok, detail


# ---------------------------------------------------------------------------
# PER-ACCOUNT EXIT SCHEME   (2026-10-13)
#
# STELLAR-3 was converted off the legacy ORB (strategy_ict_clean) onto ORB15
# with the u_shape filter and a DIFFERENT EXIT. Measured on frozen u_shape
# signals, NDX100, 7 years, M1-resolved, measured spreads
# (orb_v2/x85_exit_schemes.py, x87, x88):
#
#   exit                      WR     payoff   PF    expR    R/DD
#   tp2.5 be1.0 tr0.5 (live) 56.7%    0.97   1.27  +0.098   9.49
#   no tp, be1.0, trail 1.0  56.0%    1.04   1.33  +0.119  12.25
#
# and with STELLAR-2's buffer-derived adaptive risk on a $6,000 account,
# reset each January, NDX100 alone, at the MEASURED 0.031R entry drift:
#   live exit        passed 4/7 years, 0 blown, mean +14.28%
#   no tp trail1.0   passed 6/7 years, 0 blown, mean +20.84%
#
# WHY THE TRAIL WIDENS RATHER THAN TIGHTENS
#   The old exit's payoff was 0.97 -- the average winner equalled the average
#   loser despite a 2.5R target, because breakeven at 1R plus a 0.5R trail
#   scratched trades that would have run. x85 tested ten exit schemes; PF sat
#   in a 1.17-1.24 band across all of them, so the exit redistributes between
#   win rate and payoff rather than creating edge. This one is chosen because
#   it is the only scheme that raises payoff while KEEPING win rate above 55%.
#
# HONEST LIMIT, recorded rather than buried: x86's random-direction control
#   showed ~70% of the gain also accrues to randomly-chosen directions. The
#   old exit is simply bad for anything; the direction-attributable part of
#   the improvement is about +0.008R, not +0.027R. The money is real, but this
#   is recovering value the old exit threw away, not new edge.
#
# exit_mode "eod_trail" (strategy_v3:296) sets an unreachable target and runs
# breakeven + trail only, which is exactly "no tp, trail 1.0 from 1R".
EXIT_BY_ACCOUNT = {
    "stellar3": dict(exit_mode="eod_trail", tp_r=2.5,
                     be_trigger_r=1.0, trail_buffer_r=1.0),
}
DEFAULT_EXIT = dict(exit_mode="fixed_r", tp_r=2.5,
                    be_trigger_r=1.0, trail_buffer_r=0.5)
ACCOUNT_EXIT = EXIT_BY_ACCOUNT.get(ACCOUNT_PROFILE, DEFAULT_EXIT)


def _params():
    # ParamsV3 counts BARS, not minutes -- converting here is what the first
    # version of this test got wrong (it ran 225-minute ranges labelled "15m")
    return ParamsV3(
        or_minutes=max(1, RANGE_MINUTES // BAR_MINUTES),
        anchor_offset_minutes=0,
        entry_window_minutes=max(1, ENTRY_WINDOW_MINUTES // BAR_MINUTES),
        entry_slip_cost_mult=1.0, require_close_beyond=True,
        stop_mode="range", atr_stop_mult=0.10, atr_lookback=14,
        sl_buffer=0.0, min_sl_points=0.0, max_sl_points=1e9,
        exit_mode=ACCOUNT_EXIT["exit_mode"], partial_r=1.0, partial_frac=0.5,
        partial_trail=False, tp_r=ACCOUNT_EXIT["tp_r"],
        be_trigger_r=ACCOUNT_EXIT["be_trigger_r"],
        trail_buffer_r=ACCOUNT_EXIT["trail_buffer_r"],
        force_close_minutes_before_end=15, rel_range_min=0.0, rel_range_max=0.0,
        rel_range_lookback=14, momentum_align=False, htf_align=False,
        max_signals_per_session=1)


def _apply_risk_cap(trader):
    """Honour RISK_PER_TRADE as a CEILING on the adaptive risk.

    ADAPTIVE_RISK (config_fundednext.py) overrides RISK_PER_TRADE entirely --
    at 5.55% drawdown it returns its tier-2 value of 0.5% no matter what the
    env says, so a 0.1% validation week would silently run at 5x the intended
    size. This wraps the adaptive function with min(), so:
      * a LOWER configured risk is honoured (0.1% test week works), and
      * the adaptive step-down still applies if it wants to go lower still.
    It can only ever REDUCE risk, never raise it.
    """
    try:
        from config_fundednext import FUNDEDNEXT_RULES
        cap = float(FUNDEDNEXT_RULES.get("risk_per_trade_percent", 1.0))
    except Exception:
        return
    dd = getattr(trader, "dd_protection", None)
    if dd is None:
        return
    original = dd.get_adaptive_risk

    def capped(current_balance, trades_today=0, _o=original, _c=cap):
        try:
            adaptive = float(_o(current_balance, trades_today))
        except TypeError:
            adaptive = float(_o(current_balance))
        return min(adaptive, _c)

    dd.get_adaptive_risk = capped
    logger.info(f"Risk ceiling applied: RISK_PER_TRADE={cap}% caps adaptive risk")


class ORB15Trader(LiveTrader):
    """LiveTrader with the true-15-minute first-hour range entry."""

    STRATEGY_NAME = "ORB15_FirstHour"

    def __init__(self):
        super().__init__()
        logger.info(
            "ORB15 symbol book: %s | account=%r%s"
            % (",".join(ORB15_SYMBOLS), ACCOUNT_PROFILE or "<default>",
               "" if set(ORB15_SYMBOLS) == set(ORB15_SYMBOLS_ALL)
               else "  (removed: %s -- negative at measured spreads)"
                    % ",".join(sorted(set(ORB15_SYMBOLS_ALL)
                                      - set(ORB15_SYMBOLS)))))
        logger.info(f"ORB15Trader active | symbols={ORB15_SYMBOLS} | "
                    f"M{BAR_MINUTES} | range={RANGE_MINUTES}min | "
                    f"live SL guards UNCHANGED")
        logger.info(
            "ORB15 momentum filter: %s | account=%r | rule=%s | symbols=%s"
            % ("ACTIVE" if MOMENTUM_FILTER_ACTIVE else "OFF",
               ACCOUNT_PROFILE or "<default>", MOMENTUM_RULE or "-",
               ",".join(MOMENTUM_FILTER_SYMBOLS) if MOMENTUM_FILTER_ACTIVE
               else "-"))
        logger.info("ORB15 exit scheme: %s | tp_r=%.1f be=%.1f trail=%.1f%s"
                    % (ACCOUNT_EXIT["exit_mode"], ACCOUNT_EXIT["tp_r"],
                       ACCOUNT_EXIT["be_trigger_r"],
                       ACCOUNT_EXIT["trail_buffer_r"],
                       "  (eod_trail = no fixed target)"
                       if ACCOUNT_EXIT["exit_mode"] == "eod_trail" else ""))
        if MOMENTUM_RULE == "u_shape":
            logger.info("ORB15 u_shape: trade if disp<%.2f OR (disp>=%.2f and "
                        "body>=%.2f); reject the mushy middle"
                        % (MOMENTUM_QUIET_MAX, MOMENTUM_DISP_MIN,
                           MOMENTUM_BODY_MIN))
        elif MOMENTUM_RULE == "pass_only":
            logger.info("ORB15 pass_only: trade if disp>=%.2f and body>=%.2f "
                        "(15-trade validation from 2026-10-07 12:03:35)"
                        % (MOMENTUM_DISP_MIN, MOMENTUM_BODY_MIN))

    def initialize(self):
        """dd_protection is built inside the parent's initialize(), not
        __init__ -- applying the risk cap any earlier silently does nothing."""
        ok = super().initialize()
        if ok:
            _apply_risk_cap(self)
        return ok

    def _can_trade(self, symbol: str):
        """Same checks as the parent, but the daily cap counts only THIS
        session's symbols. Implemented by swapping the counter the parent
        reads, so every other guard it applies is untouched.

        The count is DERIVED from trade_taken_today (which the parent already
        maintains and resets daily) rather than kept as new state -- nothing
        extra to get out of sync on a restart."""
        grp = SESSION_GROUP.get(symbol)
        if grp is None:
            return super()._can_trade(symbol)
        taken = sum(1 for sym, done in self.trade_taken_today.items()
                    if done and SESSION_GROUP.get(sym) == grp)
        if taken >= MAX_TRADES_PER_SESSION:
            return False, f"max {grp}-session trades reached ({taken})"
        saved = self.state.daily_trades
        try:
            self.state.daily_trades = taken      # parent's cap now sees session count
            return super()._can_trade(symbol)
        finally:
            self.state.daily_trades = saved

    def _orb15_session_window(self, symbol, m1, config):
        """Market-local open/close -> broker time; mirrors the parent's
        DST-proof logic, with its static fallback and first-bar snap."""
        try:
            off = self._get_broker_utc_offset()
            btz = _tz(_td(hours=off))
            today = datetime.now(pytz.UTC).astimezone(btz).replace(tzinfo=None).date()
        except Exception:
            off, today = None, datetime.now().date()

        if off is not None and config.get("timezone"):
            try:
                ltz = pytz.timezone(config["timezone"])
                lo = ltz.localize(datetime(today.year, today.month, today.day,
                                           config["market_open_hour"],
                                           config["market_open_minute"]))
                lc = ltz.localize(datetime(today.year, today.month, today.day,
                                           config["market_close_hour"],
                                           config["market_close_minute"]))
                b = _tz(_td(hours=off))
                return (pd.Timestamp(lo.astimezone(b).replace(tzinfo=None)),
                        pd.Timestamp(lc.astimezone(b).replace(tzinfo=None)))
            except Exception as e:
                logger.warning(f"{symbol}: ORB15 dynamic session calc failed ({e})")

        s = pd.Timestamp(year=today.year, month=today.month, day=today.day,
                         hour=config["mt5_open_hour"], minute=config["mt5_open_minute"])
        e = pd.Timestamp(year=today.year, month=today.month, day=today.day,
                         hour=config["mt5_close_hour"], minute=config["mt5_close_minute"])
        tb = m1[m1.index.date == today]
        if len(tb):
            first = tb.index[0]
            gap = (s - first).total_seconds()
            if 30 * 60 < gap <= 90 * 60:
                logger.info(f"{symbol}: ORB15 session snapped to first bar {first}")
                dur = e - s
                s = pd.Timestamp(first)
                e = s + dur
        return s, e

    def _generate_signal(self, symbol: str) -> Optional[dict]:
        if symbol not in ORB15_SYMBOLS:
            return None
        config = get_index_config(symbol)
        if not config:
            return None

        m1 = self.mt5.get_ohlc_data(symbol, mt5.TIMEFRAME_M1, count=800)
        if m1 is None or m1.empty:
            return None
        s_start, s_end = self._orb15_session_window(symbol, m1, config)
        sess = m1[(m1.index >= s_start) & (m1.index <= s_end)]
        if len(sess) < RANGE_MINUTES + 5:
            return None

        df = sess.resample(f"{BAR_MINUTES}min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        if len(df) < 3:
            return None

        # strategy_v3 keeps a per-symbol range/ATR history for its optional
        # filters; those are OFF here (rel_range_min=0, stop_mode="range"), so
        # clearing it each call keeps this stateless and restart-safe.
        strategy_v3.reset_history()
        trades = strategy_v3.generate_signals(
            symbol, df, self._get_htf_trend(symbol), 0.0, _params())
        if not trades:
            return None

        t = trades[-1]
        age = df.index[-1] - pd.Timestamp(t.entry_time)
        if age > pd.Timedelta(minutes=BAR_MINUTES * 2):
            return None      # stale: only act on the bar that just closed

        # MOMENTUM GATE -- after staleness, so the bar is known closed.
        ok, why = _momentum_ok(df, t.signal_time, symbol,
                               last_m1=sess.index[-1] if len(sess) else None)
        if not ok:
            logger.info(f"{symbol}: ORB15 momentum filter REJECTED signal "
                        f"at {t.signal_time} -- {why}")
            return None

        return {
            "direction": t.direction,
            "entry_price": float(t.entry_price),
            "stop_loss": float(t.stop_loss),
            "tp": float(t.tp_price),
            "risk_points": float(t.risk_points),
            "entry_time": t.entry_time,
            "htf_trend": self._get_htf_trend(symbol) or "",
            "orb_range": float(t.orb_range),
            "signal_time": str(t.signal_time),
            "signal_price": float(t.entry_price),
            "breakout_time": str(t.signal_time),
            "session_end": str(s_end),
        }


def main():
    for s in ORB15_SYMBOLS:
        try:
            mt5.symbol_select(s, True)
        except Exception:
            pass
    ORB15Trader().run()


if __name__ == "__main__":
    main()
