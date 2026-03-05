#!/usr/bin/env python3
"""Generate weekly trading report from trade journal."""
import pandas as pd
import numpy as np

df = pd.read_excel('live/reports/trade_journal.xlsx')

print('='*70)
print('  WEEKLY TRADING REPORT: Feb 23 - Feb 28, 2026')
print('='*70)

print(f'\nTotal trades: {len(df)}')
print(f'Symbols traded: {list(df["Symbol"].unique())}')

total_pnl = df["P&L"].sum()
print(f'Total P&L: ${total_pnl:,.2f}')

# Per-trade summary
print(f'\n{"#":>3} {"Date":>12} {"Symbol":>8} {"Dir":>5} {"Entry":>10} {"Exit":>10} {"Exit Reason":>30} {"P&L($)":>10} {"RR":>6} {"Duration":>10}')
print('-'*115)
for _, t in df.iterrows():
    pnl = t["P&L"]
    print(f'{t["Trade ID"]:>3} {str(t["Date"]):>12} {t["Symbol"]:>8} {t["Direction"]:>5} {t["Actual Entry"]:>10.2f} {t["Exit Price"]:>10} {str(t["Exit Reason"]):>30} {pnl:>10.2f} {t["Actual RR"]:>6.2f} {str(t["Time in Trade"]):>10}')

print(f'\n--- Account ---')
print(f'Starting balance: $6,000.00')
ending = df.iloc[-1]["Balance After"]
print(f'Ending balance: ${ending:,.2f}')
print(f'Weekly return: {(ending-6000)/6000*100:.2f}%')

# Win/Loss
wins = (df["P&L"] > 0).sum()
losses = (df["P&L"] < 0).sum()
be = (df["P&L"] == 0).sum()
print(f'\nWins: {wins}, Losses: {losses}, Breakeven: {be}')
wr = wins/len(df)*100 if len(df) else 0
print(f'Win Rate: {wr:.1f}%')

# MFE / MAE analysis
print(f'\n--- Trade Quality ---')
for _, t in df.iterrows():
    tid = t["Trade ID"]
    sym = t["Symbol"]
    mfe_r = t.get("MFE (R)", np.nan)
    mae_r = t.get("MAE (R)", np.nan)
    rr = t["Actual RR"]
    pnl = t["P&L"]
    risk = t["Risk Amount"]
    print(f'  Trade #{tid} ({sym}):')
    print(f'    Entry: {t["Actual Entry"]}, SL: {t["Stop Loss"]}, TP: {t["TP"]}')
    print(f'    MFE: {mfe_r:.2f}R, MAE: {mae_r:.2f}R' if not (mfe_r != mfe_r or mae_r != mae_r) else f'    MFE: N/A, MAE: N/A')
    print(f'    Actual RR: {rr:.2f}, P&L: ${pnl:.2f}, Risk: ${risk:.2f}')
    print(f'    Duration: {t["Time in Trade"]}, Exit: {t["Exit Reason"]}')
    # Check P&L vs expected
    expected_pnl_at_rr = rr * risk
    if pnl != 0 and abs(pnl - expected_pnl_at_rr) > 1.0:
        print(f'    [FLAG] P&L mismatch: got ${pnl:.2f}, expected ~${expected_pnl_at_rr:.2f} at {rr:.2f}R')

# Flags
print(f'\n--- FLAGS ---')
flag_count = 0
for _, t in df.iterrows():
    notes = str(t.get("Notes", ""))
    exit_r = str(t.get("Exit Reason", ""))
    pnl = t["P&L"]
    rr = t["Actual RR"]
    symbol = t["Symbol"]
    tid = t["Trade ID"]
    slip = t.get("Slippage Pts", 0)
    risk_pts = t.get("R (Risk Pts)", 0)
    
    if "bug" in notes.lower() or "corrected" in notes.lower():
        print(f'  [!] Trade #{tid} ({symbol}): CORRECTED ENTRY - {notes[:100]}')
        flag_count += 1
    if "EMERGENCY" in exit_r:
        print(f'  [!] Trade #{tid} ({symbol}): Emergency close - {exit_r}')
        flag_count += 1
    if pnl == 0 and rr == 0:
        print(f'  [?] Trade #{tid} ({symbol}): P&L=$0 RR=0.00 exit="{exit_r}" - Trade closed flat')
        flag_count += 1
    mfe_r = t.get("MFE (R)", np.nan)
    if mfe_r != mfe_r:  # NaN check
        print(f'  [?] Trade #{tid} ({symbol}): MFE is NaN - tracking may have failed')
        flag_count += 1
    if abs(slip) > 0 and risk_pts > 0 and abs(slip)/risk_pts > 0.3:
        print(f'  [?] Trade #{tid} ({symbol}): High slippage {slip:.1f} pts vs risk {risk_pts:.1f} pts ({abs(slip)/risk_pts*100:.0f}%)')
        flag_count += 1
    # SL hit but P&L=0 or TP hit but P&L=0
    if "TP" in exit_r and pnl == 0:
        print(f'  [!!] Trade #{tid} ({symbol}): Exit says TP/SL but P&L=$0 - P&L RECORDING BUG?')
        flag_count += 1
    # Check BE hit column
    be_hit = str(t.get("BE Hit?", ""))
    if "bug" in be_hit.lower():
        print(f'  [!] Trade #{tid} ({symbol}): BE Hit column says: {be_hit}')
        flag_count += 1

if flag_count == 0:
    print('  No flags detected.')
else:
    print(f'\n  Total flags: {flag_count}')

# Backtest comparison
print(f'\n--- BACKTEST COMPARISON ---')
print(f'Expected from backtest (6 indices, ~8 trades/week):')
print(f'  Backtest WR: ~48%')
print(f'  Backtest avg RR: ~2.5R on winners')
print(f'  Live trades this week: {len(df)} (vs expected ~8)')
print(f'  Live WR: {wr:.0f}% (vs backtest ~48%)')
avg_winner = df[df["P&L"] > 0]["Actual RR"].mean() if wins > 0 else 0
print(f'  Live avg winner RR: {avg_winner:.2f}R (vs backtest ~2.5R)')
print(f'  NOTE: Only 3 trades in first week - too small for statistical comparison')
