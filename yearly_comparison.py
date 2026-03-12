"""Year-wise Raw vs With-Costs comparison."""
import sys, numpy as np, pandas as pd
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).parent))
from compare_raw_vs_costs import generate_all_signals, run_portfolio, ALL_SYMBOLS
from backtest_live_setup import ACCOUNT_RULES

signals = generate_all_signals()
raw_trades, _ = run_portfolio(signals, zero_costs=True)
cost_trades, _ = run_portfolio(signals, zero_costs=False)
raw_df = pd.DataFrame(raw_trades); raw_df['date'] = pd.to_datetime(raw_df['date']); raw_df['year'] = raw_df['date'].dt.year
cost_df = pd.DataFrame(cost_trades); cost_df['date'] = pd.to_datetime(cost_df['date']); cost_df['year'] = cost_df['date'].dt.year
cap = ACCOUNT_RULES['starting_balance']

print()
print('='*90)
print('  YEAR-WISE DETAILED COMPARISON: RAW vs WITH COSTS')
print('='*90)

for yr in sorted(raw_df['year'].unique()):
    ryr = raw_df[raw_df['year']==yr].reset_index(drop=True)
    cyr = cost_df[cost_df['year']==yr].reset_index(drop=True)

    rw = len(ryr[ryr['result'].isin(['win','partial_win'])])
    rl = len(ryr[ryr['result']=='loss'])
    cw = len(cyr[cyr['result'].isin(['win','partial_win'])])
    cl2 = len(cyr[cyr['result']=='loss'])

    rpnl = ryr['pnl'].sum()
    cpnl = cyr['pnl'].sum()
    ccost = cyr['costs'].sum()

    rgp = ryr[ryr['pnl']>0]['pnl'].sum()
    rgl = ryr[ryr['pnl']<0]['pnl'].sum()
    cgp = cyr[cyr['pnl']>0]['pnl'].sum()
    cgl = cyr[cyr['pnl']<0]['pnl'].sum()
    rpf = abs(rgp/rgl) if rgl!=0 else 0
    cpf = abs(cgp/cgl) if cgl!=0 else 0

    rwr = rw/len(ryr)*100 if len(ryr)>0 else 0
    cwr = cw/len(cyr)*100 if len(cyr)>0 else 0

    ryr2 = ryr.copy(); ryr2['td']=ryr2['date'].dt.date
    cyr2 = cyr.copy(); cyr2['td']=cyr2['date'].dt.date
    rdp = ryr2.groupby('td')['pnl'].sum(); cdp = cyr2.groupby('td')['pnl'].sum()
    rsh = (rdp.mean()/rdp.std()*np.sqrt(252)) if rdp.std()>0 else 0
    csh = (cdp.mean()/cdp.std()*np.sqrt(252)) if cdp.std()>0 else 0

    req = np.maximum.accumulate(ryr['balance'].values)
    rdd = ((req - ryr['balance'].values)/req*100).max()
    ceq = np.maximum.accumulate(cyr['balance'].values)
    cdd = ((ceq - cyr['balance'].values)/ceq*100).max()

    rawp = ryr[ryr['pnl_points']>0]['pnl_points'].mean() if rw>0 else 0
    ralp = abs(ryr[ryr['pnl_points']<0]['pnl_points'].mean()) if rl>0 else 0
    cawp = cyr[cyr['pnl_points']>0]['pnl_points'].mean() if cw>0 else 0
    calp = abs(cyr[cyr['pnl_points']<0]['pnl_points'].mean()) if cl2>0 else 0
    rrr = rawp/ralp if ralp>0 else 0
    crr = cawp/calp if calp>0 else 0

    # Exit reasons
    r_tp = len(ryr[ryr['exit_reason']=='TP'])/len(ryr)*100 if len(ryr)>0 else 0
    r_sl = len(ryr[ryr['exit_reason']=='SL_HIT'])/len(ryr)*100 if len(ryr)>0 else 0
    r_be = len(ryr[ryr['exit_reason']=='SL_BE'])/len(ryr)*100 if len(ryr)>0 else 0
    c_tp = len(cyr[cyr['exit_reason']=='TP'])/len(cyr)*100 if len(cyr)>0 else 0
    c_sl = len(cyr[cyr['exit_reason']=='SL_HIT'])/len(cyr)*100 if len(cyr)>0 else 0
    c_be = len(cyr[cyr['exit_reason']=='SL_BE'])/len(cyr)*100 if len(cyr)>0 else 0

    print(f"\n  === {yr} ===")
    print(f"  {'Metric':<22} {'RAW':>14} {'WITH COSTS':>14} {'Impact':>10}")
    print(f"  {'-'*62}")
    print(f"  {'Trades':<22} {len(ryr):>14} {len(cyr):>14} {len(cyr)-len(ryr):>+10}")
    print(f"  {'Wins / Losses':<22} {str(rw)+'/'+str(rl):>14} {str(cw)+'/'+str(cl2):>14}")
    print(f"  {'Win Rate':<22} {rwr:>13.1f}% {cwr:>13.1f}% {cwr-rwr:>+9.1f}%")
    print(f"  {'Profit Factor':<22} {rpf:>14.2f} {cpf:>14.2f} {cpf-rpf:>+10.2f}")
    print(f"  {'Avg Win (pts)':<22} {rawp:>14.1f} {cawp:>14.1f} {cawp-rawp:>+10.1f}")
    print(f"  {'Avg Loss (pts)':<22} {ralp:>14.1f} {calp:>14.1f} {calp-ralp:>+10.1f}")
    print(f"  {'Actual RR (W/L)':<22} {rrr:>14.2f} {crr:>14.2f} {crr-rrr:>+10.2f}")
    print(f"  {'Net P&L':<22} {'${:,.0f}'.format(rpnl):>14} {'${:,.0f}'.format(cpnl):>14} {'${:,.0f}'.format(cpnl-rpnl):>10}")
    print(f"  {'Total Costs':<22} {'$0':>14} {'${:,.0f}'.format(ccost):>14}")
    print(f"  {'Max DD %':<22} {rdd:>13.1f}% {cdd:>13.1f}% {cdd-rdd:>+9.1f}%")
    print(f"  {'Sharpe':<22} {rsh:>14.2f} {csh:>14.2f} {csh-rsh:>+10.2f}")
    print(f"  {'Exits: TP/SL/BE':<22} {r_tp:.0f}/{r_sl:.0f}/{r_be:.0f}%{'':>5} {c_tp:.0f}/{c_sl:.0f}/{c_be:.0f}%")

    # Per-index for this year
    print(f"\n  {'Index':<10} {'':>4} {'Tr':>4} {'W':>4} {'L':>4} {'WR':>7} {'PF':>7} {'Net PnL':>12} {'Costs':>10}")
    print(f"  {'-'*60}")
    for sym in ALL_SYMBOLS:
        rs2 = ryr[ryr['symbol']==sym].reset_index(drop=True)
        cs2 = cyr[cyr['symbol']==sym].reset_index(drop=True)
        if len(rs2)==0 and len(cs2)==0:
            continue
        for tag, s2 in [('RAW',rs2),('COST',cs2)]:
            if len(s2)==0:
                lbl = sym if tag=='RAW' else ''
                print(f"  {lbl:<10} {tag:>4} {0:>4} {0:>4} {0:>4} {'0.0%':>7} {'0.00':>7} {'$0':>12} {'$0':>10}")
                continue
            w2 = len(s2[s2['result'].isin(['win','partial_win'])])
            l2 = len(s2[s2['result']=='loss'])
            wr2 = w2/len(s2)*100
            gp2 = s2[s2['pnl']>0]['pnl'].sum()
            gl2 = s2[s2['pnl']<0]['pnl'].sum()
            pf2 = abs(gp2/gl2) if gl2!=0 else 0
            pnl2 = s2['pnl'].sum()
            cost2 = s2['costs'].sum()
            lbl = sym if tag=='RAW' else ''
            print(f"  {lbl:<10} {tag:>4} {len(s2):>4} {w2:>4} {l2:>4} {wr2:>6.1f}% {pf2:>6.2f} ${'':>0}{pnl2:>10,.0f} ${'':>0}{cost2:>8,.0f}")

print()
print('='*90)
