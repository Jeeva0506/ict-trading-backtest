"""
Black-Scholes synthetic option pricer for backtesting.

Used to reconstruct historical option prices when live options chain
data is unavailable.  India VIX is used as the implied volatility input
(annual %, divided by 100 for the formula).

Includes:
  - Real NIFTY expiry calendar (weekly Thu from Oct-2019, monthly before)
  - ATM / ITM strike selection with Nifty volatility skew adjustment
  - Minimum DTE guard (skip same-day / next-day expiry)

References:
  - Black, F. & Scholes, M. (1973). "The Pricing of Options and Corporate Liabilities"
  - India VIX as proxy IV: NSE methodology paper (2008)
"""
import calendar
import numpy as np
from datetime import date, timedelta
from scipy.stats import norm

# NIFTY weekly expiry started on this Thursday
_WEEKLY_EXPIRY_START = date(2019, 10, 24)
# FINNIFTY weekly expiry started on this Tuesday
_FN_WEEKLY_START = date(2022, 1, 18)
# Minimum DTE: skip expiry day and day-before to avoid pin-risk
_MIN_DTE = 2


# ── Expiry calendar ────────────────────────────────────────────────────────────

def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last occurrence of weekday (0=Mon … 6=Sun) in given month."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def _last_thursday(year: int, month: int) -> date:
    return _last_weekday(year, month, 3)   # Thursday = 3


def next_expiry(entry_date: date) -> date:
    """
    Return the nearest NIFTY option expiry on or after entry_date + MIN_DTE.

    - Before Oct-2019: monthly expiry (last Thursday of month)
    - Oct-2019 onwards: weekly expiry (every Thursday)
    Skips expiry if DTE < _MIN_DTE to avoid same-day / eve-of-expiry pricing.
    """
    min_expiry = entry_date + timedelta(days=_MIN_DTE)

    if entry_date < _WEEKLY_EXPIRY_START:
        # Monthly: last Thursday of current or next month
        exp = _last_thursday(entry_date.year, entry_date.month)
        if exp < min_expiry:
            m, y = entry_date.month + 1, entry_date.year
            if m > 12:
                m, y = 1, y + 1
            exp = _last_thursday(y, m)
        return exp
    else:
        # Weekly: next Thursday on or after min_expiry
        days_to_thu = (3 - min_expiry.weekday()) % 7
        return min_expiry + timedelta(days=days_to_thu)


def dte_to_expiry(entry_date: date) -> int:
    """Calendar days from entry_date to next NIFTY expiry."""
    return (next_expiry(entry_date) - entry_date).days


def is_expiry_day(d: date) -> bool:
    """True if d is an actual NIFTY options expiry day (Thursday)."""
    if d.weekday() != 3:          # must be Thursday
        return False
    if d < _WEEKLY_EXPIRY_START:  # pre-weekly: only last Thursday of month
        return d == _last_thursday(d.year, d.month)
    return True                   # post-weekly: every Thursday


# ── FINNIFTY expiry calendar (Tuesday) ────────────────────────────────────────

def next_expiry_fn(entry_date: date) -> date:
    """
    Nearest FINNIFTY option expiry on or after entry_date + MIN_DTE.
    - Before Jan-2022: monthly expiry (last Tuesday of month)
    - Jan-2022 onwards: weekly expiry (every Tuesday)
    """
    min_expiry = entry_date + timedelta(days=_MIN_DTE)

    if entry_date < _FN_WEEKLY_START:
        exp = _last_weekday(entry_date.year, entry_date.month, 1)   # last Tuesday
        if exp < min_expiry:
            m, y = entry_date.month + 1, entry_date.year
            if m > 12:
                m, y = 1, y + 1
            exp = _last_weekday(y, m, 1)
        return exp
    else:
        days_to_tue = (1 - min_expiry.weekday()) % 7
        return min_expiry + timedelta(days=days_to_tue)


def is_expiry_day_fn(d: date) -> bool:
    """True if d is a FINNIFTY expiry day (Tuesday)."""
    if d.weekday() != 1:          # must be Tuesday
        return False
    if d < _FN_WEEKLY_START:      # pre-weekly: only last Tuesday of month
        return d == _last_weekday(d.year, d.month, 1)
    return True                   # post-weekly: every Tuesday


