
from typing import List, Dict
import pandas as pd
import numpy as np
from datetime import datetime


def calculate_metrics(trades: List[Dict], initial_capital: float = 1000000) -> Dict:
    """
    Calculate comprehensive backtest performance metrics
    
    Metrics included:
    - Basic: Total trades, wins, losses, win rate
    - PnL: Gross profit, gross loss, net profit, profit factor
    - Risk-adjusted: Sharpe ratio, max drawdown, recovery factor
    - Trade analysis: Avg win, avg loss, largest win/loss, RR stats
    - Streaks: Max consecutive wins/losses
    """
    if not trades:
        return _empty_metrics()
    
    # Basic counts
    total_trades = len(trades)
    wins = [t for t in trades if t.get('pnl', 0) > 0]
    losses = [t for t in trades if t.get('pnl', 0) < 0]
    breakeven = [t for t in trades if t.get('pnl', 0) == 0]
    
    num_wins = len(wins)
    num_losses = len(losses)
    num_breakeven = len(breakeven)
    
    winrate = (num_wins / total_trades) * 100 if total_trades > 0 else 0
    
    # PnL calculations
    pnl_list = [t.get('pnl', 0) for t in trades]
    net_pnl_list = [t.get('net_pnl', t.get('pnl', 0)) for t in trades]
    
    gross_profit = sum(p for p in pnl_list if p > 0)
    gross_loss = abs(sum(p for p in pnl_list if p < 0))
    net_profit = sum(net_pnl_list)
    
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Average trade stats
    avg_win = np.mean([t.get('pnl', 0) for t in wins]) if wins else 0
    avg_loss = np.mean([t.get('pnl', 0) for t in losses]) if losses else 0
    avg_trade = np.mean(pnl_list) if pnl_list else 0
    
    largest_win = max(pnl_list) if pnl_list else 0
    largest_loss = min(pnl_list) if pnl_list else 0
    
    # Risk-Reward analysis
    rr_achieved = [t.get('rr_achieved', 0) for t in trades]
    avg_rr = np.mean(rr_achieved) if rr_achieved else 0
    
    # Target hit analysis
    tp1_hits = sum(1 for t in trades if t.get('tp1_hit', False))
    tp2_hits = sum(1 for t in trades if t.get('tp2_hit', False))
    final_hits = sum(1 for t in trades if t.get('final_hit', False))
    sl_hits = sum(1 for t in trades if t.get('sl_hit', False))
    
    # Equity curve and drawdown
    equity_curve = [initial_capital]
    for pnl in net_pnl_list:
        equity_curve.append(equity_curve[-1] + pnl)
    
    max_drawdown, max_drawdown_pct = calculate_max_drawdown(equity_curve)
    
    # Sharpe Ratio (annualized, assuming 252 trading days)
    if len(net_pnl_list) > 1:
        returns = np.array(net_pnl_list) / initial_capital
        sharpe_ratio = (np.mean(returns) / np.std(returns)) * np.sqrt(252) if np.std(returns) > 0 else 0
    else:
        sharpe_ratio = 0
    
    # Recovery factor
    recovery_factor = net_profit / max_drawdown if max_drawdown > 0 else float('inf')
    
    # Consecutive wins/losses
    max_consec_wins, max_consec_losses = calculate_streaks(trades)
    
    # Direction breakdown
    long_trades = [t for t in trades if t.get('direction') == 'bullish']
    short_trades = [t for t in trades if t.get('direction') == 'bearish']
    
    long_winrate = (sum(1 for t in long_trades if t.get('pnl', 0) > 0) / len(long_trades) * 100) if long_trades else 0
    short_winrate = (sum(1 for t in short_trades if t.get('pnl', 0) > 0) / len(short_trades) * 100) if short_trades else 0
    
    # ROI
    roi = (net_profit / initial_capital) * 100
    
    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        reason = t.get('exit_reason', 'UNKNOWN')
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    
    return {
        # Basic stats
        'total_trades': total_trades,
        'wins': num_wins,
        'losses': num_losses,
        'breakeven': num_breakeven,
        'winrate': winrate,
        
        # PnL stats
        'gross_profit': gross_profit,
        'gross_loss': gross_loss,
        'net_profit': net_profit,
        'amount_earned': gross_profit,  # Backward compatibility
        'amount_lost': -gross_loss,  # Backward compatibility
        'profit_factor': profit_factor,
        'roi_percent': roi,
        
        # Trade averages
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'avg_trade': avg_trade,
        'largest_win': largest_win,
        'largest_loss': largest_loss,
        'avg_rr_achieved': avg_rr,
        
        # Risk metrics
        'max_drawdown': max_drawdown,
        'max_drawdown_pct': max_drawdown_pct,
        'sharpe_ratio': sharpe_ratio,
        'recovery_factor': recovery_factor,
        
        # Target stats
        'tp1_hit_count': tp1_hits,
        'tp2_hit_count': tp2_hits,
        'final_target_hit_count': final_hits,
        'sl_hit_count': sl_hits,
        
        # Streaks
        'max_consecutive_wins': max_consec_wins,
        'max_consecutive_losses': max_consec_losses,
        
        # Direction breakdown
        'long_trades': len(long_trades),
        'short_trades': len(short_trades),
        'long_winrate': long_winrate,
        'short_winrate': short_winrate,
        
        # Exit reasons
        'exit_reasons': exit_reasons,
        
        # Capital
        'initial_capital': initial_capital,
        'final_capital': equity_curve[-1] if equity_curve else initial_capital
    }


