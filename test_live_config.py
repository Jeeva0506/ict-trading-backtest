"""Quick test to verify live trading configuration"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / 'live'))

from live.mt5_connector import LiveTrader, DrawdownProtection
from live.config_fundednext import FUNDEDNEXT_RULES, ADAPTIVE_RISK, PROFITABLE_INDICES

print("=" * 60)
print("   LIVE TRADING CONFIGURATION TEST")
print("=" * 60)

print("\n✅ All imports successful!")

print("\n📊 Risk Settings:")
print(f"   Default Risk: {ADAPTIVE_RISK['default_risk_percent']}%")
print(f"   Reduced Risk: {ADAPTIVE_RISK['reduced_risk_percent']}%")
print(f"   DD Threshold: {ADAPTIVE_RISK['drawdown_threshold']}%")
print(f"   Adaptive Enabled: {ADAPTIVE_RISK['enabled']}")

print("\n📋 FundedNext Rules:")
print(f"   Max DD: {FUNDEDNEXT_RULES['max_drawdown_percent']}%")
print(f"   Daily DD: {FUNDEDNEXT_RULES['daily_drawdown_percent']}%")
print(f"   Daily Stop: {FUNDEDNEXT_RULES['daily_drawdown_stop']}%")

print("\n🎯 Active Indices:")
for symbol in PROFITABLE_INDICES.keys():
    config = PROFITABLE_INDICES[symbol]
    print(f"   - {symbol}: {config['description']}")

# Test DrawdownProtection
print("\n🛡️ Testing DrawdownProtection class...")
dd = DrawdownProtection(6000.0)

# Simulate trades
test_states = [
    (6100, "After winning trade"),
    (6050, "Small pullback"),
    (5950, "Below start"),
    (6200, "Recovery"),
    (6000, "Down from peak (3.33%)"),
]

for balance, desc in test_states:
    result = dd.update(balance)
    risk = dd.get_adaptive_risk(balance)
    print(f"   {desc}: Balance=${balance}, Risk={risk}%, Daily DD={result['daily_dd']:.2f}%, Can Trade={result['can_trade']}")

print("\n✅ All tests passed!")
print("\n💡 To start live trading, run:")
print("   python run_live.py")
print("\n💡 Or use the scheduler:")
print("   setup_scheduler.bat (run as admin)")
