"""
Multi-Market Automated Backtesting Framework
==============================================

This framework implements the ICT-based trading strategy with:
- Break of Structure (BOS) detection
- 50% Fibonacci pullback validation
- Order Block identification
- Change of Character (CHoCH) confirmation
- Multi-target exit management (TP1, TP2, Final Target)
- Comprehensive trade logging and metrics

Supported Markets:
- NIFTY (1-minute candles, Indian session 09:15-15:15)
- FOREX (1-hour candles, London session 08:00-16:00 UTC)

Usage:
    python main.py                    # Run with default config (mode from config.yaml)
    python main.py --mode nifty       # Run NIFTY backtest
    python main.py --mode forex       # Run FOREX backtest
    python main.py --debug            # Run with debug output
    python main.py --quick            # Quick 3-month test
"""

from pathlib import Path
import yaml
import pandas as pd
import sys
import argparse
from datetime import datetime

from data.loader import DataLoader
from data.validator import DataValidator
from backtest.engine import BacktestEngine
from reports.metrics import calculate_metrics, print_metrics_report, generate_monthly_breakdown
from reports.charts import plot_dashboard, plot_equity_curve
from reports.trade_log import save_trade_log


def parse_args():
    parser = argparse.ArgumentParser(description='Multi-Market Backtesting Framework')
    parser.add_argument('--mode', type=str, 
                       choices=['nifty', 'forex', 'spx500', 'fra40', 'ger30', 'hk50', 'jp225', 'ndx100', 'uk100', 'us30'], 
                       help='Market mode (overrides config)')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quick', action='store_true', help='Run quick 3-month test')
    parser.add_argument('--config', type=str, default=None, help='Path to config file')
    parser.add_argument('--start-date', type=str, default=None, help='Override start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, default=None, help='Override end date (YYYY-MM-DD)')
    return parser.parse_args()


def get_market_config(config, mode):
    """Extract market-specific config based on mode (nifty/forex)"""
    market_config = config.get(mode, {})
    
    # Flatten the market-specific config for compatibility
    flat_config = {
        'market': market_config.get('market', {}),
        'data': market_config.get('data', {}),
        'capital': market_config.get('capital', {}),
        'execution': market_config.get('execution', {}),
        'backtest': market_config.get('backtest', {}),
        'strategy': market_config.get('strategy', {}),
        'sessions': market_config.get('sessions', {}),  # Add sessions for FOREX
        'reporting': config.get('reporting', {}),
        'mode': mode
    }
    
    return flat_config


def main():
    args = parse_args()
    
    # Load configuration
    config_path = Path(args.config) if args.config else Path(__file__).parent / 'config' / 'config.yaml'
    
    if not config_path.exists():
        print(f"Error: Config file not found at {config_path}")
        sys.exit(1)
    
    with open(config_path, 'r') as f:
        raw_config = yaml.safe_load(f)
    
    # Determine mode (command line overrides config file)
    mode = args.mode if args.mode else raw_config.get('mode', 'nifty')
    
    # Get market-specific config
    config = get_market_config(raw_config, mode)
    
    # Override dates if provided via command line
    if args.start_date:
        config['data']['start_date'] = args.start_date
    if args.end_date:
        config['data']['end_date'] = args.end_date
    
    # Quick mode: limit to 3 months
    if args.quick:
        config['backtest']['duration_limit_months'] = 3
    
    # Print header based on mode
    is_forex = (mode == 'forex')
    is_spx500 = (mode == 'spx500')
    is_global_index = mode in ['fra40', 'ger30', 'hk50', 'jp225', 'ndx100', 'uk100', 'us30']
    currency_symbol = '$' if (is_forex or is_spx500 or is_global_index) else '₹'
    market_name = config['market']['symbol']
    
    # Session info for global indices
    session_info = {
        'fra40': ('Paris', '09:00 - 17:30 CET'),
        'ger30': ('Frankfurt', '09:00 - 17:30 CET'),
        'hk50': ('Hong Kong', '09:30 - 16:00 HKT'),
        'jp225': ('Tokyo', '09:00 - 15:00 JST'),
        'ndx100': ('New York', '09:30 - 15:45 ET'),
        'uk100': ('London', '08:00 - 16:30 GMT'),
        'us30': ('New York', '09:30 - 15:45 ET'),
    }
    
    print("=" * 60)
    if is_forex:
        print("   FOREX AUTOMATED BACKTESTING FRAMEWORK")
        print("   ICT Strategy - London Session Trading")
    elif is_spx500 or is_global_index:
        print(f"   {mode.upper()} CFD AUTOMATED BACKTESTING FRAMEWORK")
        print("   ICT Strategy - Local Market Session Trading")
    else:
        print("   NIFTY FUTURES AUTOMATED BACKTESTING FRAMEWORK")
        print("   ICT Strategy Implementation")
    print("=" * 60)
    
    if args.quick:
        print("\n⚡ Running in QUICK mode (3 months)")
    
    print(f"\n🌍 Market Mode: {mode.upper()} ({market_name})")
    print(f"📅 Backtest Period: {config['data']['start_date']} to {config['data']['end_date']}")
    print(f"💰 Initial Capital: {currency_symbol}{config['capital']['initial_capital']:,}")
    print(f"📊 Risk per Trade: {config['capital']['risk_per_trade'] * 100}%")
    
    if is_forex:
        print(f"🕐 Trading Session: London (08:00 - 16:00 UTC)")
        print(f"🎯 Target Mode: PDH/PDL (Previous Day High/Low)")
    elif is_spx500:
        print(f"🕐 Trading Session: New York (09:30 - 15:45 ET)")
        print(f"🎯 Target Mode: Day High/Low")
        print(f"📈 Contract Size: {config['market'].get('contract_size', 10)}")
    elif is_global_index:
        session_name, session_time = session_info.get(mode, ('Local', '09:00 - 17:00'))
        print(f"🕐 Trading Session: {session_name} ({session_time})")
        print(f"🎯 Target Mode: Day High/Low")
        print(f"📈 Contract Size: {config['market'].get('contract_size', 1)}")
    else:
        print(f"🕐 Trading Session: Indian Market (09:15 - 15:15 IST)")
        print(f"🎯 Target Mode: Day High/Low")
    
    # Load data
    print("\n📂 Loading market data...")
    try:
        loader = DataLoader(config_path, mode=mode)
        df = loader.load_data()
        print(f"   Loaded {len(df):,} candles")
        print(f"   Date range: {df['datetime'].min()} to {df['datetime'].max()}")
    except Exception as e:
        print(f"Error loading data: {e}")
        sys.exit(1)
    
    # Validate data
    print("\n🔍 Validating data...")
    validator = DataValidator()
    
    required_columns = ['open', 'high', 'low', 'close', 'datetime']
    if not validator.validate_required_columns(df, required_columns):
        print(f"Error: Missing required columns. Required: {required_columns}")
        print(f"Found: {df.columns.tolist()}")
        sys.exit(1)
    
    # For forex, validate 1-hour candles; for nifty, validate 1-min
    if is_forex:
        print("   Validating 1-hour candles for FOREX...")
    else:
        if not validator.validate_1min_candles(df, 'datetime'):
            print("Warning: Data may not be 1-minute candles. Proceeding anyway...")
    
    print("   ✅ Data validation passed")
    
    # Initialize strategy with config
    print("\n🎯 Initializing ICT Strategy with Trend Confirmation...")
    from strategy.strategy import NiftyStrategy
    
    # Get strategy settings from config
    strategy_config = config.get('strategy', {})
    bos_required = strategy_config.get('bos_required_for_trend', 2)
    min_rr = strategy_config.get('min_rr_ratio', 2.0)
    fib_min = strategy_config.get('fib_level_min', 0.3)
    fib_max = strategy_config.get('fib_level_max', 0.5)
    
    # VWAP filter settings
    use_vwap = strategy_config.get('use_vwap_filter', True)
    use_atr_sl = strategy_config.get('use_atr_sl', False)
    atr_mult = strategy_config.get('atr_multiplier', 1.5)
    
    # Target mode for forex (PDH/PDL)
    target_mode = strategy_config.get('target_mode', 'day_high_low')
    
    # Swing lookback (reduced for 1H timeframe)
    swing_lookback = strategy_config.get('swing_lookback', 5)
    multi_day_structure = strategy_config.get('multi_day_structure', False)
    
    # Entry mode: True = Wait for OB 2.0 retest, False = Enter at CHoCH directly
    use_ob2_entry = strategy_config.get('use_ob2_entry', True)
    
    # CVD filter (for SPX500 with volume data)
    use_cvd_filter = strategy_config.get('use_cvd_filter', False)
    
    # Liquidity Sweep filter (Pre-CHoCH sweep)
    use_liquidity_sweep_filter = strategy_config.get('use_liquidity_sweep_filter', False)
    
    # OB 2.0 inside VWAP bands filter
    use_ob2_vwap_filter = strategy_config.get('use_ob2_vwap_filter', False)
    ob2_vwap_tolerance = strategy_config.get('ob2_vwap_tolerance', 3.0)
    
    # Candle confirmation filter (rejection/engulfing at OB2)
    use_candle_confirmation = strategy_config.get('use_candle_confirmation', False)
    
    # BOS Sweep filter (opposite BOS in confirmed trend = liquidity sweep)
    use_bos_sweep_filter = strategy_config.get('use_bos_sweep_filter', False)
    
    strategy = NiftyStrategy(
        lot_size=config['market'].get('lot_size', config['market'].get('contract_size', 1)),
        sl_buffer=config['market']['sl_buffer_points'] if 'sl_buffer_points' in config['market'] else strategy_config.get('sl_buffer_points', 1.0),
        min_rr=min_rr,
        bos_required_for_trend=bos_required,
        fib_level_min=fib_min,
        fib_level_max=fib_max,
        use_vwap_filter=use_vwap,
        use_atr_sl=use_atr_sl,
        atr_multiplier=atr_mult,
        mode=mode,
        target_mode=target_mode,
        swing_lookback=swing_lookback,
        use_ob2_entry=use_ob2_entry,
        use_cvd_filter=use_cvd_filter,
        use_liquidity_sweep_filter=use_liquidity_sweep_filter,
        use_ob2_vwap_filter=use_ob2_vwap_filter,
        ob2_vwap_tolerance=ob2_vwap_tolerance,
        use_candle_confirmation=use_candle_confirmation,
        use_bos_sweep_filter=use_bos_sweep_filter
    )
    
    print(f"   📌 BOS required for trend confirmation: {bos_required}")
    print(f"   📌 Minimum R:R ratio: {min_rr}")
    print(f"   📌 Fibonacci pullback zone: {int(fib_min*100)}% to {int(fib_max*100)}%")
    print(f"   📌 Entry Mode: {'OB 2.0 Retest' if use_ob2_entry else 'Direct at CHoCH'}")
    print(f"   📌 VWAP filter: {'ON' if use_vwap else 'OFF'}")
    print(f"   📌 CVD filter: {'ON' if use_cvd_filter else 'OFF'}")
    print(f"   📌 Liquidity Sweep filter: {'ON' if use_liquidity_sweep_filter else 'OFF'}")
    print(f"   📌 BOS Sweep filter: {'ON' if use_bos_sweep_filter else 'OFF'}")
    print(f"   📌 OB2 VWAP Band filter: {'ON' if use_ob2_vwap_filter else 'OFF'} (±{ob2_vwap_tolerance}pts)")
    print(f"   📌 Candle Confirmation: {'ON (Rejection/Engulfing)' if use_candle_confirmation else 'OFF'}")
    if is_forex:
        print(f"   📌 Target Mode: PDH (shorts), PDL (longs)")
        print(f"   📌 Swing Lookback: {swing_lookback} (adjusted for 1H)")
    else:
        print(f"   📌 VWAP filter: Longs above VWAP, Shorts below")
    print(f"   📌 ATR-based SL: {'ON' if use_atr_sl else 'OFF'}")
    
    # Run backtest
    print("\n🚀 Running backtest...")
    print("-" * 60)
    
    start_time = datetime.now()
    
    engine = BacktestEngine(config, df, strategy, mode=mode)
    engine.run()
    results = engine.get_results()
    
    elapsed = datetime.now() - start_time
    print(f"\n⏱️  Backtest completed in {elapsed.total_seconds():.2f} seconds")
    
    # Calculate metrics
    print("\n📊 Calculating performance metrics...")
    metrics = calculate_metrics(
        results['trades'], 
        initial_capital=config['capital']['initial_capital']
    )
    
    # Print detailed report
    print_metrics_report(metrics, currency_symbol=currency_symbol)
    
    # Session-based metrics for FOREX
    if is_forex and results.get('session_trades'):
        print("\n" + "=" * 60)
        print("              SESSION-BASED PERFORMANCE REPORT")
        print("=" * 60)
        
        for session_key, session_trades in results['session_trades'].items():
            if session_trades:
                session_name = results['sessions'][session_key]['name']
                session_metrics = calculate_metrics(
                    session_trades,
                    initial_capital=config['capital']['initial_capital']
                )
                
                print(f"\n🌍 {session_name.upper()} SESSION")
                print("-" * 40)
                print(f"   Trades:       {session_metrics['total_trades']}")
                print(f"   Win Rate:     {session_metrics['winrate']:.2f}%")
                print(f"   Net Profit:   {currency_symbol}{session_metrics['net_profit']:,.2f}")
                print(f"   Profit Factor:{session_metrics['profit_factor']:.2f}")
                print(f"   Avg Win:      {currency_symbol}{session_metrics['avg_win']:,.2f}")
                print(f"   Avg Loss:     {currency_symbol}{session_metrics['avg_loss']:,.2f}")
                
                # Direction breakdown for session
                long_trades = [t for t in session_trades if t.get('direction') == 'bullish']
                short_trades = [t for t in session_trades if t.get('direction') == 'bearish']
                long_wr = (sum(1 for t in long_trades if t.get('pnl', 0) > 0) / len(long_trades) * 100) if long_trades else 0
                short_wr = (sum(1 for t in short_trades if t.get('pnl', 0) > 0) / len(short_trades) * 100) if short_trades else 0
                
                print(f"   Long Trades:  {len(long_trades)} ({long_wr:.1f}% WR)")
                print(f"   Short Trades: {len(short_trades)} ({short_wr:.1f}% WR)")
    
    # Generate monthly breakdown
    if results['trades']:
        print("\n📆 MONTHLY BREAKDOWN")
        print("-" * 60)
        monthly = generate_monthly_breakdown(results['trades'])
        if not monthly.empty:
            print(monthly.to_string())
    
    # Detailed Per-Trade Report
    if results['trades']:
        print("\n" + "=" * 80)
        print("                    DETAILED PER-TRADE REPORT")
        print("=" * 80)
        print(f"{'#':<3} {'Date':<12} {'Dir':<6} {'Entry':<10} {'Exit':<10} {'PnL':>10} {'R:R':>6} {'Exit Reason':<15} {'TP1':>4} {'TP2':>4} {'TP3':>4}")
        print("-" * 80)
        
        for idx, trade in enumerate(results['trades'], 1):
            date = trade.get('date', '')[:10]
            direction = 'LONG' if trade.get('direction') == 'bullish' else 'SHORT'
            entry = trade.get('entry_price', 0)
            exit_p = trade.get('exit_price', 0)
            pnl = trade.get('pnl', 0)
            rr = trade.get('rr_achieved', 0)
            exit_reason = trade.get('exit_reason', '')[:15]
            tp1 = '✓' if trade.get('tp1_hit') else '✗'
            tp2 = '✓' if trade.get('tp2_hit') else '✗'
            tp3 = '✓' if trade.get('final_hit') else '✗'
            
            pnl_str = f"{currency_symbol}{pnl:,.2f}"
            print(f"{idx:<3} {date:<12} {direction:<6} {entry:<10.2f} {exit_p:<10.2f} {pnl_str:>10} {rr:>6.2f} {exit_reason:<15} {tp1:>4} {tp2:>4} {tp3:>4}")
        
        print("-" * 80)
        
        # Summary stats
        tp1_hits = sum(1 for t in results['trades'] if t.get('tp1_hit'))
        tp2_hits = sum(1 for t in results['trades'] if t.get('tp2_hit'))
        tp3_hits = sum(1 for t in results['trades'] if t.get('final_hit'))
        sl_hits = sum(1 for t in results['trades'] if t.get('sl_hit'))
        ctc_hits = sum(1 for t in results['trades'] if t.get('exit_reason') == 'SL_CTC')
        
        avg_rr = sum(t.get('rr_achieved', 0) for t in results['trades']) / len(results['trades']) if results['trades'] else 0
        max_rr = max(t.get('rr_achieved', 0) for t in results['trades']) if results['trades'] else 0
        min_rr = min(t.get('rr_achieved', 0) for t in results['trades']) if results['trades'] else 0
        
        max_win = max(t.get('pnl', 0) for t in results['trades']) if results['trades'] else 0
        max_loss = min(t.get('pnl', 0) for t in results['trades']) if results['trades'] else 0
        
        print(f"\n📊 TARGET STATISTICS:")
        print(f"   TP1 Hits (1:1 - SL to CTC): {tp1_hits} / {len(results['trades'])} ({tp1_hits/len(results['trades'])*100:.1f}%)")
        print(f"   TP2 Hits (1:2 - 50% booked): {tp2_hits} / {len(results['trades'])} ({tp2_hits/len(results['trades'])*100:.1f}%)")
        print(f"   TP3 Hits (Day H/L - Full exit): {tp3_hits} / {len(results['trades'])} ({tp3_hits/len(results['trades'])*100:.1f}%)")
        print(f"   SL Hits (Before TP1): {sl_hits - ctc_hits}")
        print(f"   CTC Hits (After TP1): {ctc_hits}")
        
        print(f"\n📈 R:R STATISTICS:")
        print(f"   Average R:R Captured: {avg_rr:.2f}R")
        print(f"   Maximum R:R Captured: {max_rr:.2f}R")
        print(f"   Minimum R:R Captured: {min_rr:.2f}R")
        
        print(f"\n💰 PER-TRADE RETURNS:")
        print(f"   Biggest Winner: {currency_symbol}{max_win:,.2f}")
        print(f"   Biggest Loser:  {currency_symbol}{max_loss:,.2f}")
        print(f"   Avg Trade PnL:  {currency_symbol}{metrics['avg_trade']:,.2f}")
    
    # Save outputs
    output_dir = Path(__file__).parent / config.get('reporting', {}).get('output_dir', 'reports/screenshots')
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Save trade log
    if config.get('reporting', {}).get('save_trade_log', True) and results['trades']:
        print("\n💾 Saving trade logs...")
        log_paths = save_trade_log(results['trades'], output_dir)
    
    # Generate charts
    if config.get('reporting', {}).get('save_charts', True):
        print("\n📈 Generating charts...")
        
        # Equity curve
        if results['equity_curve']:
            equity_path = output_dir / 'equity_curve.png'
            plot_equity_curve(results['equity_curve'], equity_path)
            print(f"   Equity curve saved to: {equity_path}")
        
        # Dashboard
        dashboard_path = output_dir / 'dashboard.png'
        plot_dashboard(metrics, results['equity_curve'], dashboard_path)
        print(f"   Dashboard saved to: {dashboard_path}")
    
    # Final summary
    print("\n" + "=" * 60)
    print("   BACKTEST COMPLETE")
    print("=" * 60)
    print(f"   Market:          {market_name}")
    print(f"   Total Trades:    {metrics['total_trades']}")
    print(f"   Win Rate:        {metrics['winrate']:.2f}%")
    print(f"   Net Profit:      {currency_symbol}{metrics['net_profit']:,.2f}")
    print(f"   ROI:             {metrics['roi_percent']:.2f}%")
    print(f"   Max Drawdown:    {metrics['max_drawdown_pct']:.2f}%")
    print(f"   Sharpe Ratio:    {metrics['sharpe_ratio']:.2f}")
    print("=" * 60)
    
    return results, metrics


if __name__ == '__main__':
    main()