def calculate_max_drawdown(equity_curve: List[float]) -> tuple:
    """Calculate maximum drawdown in absolute and percentage terms"""
    if not equity_curve or len(equity_curve) < 2:
        return 0, 0
    
    peak = equity_curve[0]
    max_dd = 0
    max_dd_pct = 0
    
    for equity in equity_curve:
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        drawdown_pct = (drawdown / peak) * 100 if peak > 0 else 0
        
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_pct = drawdown_pct
    
    return max_dd, max_dd_pct


def calculate_streaks(trades: List[Dict]) -> tuple:
    """Calculate max consecutive wins and losses"""
    if not trades:
        return 0, 0
    
    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0
    
    for trade in trades:
        pnl = trade.get('pnl', 0)
        if pnl > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        elif pnl < 0:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)
        else:
            current_wins = 0
            current_losses = 0
    
    return max_wins, max_losses


def _empty_metrics() -> Dict:
    """Return empty metrics dict when no trades"""
    return {
        'total_trades': 0,
        'wins': 0,
        'losses': 0,
        'breakeven': 0,
        'winrate': 0,
        'gross_profit': 0,
        'gross_loss': 0,
        'net_profit': 0,
        'amount_earned': 0,
        'amount_lost': 0,
        'profit_factor': 0,
        'roi_percent': 0,
        'avg_win': 0,
        'avg_loss': 0,
        'avg_trade': 0,
        'largest_win': 0,
        'largest_loss': 0,
        'avg_rr_achieved': 0,
        'max_drawdown': 0,
        'max_drawdown_pct': 0,
        'sharpe_ratio': 0,
        'recovery_factor': 0,
        'tp1_hit_count': 0,
        'tp2_hit_count': 0,
        'final_target_hit_count': 0,
        'sl_hit_count': 0,
        'max_consecutive_wins': 0,
        'max_consecutive_losses': 0,
        'long_trades': 0,
        'short_trades': 0,
        'long_winrate': 0,
        'short_winrate': 0,
        'exit_reasons': {},
        'initial_capital': 0,
        'final_capital': 0
    }


