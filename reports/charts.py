

"""
Chart Generation Module for Backtesting Reports
"""

from pathlib import Path
from typing import List, Tuple, Optional, Dict
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import numpy as np


def plot_equity_curve(equity_curve: List[float], output_path: Path, initial_capital: float = 1000000) -> None:
    """Plot equity curve with drawdown visualization"""
    if not equity_curve:
        print("No equity data to plot")
        return
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={'height_ratios': [3, 1]})
    
    # Equity curve
    x = range(len(equity_curve))
    ax1.plot(x, equity_curve, label='Equity', color='#2E86AB', linewidth=1.5)
    ax1.axhline(y=initial_capital, color='gray', linestyle='--', alpha=0.5, label='Initial Capital')
    ax1.fill_between(x, initial_capital, equity_curve, 
                     where=[e >= initial_capital for e in equity_curve], 
                     color='#28A745', alpha=0.3, label='Profit')
    ax1.fill_between(x, initial_capital, equity_curve, 
                     where=[e < initial_capital for e in equity_curve], 
                     color='#DC3545', alpha=0.3, label='Loss')
    
    ax1.set_title('Equity Curve', fontsize=14, fontweight='bold')
    ax1.set_xlabel('Trade Number')
    ax1.set_ylabel('Equity (₹)')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'₹{x:,.0f}'))
    
    # Drawdown
    peak = np.maximum.accumulate(equity_curve)
    drawdown = (peak - equity_curve) / peak * 100
    
    ax2.fill_between(x, 0, drawdown, color='#DC3545', alpha=0.5)
    ax2.plot(x, drawdown, color='#DC3545', linewidth=1)
    ax2.set_title('Drawdown %', fontsize=12)
    ax2.set_xlabel('Trade Number')
    ax2.set_ylabel('Drawdown %')
    ax2.grid(True, alpha=0.3)
    ax2.invert_yaxis()  # Drawdown should go down
    
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()


