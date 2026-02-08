"""
Trade Journal & Dashboard
=========================
Tracks all trades in Excel with automatic dashboard updates
Sends daily/weekly reports via Telegram
"""
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import logging
from typing import Optional, Dict, List
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import LineChart, BarChart, Reference
from openpyxl.utils.dataframe import dataframe_to_rows

logger = logging.getLogger(__name__)


class TradeJournal:
    """Manages trade journal in Excel with dashboard"""
    
    def __init__(self, journal_path: str = None):
        if journal_path is None:
            self.journal_path = Path(__file__).parent / 'reports' / 'trade_journal.xlsx'
        else:
            self.journal_path = Path(journal_path)
        
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize Excel file if doesn't exist
        if not self.journal_path.exists():
            self._create_journal()
    
    def _create_journal(self):
        """Create new journal with sheets"""
        wb = Workbook()
        
        # Trades sheet
        ws_trades = wb.active
        ws_trades.title = "Trades"
        headers = [
            "Trade ID", "Date", "Time", "Symbol", "Direction", "Entry Price",
            "Stop Loss", "TP1", "TP2", "TP3", "Lot Size", "Risk Amount",
            "Exit Price", "Exit Time", "Exit Reason", "P&L", "P&L %",
            "Balance After", "Drawdown %", "Notes"
        ]
        ws_trades.append(headers)
        
        # Style headers
        header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)
        for col, cell in enumerate(ws_trades[1], 1):
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")
        
        # Daily Summary sheet
        ws_daily = wb.create_sheet("Daily Summary")
        daily_headers = [
            "Date", "Trades", "Wins", "Losses", "Win Rate %", 
            "Gross P&L", "Net P&L", "Best Trade", "Worst Trade",
            "Balance", "Daily DD %", "Overall DD %"
        ]
        ws_daily.append(daily_headers)
        for cell in ws_daily[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")
        
        # Weekly Summary sheet
        ws_weekly = wb.create_sheet("Weekly Summary")
        weekly_headers = [
            "Week", "Start Date", "End Date", "Trades", "Wins", "Losses",
            "Win Rate %", "Gross P&L", "Net P&L", "Avg Trade",
            "Balance Start", "Balance End", "Weekly Return %"
        ]
        ws_weekly.append(weekly_headers)
        for cell in ws_weekly[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")
        
        # Dashboard sheet
        ws_dash = wb.create_sheet("Dashboard")
        ws_dash["A1"] = "ICT Trading Bot Dashboard"
        ws_dash["A1"].font = Font(size=20, bold=True)
        
        wb.save(self.journal_path)
        logger.info(f"Created trade journal: {self.journal_path}")
    
    def add_trade(self, trade_data: Dict) -> int:
        """Add a new trade entry, returns trade ID"""
        wb = load_workbook(self.journal_path)
        ws = wb["Trades"]
        
        # Generate trade ID
        trade_id = ws.max_row  # Simple incrementing ID
        
        now = datetime.now()
        row = [
            trade_id,
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M:%S"),
            trade_data.get('symbol', ''),
            trade_data.get('direction', ''),
            trade_data.get('entry_price', 0),
            trade_data.get('stop_loss', 0),
            trade_data.get('tp1', 0),
            trade_data.get('tp2', 0),
            trade_data.get('tp3', 0),
            trade_data.get('lot_size', 0),
            trade_data.get('risk_amount', 0),
            '',  # Exit price (filled on close)
            '',  # Exit time
            '',  # Exit reason
            '',  # P&L
            '',  # P&L %
            '',  # Balance after
            '',  # Drawdown
            trade_data.get('notes', '')
        ]
        ws.append(row)
        wb.save(self.journal_path)
        
        logger.info(f"Trade {trade_id} added to journal")
        return trade_id
    
    def close_trade(self, trade_id: int, exit_data: Dict):
        """Update trade with exit information"""
        wb = load_workbook(self.journal_path)
        ws = wb["Trades"]
        
        # Find the trade row
        for row_num in range(2, ws.max_row + 1):
            if ws.cell(row=row_num, column=1).value == trade_id:
                ws.cell(row=row_num, column=13, value=exit_data.get('exit_price', 0))
                ws.cell(row=row_num, column=14, value=datetime.now().strftime("%H:%M:%S"))
                ws.cell(row=row_num, column=15, value=exit_data.get('exit_reason', ''))
                ws.cell(row=row_num, column=16, value=exit_data.get('pnl', 0))
                ws.cell(row=row_num, column=17, value=exit_data.get('pnl_percent', 0))
                ws.cell(row=row_num, column=18, value=exit_data.get('balance_after', 0))
                ws.cell(row=row_num, column=19, value=exit_data.get('drawdown', 0))
                
                # Color code P&L
                pnl = exit_data.get('pnl', 0)
                if pnl > 0:
                    ws.cell(row=row_num, column=16).fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                elif pnl < 0:
                    ws.cell(row=row_num, column=16).fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                
                break
        
        wb.save(self.journal_path)
        logger.info(f"Trade {trade_id} closed in journal")
    
    def get_daily_stats(self, date: str = None) -> Dict:
        """Get statistics for a specific date"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        wb = load_workbook(self.journal_path)
        ws = wb["Trades"]
        
        trades = []
        for row_num in range(2, ws.max_row + 1):
            row_date = ws.cell(row=row_num, column=2).value
            if row_date == date:
                pnl = ws.cell(row=row_num, column=16).value
                if pnl is not None and pnl != '':
                    trades.append({
                        'symbol': ws.cell(row=row_num, column=4).value,
                        'direction': ws.cell(row=row_num, column=5).value,
                        'pnl': float(pnl) if pnl else 0,
                        'exit_reason': ws.cell(row=row_num, column=15).value
                    })
        
        if not trades:
            return {
                'date': date,
                'trades': 0,
                'wins': 0,
                'losses': 0,
                'win_rate': 0,
                'gross_pnl': 0,
                'best_trade': 0,
                'worst_trade': 0
            }
        
        wins = len([t for t in trades if t['pnl'] > 0])
        losses = len([t for t in trades if t['pnl'] < 0])
        pnls = [t['pnl'] for t in trades]
        
        return {
            'date': date,
            'trades': len(trades),
            'wins': wins,
            'losses': losses,
            'win_rate': (wins / len(trades) * 100) if trades else 0,
            'gross_pnl': sum(pnls),
            'best_trade': max(pnls) if pnls else 0,
            'worst_trade': min(pnls) if pnls else 0,
            'trade_details': trades
        }
    
    def get_weekly_stats(self, week_start: str = None) -> Dict:
        """Get statistics for a week"""
        if week_start is None:
            # Get Monday of current week
            today = datetime.now()
            monday = today - timedelta(days=today.weekday())
            week_start = monday.strftime("%Y-%m-%d")
        
        start_date = datetime.strptime(week_start, "%Y-%m-%d")
        end_date = start_date + timedelta(days=6)
        
        wb = load_workbook(self.journal_path)
        ws = wb["Trades"]
        
        trades = []
        for row_num in range(2, ws.max_row + 1):
            row_date_str = ws.cell(row=row_num, column=2).value
            if row_date_str:
                try:
                    row_date = datetime.strptime(row_date_str, "%Y-%m-%d")
                    if start_date <= row_date <= end_date:
                        pnl = ws.cell(row=row_num, column=16).value
                        if pnl is not None and pnl != '':
                            trades.append({
                                'date': row_date_str,
                                'symbol': ws.cell(row=row_num, column=4).value,
                                'pnl': float(pnl) if pnl else 0
                            })
                except:
                    pass
        
        if not trades:
            return {
                'week_start': week_start,
                'week_end': end_date.strftime("%Y-%m-%d"),
                'trades': 0,
                'wins': 0,
                'losses': 0,
                'win_rate': 0,
                'gross_pnl': 0,
                'avg_trade': 0
            }
        
        wins = len([t for t in trades if t['pnl'] > 0])
        losses = len([t for t in trades if t['pnl'] < 0])
        pnls = [t['pnl'] for t in trades]
        
        return {
            'week_start': week_start,
            'week_end': end_date.strftime("%Y-%m-%d"),
            'trades': len(trades),
            'wins': wins,
            'losses': losses,
            'win_rate': (wins / len(trades) * 100) if trades else 0,
            'gross_pnl': sum(pnls),
            'avg_trade': sum(pnls) / len(pnls) if pnls else 0,
            'best_day': None,
            'worst_day': None
        }
    
    def update_daily_summary(self, date: str, balance: float, daily_dd: float, overall_dd: float):
        """Update or add daily summary row"""
        stats = self.get_daily_stats(date)
        
        wb = load_workbook(self.journal_path)
        ws = wb["Daily Summary"]
        
        # Check if date exists
        row_to_update = None
        for row_num in range(2, ws.max_row + 1):
            if ws.cell(row=row_num, column=1).value == date:
                row_to_update = row_num
                break
        
        row_data = [
            date,
            stats['trades'],
            stats['wins'],
            stats['losses'],
            round(stats['win_rate'], 1),
            round(stats['gross_pnl'], 2),
            round(stats['gross_pnl'], 2),
            round(stats['best_trade'], 2),
            round(stats['worst_trade'], 2),
            round(balance, 2),
            round(daily_dd, 2),
            round(overall_dd, 2)
        ]
        
        if row_to_update:
            for col, value in enumerate(row_data, 1):
                ws.cell(row=row_to_update, column=col, value=value)
        else:
            ws.append(row_data)
        
        # Color code P&L
        pnl_col = 6
        last_row = row_to_update or ws.max_row
        pnl_value = ws.cell(row=last_row, column=pnl_col).value
        if pnl_value and pnl_value > 0:
            ws.cell(row=last_row, column=pnl_col).fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
        elif pnl_value and pnl_value < 0:
            ws.cell(row=last_row, column=pnl_col).fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
        
        wb.save(self.journal_path)
    
    def update_dashboard(self, balance: float, initial_balance: float = 100000):
        """Update dashboard with latest stats"""
        wb = load_workbook(self.journal_path)
        ws = wb["Dashboard"]
        
        # Clear old content (except title)
        for row in range(3, 30):
            for col in range(1, 10):
                ws.cell(row=row, column=col, value=None)
        
        # Get all-time stats
        ws_trades = wb["Trades"]
        all_trades = []
        for row_num in range(2, ws_trades.max_row + 1):
            pnl = ws_trades.cell(row=row_num, column=16).value
            if pnl is not None and pnl != '':
                all_trades.append(float(pnl))
        
        total_trades = len(all_trades)
        wins = len([p for p in all_trades if p > 0])
        losses = len([p for p in all_trades if p < 0])
        total_pnl = sum(all_trades)
        
        # Dashboard content
        ws["A3"] = "📊 ACCOUNT OVERVIEW"
        ws["A3"].font = Font(size=14, bold=True)
        
        ws["A5"] = "Current Balance:"
        ws["B5"] = f"${balance:,.2f}"
        ws["B5"].font = Font(bold=True, size=12)
        
        ws["A6"] = "Initial Balance:"
        ws["B6"] = f"${initial_balance:,.2f}"
        
        ws["A7"] = "Total P&L:"
        ws["B7"] = f"${total_pnl:,.2f}"
        ws["B7"].font = Font(bold=True, color="00AA00" if total_pnl >= 0 else "AA0000")
        
        ws["A8"] = "Return:"
        ws["B8"] = f"{((balance - initial_balance) / initial_balance * 100):.2f}%"
        
        ws["A10"] = "📈 TRADING STATS"
        ws["A10"].font = Font(size=14, bold=True)
        
        ws["A12"] = "Total Trades:"
        ws["B12"] = total_trades
        
        ws["A13"] = "Wins:"
        ws["B13"] = wins
        
        ws["A14"] = "Losses:"
        ws["B14"] = losses
        
        ws["A15"] = "Win Rate:"
        ws["B15"] = f"{(wins/total_trades*100):.1f}%" if total_trades > 0 else "N/A"
        
        ws["A16"] = "Avg Win:"
        avg_win = sum([p for p in all_trades if p > 0]) / wins if wins > 0 else 0
        ws["B16"] = f"${avg_win:,.2f}"
        
        ws["A17"] = "Avg Loss:"
        avg_loss = sum([p for p in all_trades if p < 0]) / losses if losses > 0 else 0
        ws["B17"] = f"${avg_loss:,.2f}"
        
        ws["A18"] = "Profit Factor:"
        gross_wins = sum([p for p in all_trades if p > 0])
        gross_losses = abs(sum([p for p in all_trades if p < 0]))
        pf = gross_wins / gross_losses if gross_losses > 0 else 0
        ws["B18"] = f"{pf:.2f}"
        
        ws["A20"] = f"Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ws["A20"].font = Font(italic=True, color="666666")
        
        wb.save(self.journal_path)
        logger.info("Dashboard updated")


class ReportScheduler:
    """Manages scheduled report sending"""
    
    def __init__(self, journal: TradeJournal, telegram):
        self.journal = journal
        self.telegram = telegram
        self.last_daily_report = None
        self.last_weekly_report = None
    
    def check_and_send_reports(self, balance: float, daily_dd: float, overall_dd: float):
        """Check if reports need to be sent"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        
        # Daily report at 3:00 AM IST (end of trading day)
        if now.hour == 3 and now.minute < 10 and self.last_daily_report != today:
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            self.send_daily_report(yesterday, balance, daily_dd, overall_dd)
            self.last_daily_report = today
        
        # Weekly report on Saturday at 8:00 AM
        if now.weekday() == 5 and now.hour == 8 and now.minute < 10 and self.last_weekly_report != today:
            self.send_weekly_report(balance)
            self.last_weekly_report = today
    
    def send_daily_report(self, date: str, balance: float, daily_dd: float, overall_dd: float):
        """Send daily trading report via Telegram"""
        stats = self.journal.get_daily_stats(date)
        
        # Update Excel
        self.journal.update_daily_summary(date, balance, daily_dd, overall_dd)
        self.journal.update_dashboard(balance)
        
        # Build message
        emoji = "📈" if stats['gross_pnl'] >= 0 else "📉"
        
        # Trade breakdown
        trade_lines = ""
        if stats.get('trade_details'):
            for t in stats['trade_details']:
                t_emoji = "✅" if t['pnl'] > 0 else "❌"
                trade_lines += f"\n   {t_emoji} {t['symbol']} {t['direction']}: ${t['pnl']:+,.2f}"
        
        message = f"""
{emoji} <b>DAILY REPORT - {date}</b>

<b>📊 Summary:</b>
├─ Trades: {stats['trades']}
├─ Wins: {stats['wins']} | Losses: {stats['losses']}
├─ Win Rate: {stats['win_rate']:.0f}%
└─ P&L: <b>${stats['gross_pnl']:+,.2f}</b>

<b>🎯 Best/Worst:</b>
├─ Best: ${stats['best_trade']:+,.2f}
└─ Worst: ${stats['worst_trade']:+,.2f}

<b>💰 Account:</b>
├─ Balance: ${balance:,.2f}
├─ Daily DD: {daily_dd:.2f}%
└─ Overall DD: {overall_dd:.2f}%
{f"<b>📋 Trades:</b>{trade_lines}" if trade_lines else ""}

━━━━━━━━━━━━━━━━━━
"""
        self.telegram.send_message(message)
        logger.info(f"Daily report sent for {date}")
    
    def send_weekly_report(self, balance: float):
        """Send weekly trading report via Telegram"""
        # Get last week's Monday
        today = datetime.now()
        last_monday = today - timedelta(days=today.weekday() + 7)
        
        stats = self.journal.get_weekly_stats(last_monday.strftime("%Y-%m-%d"))
        
        emoji = "📈" if stats['gross_pnl'] >= 0 else "📉"
        
        message = f"""
{emoji} <b>WEEKLY REPORT</b>
<b>{stats['week_start']} to {stats['week_end']}</b>

<b>📊 Summary:</b>
├─ Total Trades: {stats['trades']}
├─ Wins: {stats['wins']} | Losses: {stats['losses']}
├─ Win Rate: {stats['win_rate']:.0f}%
├─ Avg Trade: ${stats['avg_trade']:+,.2f}
└─ <b>Weekly P&L: ${stats['gross_pnl']:+,.2f}</b>

<b>💰 Account:</b>
└─ Balance: ${balance:,.2f}

━━━━━━━━━━━━━━━━━━
"""
        self.telegram.send_message(message)
        logger.info(f"Weekly report sent for {stats['week_start']}")
    
    def force_daily_report(self, balance: float, daily_dd: float, overall_dd: float):
        """Force send today's daily report (for testing)"""
        today = datetime.now().strftime("%Y-%m-%d")
        self.send_daily_report(today, balance, daily_dd, overall_dd)
    
    def force_weekly_report(self, balance: float):
        """Force send current week's report (for testing)"""
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        stats = self.journal.get_weekly_stats(monday.strftime("%Y-%m-%d"))
        
        emoji = "📈" if stats['gross_pnl'] >= 0 else "📉"
        message = f"""
{emoji} <b>WEEKLY REPORT (Current Week)</b>
<b>{stats['week_start']} to {stats['week_end']}</b>

<b>📊 Summary:</b>
├─ Total Trades: {stats['trades']}
├─ Wins: {stats['wins']} | Losses: {stats['losses']}
├─ Win Rate: {stats['win_rate']:.0f}%
└─ <b>Weekly P&L: ${stats['gross_pnl']:+,.2f}</b>

<b>💰 Account:</b>
└─ Balance: ${balance:,.2f}
"""
        self.telegram.send_message(message)


if __name__ == "__main__":
    # Test the journal
    journal = TradeJournal()
    print(f"Journal created at: {journal.journal_path}")
