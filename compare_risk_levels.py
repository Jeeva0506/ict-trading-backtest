"""
Compare Risk Levels Across All Indices
======================================
Runs backtests at 1.5% and 2% risk and compares results
"""
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed
import sys

sys.path.insert(0, str(Path(__file__).parent))
from strategy.strategy_ict_clean import StrategyICTClean

# Broker point values ($ per 1 full index point per 1 lot)
BROKER_POINT_VALUES = {
    'SPX500': 10.0,
    'GER30': 11.81,
    'FRA40': 11.81,
    'HK50': 1.29,
    'UK100': 10.0,
    'JP225': 0.08,
    'NDX100': 10.0,
    'US30': 10.0,
}

BROKER_MARGIN_PER_LOT = {
    'SPX500': 3461,
    'GER30': 14644,
    'FRA40': 4906,
    'HK50': 1739,
    'UK100': 5000,
    'JP225': 2000,
    'NDX100': 4000,
    'US30': 3500,
}

# Index configurations
INDEX_CONFIG = {
    'SPX500': {
        'local_file': 'spx500_1min.csv',
        'session_start_hour': 16,
        'session_start_minute': 30,
        'sl_buffer': 1.0,
        'name': 'S&P 500'
    },
    'GER30': {
        'local_file': 'GER30_PERIOD_M1.csv',
        'session_start_hour': 10,
        'session_start_minute': 0,
        'sl_buffer': 2.0,
        'name': 'DAX 40'
    },
    'FRA40': {
        'local_file': 'FRA40_PERIOD_M1.csv',
        'session_start_hour': 10,
        'session_start_minute': 0,
        'sl_buffer': 1.0,
        'name': 'CAC 40'
    },
    'HK50': {
        'local_file': 'HK50_PERIOD_M1.csv',
        'session_start_hour': 3,
        'session_start_minute': 30,
        'sl_buffer': 2.5,
        'name': 'Hang Seng'
    },
    'US30': {
        'local_file': 'US30_M1.csv',
        'session_start_hour': 16,
        'session_start_minute': 30,
        'sl_buffer': 5.0,
        'name': 'Dow Jones 30'
    },
    'UK100': {
        'local_file': 'UK100_PERIOD_M1.csv',
        'session_start_hour': 9,
        'session_start_minute': 0,
        'sl_buffer': 2.0,
        'name': 'FTSE 100'
    },
    'NDX100': {
        'local_file': 'NDX100_PERIOD_M1.csv',
        'session_start_hour': 16,
        'session_start_minute': 30,
        'sl_buffer': 3.0,
        'name': 'Nasdaq 100'
    },
    'JP225': {
        'local_file': 'JP225_PERIOD_M1.csv',
        'session_start_hour': 2,
        'session_start_minute': 0,
        'sl_buffer': 20.0,
        'name': 'Nikkei 225'
    },
}


def load_data(filename: str) -> pd.DataFrame:
    """Load local CSV data"""
    data_path = Path(__file__).parent / 'data' / 'raw' / filename
    if not data_path.exists():
        return None
    
    df = pd.read_csv(data_path, sep='\t', parse_dates=['Time'])
    df.set_index('Time', inplace=True)
    df.columns = [col.lower() for col in df.columns]
    return df