def plot_dashboard(metrics: Dict, equity_curve: List[float], output_path: Path) -> None:
    """Generate comprehensive dashboard with metrics and charts"""
    fig = plt.figure(figsize=(16, 12))
    
    # Create grid layout
    gs = fig.add_gridspec(3, 3, hspace=0.3, wspace=0.3)
    
    # 1. Equity Curve (top, spanning 2 columns)
    ax1 = fig.add_subplot(gs[0, :2])
    if equity_curve:
        x = range(len(equity_curve))
        ax1.plot(x, equity_curve, color='#2E86AB', linewidth=2)
        ax1.fill_between(x, min(equity_curve) * 0.98, equity_curve, alpha=0.3, color='#2E86AB')
        ax1.set_title('Equity Curve', fontsize=12, fontweight='bold')
        ax1.set_xlabel('Trade Number')
        ax1.set_ylabel('Equity (₹)')
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'₹{x/1000:.0f}K'))
        ax1.grid(True, alpha=0.3)
    
    # 2. Win/Loss Pie Chart
    ax2 = fig.add_subplot(gs[0, 2])
    wins = metrics.get('wins', 0)
    losses = metrics.get('losses', 0)
    if wins + losses > 0:
        sizes = [wins, losses]
        colors = ['#28A745', '#DC3545']
        labels = [f'Wins\n{wins}', f'Losses\n{losses}']
        ax2.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', 
                startangle=90, explode=(0.05, 0))
        ax2.set_title('Win/Loss Distribution', fontsize=12, fontweight='bold')
    
    # 3. Key Metrics Panel
    ax3 = fig.add_subplot(gs[1, 0])
    ax3.axis('off')
    metrics_text = f"""
    PERFORMANCE SUMMARY
    ━━━━━━━━━━━━━━━━━━━━━━━━━
    Total Trades:     {metrics.get('total_trades', 0)}
    Win Rate:         {metrics.get('winrate', 0):.1f}%
    Profit Factor:    {metrics.get('profit_factor', 0):.2f}
    
    Net Profit:       ₹{metrics.get('net_profit', 0):,.0f}
    ROI:              {metrics.get('roi_percent', 0):.1f}%
    
    Max Drawdown:     {metrics.get('max_drawdown_pct', 0):.1f}%
    Sharpe Ratio:     {metrics.get('sharpe_ratio', 0):.2f}
    """
    ax3.text(0.1, 0.9, metrics_text, transform=ax3.transAxes, fontsize=11,
             verticalalignment='top', fontfamily='monospace',
             bbox=dict(boxstyle='round', facecolor='#f8f9fa', alpha=0.8))
    
    # 4. Trade Averages
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.axis('off')
    avg_text = f"""
    TRADE AVERAGES
    ━━━━━━━━━━━━━━━━━━━━━━━━━
    Avg Win:          ₹{metrics.get('avg_win', 0):,.0f}
    Avg Loss:         ₹{metrics.get('avg_loss', 0):,.0f}
    Avg Trade:        ₹{metrics.get('avg_trade', 0):,.0f}
    
    Largest Win:      ₹{metrics.get('largest_win', 0):,.0f}
    Largest Loss:     ₹{metrics.get('largest_loss', 0):,.0f}
    
    Avg R:R:          {metrics.get('avg_rr_achieved', 0):.2f}
    """
    ax4.text(0.1, 0.9, avg_text, transform=ax4.transAxes, fontsize=11,
             verticalalignment='top', fontfamily='monospace',
             bbox=dict(boxstyle='round', facecolor='#f8f9fa', alpha=0.8))
    
    # 5. Target Analysis
    ax5 = fig.add_subplot(gs[1, 2])
    targets = ['TP1', 'TP2', 'Final', 'SL']
    values = [
        metrics.get('tp1_hit_count', 0),
        metrics.get('tp2_hit_count', 0),
        metrics.get('final_target_hit_count', 0),
        metrics.get('sl_hit_count', 0)
    ]
    colors = ['#28A745', '#17A2B8', '#FFC107', '#DC3545']
    bars = ax5.bar(targets, values, color=colors)
    ax5.set_title('Exit Analysis', fontsize=12, fontweight='bold')
    ax5.set_ylabel('Count')
    for bar, val in zip(bars, values):
        ax5.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5, 
                 str(val), ha='center', va='bottom', fontweight='bold')
    
    # 6. Direction Breakdown
    ax6 = fig.add_subplot(gs[2, 0])
    directions = ['Long', 'Short']
    counts = [metrics.get('long_trades', 0), metrics.get('short_trades', 0)]
    winrates = [metrics.get('long_winrate', 0), metrics.get('short_winrate', 0)]
    
    x_pos = np.arange(len(directions))
    width = 0.35
    
    bars1 = ax6.bar(x_pos - width/2, counts, width, label='Trades', color='#2E86AB')
    ax6.set_ylabel('Trade Count')
    ax6.set_title('Direction Breakdown', fontsize=12, fontweight='bold')
    ax6.set_xticks(x_pos)
    ax6.set_xticklabels(directions)
    
    ax6_twin = ax6.twinx()
    bars2 = ax6_twin.bar(x_pos + width/2, winrates, width, label='Win Rate %', color='#28A745', alpha=0.7)
    ax6_twin.set_ylabel('Win Rate %')
    ax6_twin.set_ylim(0, 100)
    
    # 7. Drawdown Chart
    ax7 = fig.add_subplot(gs[2, 1:])
    if equity_curve:
        peak = np.maximum.accumulate(equity_curve)
        drawdown = (peak - np.array(equity_curve)) / peak * 100
        x = range(len(drawdown))
        ax7.fill_between(x, 0, drawdown, color='#DC3545', alpha=0.5)
        ax7.plot(x, drawdown, color='#DC3545', linewidth=1)
        ax7.set_title('Drawdown %', fontsize=12, fontweight='bold')
        ax7.set_xlabel('Trade Number')
        ax7.set_ylabel('Drawdown %')
        ax7.grid(True, alpha=0.3)
        ax7.invert_yaxis()
    
    # Main title
    fig.suptitle('NIFTY Futures Backtest Report', fontsize=16, fontweight='bold', y=0.98)
    
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()


