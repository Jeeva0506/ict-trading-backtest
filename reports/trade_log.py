"""
Trade Logging Module - STEP 10 of Strategy

Logs all trade details as specified:
- Date
- Entry price
- SL
- TP1, TP2, Final target
- Exit price
- R:R achieved
- Win / Loss
- Screenshot path
"""

from pathlib import Path
from typing import List, Dict, Optional
import pandas as pd
from datetime import datetime
import json


class TradeLogger:
    """Comprehensive trade logging for backtesting and live trading"""
    
    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or Path(__file__).parent / 'screenshots'
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.trades: List[Dict] = []
    
    def log_trade(self, trade: Dict) -> None:
        """Add a trade to the log"""
        self.trades.append(trade)
    
    def export_to_csv(self, filename: str = None) -> Path:
        """Export trades to CSV file"""
        if not self.trades:
            print("No trades to export")
            return None
        
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'trade_log_{timestamp}.csv'
        
        filepath = self.output_dir / filename
        
        # Flatten trades for CSV export
        flat_trades = []
        for trade in self.trades:
            ob_zone = trade.get('ob_zone', (0, 0))
            ob2_zone = trade.get('ob2_zone', trade.get('ob_zone', (0, 0)))
            
            flat_trade = {
                'date': trade.get('date', ''),
                'direction': trade.get('direction', ''),
                'entry_time': str(trade.get('entry_time', '')),
                'entry_price': trade.get('entry_price', 0),
                'stop_loss': trade.get('stop_loss', 0),
                'tp1': trade.get('tp1', 0),
                'tp2': trade.get('tp2', 0),
                'final_target': trade.get('final_target', 0),
                'exit_time': str(trade.get('exit_time', '')),
                'exit_price': trade.get('exit_price', 0),
                'exit_reason': trade.get('exit_reason', ''),
                'pnl': trade.get('pnl', 0),
                'net_pnl': trade.get('net_pnl', trade.get('pnl', 0)),
                'rr_achieved': trade.get('rr_achieved', 0),
                'win': trade.get('win', False),
                'tp1_hit': trade.get('tp1_hit', False),
                'tp2_hit': trade.get('tp2_hit', False),
                'final_hit': trade.get('final_hit', False),
                'sl_hit': trade.get('sl_hit', False),
                'ob1_high': ob_zone[0] if ob_zone else 0,
                'ob1_low': ob_zone[1] if ob_zone else 0,
                'ob2_high': ob2_zone[0] if ob2_zone else 0,
                'ob2_low': ob2_zone[1] if ob2_zone else 0,
                'bos_idx': trade.get('bos_idx', None),
                'choch_idx': trade.get('choch_idx', None),
                'position_size': trade.get('position_size', 1),
                # Trend confirmation columns
                'confirmed_trend': trade.get('confirmed_trend', ''),
                'bos_count_bullish': trade.get('bos_count_bullish', 0),
                'bos_count_bearish': trade.get('bos_count_bearish', 0),
            }
            flat_trades.append(flat_trade)
        
        df = pd.DataFrame(flat_trades)
        df.to_csv(filepath, index=False)
        print(f"Trade log exported to: {filepath}")
        
        return filepath
    
    def export_to_json(self, filename: str = None) -> Path:
        """Export trades to JSON file"""
        if not self.trades:
            print("No trades to export")
            return None
        
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'trade_log_{timestamp}.json'
        
        filepath = self.output_dir / filename
        
        # Convert datetime objects to strings for JSON serialization
        json_trades = []
        for trade in self.trades:
            json_trade = {}
            for key, value in trade.items():
                if hasattr(value, 'isoformat'):
                    json_trade[key] = value.isoformat()
                elif isinstance(value, tuple):
                    json_trade[key] = list(value)
                else:
                    json_trade[key] = value
            json_trades.append(json_trade)
        
        with open(filepath, 'w') as f:
            json.dump(json_trades, f, indent=2, default=str)
        
        print(f"Trade log exported to: {filepath}")
        return filepath
    
    def generate_summary_report(self) -> str:
        """Generate a text summary of all trades"""
        if not self.trades:
            return "No trades recorded"
        
        report = []
        report.append("=" * 80)
        report.append("TRADE LOG SUMMARY - OB 2.0 Entry Method")
        report.append("=" * 80)
        
        for i, trade in enumerate(self.trades, 1):
            ob_zone = trade.get('ob_zone', (0, 0))
            ob2_zone = trade.get('ob2_zone', trade.get('ob_zone', (0, 0)))
            
            report.append(f"\nTrade #{i}")
            report.append("-" * 40)
            report.append(f"  Date:          {trade.get('date', 'N/A')}")
            report.append(f"  Direction:     {trade.get('direction', 'N/A').upper()}")
            report.append(f"  Entry Time:    {trade.get('entry_time', 'N/A')}")
            report.append(f"  Entry Price:   ₹{trade.get('entry_price', 0):,.2f}")
            report.append(f"  Stop Loss:     ₹{trade.get('stop_loss', 0):,.2f}")
            report.append(f"  TP1:           ₹{trade.get('tp1', 0):,.2f}")
            report.append(f"  TP2:           ₹{trade.get('tp2', 0):,.2f}")
            report.append(f"  Final Target:  ₹{trade.get('final_target', 0):,.2f}")
            if ob_zone:
                report.append(f"  OB 1 Zone:     {ob_zone[0]:,.2f} - {ob_zone[1]:,.2f}")
            if ob2_zone:
                report.append(f"  OB 2.0 Zone:   {ob2_zone[0]:,.2f} - {ob2_zone[1]:,.2f}")
            report.append(f"  Exit Time:     {trade.get('exit_time', 'N/A')}")
            report.append(f"  Exit Price:    ₹{trade.get('exit_price', 0):,.2f}")
            report.append(f"  Exit Reason:   {trade.get('exit_reason', 'N/A')}")
            report.append(f"  PnL:           ₹{trade.get('pnl', 0):,.2f}")
            report.append(f"  R:R Achieved:  {trade.get('rr_achieved', 0):.2f}")
            report.append(f"  TP1 Hit:       {'✅' if trade.get('tp1_hit', False) else '❌'}")
            report.append(f"  TP2 Hit:       {'✅' if trade.get('tp2_hit', False) else '❌'}")
            report.append(f"  Result:        {'✅ WIN' if trade.get('win', False) else '❌ LOSS'}")
        
        report.append("\n" + "=" * 80)
        
        return "\n".join(report)


def save_trade_log(trades: List[Dict], output_dir: Path = None) -> Dict[str, Path]:
    """
    Convenience function to save trade log in multiple formats
    
    Returns dict with paths to saved files
    """
    logger = TradeLogger(output_dir)
    logger.trades = trades
    
    paths = {}
    paths['csv'] = logger.export_to_csv()
    paths['json'] = logger.export_to_json()
    
    # Save summary report
    summary = logger.generate_summary_report()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    summary_path = logger.output_dir / f'trade_summary_{timestamp}.txt'
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write(summary)
    paths['summary'] = summary_path
    
    print(f"Summary saved to: {summary_path}")
    
    return paths


def load_trade_log(filepath: Path) -> List[Dict]:
    """Load trade log from file"""
    if filepath.suffix == '.csv':
        df = pd.read_csv(filepath)
        return df.to_dict('records')
    elif filepath.suffix == '.json':
        with open(filepath, 'r') as f:
            return json.load(f)
    else:
        raise ValueError(f"Unsupported file format: {filepath.suffix}")
