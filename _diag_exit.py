#!/usr/bin/env python3
"""Diagnose exit reasons and WR breakdown for ICT v4 results."""
import pandas as pd
import numpy as np

for sym in ['EURUSD', 'USDJPY', 'XAUUSD']:
    df = pd.read_csv(f'reports/ob_ict_v4_6mo_is/{sym}_ict_v4_trades.csv')
    r = df['R']
    print(f'\n{"="*50}')
    print(f'  {sym} ({len(df)} trades)')
    print(f'{"="*50}')
    
    # Exit reason distribution
    print('Exit reasons:')
    for reason, g in df.groupby('exit_reason'):
        print(f'  {reason:12s}: n={len(g):3d}, avgR={g["R"].mean():+.2f}, sumR={g["R"].sum():+.1f}')
    
    # Win/BE/Loss breakdown
    print(f'\nR > 0 (win):  {(r>0).sum():3d} ({(r>0).mean():.1%})')
    print(f'R == 0 (BE):  {(r==0).sum():3d} ({(r==0).mean():.1%})')
    print(f'R < 0 (loss): {(r<0).sum():3d} ({(r<0).mean():.1%})')
    
    wins = r[r > 0]
    if len(wins):
        print(f'Avg winner: {wins.mean():.2f}R, Median: {wins.median():.2f}R')
        print(f'Top winners: {[round(x,1) for x in sorted(wins.values, reverse=True)[:8]]}')
    
    # How many reached 1R (any R >= 0 means reached 1R for Model C)
    reached_1r = (r >= 0).sum()
    print(f'Reached 1R (R>=0): {reached_1r} ({reached_1r/len(r):.1%})')
    
    # Direction breakdown  
    for d in ['LONG', 'SHORT']:
        sub = df[df['direction'] == d]
        if len(sub) == 0:
            continue
        sr = sub['R']
        print(f'\n  {d}: {len(sub)} trades, WR {(sr>0).mean():.1%}, SumR {sr.sum():+.1f}')
        reasons = sub['exit_reason'].value_counts()
        for reason, count in reasons.items():
            print(f'    {reason}: {count}')

    # Risk analysis
    if 'entry_price' in df.columns and 'sl' in df.columns:
        risk_abs = abs(df['entry_price'] - df['sl'])
        print(f'\nRisk stats: median={risk_abs.median():.4f}, max={risk_abs.max():.4f}, min={risk_abs.min():.4f}')