def run_backtest_for_index(symbol: str, risk_percent: float, capital: float = 100000.0) -> dict:
    """Run backtest for a single index at specified risk level"""
    config = INDEX_CONFIG.get(symbol)
    if not config:
        return None
    
    df = load_data(config['local_file'])
    if df is None or len(df) == 0:
        return None
    
    point_value = BROKER_POINT_VALUES.get(symbol, 10.0)
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
        
        session_start = pd.Timestamp(day).replace(
            hour=config['session_start_hour'], 
            minute=config['session_start_minute']
        )
        market_hours_data = current_day_data[current_day_data.index >= session_start]
        
        if len(market_hours_data) > 0:
            day_high = market_hours_data['high'].max()
            day_low = market_hours_data['low'].min()
        else:
            day_high = current_day_data['high'].max()
            day_low = current_day_data['low'].min()
        
        strategy = StrategyICTClean(
            lot_size=1,
            sl_buffer=config['sl_buffer'],
            min_rr=2.0,
            bos_required=2,
            capital=capital,
            risk_percent=risk_percent,
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
            risk_amount = capital * (risk_percent / 100)
            risk_points = signal.risk_points
            
            if risk_points > 0:
                lots = risk_amount / (risk_points * point_value)
                lots = max(0.01, min(40.0, lots))
                lots = int(lots * 100) / 100
                
                # Determine result based on signal attributes
                if signal.sl_hit and not signal.tp1_hit:
                    result = 'loss'
                    pnl = -risk_amount
                    rr_achieved = -1.0
                elif signal.tp1_hit:
                    result = 'win'
                    # Calculate P&L based on which TPs were hit
                    pnl_points = signal.pnl_points
                    rr_achieved = pnl_points / risk_points if risk_points > 0 else 0
                    pnl = pnl_points * lots * point_value
                elif signal.sl_moved_to_be and signal.sl_hit:
                    result = 'breakeven'
                    pnl = 0
                    rr_achieved = 0
                else:
                    # Fallback
                    result = 'loss'
                    pnl = -risk_amount
                    rr_achieved = -1.0
                
                all_trades.append({
                    'symbol': symbol,
                    'date': signal.entry_time,
                    'direction': signal.direction,
                    'result': result,
                    'rr_achieved': rr_achieved,
                    'pnl': pnl,
                    'lots': lots,
                    'risk_amount': risk_amount,
                })
    
    # Calculate summary stats
    if len(all_trades) == 0:
        return {
            'symbol': symbol,
            'name': config['name'],
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'return_pct': 0,
            'profit_factor': 0,
            'max_drawdown': 0,
            'risk_percent': risk_percent,
        }
    
    trades_df = pd.DataFrame(all_trades)
    wins = (trades_df['result'] == 'win').sum()
    losses = (trades_df['result'] == 'loss').sum()
    total_pnl = trades_df['pnl'].sum()
    
    gross_profit = trades_df[trades_df['pnl'] > 0]['pnl'].sum()
    gross_loss = abs(trades_df[trades_df['pnl'] < 0]['pnl'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Calculate max drawdown
    cumulative = trades_df['pnl'].cumsum()
    rolling_max = cumulative.cummax()
    drawdown = rolling_max - cumulative
    max_dd = drawdown.max()
    max_dd_pct = (max_dd / capital) * 100
    
    return {
        'symbol': symbol,
        'name': config['name'],
        'trades': len(all_trades),
        'wins': wins,
        'losses': losses,
        'win_rate': (wins / len(all_trades)) * 100 if len(all_trades) > 0 else 0,
        'total_pnl': total_pnl,
        'return_pct': (total_pnl / capital) * 100,
        'profit_factor': profit_factor,
        'max_drawdown': max_dd_pct,
        'risk_percent': risk_percent,
    }


def main():
    print("=" * 100)
    print("   RISK LEVEL COMPARISON: 1.5% vs 2%")
    print("=" * 100)
    
    capital = 100000.0
    risk_levels = [1.5, 2.0]
    
    all_results = []
    
    for risk in risk_levels:
        print(f"\n📊 Running backtests at {risk}% risk...")
        
        for symbol in INDEX_CONFIG.keys():
            result = run_backtest_for_index(symbol, risk, capital)
            if result:
                all_results.append(result)
                status = "🟢" if result['total_pnl'] > 0 else "🔴"
                print(f"   {status} {symbol}: {result['trades']} trades, ${result['total_pnl']:,.2f}")
    
    # Create comparison table
    print("\n" + "=" * 100)
    print("   COMPARISON RESULTS")
    print("=" * 100)
    
    # Print header
    print(f"\n{'Index':<12} {'Risk %':<8} {'Trades':<8} {'Win Rate':<10} {'P&L':<15} {'Return %':<12} {'PF':<8} {'Max DD %':<10}")
    print("-" * 100)
    
    # Group by symbol
    df = pd.DataFrame(all_results)
    
    for symbol in INDEX_CONFIG.keys():
        symbol_data = df[df['symbol'] == symbol]
        for _, row in symbol_data.iterrows():
            pnl_str = f"${row['total_pnl']:,.2f}"
            pf_str = f"{row['profit_factor']:.2f}" if row['profit_factor'] < 100 else "∞"
            print(f"{row['symbol']:<12} {row['risk_percent']:<8} {row['trades']:<8} {row['win_rate']:.1f}%{'':<5} {pnl_str:<15} {row['return_pct']:.2f}%{'':<6} {pf_str:<8} {row['max_drawdown']:.2f}%")
        if len(symbol_data) > 1:
            print("-" * 50)
    
    # Summary comparison
    print("\n" + "=" * 100)
    print("   SUMMARY BY RISK LEVEL")
    print("=" * 100)
    
    for risk in risk_levels:
        risk_data = df[df['risk_percent'] == risk]
        total_trades = risk_data['trades'].sum()
        total_pnl = risk_data['total_pnl'].sum()
        avg_return = risk_data['return_pct'].mean()
        max_dd = risk_data['max_drawdown'].max()
        
        print(f"\n🎯 {risk}% Risk:")
        print(f"   Total Trades: {total_trades}")
        print(f"   Total P&L: ${total_pnl:,.2f}")
        print(f"   Avg Return per Index: {avg_return:.2f}%")
        print(f"   Worst Max DD: {max_dd:.2f}%")
        
        # Trades needed for 8% target
        avg_per_trade = total_pnl / total_trades if total_trades > 0 else 0
        trades_for_target = (capital * 0.08) / avg_per_trade if avg_per_trade > 0 else float('inf')
        print(f"   Avg $/Trade: ${avg_per_trade:,.2f}")
        print(f"   Est. trades for 8% target: {trades_for_target:.1f}")


if __name__ == "__main__":
    main()
