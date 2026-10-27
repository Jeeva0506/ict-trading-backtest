"""
Performance metrics for options backtesting.
All metrics are computed on after-cost P&L.
"""
import numpy as np
import pandas as pd
from typing import List


def compute_metrics(trades: List[dict], capital: float = 1_000_000) -> dict:
    """
    Compute full performance metrics from a list of closed trades.

    Each trade dict must have:
        pnl_net   : float  (net P&L after costs, Rs)
        entry_date: str/date
        exit_date : str/date
        direction : 'call' or 'put'
        exit_type : 'TP' | 'SL' | 'TIME'

    Returns dict with all key metrics.
    """
    if not trades:
        return {"error": "No trades"}

    pnls   = np.array([t["pnl_net"] for t in trades])
    wins   = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    n_total  = len(pnls)
    n_wins   = len(wins)
    n_losses = len(losses)
    win_rate = n_wins / n_total if n_total else 0

    gross_profit = wins.sum()  if len(wins)   else 0
    gross_loss   = abs(losses.sum()) if len(losses) else 1e-9

    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    avg_win  = wins.mean()   if len(wins)   else 0
    avg_loss = losses.mean() if len(losses) else 0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    # Equity curve
    equity = capital + np.cumsum(pnls)
    peak   = np.maximum.accumulate(equity)
    dd     = (equity - peak) / peak * 100  # percentage drawdown
    max_dd = dd.min()

    # Calmar ratio (annualised return / max drawdown)
    total_return = pnls.sum() / capital * 100
    entry_dates  = pd.to_datetime([t["entry_date"] for t in trades])
    years        = (entry_dates.max() - entry_dates.min()).days / 365.25
    cagr         = ((1 + pnls.sum() / capital) ** (1 / max(years, 0.1)) - 1) * 100
    calmar        = cagr / abs(max_dd) if max_dd != 0 else np.inf

    # Sharpe (annualised, risk-free = 6.5%)
    rfr_daily = 0.065 / 252
    # Convert trade P&L to daily returns (approximate: distribute over holding period)
    holding_days = np.array([max((pd.to_datetime(t["exit_date"]) -
                                   pd.to_datetime(t["entry_date"])).days, 1)
                              for t in trades])
    daily_equiv  = pnls / (holding_days * capital)  # approximate daily return
    sharpe = ((daily_equiv.mean() - rfr_daily) /
              daily_equiv.std() * np.sqrt(252)) if daily_equiv.std() > 0 else 0

    # Exit type breakdown
    exit_types = {}
    for t in trades:
        et = t.get("exit_type", "UNK")
        exit_types[et] = exit_types.get(et, 0) + 1

    return {
        "n_trades"        : n_total,
        "win_rate_pct"    : round(win_rate * 100, 2),
        "profit_factor"   : round(profit_factor, 3),
        "gross_profit"    : round(gross_profit, 2),
        "gross_loss"      : round(gross_loss, 2),
        "net_pnl"         : round(pnls.sum(), 2),
        "avg_win"         : round(avg_win, 2),
        "avg_loss"        : round(avg_loss, 2),
        "expectancy"      : round(expectancy, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "total_return_pct": round(total_return, 2),
        "cagr_pct"        : round(cagr, 2),
        "sharpe"          : round(sharpe, 3),
        "calmar"          : round(calmar, 3),
        "exit_breakdown"  : exit_types,
        "years"           : round(years, 2),
    }


def print_report(metrics: dict, label: str = "Backtest") -> None:
    sep = "-" * 50
    print(f"\n{'='*50}")
    print(f"  {label}")
    print(f"{'='*50}")
    if "error" in metrics:
        print(f"  ERROR: {metrics['error']}")
        return
    print(f"  Period       : {metrics['years']:.1f} years")
    print(f"  Trades       : {metrics['n_trades']}")
    print(sep)
    print(f"  Win Rate     : {metrics['win_rate_pct']:.1f}%  {'[PASS OK]' if metrics['win_rate_pct'] > 55 else '[FAIL XX]'}")
    print(f"  Profit Factor: {metrics['profit_factor']:.3f}  {'[PASS OK]' if metrics['profit_factor'] > 2.0 else '[FAIL XX]'}")
    print(sep)
    print(f"  Net P&L      : Rs{metrics['net_pnl']:>12,.2f}")
    print(f"  CAGR         : {metrics['cagr_pct']:.1f}%")
    print(f"  Sharpe       : {metrics['sharpe']:.3f}")
    print(f"  Calmar       : {metrics['calmar']:.3f}")
    print(f"  Max Drawdown : {metrics['max_drawdown_pct']:.1f}%")
    print(sep)
    print(f"  Avg Win      : Rs{metrics['avg_win']:>10,.2f}")
    print(f"  Avg Loss     : Rs{metrics['avg_loss']:>10,.2f}")
    print(f"  Expectancy   : Rs{metrics['expectancy']:>10,.2f}")
    print(sep)
    print(f"  Exit breakdown: {metrics['exit_breakdown']}")
    print(f"{'='*50}")
    if metrics['profit_factor'] > 2.0 and metrics['win_rate_pct'] > 55:
        print("  >>> STRATEGY PASSES 3-YR GATE — proceed to 10-yr validation")
    else:
        print("  >>> STRATEGY DOES NOT PASS GATE — need further optimization")
    print(f"{'='*50}\n")