def plot_monthly_returns(trades: List[Dict], output_path: Path) -> None:
    """Plot monthly returns heatmap"""
    if not trades:
        return
    
    monthly_pnl = {}
    for trade in trades:
        date_str = trade.get('date', '')
        if not date_str:
            continue
        try:
            date = pd.to_datetime(date_str)
            key = (date.year, date.month)
            pnl = trade.get('net_pnl', trade.get('pnl', 0))
            monthly_pnl[key] = monthly_pnl.get(key, 0) + pnl
        except:
            continue
    
    if not monthly_pnl:
        return
    
    # Create DataFrame
    years = sorted(set(k[0] for k in monthly_pnl.keys()))
    months = range(1, 13)
    
    data = []
    for year in years:
        row = []
        for month in months:
            row.append(monthly_pnl.get((year, month), 0))
        data.append(row)
    
    df = pd.DataFrame(data, index=years, 
                      columns=['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])
    
    fig, ax = plt.subplots(figsize=(14, 6))
    
    # Create heatmap
    cmap = plt.cm.RdYlGn
    norm = plt.Normalize(vmin=df.min().min(), vmax=df.max().max())
    
    im = ax.imshow(df.values, cmap=cmap, aspect='auto')
    
    ax.set_xticks(np.arange(len(df.columns)))
    ax.set_yticks(np.arange(len(df.index)))
    ax.set_xticklabels(df.columns)
    ax.set_yticklabels(df.index)
    
    # Add text annotations
    for i in range(len(df.index)):
        for j in range(len(df.columns)):
            val = df.iloc[i, j]
            text = ax.text(j, i, f'₹{val/1000:.0f}K' if val != 0 else '-',
                          ha='center', va='center', 
                          color='white' if abs(val) > df.abs().max().max() * 0.5 else 'black')
    
    ax.set_title('Monthly Returns Heatmap', fontsize=14, fontweight='bold')
    
    plt.colorbar(im, ax=ax, label='P&L (₹)')
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()


def plot_price_chart_with_markings(
    df: pd.DataFrame,
    trades: List[dict],
    output_path: Path
) -> None:
    """Plot price chart with trade markings"""
    plt.figure(figsize=(14, 7))
    plt.plot(df['datetime'], df['close'], label='Close Price', color='black', linewidth=1)

    for idx, trade in enumerate(trades):
        # OB zone
        if 'ob_zone' in trade:
            ob_high, ob_low = trade['ob_zone']
            entry_idx = df.index[df['datetime'] == trade['entry_time']][0] if 'entry_time' in trade else None
            ob_start_idx = max(0, entry_idx-10) if entry_idx else 0
            ob_end_idx = entry_idx if entry_idx else 0
            plt.fill_between(df['datetime'][ob_start_idx:ob_end_idx+1], ob_low, ob_high, 
                           color='saddlebrown', alpha=0.3, label='OB' if idx==0 else None)
            plt.text(df['datetime'][ob_start_idx], ob_high, 'OB', color='red', fontsize=12, va='bottom')
        
        # Entry
        if 'entry_time' in trade and 'entry_price' in trade:
            plt.scatter(trade['entry_time'], trade['entry_price'], 
                       color='lime', marker='^', s=120, label='Entry' if idx==0 else None)
        
        # Stop Loss
        if 'stop_loss' in trade and 'entry_time' in trade:
            plt.scatter(trade['entry_time'], trade['stop_loss'], 
                       color='red', marker='x', s=100, label='SL' if idx==0 else None)
        
        # Targets
        if 'tp1' in trade and 'entry_time' in trade:
            plt.scatter(trade['entry_time'], trade['tp1'], 
                       color='orange', marker='*', s=80, label='TP1' if idx==0 else None)
        
        if 'tp2' in trade and 'entry_time' in trade:
            plt.scatter(trade['entry_time'], trade['tp2'], 
                       color='yellow', marker='*', s=80, label='TP2' if idx==0 else None)
        
        # Exit
        if 'exit_time' in trade and 'exit_price' in trade:
            color = 'green' if trade.get('win', False) else 'red'
            plt.scatter(trade['exit_time'], trade['exit_price'], 
                       color=color, marker='o', s=100, label='Exit' if idx==0 else None)

    plt.title('Price Chart with Trade Markings')
    plt.xlabel('Datetime')
    plt.ylabel('Price')
    plt.legend(loc='best')
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150)
    plt.close()
