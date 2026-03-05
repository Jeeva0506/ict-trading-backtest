"""Check SWI20 across all backtest variants and compare live filters."""
import json
import os

files = [
    'reports/3year_backtest/metrics_3year.json',
    'reports/3year_backtest/metrics_3year_LB15_6idx.json',
    'reports/3year_backtest/metrics_3year_LB5_6idx.json',
    'reports/3year_backtest/metrics_3year_SEED15_6idx.json',
    'reports/3year_backtest/metrics_3year_OLD_6idx.json',
    'reports/3year_backtest/metrics_3year_NEW_6idx.json',
    'reports/3year_backtest/metrics_3year_BASELINE.json',
]

print("=" * 100)
print("ALL BACKTEST VARIANTS — SWI20 + OVERALL")
print("=" * 100)
for f in files:
    try:
        d = json.load(open(f))
        label = d.get('overall', {}).get('label', '?')
        swi = d.get('per_index', {}).get('SWI20', {})
        ov = d.get('overall', {})
        fname = os.path.basename(f)
        print(f"\n{fname}")
        print(f"  Overall: PF={ov.get('profit_factor','?')}, WR={ov.get('win_rate','?')}%, "
              f"trades={ov.get('total_trades','?')}, net=${ov.get('net_pnl','?')}")
        if swi:
            print(f"  SWI20:   PF={swi.get('profit_factor','?')}, WR={swi.get('win_rate','?')}%, "
                  f"trades={swi.get('total_trades','?')}, net=${swi.get('net_pnl','?')}, "
                  f"expectancy=${swi.get('expectancy','?')}")
        else:
            print("  SWI20: NOT IN THIS BACKTEST")
        
        # Show per-index summary 
        pi = d.get('per_index', {})
        for sym in ['SPX500', 'NDX100', 'GER30', 'US30', 'US2000']:
            s = pi.get(sym, {})
            if s:
                print(f"  {sym:8s}: PF={s.get('profit_factor','?')}, WR={s.get('win_rate','?')}%, "
                      f"trades={s.get('total_trades','?')}, net=${s.get('net_pnl','?')}")
    except Exception as e:
        print(f"  ERROR: {e}")

# Also check walkforward results
print("\n" + "=" * 100)
print("WALKFORWARD RESULTS")
print("=" * 100)
for wf in ['reports/walkforward_results.txt', 'reports/walkforward_universal_results.txt',
           'reports/walkforward_universal_fast.txt']:
    if os.path.exists(wf):
        print(f"\n--- {wf} ---")
        with open(wf) as f:
            content = f.read()
        # Look for SWI20 mentions
        for line in content.split('\n'):
            if 'SWI20' in line or 'OVERALL' in line.upper() or 'Summary' in line:
                print(f"  {line.strip()}")

# Check live simulation reports
print("\n" + "=" * 100)
print("LIVE SIMULATION REPORTS")
print("=" * 100)
sim_dir = 'reports/live_simulation'
if os.path.exists(sim_dir):
    for f in os.listdir(sim_dir):
        print(f"  {f}")
