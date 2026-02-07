"""
Complete Multi-Index Analysis
==============================
- Verify MT5 vs Local data match for all indices
- Calculate max drawdown, profit per month/day
- Average trades per week
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import sys

sys.path.insert(0, str(Path(__file__).parent))
from strategy.strategy_ict_clean import StrategyICTClean

# Index configurations
INDEX_CONFIG = {
    'SPX500': {
        'local_file': 'spx500_1min.csv',
        'mt5_symbol': 'SPX500',
        'session_start_hour': 16,
        'session_start_minute': 30,
        'sl_buffer': 1.0,
        'name': 'S&P 500'
    },
    'GER30': {
        'local_file': 'GER30_PERIOD_M1.csv',
        'mt5_symbol': 'GER30',
        'session_start_hour': 10,
        'session_start_minute': 0,
        'sl_buffer': 2.0,
        'name': 'DAX 40'
    },
    'FRA40': {
        'local_file': 'FRA40_PERIOD_M1.csv',
        'mt5_symbol': 'FRA40',
        'session_start_hour': 10,
        'session_start_minute': 0,
        'sl_buffer': 1.0,
        'name': 'CAC 40'
    },
    'HK50': {
        'local_file': 'HK50_PERIOD_M1.csv',
        'mt5_symbol': 'HK50',
        'session_start_hour': 3,
        'session_start_minute': 30,
        'sl_buffer': 2.5,
        'name': 'Hang Seng'
    },
}


def load_local_data(filename: str) -> pd.DataFrame:
    """Load local CSV data"""
    data_path = Path(__file__).parent / 'data' / 'raw' / filename
    if not data_path.exists():
        return None
    
    df = pd.read_csv(data_path, sep='\t', parse_dates=['Time'])
    df.set_index('Time', inplace=True)
    df.columns = [col.lower() for col in df.columns]
    return df


def download_mt5_data(symbol: str, days: int = 60) -> pd.DataFrame:
    """Download MT5 data"""
    utc_to = datetime.now()
    utc_from = utc_to - timedelta(days=days)
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, utc_from, utc_to)
    
    if rates is None or len(rates) == 0:
        return None
    
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df.set_index('time')
    df = df[['open', 'high', 'low', 'close', 'tick_volume']]
    df.columns = ['open', 'high', 'low', 'close', 'volume']
    return df


def run_strategy(df: pd.DataFrame, config: dict) -> list:
    """Run ICT strategy with correct settings"""
    CAPITAL = 100000.0
    RISK_PERCENT = 1.0
    
    trading_days = sorted(set(df.index.date))
    all_trades = []
    
    for day in trading_days:
        prev_day = day - timedelta(days=1)
        while prev_day.weekday() >= 5:
            prev_day -= timedelta(days=1)
        
        day_start = pd.Timestamp(day) - timedelta(hours=12)
        day_end = pd.Timestamp(day) + timedelta(hours=23, minutes=59)
        
        day_df = df[(df.index >= day_start) & (df.index <= day_end)].copy()
        
        if len(day_df) < 100:
            continue
        
        prev_day_data = df[df.index.date == prev_day]
        pdh = prev_day_data['high'].max() if len(prev_day_data) > 0 else None
        pdl = prev_day_data['low'].min() if len(prev_day_data) > 0 else None
        
        current_day_data = day_df[day_df.index.date == day]
        if len(current_day_data) == 0:
            continue
        
        # Calculate day_high/day_low from market open only (not premarket)
        session_start = pd.Timestamp(day).replace(
            hour=config['session_start_hour'], 
            minute=config['session_start_minute']
        )
        market_hours_data = current_day_data[current_day_data.index >= session_start]
        
        if len(market_hours_data) > 0:
            day_high = market_hours_data['high'].max()
            day_low = market_hours_data['low'].min()
        else:
            # Fallback if no market hours data yet
            day_high = current_day_data['high'].max()
            day_low = current_day_data['low'].min()
        
        strategy = StrategyICTClean(
            lot_size=1,
            sl_buffer=config['sl_buffer'],
            min_rr=2.0,
            bos_required=2,
            capital=CAPITAL,
            risk_percent=RISK_PERCENT,
            session_start_hour=config['session_start_hour'],
            session_start_minute=config['session_start_minute']
        )
        
        signals = strategy.generate_signals(
            day_df, 
            day_high=day_high, 
            day_low=day_low,
            pdh=pdh,
            pdl=pdl,
            debug=False
        )
        
        for signal in signals:
            all_trades.append({
                'date': day,
                'entry_time': signal.entry_time,
                'direction': signal.direction,
                'entry_price': signal.entry_price,
                'tp1_hit': signal.tp1_hit,
                'sl_hit': signal.sl_hit,
                'pnl': signal.realized_pnl,
                'week': day.isocalendar()[1],
                'month': day.strftime('%Y-%m'),
                'weekday': day.strftime('%A')
            })
    
    return all_trades


def calculate_metrics(trades: list, symbol: str) -> dict:
    """Calculate comprehensive metrics"""
    if not trades:
        return None
    
    total = len(trades)
    tp1 = sum(1 for t in trades if t['tp1_hit'])
    sl = sum(1 for t in trades if t['sl_hit'])
    total_pnl = sum(t['pnl'] for t in trades)
    
    # Win/Loss
    winners = [t for t in trades if t['pnl'] > 0]
    losers = [t for t in trades if t['pnl'] < 0]
    breakeven = [t for t in trades if t['pnl'] == 0]
    
    gross_profit = sum(t['pnl'] for t in winners)
    gross_loss = abs(sum(t['pnl'] for t in losers))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Max Drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    equity_curve = []
    
    for t in sorted(trades, key=lambda x: x['date']):
        cumulative += t['pnl']
        equity_curve.append(cumulative)
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    
    # By Month
    monthly_pnl = defaultdict(float)
    monthly_trades = defaultdict(int)
    for t in trades:
        monthly_pnl[t['month']] += t['pnl']
        monthly_trades[t['month']] += 1
    
    # By Day of Week
    daily_pnl = defaultdict(float)
    daily_trades = defaultdict(int)
    for t in trades:
        daily_pnl[t['weekday']] += t['pnl']
        daily_trades[t['weekday']] += 1
    
    # Trades per week
    weeks = set(f"{t['date'].year}-W{t['week']:02d}" for t in trades)
    trades_per_week = total / len(weeks) if weeks else 0
    
    # Date range
    dates = [t['date'] for t in trades]
    date_range = (max(dates) - min(dates)).days + 1
    
    return {
        'symbol': symbol,
        'total_trades': total,
        'tp1_hits': tp1,
        'tp1_rate': tp1 / total * 100,
        'sl_hits': sl,
        'winners': len(winners),
        'losers': len(losers),
        'breakeven': len(breakeven),
        'total_pnl': total_pnl,
        'profit_factor': pf,
        'max_drawdown': max_dd,
        'monthly_pnl': dict(monthly_pnl),
        'monthly_trades': dict(monthly_trades),
        'daily_pnl': dict(daily_pnl),
        'daily_trades': dict(daily_trades),
        'trades_per_week': trades_per_week,
        'weeks_traded': len(weeks),
        'date_range_days': date_range,
        'trades': trades
    }


def compare_data(local_trades: list, mt5_trades: list, symbol: str) -> bool:
    """Compare local vs MT5 trades"""
    local_dates = sorted([str(t['date']) for t in local_trades])
    mt5_dates = sorted([str(t['date']) for t in mt5_trades])
    
    if local_dates == mt5_dates:
        # Check entry prices
        for lt, mt in zip(sorted(local_trades, key=lambda x: x['date']), 
                         sorted(mt5_trades, key=lambda x: x['date'])):
            if abs(lt['entry_price'] - mt['entry_price']) > 0.1:
                return False
        return True
    return False


def main():
    print("="*80)
    print("   COMPLETE MULTI-INDEX ANALYSIS")
    print("   MT5 vs Local Data Verification + Detailed Metrics")
    print("="*80)
    
    if not mt5.initialize():
        print("❌ MT5 initialization failed")
        return
    
    print("✅ Connected to MT5\n")
    
    all_results = {}
    portfolio_trades = []
    
    for symbol, config in INDEX_CONFIG.items():
        print("\n" + "="*80)
        print(f"   {symbol} ({config['name']})")
        print("="*80)
        
        # Load local data
        local_df = load_local_data(config['local_file'])
        if local_df is None:
            print(f"   ⚠️ Local file not found: {config['local_file']}")
            continue
        print(f"   📂 Local: {local_df.index[0]} to {local_df.index[-1]}")
        
        # Load MT5 data
        mt5_df = download_mt5_data(config['mt5_symbol'], days=60)
        if mt5_df is None:
            print(f"   ⚠️ MT5 data not available")
            continue
        print(f"   📂 MT5: {mt5_df.index[0]} to {mt5_df.index[-1]}")
        
        # Find overlap
        overlap_start = max(local_df.index[0], mt5_df.index[0])
        overlap_end = min(local_df.index[-1], mt5_df.index[-1])
        print(f"   📅 Overlap: {overlap_start.date()} to {overlap_end.date()}")
        
        # Filter
        local_filtered = local_df[(local_df.index >= overlap_start) & (local_df.index <= overlap_end)]
        mt5_filtered = mt5_df[(mt5_df.index >= overlap_start) & (mt5_df.index <= overlap_end)]
        
        # Run backtests
        print(f"\n   🔬 Running backtests...")
        local_trades = run_strategy(local_filtered, config)
        mt5_trades = run_strategy(mt5_filtered, config)
        
        # Compare
        if compare_data(local_trades, mt5_trades, symbol):
            print(f"   ✅ DATA MATCH CONFIRMED!")
        else:
            print(f"   ⚠️ DATA MISMATCH!")
            print(f"      Local trades: {len(local_trades)}")
            print(f"      MT5 trades: {len(mt5_trades)}")
        
        # Calculate metrics (use MT5 data)
        metrics = calculate_metrics(mt5_trades, symbol)
        if metrics:
            all_results[symbol] = metrics
            
            # Add to portfolio
            for t in mt5_trades:
                t['symbol'] = symbol
                portfolio_trades.append(t)
            
            # Print summary
            print(f"\n   📊 METRICS:")
            print(f"      Total Trades: {metrics['total_trades']}")
            print(f"      TP1 Rate: {metrics['tp1_rate']:.1f}%")
            print(f"      Profit Factor: {metrics['profit_factor']:.2f}")
            print(f"      Total P&L: ${metrics['total_pnl']:,.2f}")
            print(f"      Max Drawdown: ${metrics['max_drawdown']:,.2f}")
            print(f"      Trades/Week: {metrics['trades_per_week']:.2f}")
            
            print(f"\n   📅 MONTHLY BREAKDOWN:")
            for month in sorted(metrics['monthly_pnl'].keys()):
                pnl = metrics['monthly_pnl'][month]
                trades = metrics['monthly_trades'][month]
                print(f"      {month}: {trades} trades, ${pnl:,.2f}")
            
            print(f"\n   📆 BY DAY OF WEEK:")
            for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
                if day in metrics['daily_trades']:
                    pnl = metrics['daily_pnl'][day]
                    trades = metrics['daily_trades'][day]
                    print(f"      {day}: {trades} trades, ${pnl:,.2f}")
    
    # Portfolio Summary
    if all_results:
        print("\n" + "="*80)
        print("   PORTFOLIO SUMMARY")
        print("="*80)
        
        portfolio_metrics = calculate_metrics(portfolio_trades, "PORTFOLIO")
        
        print(f"\n   📊 OVERALL METRICS:")
        print(f"      Indices: {len(all_results)}")
        print(f"      Total Trades: {portfolio_metrics['total_trades']}")
        print(f"      TP1 Rate: {portfolio_metrics['tp1_rate']:.1f}%")
        print(f"      Profit Factor: {portfolio_metrics['profit_factor']:.2f}")
        print(f"      Total P&L: ${portfolio_metrics['total_pnl']:,.2f}")
        print(f"      Max Drawdown: ${portfolio_metrics['max_drawdown']:,.2f}")
        print(f"      Profit/DD Ratio: {portfolio_metrics['total_pnl']/portfolio_metrics['max_drawdown']:.1f}x" if portfolio_metrics['max_drawdown'] > 0 else "      Profit/DD Ratio: N/A")
        
        print(f"\n   📈 TRADES PER WEEK:")
        print(f"      Portfolio Average: {portfolio_metrics['trades_per_week']:.2f} trades/week")
        print(f"      Over {portfolio_metrics['weeks_traded']} weeks")
        
        print(f"\n   📊 BY INDEX:")
        print(f"      {'Index':<10} | {'Trades':>6} | {'TP1%':>6} | {'PF':>6} | {'P&L':>12} | {'MaxDD':>10} | {'Tr/Wk':>6}")
        print(f"      {'-'*70}")
        for symbol, m in all_results.items():
            print(f"      {symbol:<10} | {m['total_trades']:>6} | {m['tp1_rate']:>5.1f}% | {m['profit_factor']:>5.2f} | ${m['total_pnl']:>10,.0f} | ${m['max_drawdown']:>8,.0f} | {m['trades_per_week']:>5.2f}")
        
        print(f"\n   📅 PORTFOLIO MONTHLY:")
        for month in sorted(portfolio_metrics['monthly_pnl'].keys()):
            pnl = portfolio_metrics['monthly_pnl'][month]
            trades = portfolio_metrics['monthly_trades'][month]
            print(f"      {month}: {trades} trades, ${pnl:,.2f}")
        
        print(f"\n   📆 PORTFOLIO BY DAY:")
        for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
            if day in portfolio_metrics['daily_trades']:
                pnl = portfolio_metrics['daily_pnl'][day]
                trades = portfolio_metrics['daily_trades'][day]
                avg = pnl / trades if trades > 0 else 0
                print(f"      {day}: {trades} trades, ${pnl:,.2f} (avg ${avg:,.0f}/trade)")
        
        print("\n" + "="*80)
        print("   TRADE LOG")
        print("="*80)
        
        print(f"\n   All {len(portfolio_trades)} trades sorted by date:")
        for t in sorted(portfolio_trades, key=lambda x: x['date']):
            status = "✅" if t['tp1_hit'] else "❌"
            print(f"      {t['date']} | {t['symbol']:<7} | {t['direction']:<7} | {status} | ${t['pnl']:>8,.0f}")


if __name__ == "__main__":
    main()
