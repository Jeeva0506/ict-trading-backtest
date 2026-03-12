"""Analyze all live trades from trade journal"""
import openpyxl
import json

# Read trade journal
wb = openpyxl.load_workbook('live/reports/trade_journal.xlsx')
ws = wb.active
headers = [ws.cell(1, c).value for c in range(1, ws.max_column+1)]

fields = ['Date','Time','Symbol','Direction','Planned Entry','Actual Entry','Slippage Pts',
          'Stop Loss','TP','Lot Size','Risk Amount','R (Risk Pts)','Planned RR',
          'HTF Trend','ORB Range','Exit Price','Exit Time','Exit Reason',
          'P&L','P&L %','Actual RR','MFE (pts)','MAE (pts)','MFE (R)','MAE (R)',
          'Time in Trade','BE Hit?','Balance After','Session','Weekday','Notes']

trades = []
for r in range(2, ws.max_row+1):
    row = {headers[c]: ws.cell(r, c+1).value for c in range(len(headers))}
    trades.append(row)
    tid = row.get('Trade ID', r-1)
    print(f"\n{'='*60}")
    print(f"TRADE #{tid}: {row['Symbol']} {row['Direction']} on {row['Date']}")
    print(f"{'='*60}")
    for k in fields:
        v = row.get(k)
        if v is not None:
            print(f"  {k:20s}: {v}")

# Summary
print(f"\n{'='*60}")
print("PORTFOLIO SUMMARY")
print(f"{'='*60}")
pnls = [t['P&L'] for t in trades if t['P&L'] is not None]
wins = [p for p in pnls if p > 0]
losses = [p for p in pnls if p <= 0]
print(f"  Total Trades:    {len(pnls)}")
print(f"  Wins:            {len(wins)}")
print(f"  Losses:          {len(losses)}")
print(f"  Win Rate:        {len(wins)/len(pnls)*100:.1f}%")
print(f"  Net P&L:         ${sum(pnls):.2f}")
print(f"  Avg Win:         ${sum(wins)/len(wins):.2f}" if wins else "  Avg Win:         N/A")
print(f"  Avg Loss:        ${sum(losses)/len(losses):.2f}" if losses else "  Avg Loss:        N/A")
print(f"  Profit Factor:   {abs(sum(wins))/abs(sum(losses)):.2f}" if losses and wins else "  Profit Factor:   N/A")
print(f"  Best Trade:      ${max(pnls):.2f}")
print(f"  Worst Trade:     ${min(pnls):.2f}")
print(f"  Current Balance: ${trades[-1]['Balance After']:.2f}")
print(f"  Max Drawdown:    {(6000 - min(t['Balance After'] for t in trades if t['Balance After']))/6000*100:.2f}%")

# RR analysis
rrs = [t['Actual RR'] for t in trades if t['Actual RR'] is not None]
print(f"\n  Avg RR:          {sum(rrs)/len(rrs):.2f}R")
print(f"  Best RR:         {max(rrs):.2f}R")
print(f"  Worst RR:        {min(rrs):.2f}R")

# MFE/MAE analysis
print(f"\n--- MFE/MAE Analysis (Did price go your way before SL?) ---")
for t in trades:
    tid = t.get('Trade ID')
    mfe_r = t.get('MFE (R)')
    mae_r = t.get('MAE (R)')
    mfe_pts = t.get('MFE (pts)', 0), 
    mae_pts = t.get('MAE (pts)', 0)
    pnl = t.get('P&L', 0)
    r_pts = t.get('R (Risk Pts)', 0)
    symbol = t.get('Symbol')
    direction = t.get('Direction')
    
    if mfe_r is not None:
        print(f"  Trade #{tid} ({symbol} {direction}): MFE={mfe_r:.2f}R, MAE={mae_r:.2f}R, P&L=${pnl:.2f}")
        if mfe_r > 0.5 and pnl < 0:
            print(f"    ⚠ Price moved {mfe_r:.2f}R in your favor before reversal!")
    else:
        print(f"  Trade #{tid} ({symbol}): MFE/MAE data not available")

# Time analysis
print(f"\n--- Time in Trade ---")
for t in trades:
    print(f"  Trade #{t.get('Trade ID')} ({t['Symbol']}): {t.get('Time in Trade', 'N/A')} | Entry: {t.get('Time')} | Exit: {t.get('Exit Time')}")

# Symbol distribution
print(f"\n--- By Symbol ---")
from collections import defaultdict
by_symbol = defaultdict(list)
for t in trades:
    by_symbol[t['Symbol']].append(t['P&L'])
for sym, pnls_s in by_symbol.items():
    print(f"  {sym}: {len(pnls_s)} trades, Net ${sum(pnls_s):.2f}, WR {len([p for p in pnls_s if p>0])/len(pnls_s)*100:.0f}%")

# Day analysis
print(f"\n--- By Weekday ---")
by_day = defaultdict(list)
for t in trades:
    by_day[t.get('Weekday', 'Unknown')].append(t['P&L'])
for day, pnls_d in by_day.items():
    print(f"  {day}: {len(pnls_d)} trades, Net ${sum(pnls_d):.2f}")

# Session analysis
print(f"\n--- By Session ---")
by_session = defaultdict(list)
for t in trades:
    by_session[t.get('Session', 'Unknown')].append(t['P&L'])
for sess, pnls_s in by_session.items():
    print(f"  {sess}: {len(pnls_s)} trades, Net ${sum(pnls_s):.2f}")

# Slippage analysis
print(f"\n--- Slippage Analysis ---")
for t in trades:
    slip_pts = t.get('Slippage Pts', 0) or 0
    slip_usd = t.get('Slippage $', 0) or 0
    r_pts = t.get('R (Risk Pts)', 0) or 1
    print(f"  Trade #{t.get('Trade ID')} ({t['Symbol']}): Slip={slip_pts:.1f}pts (${slip_usd:.2f}), R={r_pts:.1f}pts, Slip/R={abs(slip_pts)/r_pts*100:.1f}%")

# ORB Range analysis
print(f"\n--- ORB Range (market volatility gauge) ---")
for t in trades:
    orb = t.get('ORB Range', 0) or 0
    r_pts = t.get('R (Risk Pts)', 0) or 1
    print(f"  Trade #{t.get('Trade ID')} ({t['Symbol']}): ORB={orb:.1f}pts, R={r_pts:.1f}pts, R/ORB={r_pts/orb*100:.1f}%")

# Streak analysis  
print(f"\n--- Streak Analysis ---")
streak = 0
max_streak = 0
for p in [t['P&L'] for t in trades]:
    if p < 0:
        streak += 1
        max_streak = max(max_streak, streak)
    else:
        streak = 0
print(f"  Current loss streak: {streak}")
print(f"  Max loss streak:     {max_streak}")

# Backtest comparison
print(f"\n--- vs 3-Year Backtest ---")
print(f"  Backtest: 459 trades, PF 1.82, WR 59%, Max DD 9.6%")
print(f"  Live:     {len(pnls)} trades, PF {abs(sum(wins))/abs(sum(losses)):.2f}" if wins and losses else "", end="")
print(f", WR {len(wins)/len(pnls)*100:.0f}%, DD {(6000-min(t['Balance After'] for t in trades if t['Balance After']))/6000*100:.1f}%")
print(f"  NOTE: 5 trades is far too small for statistical comparison")
