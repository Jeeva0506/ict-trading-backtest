import json

with open('live/logs/edge_guard.json') as f:
    eg = json.load(f)

for t in eg['trades']:
    if t.get('symbol') == 'SWI20' and t.get('date') == '2026-02-26':
        t['pnl'] = -46.27
        t['actual_rr'] = -0.50
        print(f"Fixed SWI20: pnl={t['pnl']}, rr={t['actual_rr']}")
    elif t.get('symbol') == 'US2000' and t.get('date') == '2026-02-27':
        t['pnl'] = -103.29
        t['actual_rr'] = -1.13
        print(f"Fixed US2000: pnl={t['pnl']}, rr={t['actual_rr']}")
    elif t.get('symbol') == 'US30' and t.get('date') == '2026-03-03':
        t['pnl'] = -87.66
        t['actual_rr'] = -1.92
        t['balance_after'] = 5900.82
        print(f"Fixed US30: pnl={t['pnl']}, rr={t['actual_rr']}")

# Fix equity curve
eg['equity_curve'] = [6138.04, 6000.0, 6100.0, 6091.77, 5988.48, 5900.82]
print(f"Fixed equity curve: {eg['equity_curve']}")

with open('live/logs/edge_guard.json', 'w') as f:
    json.dump(eg, f, indent=2)
print("Saved.")
