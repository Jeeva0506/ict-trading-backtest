"""
Zerodha Options Transaction Cost Model
Reference: https://zerodha.com/charges

For option BUYING (squaring off before expiry):
  Brokerage    : ₹20 per executed order (flat, buy + sell = ₹40 round-trip)
  STT          : 0.1% of premium on SELL side only
  Exchange (NSE): 0.053% of premium turnover (each leg)
  SEBI charges : ₹10 per crore of turnover = 0.00001 × turnover
  GST          : 18% on (brokerage + exchange + SEBI)
  Stamp duty   : 0.003% of premium on BUY side only

Assumptions:
  - Always square off before expiry (avoid exercise STT of 0.125% on intrinsic)
  - Turnover = premium × quantity per leg
"""

def round_trip_cost(buy_premium: float, sell_premium: float,
                    quantity: int) -> float:
    """
    Total cost for one round-trip options trade.

    Parameters
    ----------
    buy_premium  : premium paid per unit (entry)
    sell_premium : premium received per unit (exit)
    quantity     : total units (lots × lot_size)

    Returns
    -------
    float : total cost in ₹ (always positive, subtracted from P&L)
    """
    buy_turnover  = buy_premium  * quantity
    sell_turnover = sell_premium * quantity

    # ── Brokerage (₹20 per leg, capped at 0.03% but options are flat ₹20) ──
    brokerage = 20 + 20  # buy + sell

    # ── STT (0.1% on sell premium only) ──
    stt = 0.001 * sell_turnover

    # ── NSE Exchange transaction charges (0.053% each leg) ──
    exchange = 0.00053 * (buy_turnover + sell_turnover)

    # ── SEBI charges (₹10 per crore = 0.00001 × turnover) ──
    sebi = 0.00001 * (buy_turnover + sell_turnover)

    # ── GST 18% on brokerage + exchange + SEBI ──
    gst = 0.18 * (brokerage + exchange + sebi)

    # ── Stamp duty 0.003% on buy turnover only ──
    stamp = 0.00003 * buy_turnover

    return brokerage + stt + exchange + sebi + gst + stamp


def cost_per_unit(buy_premium: float, sell_premium: float,
                  quantity: int) -> float:
    """Cost per unit (₹ per share equivalent) — useful for P&L per unit."""
    return round_trip_cost(buy_premium, sell_premium, quantity) / quantity


# ── Quick sanity check ────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Example: buy 1 lot Nifty (50 units) at ₹150 premium, sell at ₹300
    qty = 50
    cost = round_trip_cost(150, 300, qty)
    gross_pnl = (300 - 150) * qty       # ₹7,500
    net_pnl   = gross_pnl - cost
    print(f"Gross P&L : ₹{gross_pnl:,.2f}")
    print(f"Cost      : ₹{cost:,.2f}")
    print(f"Net P&L   : ₹{net_pnl:,.2f}")
    print(f"Cost %    : {cost/gross_pnl*100:.2f}% of gross")
