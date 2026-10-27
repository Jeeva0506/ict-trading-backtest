"""
Zerodha Equity Delivery Transaction Cost Model
Reference: https://zerodha.com/charges

Delivery (CNC) trades:
  Brokerage    : ZERO (free delivery on Zerodha)
  STT          : 0.1% on both buy AND sell side
  Exchange (NSE): 0.00325% each side
  SEBI charges : 0.0001% of turnover each side
  GST          : 18% on (brokerage + exchange + SEBI)
  Stamp duty   : 0.015% on buy turnover only
"""


def equity_round_trip_cost(buy_price: float, sell_price: float, qty: int) -> float:
    """
    Total cost for one round-trip equity delivery trade.

    Parameters
    ----------
    buy_price  : price paid per share at entry
    sell_price : price received per share at exit
    qty        : number of shares

    Returns
    -------
    float : total cost in Rs (always positive)
    """
    buy_turnover  = buy_price  * qty
    sell_turnover = sell_price * qty

    brokerage = 0.0  # free for delivery

    stt      = 0.001  * (buy_turnover + sell_turnover)   # 0.1% both sides
    exchange = 0.0000325 * (buy_turnover + sell_turnover) # 0.00325% each
    sebi     = 0.000001  * (buy_turnover + sell_turnover) # 0.0001% each
    gst      = 0.18 * (brokerage + exchange + sebi)
    stamp    = 0.00015 * buy_turnover                     # 0.015% on buy only

    return brokerage + stt + exchange + sebi + gst + stamp


def cost_per_share(buy_price: float, sell_price: float, qty: int) -> float:
    return equity_round_trip_cost(buy_price, sell_price, qty) / qty
