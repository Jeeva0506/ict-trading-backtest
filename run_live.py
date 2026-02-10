"""
FundedNext ICT Trading Bot - Main Entry Point
==============================================
Run this script to start the 24/7 trading bot

Usage:
    python run_live.py              # Start live trading
    python run_live.py --test       # Test mode (no real trades)
    python run_live.py --status     # Check current status
"""
import sys
import io
import argparse
from pathlib import Path
from datetime import datetime

# Fix Windows console encoding for Unicode characters
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent))  # backtest_engine folder
sys.path.insert(0, str(Path(__file__).parent / 'live'))  # live folder

from live.mt5_connector import LiveTrader, MT5Connector, get_active_indices
from live.news_filter import EconomicCalendar
from config_fundednext import FUNDEDNEXT_RULES, PROFITABLE_INDICES


def show_banner():
    """Display startup banner"""
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║         FUNDEDNEXT ICT TRADING BOT v1.0                     ║
    ║         ─────────────────────────────────                   ║
    ║         4 Profitable Indices | 24/7 Operation               ║
    ╚══════════════════════════════════════════════════════════════╝
    """)


def show_status():
    """Show current trading status"""
    print("\n📊 TRADING STATUS")
    print("=" * 60)
    
    # Connect to MT5
    mt5 = MT5Connector()
    if not mt5.connect():
        print("❌ Cannot connect to MT5")
        return
    
    # Account info
    account = mt5.get_account_info()
    print(f"\n💰 Account:")
    print(f"   Balance: ${account['balance']:,.2f}")
    print(f"   Equity: ${account['equity']:,.2f}")
    print(f"   Profit: ${account['profit']:,.2f}")
    print(f"   Free Margin: ${account['free_margin']:,.2f}")
    
    # Open positions
    positions = mt5.get_open_positions()
    print(f"\n📈 Open Positions: {len(positions)}")
    for pos in positions:
        icon = '🟢' if pos['profit'] > 0 else '🔴'
        print(f"   {icon} {pos['symbol']} {pos['type'].upper()} {pos['volume']} lots | P&L: ${pos['profit']:,.2f}")
    
    # Active markets
    active = get_active_indices()
    print(f"\n⏰ Active Markets Now: {active if active else 'None'}")
    
    # All markets status
    print(f"\n📍 Index Trading Hours (MT5 Server Time):")
    for symbol, config in PROFITABLE_INDICES.items():
        is_active = "🟢 OPEN" if symbol in active else "🔴 CLOSED"
        print(f"   {symbol}: {config['mt5_open_hour']:02d}:{config['mt5_open_minute']:02d} - {config['mt5_close_hour']:02d}:{config['mt5_close_minute']:02d} {is_active}")
    
    # News check
    calendar = EconomicCalendar()
    print(f"\n📰 High-Impact News Today:")
    today_events = calendar.get_all_today_events()
    if today_events:
        for event in today_events[:5]:  # Show first 5
            print(f"   {event['time']} | {event['currency']} | {event['title']}")
    else:
        print("   No high-impact events")
    
    # Don't disconnect - just leave MT5 running
    # mt5.disconnect() - this would disable auto trading in MT5
    print("\n" + "=" * 60)


def show_config():
    """Display current configuration"""
    print("\n⚙️ TRADING CONFIGURATION")
    print("=" * 60)
    
    print("\n📋 FundedNext Stellar-2 Rules:")
    print(f"   Max Drawdown: {FUNDEDNEXT_RULES['max_drawdown_percent']}%")
    print(f"   Daily Drawdown: {FUNDEDNEXT_RULES['daily_drawdown_percent']}%")
    print(f"   Daily DD Stop: {FUNDEDNEXT_RULES['daily_drawdown_stop']}%")
    print(f"   Risk per Trade: {FUNDEDNEXT_RULES['risk_per_trade_percent']}%")
    print(f"   News Buffer: {FUNDEDNEXT_RULES['news_buffer_minutes']} minutes")
    
    print("\n📊 Profitable Indices:")
    for symbol, config in PROFITABLE_INDICES.items():
        print(f"\n   {symbol} ({config['description']}):")
        print(f"      Contract Size: {config['contract_size']}")
        print(f"      SL Buffer: {config['sl_buffer']} pts")
        print(f"      SL Range: {config['min_sl_distance']}-{config['max_sl_distance']} pts")


def run_test_mode():
    """Run in test mode - generate signals but don't execute"""
    print("\n🧪 TEST MODE - No real trades will be placed")
    print("=" * 60)
    
    mt5 = MT5Connector()
    if not mt5.connect():
        print("❌ Cannot connect to MT5")
        return
    
    calendar = EconomicCalendar()
    
    # Check each symbol
    for symbol in PROFITABLE_INDICES.keys():
        print(f"\n📍 Checking {symbol}...")
        
        # Market status
        active = get_active_indices()
        if symbol not in active:
            print(f"   ⏸️ Market closed")
            continue
        
        print(f"   ✅ Market open")
        
        # News check
        is_news, event = calendar.is_news_time(symbol)
        if is_news:
            print(f"   ⚠️ News time: {event['title']}")
            continue
        
        print(f"   ✅ No high-impact news")
        
        # Get data
        df = mt5.get_ohlc_data(symbol, count=200)
        if df.empty:
            print(f"   ❌ Cannot get data")
            continue
        
        print(f"   📊 Got {len(df)} candles")
        print(f"   📈 Current price: {df['close'].iloc[-1]:.2f}")
        print(f"   📉 Today's range: {df['low'].min():.2f} - {df['high'].max():.2f}")
    
    mt5.disconnect()
    print("\n✅ Test complete")


def run_live():
    """Start live trading"""
    show_banner()
    show_config()
    
    print("\n" + "=" * 60)
    print("🚀 STARTING LIVE TRADING")
    print("=" * 60)
    print(f"   Start time: {datetime.now()}")
    print(f"   Mode: 24/7 Automated")
    print(f"   Indices: SPX500, GER30, FRA40, HK50")
    print("\n   Press Ctrl+C to stop\n")
    
    trader = LiveTrader()
    
    if trader.initialize():
        trader.run()
    else:
        print("❌ Failed to initialize trader")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='FundedNext ICT Trading Bot')
    parser.add_argument('--test', action='store_true', help='Run in test mode')
    parser.add_argument('--status', action='store_true', help='Show current status')
    parser.add_argument('--config', action='store_true', help='Show configuration')
    
    args = parser.parse_args()
    
    if args.status:
        show_status()
    elif args.config:
        show_config()
    elif args.test:
        run_test_mode()
    else:
        run_live()


if __name__ == '__main__':
    main()
