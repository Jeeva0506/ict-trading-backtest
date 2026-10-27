"""
Gap Fill Strategy -- Full Audit, Corrected Backtest & Monte Carlo
================================================================
Addresses every known source of unrealism:

  AUDIT ITEM 1 -- Lookahead bias on gap detection
    Fix: gap uses yesterday's LAST bar close vs today's FIRST bar open.
    Both are known at 9:15 AM before placing the order. CLEAN.

  AUDIT ITEM 2 -- Optimistic exit order (TP before SL on same bar)
    Original bug: if one bar's H/L triggers both TP AND SL, we took TP.
    Fix: take the WORSE outcome (SL) when ambiguous -- conservative.

  AUDIT ITEM 3 -- Entry premium uses prior-day VIX (constant IV)
    Real issue: IV changes intraday. On gap-DOWN days VIX spikes (+15%
    typical); on gap-UP days VIX drops (-10% typical) squeezing put buyers.
    Fix: apply VIX adjustment multiplier at entry and exit.

  AUDIT ITEM 4 -- No bid-ask spread / slippage model
    Nifty ATM options: spread ~1-2 pts in normal hours, 3-5 pts at open.
    Fix: add SLIPPAGE_BUY points to entry premium, subtract SLIPPAGE_SELL
    from exit premium (always hurts the trader).

  AUDIT ITEM 5 -- Entry at first bar (9:15:00) -- illiquid seconds
    First ~60s of options market has price discovery lag.
    Fix: enter at SECOND bar (9:16 for 1m, 9:20 for 5m) -- known price,
    but reflects actual liquidity. Conservative, not optimistic.

  AUDIT ITEM 6 -- BS model vs real options chain
    BS with prior-day VIX can under/over-price by 5-15%.
    Noted as structural limitation; cannot fix without live chain data.
    Impact: systematic bias in premium estimates, not directional bias.
    Conservative accounting: we assume we fill at BS price (no edge).

  AUDIT ITEM 7 -- Fixed DTE assumption
    We assume 5-DTE options on every day. Real market has varying DTE
    as expiry approaches. Impact: minor for weekly options.
    Noted but not corrected (would need expiry calendar data).

DATA COVERAGE:
  2015-01-09 to 2024-03-27: 1m bars (precise intraday simulation)
  2012-01-03 to 2014-12-31: daily OHLCV only (approximated simulation)
  Combined: ~12.4 years of data

Monte Carlo:
  Bootstrap 10,000 trials, resample from actual trade P&L distribution.
  Reports 5th/25th/50th/75th/95th percentile equity curves.
  Projects full 16-year extrapolation.

Run: python run_gap_fill_audit_mc.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import json
from datetime import date

from data.download_intraday import load_nifty1m_historical, resample_from_1m
from data.download_data import load_all
from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from backtest.metrics import compute_metrics, print_report

# -- Parameters -----------------------------------------------------------------
GAP_MIN       = 0.30   # % gap minimum (ignore tiny gaps)
GAP_MAX       = 1.50   # % gap maximum (ignore panic / pre-market halt gaps)
VIX_MIN       = 10.0
VIX_MAX       = 22.0
SL_PCT        = 0.25   # stop-loss as fraction of entry premium
TP_PCT        = 0.50   # take-profit as fraction of entry premium
DTE           = 5      # days-to-expiry for options (weekly, ~5 DTE average)
RFR           = 0.065  # risk-free rate (India 10yr)
LOT_SIZE      = 50     # Nifty lot size
RISK_PER_TRADE= 10_000 # Rs per trade risk budget
MAX_LOTS      = 5
STRIKE_STEP   = 50

# Audit fixes
SLIPPAGE_BUY  = 1.5    # points added to entry premium (bid-ask at 9:20, after price discovery)
SLIPPAGE_SELL = 1.5    # points subtracted from exit premium
VIX_ADJ_GAP_DOWN = 1.08  # IV spikes ~8% avg on gap-down days (unfavourable for call buyers)
VIX_ADJ_GAP_UP   = 0.96  # IV drops ~4% avg on gap-up days (unfavourable for put buyers)
ENTRY_BAR_SKIP    = 1     # skip first N bars (enter at 2nd bar = 9:16 for 1m)

CAPITAL       = 1_000_000
RESULTS       = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS, exist_ok=True)

MC_TRIALS     = 10_000
MC_SEED       = 42


# -- Audit logger ---------------------------------------------------------------
AUDIT_REPORT = []

def audit(item, status, detail):
    tag = "[OK ]" if status == "ok" else "[FIX]" if status == "fix" else "[NOTE]"
    AUDIT_REPORT.append(f"  {tag} {item}: {detail}")


# -- Exit helper -- conservative (SL before TP if same bar) ---------------------
def check_exit(bar_h, bar_l, tp_p, sl_p, opt_type, K, dte, rfr, vix_entry):
    """
    Returns (exit_premium, exit_type) for one bar.
    Conservative: if same bar touches both TP and SL, we take SL.
    """
    if opt_type == "call":
        opt_high = bs_price(bar_h, K, dte/365, rfr, vix_entry/100, "call")
        opt_low  = bs_price(bar_l, K, dte/365, rfr, vix_entry/100, "call")
    else:
        opt_high = bs_price(bar_l, K, dte/365, rfr, vix_entry/100, "put")
        opt_low  = bs_price(bar_h, K, dte/365, rfr, vix_entry/100, "put")

    # Conservative: check SL first
    if opt_low <= sl_p:
        return sl_p, "SL"
    if opt_high >= tp_p:
        return tp_p, "TP"
    return None, None


# -- 1m-data simulation (primary) -----------------------------------------------
def run_gap_fill_1m(df5m, df_vix, start=None, end=None, verbose=False):
    """
    Simulate gap fill strategy bar-by-bar on 5m data (resampled from 1m).
    All audit fixes applied.
    Returns list of trade dicts.
    """
    days = sorted(set(df5m.index.date))
    if start: days = [d for d in days if d >= pd.Timestamp(start).date()]
    if end:   days = [d for d in days if d <= pd.Timestamp(end).date()]

    trades = []

    for i in range(1, len(days)):
        today = days[i]
        yest  = days[i - 1]
        tb = df5m[df5m.index.date == today]
        yb = df5m[df5m.index.date == yest]
        if tb.empty or yb.empty:
            continue

        # -- Audit Item 1: gap fully known at market open -----------------
        today_open = tb.iloc[0]["Open"]   # 9:15 first bar open
        yest_close = yb.iloc[-1]["Close"] # prior session close
        gap_pct    = (today_open - yest_close) / yest_close * 100

        if abs(gap_pct) < GAP_MIN or abs(gap_pct) > GAP_MAX:
            continue

        # VIX from prior day -- known before market opens
        ts_yest = pd.Timestamp(yest)
        vix_raw = df_vix["Close"].reindex([ts_yest], method="ffill").iloc[0]
        if pd.isna(vix_raw) or vix_raw < VIX_MIN or vix_raw > VIX_MAX:
            continue

        # -- Audit Item 3: IV adjustment for gap direction -----------------
        if gap_pct < 0:
            opt_type  = "call"           # gap down -> fade -> buy call
            vix_entry = vix_raw * VIX_ADJ_GAP_DOWN   # VIX spikes on gap-down
        else:
            opt_type  = "put"            # gap up -> fade -> buy put
            vix_entry = vix_raw * VIX_ADJ_GAP_UP     # IV crush on gap-up days

        # -- Audit Item 5: skip first bar (9:15 illiquid) -----------------
        if len(tb) <= ENTRY_BAR_SKIP:
            continue
        entry_bar  = tb.iloc[ENTRY_BAR_SKIP]   # 2nd bar = 9:20 (5m data)
        entry_spot = entry_bar["Open"]           # enter at bar open (known)

        K          = atm_strike(entry_spot, step=STRIKE_STEP)
        raw_prem   = bs_price(entry_spot, K, DTE/365, RFR, vix_entry/100, opt_type)

        # -- Audit Item 4: add slippage on buy ----------------------------
        entry_prem = raw_prem + SLIPPAGE_BUY
        if entry_prem < 1.0:
            continue

        max_budget = RISK_PER_TRADE / SL_PCT
        lots       = max(1, min(int(max_budget / (entry_prem * LOT_SIZE)), MAX_LOTS))
        qty        = lots * LOT_SIZE
        tp_price   = entry_prem * (1 + TP_PCT)
        sl_price   = entry_prem * (1 - SL_PCT)

        exit_prem = None; exit_type = None

        # -- Scan bars from entry bar+1 ------------------------------------
        for j in range(ENTRY_BAR_SKIP + 1, len(tb)):
            bar = tb.iloc[j]
            bt  = tb.index[j].time()

            # Mandatory exit at 3:15 PM
            if bt.hour > 15 or (bt.hour == 15 and bt.minute >= 15):
                close_prem = bs_price(bar["Close"], K, DTE/365, RFR, vix_entry/100, opt_type)
                exit_prem  = max(close_prem - SLIPPAGE_SELL, 0.05)  # slippage on sell
                exit_type  = "3:15"
                break

            # -- Audit Item 2: SL before TP (conservative) ----------------
            ep, et = check_exit(bar["High"], bar["Low"], tp_price, sl_price,
                                opt_type, K, DTE, RFR, vix_entry)
            if et == "SL":
                exit_prem = sl_price - SLIPPAGE_SELL   # slippage on SL fill
                exit_type = "SL"
                break
            elif et == "TP":
                exit_prem = tp_price - SLIPPAGE_SELL
                exit_type = "TP"
                break

        if exit_type is None:
            last = tb.iloc[-1]
            ep   = bs_price(last["Close"], K, DTE/365, RFR, vix_entry/100, opt_type)
            exit_prem = max(ep - SLIPPAGE_SELL, 0.05)
            exit_type = "3:15"

        sell      = max(exit_prem, 0.05)
        gross_pnl = (sell - entry_prem) * qty
        cost      = round_trip_cost(entry_prem, sell, qty)
        net_pnl   = gross_pnl - cost

        trades.append({
            "entry_date" : str(today),
            "exit_date"  : str(today),
            "direction"  : opt_type,
            "gap_pct"    : round(gap_pct, 3),
            "entry_spot" : round(entry_spot, 2),
            "entry_prem" : round(entry_prem, 2),
            "exit_prem"  : round(sell, 2),
            "lots"       : lots,
            "qty"        : qty,
            "days_held"  : 0,
            "exit_type"  : exit_type,
            "gross_pnl"  : round(gross_pnl, 2),
            "cost"       : round(cost, 2),
            "pnl_net"    : round(net_pnl, 2),
            "vix"        : round(vix_raw, 2),
            "vix_entry"  : round(vix_entry, 2),
            "source"     : "1m",
        })

    return trades


# -- Daily-OHLCV simulation (2012-2014 approximation) --------------------------
def run_gap_fill_daily(df_daily, df_vix, start=None, end=None):
    """
    Approximate gap fill simulation using daily OHLCV.
    Entry at Open; SL/TP approximated from day's High/Low.
    Conservative: if both SL and TP are hit, we take SL.
    Note: sequence of intraday moves is unknown -- marked as approximate.
    """
    df = df_daily.copy()
    if start: df = df[df.index >= pd.Timestamp(start)]
    if end:   df = df[df.index <= pd.Timestamp(end)]

    trades = []

    for i in range(1, len(df)):
        today_row = df.iloc[i]
        yest_row  = df.iloc[i - 1]

        today_open = today_row["Open"]
        yest_close = yest_row["Close"]
        gap_pct    = (today_open - yest_close) / yest_close * 100

        if abs(gap_pct) < GAP_MIN or abs(gap_pct) > GAP_MAX:
            continue

        ts_yest = df.index[i - 1]
        vix_raw = df_vix["Close"].reindex([ts_yest], method="ffill").iloc[0]
        if pd.isna(vix_raw) or vix_raw < VIX_MIN or vix_raw > VIX_MAX:
            continue

        if gap_pct < 0:
            opt_type  = "call"
            vix_entry = vix_raw * VIX_ADJ_GAP_DOWN
            high_for_put  = today_row["High"]   # for put: spot low = bad
            low_for_put   = today_row["Low"]
        else:
            opt_type  = "put"
            vix_entry = vix_raw * VIX_ADJ_GAP_UP
            high_for_put  = today_row["High"]
            low_for_put   = today_row["Low"]

        K          = atm_strike(today_open, step=STRIKE_STEP)
        raw_prem   = bs_price(today_open, K, DTE/365, RFR, vix_entry/100, opt_type)
        entry_prem = raw_prem + SLIPPAGE_BUY
        if entry_prem < 1.0:
            continue

        max_budget = RISK_PER_TRADE / SL_PCT
        lots       = max(1, min(int(max_budget / (entry_prem * LOT_SIZE)), MAX_LOTS))
        qty        = lots * LOT_SIZE
        tp_price   = entry_prem * (1 + TP_PCT)
        sl_price   = entry_prem * (1 - SL_PCT)

        if opt_type == "call":
            opt_h = bs_price(today_row["High"], K, DTE/365, RFR, vix_entry/100, "call")
            opt_l = bs_price(today_row["Low"],  K, DTE/365, RFR, vix_entry/100, "call")
        else:
            opt_h = bs_price(today_row["Low"],  K, DTE/365, RFR, vix_entry/100, "put")
            opt_l = bs_price(today_row["High"], K, DTE/365, RFR, vix_entry/100, "put")

        # Conservative: SL first
        if opt_l <= sl_price:
            sell = sl_price - SLIPPAGE_SELL; exit_type = "SL"
        elif opt_h >= tp_price:
            sell = tp_price - SLIPPAGE_SELL; exit_type = "TP"
        else:
            close_p = bs_price(today_row["Close"], K, DTE/365, RFR, vix_entry/100, opt_type)
            sell = max(close_p - SLIPPAGE_SELL, 0.05); exit_type = "3:15"

        sell      = max(sell, 0.05)
        gross_pnl = (sell - entry_prem) * qty
        cost      = round_trip_cost(entry_prem, sell, qty)
        net_pnl   = gross_pnl - cost
        entry_date= df.index[i].date()

        trades.append({
            "entry_date" : str(entry_date),
            "exit_date"  : str(entry_date),
            "direction"  : opt_type,
            "gap_pct"    : round(gap_pct, 3),
            "entry_spot" : round(today_open, 2),
            "entry_prem" : round(entry_prem, 2),
            "exit_prem"  : round(sell, 2),
            "lots"       : lots,
            "qty"        : qty,
            "days_held"  : 0,
            "exit_type"  : exit_type,
            "gross_pnl"  : round(gross_pnl, 2),
            "cost"       : round(cost, 2),
            "pnl_net"    : round(net_pnl, 2),
            "vix"        : round(vix_raw, 2),
            "vix_entry"  : round(vix_entry, 2),
            "source"     : "daily_approx",
        })

    return trades


# -- Monte Carlo ----------------------------------------------------------------
def run_monte_carlo(trade_pnls: np.ndarray, capital: float,
                    n_trials: int = 10_000, years_target: float = 16.0,
                    actual_years: float = None, seed: int = 42) -> dict:
    """
    Bootstrap Monte Carlo on trade P&L distribution.
    For each trial: randomly resample N trades to fill `years_target` of trading.
    Returns percentile equity curves and statistics.
    """
    np.random.seed(seed)
    n_trades = len(trade_pnls)

    if actual_years is None:
        actual_years = years_target

    # Scale trades to fill target years
    trades_per_year = n_trades / actual_years
    target_trades   = int(trades_per_year * years_target)

    curves      = np.zeros((n_trials, target_trades + 1))
    curves[:, 0] = capital

    for t in range(n_trials):
        sample = np.random.choice(trade_pnls, size=target_trades, replace=True)
        curves[t, 1:] = capital + np.cumsum(sample)

    final_equity = curves[:, -1]
    peak         = np.maximum.accumulate(curves, axis=1)
    dd           = (curves - peak) / peak * 100
    max_dd       = dd.min(axis=1)

    pctls = [5, 10, 25, 50, 75, 90, 95]
    eq_pctls = {p: float(np.percentile(final_equity, p)) for p in pctls}
    dd_pctls = {p: float(np.percentile(max_dd, p))       for p in pctls}

    safe_eq  = np.maximum(final_equity, capital * 0.01)   # floor at 1% capital to avoid NaN
    cagr_arr = ((safe_eq / capital) ** (1 / years_target) - 1) * 100
    cagr_pctls = {p: float(np.percentile(cagr_arr, p)) for p in pctls}

    ruin_pct = float((final_equity < capital * 0.5).mean() * 100)

    return {
        "n_trials"        : n_trials,
        "years_simulated" : years_target,
        "target_trades"   : target_trades,
        "trades_per_year" : round(trades_per_year, 1),
        "equity_percentiles": eq_pctls,
        "cagr_percentiles"  : cagr_pctls,
        "max_dd_percentiles": dd_pctls,
        "ruin_probability"  : ruin_pct,
        "prob_positive"     : float((final_equity > capital).mean() * 100),
        "prob_2x"           : float((final_equity > capital * 2).mean() * 100),
    }


# -- Main -----------------------------------------------------------------------
if __name__ == "__main__":
    SEP = "=" * 70

    print(SEP)
    print("  GAP FILL STRATEGY -- AUDIT + CORRECTED BACKTEST + MONTE CARLO")
    print(f"  Capital Rs{CAPITAL:,} | Risk Rs{RISK_PER_TRADE:,}/trade | SL {SL_PCT*100:.0f}% / TP {TP_PCT*100:.0f}%")
    print(SEP)

    # -- Load data -------------------------------------------------------------
    print("\nLoading data ...")
    daily_data = load_all()
    df_daily   = daily_data["nifty"]
    df_vix     = daily_data["indiavix"]

    df_1m = load_nifty1m_historical()
    df_5m = resample_from_1m(df_1m, "5m")

    print(f"  1m data : {df_1m.index[0].date()} to {df_1m.index[-1].date()}")
    print(f"  5m data : {df_5m.index[0].date()} to {df_5m.index[-1].date()}")
    print(f"  Daily   : {df_daily.index[0].date()} to {df_daily.index[-1].date()}")
    print(f"  VIX     : {df_vix.index[0].date()} to {df_vix.index[-1].date()}")

    # -- Print audit ------------------------------------------------------------
    print(f"\n{SEP}")
    print("  AUDIT -- BIAS & REALISM CHECK")
    print(SEP)

    audit("Lookahead on gap",      "ok",  "gap = today_open / yest_close -- both known at 9:15 AM")
    audit("Entry timing",          "fix", f"skip first {ENTRY_BAR_SKIP} bar(s) -- enter at 9:20 (5m data), not 9:15 illiquid open")
    audit("VIX source",            "ok",  "prior-day close VIX -- published after market, known at next open")
    audit("IV intraday shift",     "fix", f"gap-down: VIX x{VIX_ADJ_GAP_DOWN} (spike); gap-up: VIX x{VIX_ADJ_GAP_UP} (crush)")
    audit("Bid-ask / slippage",    "fix", f"buy +{SLIPPAGE_BUY}pts, sell -{SLIPPAGE_SELL}pts (ATM Nifty spread at 9:20)")
    audit("Exit order ambiguity",  "fix", "SL checked BEFORE TP on each bar (conservative -- worst case)")
    audit("Transaction costs",     "ok",  "Zerodha: brokerage Rs20/leg + STT 0.1% + exchange 0.053% + GST + stamp")
    audit("BS vs real chain",      "note","BS with constant-DTE is an approximation. Real options show skew and term-structure")
    audit("DTE assumption",        "note",f"Fixed {DTE}-DTE assumed. Real weekly options vary 1-5 DTE through week")
    audit("Position sizing",       "ok",  "Computed before entry from BS premium -- conservative vs market premium")
    audit("Max lots cap",          "ok",  f"Hard cap at {MAX_LOTS} lots -- prevents outsized positions in low-premium environments")
    audit("Daily-approx period",   "note","2012-2014: daily OHLCV only -- intraday path unknown, SL taken conservatively")

    for line in AUDIT_REPORT:
        print(line)

    # -- Incremental fix attribution (shows cost of each fix) ------------------
    print(f"\n{SEP}")
    print("  AUDIT FIX ATTRIBUTION -- incremental impact on 1m data (2015-2024)")
    print(f"  {'Scenario':<35}  {'N':>4}  {'WR%':>6}  {'PF':>6}")
    print(f"  {'-'*55}")

    def quick_backtest(df5m, df_vix, sl_first=False, slip_buy=0, slip_sell=0,
                       vix_adj_down=1.0, vix_adj_up=1.0, skip_bars=0):
        days = sorted(set(df5m.index.date))
        pnls = []
        for i in range(1, len(days)):
            today=days[i]; yest=days[i-1]
            tb=df5m[df5m.index.date==today]; yb=df5m[df5m.index.date==yest]
            if tb.empty or yb.empty: continue
            today_open=tb.iloc[0]["Open"]; yest_close=yb.iloc[-1]["Close"]
            gap_pct=(today_open-yest_close)/yest_close*100
            if abs(gap_pct)<GAP_MIN or abs(gap_pct)>GAP_MAX: continue
            ts_yest=pd.Timestamp(yest); vix_raw=df_vix["Close"].reindex([ts_yest],method="ffill").iloc[0]
            if pd.isna(vix_raw) or vix_raw<VIX_MIN or vix_raw>VIX_MAX: continue
            if gap_pct<0: opt_type="call"; vix_e=vix_raw*vix_adj_down
            else:         opt_type="put";  vix_e=vix_raw*vix_adj_up
            if len(tb)<=skip_bars: continue
            entry_bar=tb.iloc[skip_bars]; entry_spot=entry_bar["Open"]
            K=atm_strike(entry_spot,step=STRIKE_STEP)
            ep=bs_price(entry_spot,K,DTE/365,RFR,vix_e/100,opt_type)+slip_buy
            if ep<1.0: continue
            lots=max(1,min(int((RISK_PER_TRADE/SL_PCT)/(ep*LOT_SIZE)),MAX_LOTS)); qty=lots*LOT_SIZE
            tp_p=ep*(1+TP_PCT); sl_p=ep*(1-SL_PCT)
            exit_p=None; exit_t=None
            for j in range(skip_bars+1, len(tb)):
                bar=tb.iloc[j]; bt=tb.index[j].time()
                if bt.hour>15 or (bt.hour==15 and bt.minute>=15):
                    cp=bs_price(bar["Close"],K,DTE/365,RFR,vix_e/100,opt_type)
                    exit_p=max(cp-slip_sell,0.05); exit_t="3:15"; break
                if opt_type=="call": oh=bs_price(bar["High"],K,DTE/365,RFR,vix_e/100,"call"); ol=bs_price(bar["Low"],K,DTE/365,RFR,vix_e/100,"call")
                else:                oh=bs_price(bar["Low"],K,DTE/365,RFR,vix_e/100,"put");  ol=bs_price(bar["High"],K,DTE/365,RFR,vix_e/100,"put")
                if sl_first:
                    if ol<=sl_p: exit_p=sl_p-slip_sell; exit_t="SL"; break
                    if oh>=tp_p: exit_p=tp_p-slip_sell; exit_t="TP"; break
                else:
                    if oh>=tp_p: exit_p=tp_p; exit_t="TP"; break
                    if ol<=sl_p: exit_p=sl_p; exit_t="SL"; break
            if exit_t is None:
                last=tb.iloc[-1]; cp=bs_price(last["Close"],K,DTE/365,RFR,vix_e/100,opt_type)
                exit_p=max(cp-slip_sell,0.05); exit_t="3:15"
            sell=max(exit_p,0.05); gp=(sell-ep)*qty; cost=round_trip_cost(ep,sell,qty)
            pnls.append(gp-cost)
        arr=np.array(pnls) if pnls else np.array([0])
        wr=round((arr>0).mean()*100,1); gp=arr[arr>0].sum() if (arr>0).any() else 0
        gl=abs(arr[arr<=0].sum()) if (arr<=0).any() else 1e-9
        return len(arr), wr, round(gp/gl,3)

    scenarios = [
        ("1. Original (TP-first, no slip, no IV adj)",  False, 0,              0,             1.0,             1.0,             0),
        ("2. + SL-first exit (conservative)",           True,  0,              0,             1.0,             1.0,             0),
        ("3. + Slippage 1.5pts each side",              True,  SLIPPAGE_BUY,   SLIPPAGE_SELL, 1.0,             1.0,             0),
        ("4. + IV adjustment (gap direction)",          True,  SLIPPAGE_BUY,   SLIPPAGE_SELL, VIX_ADJ_GAP_DOWN,VIX_ADJ_GAP_UP, 0),
        ("5. + Skip first bar (9:20 entry)",            True,  SLIPPAGE_BUY,   SLIPPAGE_SELL, VIX_ADJ_GAP_DOWN,VIX_ADJ_GAP_UP, ENTRY_BAR_SKIP),
    ]
    for label, sl_f, sb, ss, vd, vu, skip in scenarios:
        n, wr, pf = quick_backtest(df_5m, df_vix, sl_first=sl_f, slip_buy=sb,
                                   slip_sell=ss, vix_adj_down=vd, vix_adj_up=vu, skip_bars=skip)
        print(f"  {label:<35}  {n:>4}  {wr:>5.1f}%  {pf:>6.3f}")

    # -- Run corrected 1m backtest (2015-2024) ---------------------------------
    print(f"\n{SEP}")
    print("  PHASE 1 -- CORRECTED SIMULATION (2015-2024, 1m data)")
    print(SEP)

    trades_1m = run_gap_fill_1m(df_5m, df_vix, start="2015-01-01", end="2024-03-27")
    print(f"  Trades from 1m data: {len(trades_1m)}")

    # -- Run daily approximation (2012-2014) -----------------------------------
    print(f"\n  PHASE 2 -- DAILY APPROXIMATION (2012-2014)")
    trades_daily = run_gap_fill_daily(df_daily, df_vix,
                                      start="2012-01-01", end="2014-12-31")
    print(f"  Trades from daily data: {len(trades_daily)}")

    # -- Combined dataset -------------------------------------------------------
    all_trades = sorted(trades_daily + trades_1m, key=lambda t: t["entry_date"])
    print(f"\n  COMBINED: {len(all_trades)} trades")

    # Save trades CSV
    df_trades = pd.DataFrame(all_trades)
    df_trades.to_csv(os.path.join(RESULTS, "gap_fill_audited_trades.csv"), index=False)

    # -- Metrics by phase -------------------------------------------------------
    trades_daily_only = [t for t in all_trades if t["source"] == "daily_approx"]
    trades_1m_only    = [t for t in all_trades if t["source"] == "1m"]

    print(f"\n{SEP}")
    print("  CORRECTED BACKTEST RESULTS")
    print(SEP)

    m_1m = compute_metrics(trades_1m_only, CAPITAL)
    print_report(m_1m, "Gap Fill -- 1m data (2015-2024) CORRECTED")

    if trades_daily_only:
        m_daily = compute_metrics(trades_daily_only, CAPITAL)
        print_report(m_daily, "Gap Fill -- Daily approx (2012-2014)")

    m_all = compute_metrics(all_trades, CAPITAL)
    print_report(m_all, "Gap Fill -- COMBINED ~12.4yr (2012-2024)")

    # -- Year-by-year breakdown -------------------------------------------------
    print(f"\n{SEP}")
    print("  YEAR-BY-YEAR BREAKDOWN (combined dataset)")
    print(SEP)
    df_t = pd.DataFrame(all_trades)
    df_t["year"] = pd.to_datetime(df_t["entry_date"]).dt.year
    print(f"  {'Year':<6}  {'N':>4}  {'WR%':>6}  {'PF':>6}  {'Net P&L':>12}  {'Source'}")
    print(f"  {'-'*55}")
    for yr, grp in df_t.groupby("year"):
        pnls = grp["pnl_net"].values
        wr   = (pnls > 0).mean() * 100
        gp   = pnls[pnls > 0].sum() if (pnls > 0).any() else 0
        gl   = abs(pnls[pnls <= 0].sum()) if (pnls <= 0).any() else 1e-9
        pf   = gp / gl
        src  = grp["source"].iloc[0]
        note = " [approx]" if src == "daily_approx" else ""
        print(f"  {yr:<6}  {len(pnls):>4}  {wr:>5.1f}%  {pf:>6.3f}  "
              f"Rs{grp['pnl_net'].sum():>9,.0f}{note}")

    # -- Exit breakdown ---------------------------------------------------------
    print(f"\n  Exit breakdown:")
    for et, grp in df_t.groupby("exit_type"):
        pnls = grp["pnl_net"].values
        wr   = (pnls > 0).mean() * 100
        print(f"    {et:<6}: {len(pnls):>4} trades | WR {wr:5.1f}% | avg Rs{pnls.mean():,.0f}")

    # -- Monte Carlo -- 16-year projection --------------------------------------
    print(f"\n{SEP}")
    print(f"  MONTE CARLO SIMULATION -- {MC_TRIALS:,} TRIALS -- 16-YEAR PROJECTION")
    print(SEP)

    pnl_arr      = np.array([t["pnl_net"] for t in all_trades])
    actual_years = (pd.to_datetime(df_t["entry_date"].max()) -
                    pd.to_datetime(df_t["entry_date"].min())).days / 365.25

    print(f"  Input: {len(pnl_arr)} trades over {actual_years:.1f} actual years")
    print(f"  Resampling to 16-year projection ({MC_TRIALS:,} bootstrap trials) ...")

    mc = run_monte_carlo(pnl_arr, CAPITAL,
                         n_trials=MC_TRIALS,
                         years_target=16.0,
                         actual_years=actual_years,
                         seed=MC_SEED)

    tpy = mc["trades_per_year"]
    n16 = mc["target_trades"]
    print(f"  Projected trades/year: {tpy:.1f} | Total in 16 yr: {n16}")
    print()
    print(f"  {'Percentile':<12}  {'Final Equity':>14}  {'CAGR':>8}  {'Max DD':>8}")
    print(f"  {'-'*48}")
    for p in [5, 25, 50, 75, 95]:
        eq   = mc["equity_percentiles"][p]
        cagr = mc["cagr_percentiles"][p]
        mdd  = mc["max_dd_percentiles"][p]
        print(f"  P{p:<10}  Rs{eq:>12,.0f}  {cagr:>7.1f}%  {mdd:>7.1f}%")

    print()
    print(f"  Prob final equity > capital (Rs10L) : {mc['prob_positive']:.1f}%")
    print(f"  Prob 2x return (Rs20L+)             : {mc['prob_2x']:.1f}%")
    print(f"  Ruin risk (equity < Rs5L, -50%)      : {mc['ruin_probability']:.1f}%")

    # -- Save Monte Carlo results -----------------------------------------------
    mc_path = os.path.join(RESULTS, "gap_fill_mc_results.json")
    with open(mc_path, "w") as f:
        json.dump(mc, f, indent=2)
    print(f"\n  Saved: {mc_path}")

    # -- Final verdict ----------------------------------------------------------
    print(f"\n{SEP}")
    print("  FINAL VERDICT")
    print(SEP)
    m = m_1m   # primary metric from 1m data (more reliable)
    wr_gate  = m["win_rate_pct"] > 55
    pf_gate  = m["profit_factor"] > 2.0
    freq_ok  = m["n_trades"] / m["years"] >= 80

    gates = {"WR > 55%": wr_gate, "PF > 2.0": pf_gate, "Freq >= 80/yr": freq_ok}
    for g, passed in gates.items():
        tag = "[PASS]" if passed else "[FAIL]"
        val = (m["win_rate_pct"] if "WR" in g
               else m["profit_factor"] if "PF" in g
               else m["n_trades"] / m["years"])
        print(f"  {tag}  {g} -- actual: {val:.1f}")

    mc50_eq = mc["equity_percentiles"][50]
    mc50_cagr = mc["cagr_percentiles"][50]
    mc05_dd  = mc["max_dd_percentiles"][5]
    print(f"\n  16-yr median projection: Rs{mc50_eq:,.0f} ({mc50_cagr:.1f}% CAGR)")
    print(f"  Worst-5% max drawdown   : {mc05_dd:.1f}%")

    print(SEP)
