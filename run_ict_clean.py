"""
Run Clean ICT Strategy on SPX500
=================================

Parallel execution across all trading days
"""

import pandas as pd
import sys
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

sys.path.insert(0, str(Path(__file__).parent))

from strategy.strategy_ict_clean import StrategyICTClean, TradeSignalICT


def get_trading_days(df: pd.DataFrame) -> list:
    """Get all unique trading days"""
    return sorted(set(df.index.date))


def calculate_trade_pnl(signal: TradeSignalICT) -> dict:
    """Calculate P&L for a trade"""
    results = {
        'direction': signal.direction,
        'entry_time': str(signal.entry_time),
        'entry_price': signal.entry_price,
        'stop_loss': signal.stop_loss,
        'tp1': signal.tp1,
        'tp2': signal.tp2,
        'tp3': signal.tp3,
        'tp_final': signal.tp_final,
        'tp1_hit': signal.tp1_hit,
        'tp2_hit': signal.tp2_hit,
        'tp3_hit': signal.tp3_hit,
        'tp_final_hit': signal.tp_final_hit,
        'sl_hit': signal.sl_hit,
        'sl_moved_to_be': signal.sl_moved_to_be,
        'remaining_position': signal.position_size,
        'risk_points': signal.risk_points,
        'risk_amount': signal.risk_amount,
        'dollar_per_point': signal.dollar_per_point,
        'ob1_zone': str(signal.ob1_zone),
        'ob2_zone': str(signal.ob2_zone),
        'ob2_extended': str(signal.ob2_extended_zone),
        'bos_count': signal.bos_count,
        'choch_level': signal.choch_level,
        'sweep_level': signal.sweep_level
    }
    
    risk = signal.risk_points
    pnl_points = signal.pnl_points
    
    # If trade didn't complete, calculate based on state
    if signal.position_size > 0:
        # Trade still open - mark as incomplete
        pnl_points = 0
    elif not signal.tp1_hit and not signal.sl_hit:
        # Neither TP1 nor SL hit - something wrong
        pnl_points = -risk
    
    results['pnl_points'] = pnl_points
    results['rr_achieved'] = pnl_points / risk if risk > 0 else 0
    results['pnl_dollars'] = pnl_points * signal.dollar_per_point
    
    return results


