import openpyxl

wb = openpyxl.load_workbook('live/reports/trade_journal.xlsx')
ws = wb.active
headers = [cell.value for cell in ws[1]]

# Find column indices
col = {h: i+1 for i, h in enumerate(headers)}

# Fix Trade #4 (row 5) - US30
row = 5
# Exit price: was 13973.8 (wrong symbol), should be 47811.12 (SL hit)
ws.cell(row=row, column=col['Exit Price']).value = 47811.12
# Exit reason: was BROKER(r=4), should be SL_HIT
ws.cell(row=row, column=col['Exit Reason']).value = 'SL_HIT'
# P&L: was -149.56 (wrong deals), should be -87.66 (MT5 actual)
ws.cell(row=row, column=col['P&L']).value = -87.66
# Actual RR: was -3.28, should be -1.92 (actual_R=54.54 vs designed R=28.5, loss at SL)
ws.cell(row=row, column=col['Actual RR']).value = -1.92
# Balance after: was 5751.26, should be 5900.82
ws.cell(row=row, column=col['Balance After']).value = 5900.82

wb.save('live/reports/trade_journal.xlsx')
print("Trade #4 journal entry corrected:")
print(f"  Exit Price: 47811.12 (was 13973.8)")
print(f"  Exit Reason: SL_HIT (was BROKER(r=4))")
print(f"  P&L: -87.66 (was -149.56)")
print(f"  Actual RR: -1.92 (was -3.28)")
print(f"  Balance After: 5900.82 (was 5751.26)")