def generate_monthly_breakdown(trades: List[Dict]) -> pd.DataFrame:
    """Generate monthly performance breakdown"""
    if not trades:
        return pd.DataFrame()
    
    monthly_data = {}
    
    for trade in trades:
        date_str = trade.get('date', '')
        if not date_str:
            continue
        
        try:
            date = pd.to_datetime(date_str)
            month_key = date.strftime('%Y-%m')
        except:
            continue
        
        if month_key not in monthly_data:
            monthly_data[month_key] = {
                'trades': 0,
                'wins': 0,
                'losses': 0,
                'pnl': 0
            }
        
        monthly_data[month_key]['trades'] += 1
        monthly_data[month_key]['pnl'] += trade.get('net_pnl', trade.get('pnl', 0))
        
        if trade.get('pnl', 0) > 0:
            monthly_data[month_key]['wins'] += 1
        elif trade.get('pnl', 0) < 0:
            monthly_data[month_key]['losses'] += 1
    
    df = pd.DataFrame.from_dict(monthly_data, orient='index')
    df.index.name = 'month'
    df['winrate'] = (df['wins'] / df['trades'] * 100).round(2)
    df = df.sort_index()
    
    return df


def print_metrics_report(metrics: Dict, currency_symbol: str = '₹') -> None:
    """Print a formatted metrics report to console"""
    cs = currency_symbol
    print("\n" + "=" * 60)
    print("                 BACKTEST PERFORMANCE REPORT")
    print("=" * 60)
    
    print(f"\n📊 TRADE STATISTICS")
    print(f"   Total Trades:     {metrics['total_trades']}")
    print(f"   Wins:             {metrics['wins']}")
    print(f"   Losses:           {metrics['losses']}")
    print(f"   Win Rate:         {metrics['winrate']:.2f}%")
    
    print(f"\n💰 PROFIT & LOSS")
    print(f"   Gross Profit:     {cs}{metrics['gross_profit']:,.2f}")
    print(f"   Gross Loss:       {cs}{metrics['gross_loss']:,.2f}")
    print(f"   Net Profit:       {cs}{metrics['net_profit']:,.2f}")
    print(f"   Profit Factor:    {metrics['profit_factor']:.2f}")
    print(f"   ROI:              {metrics['roi_percent']:.2f}%")
    
    print(f"\n📈 TRADE AVERAGES")
    print(f"   Avg Win:          {cs}{metrics['avg_win']:,.2f}")
    print(f"   Avg Loss:         {cs}{metrics['avg_loss']:,.2f}")
    print(f"   Avg Trade:        {cs}{metrics['avg_trade']:,.2f}")
    print(f"   Avg R:R Achieved: {metrics['avg_rr_achieved']:.2f}")
    
    print(f"\n⚠️  RISK METRICS")
    print(f"   Max Drawdown:     {cs}{metrics['max_drawdown']:,.2f} ({metrics['max_drawdown_pct']:.2f}%)")
    print(f"   Sharpe Ratio:     {metrics['sharpe_ratio']:.2f}")
    print(f"   Recovery Factor:  {metrics['recovery_factor']:.2f}")
    
    print(f"\n🎯 TARGET ANALYSIS")
    print(f"   TP1 Hits:         {metrics['tp1_hit_count']}")
    print(f"   TP2 Hits:         {metrics['tp2_hit_count']}")
    print(f"   Final Target:     {metrics['final_target_hit_count']}")
    print(f"   SL Hits:          {metrics['sl_hit_count']}")
    
    print(f"\n📊 DIRECTION BREAKDOWN")
    print(f"   Long Trades:      {metrics['long_trades']} (Win Rate: {metrics['long_winrate']:.2f}%)")
    print(f"   Short Trades:     {metrics['short_trades']} (Win Rate: {metrics['short_winrate']:.2f}%)")
    
    print(f"\n🔥 STREAKS")
    print(f"   Max Consec Wins:  {metrics['max_consecutive_wins']}")
    print(f"   Max Consec Losses:{metrics['max_consecutive_losses']}")
    
    print("\n" + "=" * 60)