# ── IV skew (simple Nifty skew model) ─────────────────────────────────────────

def skew_adjusted_iv(vix: float, direction: str, itm_strikes: int = 0) -> float:
    """
    Nifty exhibits a persistent put skew:
      - Calls: IV decreases ~2% per strike ITM (flatter left wing)
      - Puts : IV increases ~3% per strike ITM (steeper left wing / put premium)

    itm_strikes : 0 = ATM, 1 = 1 strike ITM, 2 = 2 strikes ITM
    """
    if direction == "call":
        factor = 1.0 - 0.02 * itm_strikes
    else:
        factor = 1.0 + 0.03 * itm_strikes
    return vix * max(factor, 0.80)   # floor at 80% of VIX


# ── Core BS pricer ─────────────────────────────────────────────────────────────

def bs_price(S: float, K: float, T: float, r: float,
             sigma: float, option_type: str = "call") -> float:
    """Black-Scholes European option price."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, S - K) if option_type == "call" else max(0.0, K - S)

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if option_type == "call":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_delta(S: float, K: float, T: float, r: float,
             sigma: float, option_type: str = "call") -> float:
    """
    Black-Scholes delta — rate of change of option price per 1-point spot move.
    Call delta: 0 to 1  (ATM ≈ 0.50)
    Put delta : -1 to 0 (ATM ≈ -0.50)
    Returns absolute value — sign is implied by direction.
    """
    if T <= 0 or sigma <= 0:
        if option_type == "call":
            return 1.0 if S > K else 0.0
        else:
            return 1.0 if S < K else 0.0

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    if option_type == "call":
        return norm.cdf(d1)          # 0 to 1
    else:
        return abs(norm.cdf(d1) - 1) # absolute value of put delta (0 to 1)


def atm_strike(spot: float, step: float = 50.0) -> float:
    """Nearest ATM strike rounded to exchange tick step (Nifty=50)."""
    return round(spot / step) * step


def select_strike(spot: float, direction: str,
                  step: float = 50.0, itm_strikes: int = 0) -> float:
    """
    Select strike: ATM (itm_strikes=0) or ITM.
    For calls: ITM means K < spot  → K = ATM - itm_strikes * step
    For puts : ITM means K > spot  → K = ATM + itm_strikes * step
    """
    K_atm = atm_strike(spot, step)
    if direction == "call":
        return K_atm - itm_strikes * step
    else:
        return K_atm + itm_strikes * step


def price_option(spot: float, entry_date: date, vix: float,
                 direction: str, rfr: float = 0.065,
                 step: float = 50.0, itm_strikes: int = 0) -> tuple:
    """
    Full option pricing with real expiry, strike selection, and IV skew.

    Returns (strike, dte, iv_used, premium)
    """
    K   = select_strike(spot, direction, step, itm_strikes)
    dte = dte_to_expiry(entry_date)
    T   = dte / 365.0
    iv  = skew_adjusted_iv(vix, direction, itm_strikes) / 100.0
    prem = bs_price(spot, K, T, rfr, iv, direction)
    return K, dte, iv * 100, max(prem, 0.05)


def option_premium(spot: float, dte: int, vix: float,
                   option_type: str = "call",
                   moneyness: float = 1.0,
                   rfr: float = 0.065,
                   strike_step: float = 50.0) -> tuple:
    """Legacy helper — kept for backwards compatibility with older runners."""
    K = atm_strike(spot * moneyness, step=strike_step)
    T = dte / 365.0
    sigma = vix / 100.0
    premium = bs_price(spot, K, T, rfr, sigma, option_type)
    return K, max(premium, 0.05)


# ── Sanity check ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from datetime import date as d
    today = d(2024, 5, 13)   # Monday, next expiry = Thursday 16th
    exp   = next_expiry(today)
    print(f"Entry {today} → expiry {exp} ({dte_to_expiry(today)} DTE)")

    spot = 22200.0
    for direction in ("call", "put"):
        for itm in (0, 1):
            K, dte, iv, prem = price_option(spot, today, 14.5, direction, itm_strikes=itm)
            label = "ATM" if itm == 0 else "1-ITM"
            print(f"  {direction.upper()} {label:5s}  K={K:.0f}  DTE={dte}  "
                  f"IV={iv:.1f}%  Premium=Rs{prem:.2f}")