def process_day_batch(args):
    """Process a batch of trading days"""
    batch_id, days_batch, data_path, capital, risk_percent = args
    
    # Load data
    df = pd.read_csv(data_path, sep='\t', parse_dates=['Time'])
    df.set_index('Time', inplace=True)
    df.columns = [col.lower() for col in df.columns]
    
    results = []
    
    for day in days_batch:
        prev_day = day - timedelta(days=1)
        while prev_day.weekday() >= 5:
            prev_day -= timedelta(days=1)
        
        # Get data window (previous day for context + current day)
        day_start = pd.Timestamp(day) - timedelta(hours=12)
        day_end = pd.Timestamp(day) + timedelta(hours=23, minutes=59)
        
        day_df = df[(df.index >= day_start) & (df.index <= day_end)].copy()
        
        if len(day_df) < 100:
            continue
        
        # Get PDH/PDL
        prev_day_data = df[df.index.date == prev_day]
        if len(prev_day_data) == 0:
            continue
        
        pdh = prev_day_data['high'].max()
        pdl = prev_day_data['low'].min()
        
        # Get day high/low from market open (session start), not premarket
        current_day_data = day_df[day_df.index.date == day]
        if len(current_day_data) == 0:
            continue
        
        # Session start time (exclude premarket for day high/low calculation)
        session_start = pd.Timestamp(day).replace(hour=16, minute=30)
        market_hours_data = current_day_data[current_day_data.index >= session_start]
        
        if len(market_hours_data) > 0:
            day_high = market_hours_data['high'].max()
            day_low = market_hours_data['low'].min()
        else:
            day_high = current_day_data['high'].max()
            day_low = current_day_data['low'].min()
        
        # Create strategy
        strategy = StrategyICTClean(
            lot_size=1,
            sl_buffer=1.0,  # 1 point buffer
            min_rr=2.0,
            bos_required=2,
            capital=capital,
            risk_percent=risk_percent,
            session_start_hour=16,
            session_start_minute=30
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
            trade_result = calculate_trade_pnl(signal)
            trade_result['date'] = str(day)
            trade_result['batch_id'] = batch_id
            results.append(trade_result)
    
    return batch_id, results


def run_backtest():
    """Run backtest using parallel processing"""
    
    print("=" * 80)
    print("   CLEAN ICT STRATEGY - SPX500 BACKTEST")
    print("=" * 80)
    
    # Load data
    print("\n📂 Loading SPX500 data...")
    data_path = Path(__file__).parent / 'data' / 'raw' / 'spx500_1min.csv'
    df = pd.read_csv(data_path, sep='\t', parse_dates=['Time'])
    df.set_index('Time', inplace=True)
    df.columns = [col.lower() for col in df.columns]
    
    print(f"   Total candles: {len(df):,}")
    print(f"   Date range: {df.index[0]} to {df.index[-1]}")
    
    # Get trading days
    trading_days = get_trading_days(df)
    print(f"\n📅 Total Trading Days: {len(trading_days)}")
    
    # Risk Management
    CAPITAL = 100000.0
    RISK_PERCENT = 1.0
    
    print(f"\n💰 Risk Management:")
    print(f"   Capital: ${CAPITAL:,.0f}")
    print(f"   Risk per trade: {RISK_PERCENT}% (${CAPITAL * RISK_PERCENT / 100:,.0f})")
    print(f"   SL Buffer: 1 point")
    print(f"   BOS Required: 2 (trend confirmation)")
    print(f"   Filter: BOS Liquidity Sweep only")
    
    # Split into 8 batches
    num_threads = 8
    batch_size = len(trading_days) // num_threads + 1
    batches = []
    
    for i in range(num_threads):
        start_idx = i * batch_size
        end_idx = min((i + 1) * batch_size, len(trading_days))
        if start_idx < len(trading_days):
            batch_days = trading_days[start_idx:end_idx]
            batches.append((i, batch_days, str(data_path), CAPITAL, RISK_PERCENT))
    
    print(f"\n🚀 Starting {len(batches)} parallel workers...")
    
    all_trades = []
    start_time = datetime.now()
    
    with ProcessPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(process_day_batch, batch): batch[0] for batch in batches}
        
        for future in as_completed(futures):
            batch_id = futures[future]
            try:
                result_batch_id, trades = future.result()
                all_trades.extend(trades)
                print(f"   ✅ Worker {result_batch_id + 1} complete: {len(trades)} trades")
            except Exception as e:
                print(f"   ❌ Worker {batch_id + 1} failed: {e}")
                import traceback
                traceback.print_exc()
    
    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n⏱️ Processing time: {elapsed:.1f} seconds")
    
    # Results
    print("\n" + "=" * 80)
    print("   PERFORMANCE SUMMARY")
    print("=" * 80)
    
    if not all_trades:
        print("\n❌ No trades generated!")
        return
    
    trades_df = pd.DataFrame(all_trades)
    trades_df = trades_df.sort_values('entry_time')
    
    total_trades = len(trades_df)
    wins = len(trades_df[trades_df['pnl_dollars'] > 0])
    losses = len(trades_df[trades_df['pnl_dollars'] < 0])
    breakeven = len(trades_df[trades_df['pnl_dollars'] == 0])
    
    total_pnl = trades_df['pnl_dollars'].sum()
    win_rate = wins / total_trades * 100 if total_trades > 0 else 0
    
    avg_winner = trades_df[trades_df['pnl_dollars'] > 0]['pnl_dollars'].mean() if wins > 0 else 0
    avg_loser = trades_df[trades_df['pnl_dollars'] < 0]['pnl_dollars'].mean() if losses > 0 else 0
    
    gross_profit = trades_df[trades_df['pnl_dollars'] > 0]['pnl_dollars'].sum()
    gross_loss = abs(trades_df[trades_df['pnl_dollars'] < 0]['pnl_dollars'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    tp1_hits = trades_df['tp1_hit'].sum()
    tp2_hits = trades_df['tp2_hit'].sum()
    tp3_hits = trades_df['tp3_hit'].sum()
    tp_final_hits = trades_df['tp_final_hit'].sum() if 'tp_final_hit' in trades_df.columns else 0
    
    avg_rr = trades_df['rr_achieved'].mean()
    
    print(f"\n📊 Total Trades: {total_trades}")
    print(f"   Wins: {wins} | Losses: {losses} | Breakeven: {breakeven}")
    print(f"   Win Rate: {win_rate:.1f}%")
    
    print(f"\n💰 P&L Summary:")
    print(f"   Total P&L: ${total_pnl:,.2f}")
    print(f"   Avg Winner: ${avg_winner:,.2f}")
    print(f"   Avg Loser: ${avg_loser:,.2f}")
    print(f"   Return on Capital: {total_pnl / CAPITAL * 100:.2f}%")
    print(f"   Profit Factor: {profit_factor:.2f}")
    
    print(f"\n🎯 Target Stats:")
    print(f"   TP1 Hit Rate: {tp1_hits}/{total_trades} ({tp1_hits/total_trades*100:.1f}%)" if total_trades > 0 else "   No trades")
    print(f"   TP2 Hit Rate: {tp2_hits}/{total_trades} ({tp2_hits/total_trades*100:.1f}%)" if total_trades > 0 else "")
    print(f"   TP3 (3:1) Hit Rate: {tp3_hits}/{total_trades} ({tp3_hits/total_trades*100:.1f}%)" if total_trades > 0 else "")
    print(f"   Final Target Hit Rate: {tp_final_hits}/{total_trades} ({tp_final_hits/total_trades*100:.1f}%)" if total_trades > 0 else "")
    
    print(f"\n📈 Average R:R Achieved: {avg_rr:.2f}")
    
    # Monthly breakdown
    if total_trades > 0:
        trades_df['month'] = pd.to_datetime(trades_df['date']).dt.to_period('M')
        print("\n📅 Monthly Breakdown:")
        print("-" * 60)
        for month in sorted(trades_df['month'].unique()):
            m = trades_df[trades_df['month'] == month]
            pnl = m['pnl_dollars'].sum()
            trades = len(m)
            tp1 = int(m['tp1_hit'].sum())
            icon = "🟢" if pnl > 0 else "🔴"
            print(f"   {month}: {trades} trades | {icon} ${pnl:,.2f} | TP1: {tp1}/{trades}")
    
    # Save results
    output_dir = Path(__file__).parent / 'reports' / 'ict_clean'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    trades_df.to_csv(output_dir / 'all_trades.csv', index=False)
    print(f"\n📁 Trade log saved: {output_dir / 'all_trades.csv'}")
    
    # Generate HTML dashboard
    generate_dashboard(trades_df, output_dir, CAPITAL, RISK_PERCENT)
    
    print("\n" + "=" * 80)
    print("   BACKTEST COMPLETE")
    print("=" * 80)


def generate_dashboard(trades_df, output_dir, capital, risk_percent):
    """Generate HTML dashboard"""
    
    total_pnl = trades_df['pnl_dollars'].sum()
    total_trades = len(trades_df)
    wins = len(trades_df[trades_df['pnl_dollars'] > 0])
    win_rate = wins / total_trades * 100 if total_trades > 0 else 0
    
    gross_profit = trades_df[trades_df['pnl_dollars'] > 0]['pnl_dollars'].sum()
    gross_loss = abs(trades_df[trades_df['pnl_dollars'] < 0]['pnl_dollars'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
    
    # Equity curve
    trades_df = trades_df.sort_values('entry_time')
    equity = [capital]
    for pnl in trades_df['pnl_dollars']:
        equity.append(equity[-1] + pnl)
    
    # Max drawdown
    peak = capital
    max_dd = 0
    for eq in equity:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100
        max_dd = max(max_dd, dd)
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Clean ICT Strategy - SPX500 Results</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #1a1a2e; color: #eee; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        h1 {{ color: #00d4ff; text-align: center; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 20px; margin: 20px 0; }}
        .stat-card {{ background: #16213e; padding: 20px; border-radius: 10px; text-align: center; }}
        .stat-value {{ font-size: 28px; font-weight: bold; color: #00d4ff; }}
        .stat-label {{ color: #888; margin-top: 5px; }}
        .positive {{ color: #00ff88; }}
        .negative {{ color: #ff4444; }}
        .chart-container {{ background: #16213e; padding: 20px; border-radius: 10px; margin: 20px 0; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #333; font-size: 12px; }}
        th {{ background: #0f3460; color: #00d4ff; }}
        tr:hover {{ background: #1f4068; }}
        .filter-info {{ background: #0f3460; padding: 15px; border-radius: 10px; margin: 20px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Clean ICT Strategy - SPX500 Backtest</h1>
        <p style="text-align: center; color: #888;">
            {trades_df['date'].min()} to {trades_df['date'].max()} | 
            Capital: ${capital:,.0f} | Risk: {risk_percent}%/trade
        </p>
        
        <div class="filter-info">
            <strong>Strategy Rules:</strong> 2 BOS Trend Confirmation → BOS Liquidity Sweep → OB1 → CHoCH → OB2.0 Entry (Extended for wicks) → SL Buffer: 1pt
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value">{total_trades}</div>
                <div class="stat-label">Total Trades</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{win_rate:.1f}%</div>
                <div class="stat-label">Win Rate</div>
            </div>
            <div class="stat-card">
                <div class="stat-value {'positive' if total_pnl > 0 else 'negative'}">${total_pnl:,.2f}</div>
                <div class="stat-label">Total P&L</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{profit_factor:.2f}</div>
                <div class="stat-label">Profit Factor</div>
            </div>
            <div class="stat-card">
                <div class="stat-value negative">{max_dd:.1f}%</div>
                <div class="stat-label">Max Drawdown</div>
            </div>
        </div>
        
        <div class="chart-container">
            <h3>Equity Curve</h3>
            <canvas id="equityChart"></canvas>
        </div>
        
        <h3>Trade Log</h3>
        <table>
            <tr>
                <th>Date</th>
                <th>Dir</th>
                <th>Entry</th>
                <th>SL</th>
                <th>TP1</th>
                <th>TP2</th>
                <th>TP3</th>
                <th>Result</th>
                <th>P&L</th>
                <th>R:R</th>
            </tr>
"""
    
    for _, trade in trades_df.iterrows():
        result = ""
        if trade['tp1_hit']:
            result += "TP1 "
        if trade['tp2_hit']:
            result += "TP2 "
        if trade['tp3_hit']:
            result += "TP3"
        if trade['sl_hit']:
            result = "SL"
        if trade['sl_moved_to_be'] and trade['remaining_position'] == 0 and not trade['tp3_hit']:
            result += "BE"
        
        pnl_class = 'positive' if trade['pnl_dollars'] > 0 else 'negative' if trade['pnl_dollars'] < 0 else ''
        
        html += f"""
            <tr>
                <td>{trade['date']}</td>
                <td>{'🟢' if trade['direction'] == 'bullish' else '🔴'}</td>
                <td>{trade['entry_price']:.2f}</td>
                <td>{trade['stop_loss']:.2f}</td>
                <td>{trade['tp1']:.2f}</td>
                <td>{trade['tp2']:.2f}</td>
                <td>{trade['tp3']:.2f}</td>
                <td>{result}</td>
                <td class="{pnl_class}">${trade['pnl_dollars']:,.2f}</td>
                <td>{trade['rr_achieved']:.2f}</td>
            </tr>
"""
    
    equity_data = [round(e, 2) for e in equity]
    
    html += f"""
        </table>
    </div>
    
    <script>
        const ctx = document.getElementById('equityChart').getContext('2d');
        new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: {list(range(len(equity_data)))},
                datasets: [{{
                    label: 'Equity',
                    data: {equity_data},
                    borderColor: '#00d4ff',
                    backgroundColor: 'rgba(0, 212, 255, 0.1)',
                    fill: true,
                    tension: 0.1
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{
                    y: {{ grid: {{ color: '#333' }}, ticks: {{ color: '#888' }} }},
                    x: {{ grid: {{ color: '#333' }}, ticks: {{ display: false }} }}
                }}
            }}
        }});
    </script>
</body>
</html>
"""
    
    with open(output_dir / 'dashboard.html', 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"📁 Dashboard saved: {output_dir / 'dashboard.html'}")


if __name__ == '__main__':
    multiprocessing.freeze_support()
    run_backtest()
