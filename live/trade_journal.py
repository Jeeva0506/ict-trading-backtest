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
        else:
            self._migrate_schema()
    
    def _migrate_schema(self):
        """Upgrade existing journal to new schema if needed."""
        try:
            wb = load_workbook(self.journal_path)
            ws = wb["Trades"]
            current_headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
            header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
            header_font = Font(color="FFFFFF", bold=True)
            
            # Detect old 20-column schema (has "Entry Price" at col F instead of "Planned Entry")
            if "Entry Price" in current_headers and "Planned Entry" not in current_headers:
                logger.info("Migrating trade journal from v1 (20-col) to v2 (35-col) schema...")
                # Rename existing file as backup
                backup = self.journal_path.with_suffix('.v1_backup.xlsx')
                import shutil
                shutil.copy2(self.journal_path, backup)
                logger.info(f"Backup saved to {backup}")
                
                # Create new journal with updated headers
                self._create_journal()
                logger.info("New journal created with v2 schema. Old data preserved in backup.")
                return  # fresh journal already has v3 columns
            elif len(current_headers) < 35:
                # Partial migration - just check header count
                logger.info(f"Journal has {len(current_headers)} columns (expected 40). "
                            f"Will continue with current schema.")

            # v2 → v3 migration: add 5 edge-diagnostic columns (35 → 40)
            current_headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
            if len(current_headers) >= 35 and "Spread Entry" not in current_headers:
                logger.info("Migrating trade journal from v2 (35-col) to v3 (40-col)...")
                new_cols = ["Spread Entry", "Spread Exit", "Exit Slip Pts", "Session", "Weekday"]
                for i, hdr in enumerate(new_cols, len(current_headers) + 1):
                    cell = ws.cell(row=1, column=i, value=hdr)
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal="center")
                wb.save(self.journal_path)
                logger.info("Migrated to v3 schema (40 columns)")

            # v3 → v4 migration: add commission/swap/risk_tier columns (40 → 43)
            current_headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
            if "Commission" not in current_headers:
                logger.info("Migrating trade journal to v4 (adding Commission/Swap/Risk Tier)...")
                new_cols = ["Commission", "Swap", "Risk Tier"]
                for i, hdr in enumerate(new_cols, len(current_headers) + 1):
                    cell = ws.cell(row=1, column=i, value=hdr)
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal="center")
                wb.save(self.journal_path)
                logger.info("Migrated to v4 schema (43 columns)")
        except Exception as e:
            logger.warning(f"Schema migration check failed: {e}")
    
    def _create_journal(self):
        """Create new journal with sheets"""
        wb = Workbook()
        
        # Trades sheet
        ws_trades = wb.active
        ws_trades.title = "Trades"
        headers = [
            "Trade ID",          # A
            "Date",              # B
            "Time",              # C
            "Symbol",            # D
            "Direction",         # E
            "Planned Entry",     # F  - strategy signal price
            "Actual Entry",      # G  - broker fill price
            "Slippage Pts",      # H  - points (actual - planned)
            "Slippage $",        # I  - dollar impact
            "Stop Loss",         # J
            "TP",                # K
            "Lot Size",          # L
            "Risk Amount",       # M
            "R (Risk Pts)",      # N  - SL distance in points
            "Planned RR",        # O  - target RR (2.5)
            "Signal Time",       # P  - when ORB breakout detected
            "Fill Time",         # Q  - when order filled
            "Exec Delay (s)",    # R  - signal-to-fill seconds
            "HTF Trend",         # S  - H1 EMA trend at entry
            "ORB Range",         # T  - ORB high-low range
            "Exit Price",        # U
            "Exit Time",         # V
            "Exit Reason",       # W
            "P&L",               # X
            "P&L %",             # Y
            "Actual RR",         # Z  - realized P&L / R
            "MFE (pts)",         # AA - max favorable excursion
            "MAE (pts)",         # AB - max adverse excursion
            "MFE (R)",           # AC - MFE in R-multiples
            "MAE (R)",           # AD - MAE in R-multiples
            "Time in Trade",     # AE - duration string
            "BE Hit?",           # AF - Yes/No
            "Balance After",     # AG
            "Drawdown %",        # AH
            "Notes",             # AI
            "Spread Entry",      # AJ - spread in broker points at entry
            "Spread Exit",       # AK - spread in broker points at exit
            "Exit Slip Pts",     # AL - exit slippage in points
            "Session",           # AM - US/EU/Other
            "Weekday",           # AN - Monday-Friday
            "Commission",        # AO - broker commission ($)
            "Swap",              # AP - broker swap ($)
            "Risk Tier",         # AQ - TIER 1/2/3 at exit
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
        planned = trade_data.get('planned_entry', 0)
        actual = trade_data.get('entry_price', 0)
        slip_pts = round(actual - planned, 4) if planned and actual else 0
        risk_pts = trade_data.get('risk_points', 0)
        point_value = trade_data.get('point_value', 1.0)
        lots = trade_data.get('lot_size', 0)
        slip_dollars = round(abs(slip_pts) * point_value * lots, 2) if slip_pts else 0
        planned_rr = trade_data.get('planned_rr', 2.5)

        # Execution delay from signal time to fill
        signal_time_str = trade_data.get('signal_time', '')
        fill_time_str = now.strftime("%H:%M:%S")
        exec_delay = ''
        if signal_time_str:
            try:
                sig_dt = datetime.strptime(signal_time_str, "%Y-%m-%d %H:%M:%S")
                exec_delay = round((now - sig_dt).total_seconds(), 1)
            except Exception:
                exec_delay = ''

        row = [
            trade_id,                                        # A: Trade ID
            now.strftime("%Y-%m-%d"),                        # B: Date
            now.strftime("%H:%M:%S"),                        # C: Time
            trade_data.get('symbol', ''),                     # D: Symbol
            trade_data.get('direction', ''),                  # E: Direction
            planned,                                         # F: Planned Entry
            actual,                                          # G: Actual Entry
            slip_pts,                                        # H: Slippage Pts
            slip_dollars,                                    # I: Slippage $
            trade_data.get('stop_loss', 0),                  # J: Stop Loss
            trade_data.get('tp1', 0),                        # K: TP
            lots,                                            # L: Lot Size
            trade_data.get('risk_amount', 0),                # M: Risk Amount
            risk_pts,                                        # N: R (Risk Pts)
            planned_rr,                                      # O: Planned RR
            signal_time_str,                                 # P: Signal Time
            fill_time_str,                                   # Q: Fill Time
            exec_delay,                                      # R: Exec Delay (s)
            trade_data.get('htf_trend', ''),                 # S: HTF Trend
            trade_data.get('orb_range', ''),                 # T: ORB Range
            '',  # U: Exit Price
            '',  # V: Exit Time
            '',  # W: Exit Reason
            '',  # X: P&L
            '',  # Y: P&L %
            '',  # Z: Actual RR
            '',  # AA: MFE pts
            '',  # AB: MAE pts
            '',  # AC: MFE R
            '',  # AD: MAE R
            '',  # AE: Time in Trade
            '',  # AF: BE Hit?
            '',  # AG: Balance After
            '',  # AH: Drawdown
            trade_data.get('notes', ''),                     # AI: Notes
            trade_data.get('spread_entry', ''),              # AJ: Spread Entry
            '',                                               # AK: Spread Exit
            '',                                               # AL: Exit Slip Pts
            trade_data.get('session', ''),                    # AM: Session
            trade_data.get('weekday', ''),                    # AN: Weekday
        ]
        ws.append(row)

        # Color-code slippage
        last_row = ws.max_row
        if slip_pts != 0:
            fill_bad = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
            fill_ok = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
            # Negative slippage = favorable for sells, unfavorable for buys
            direction = trade_data.get('direction', '').lower()
            unfavorable = (slip_pts > 0 and direction == 'buy') or (slip_pts < 0 and direction == 'sell')
            ws.cell(row=last_row, column=8).fill = fill_bad if unfavorable else fill_ok

        wb.save(self.journal_path)
        logger.info(f"Trade {trade_id} added to journal (slippage: {slip_pts:+.2f} pts / ${slip_dollars:.2f})")
        return trade_id
    
    def close_trade(self, trade_id: int, exit_data: Dict):
        """Update trade with exit information including MFE/MAE, actual RR, etc."""
        wb = load_workbook(self.journal_path)
        ws = wb["Trades"]
        
        # Find the trade row
        for row_num in range(2, ws.max_row + 1):
            if ws.cell(row=row_num, column=1).value == trade_id:
                pnl = exit_data.get('pnl', 0)
                risk_pts = ws.cell(row=row_num, column=14).value  # N: R (Risk Pts)
                risk_pts = float(risk_pts) if risk_pts else 0

                # Actual RR
                actual_rr = round(pnl / (risk_pts * exit_data.get('point_value', 1.0) * exit_data.get('lots', 1)) , 2) if risk_pts and exit_data.get('lots') else ''
                try:
                    # Simpler: pnl_points / R
                    pnl_pts = exit_data.get('pnl_points', 0)
                    actual_rr = round(pnl_pts / risk_pts, 2) if risk_pts else ''
                except Exception:
                    actual_rr = ''

                # MFE / MAE
                mfe_pts = exit_data.get('mfe_points', '')
                mae_pts = exit_data.get('mae_points', '')
                mfe_r = round(mfe_pts / risk_pts, 2) if mfe_pts is not None and mfe_pts != '' and risk_pts else ''
                mae_r = round(mae_pts / risk_pts, 2) if mae_pts is not None and mae_pts != '' and risk_pts else ''

                # Time in trade
                entry_time_str = ws.cell(row=row_num, column=3).value  # C: Time
                entry_date_str = ws.cell(row=row_num, column=2).value  # B: Date
                time_in_trade = ''
                try:
                    entry_dt = datetime.strptime(f"{entry_date_str} {entry_time_str}", "%Y-%m-%d %H:%M:%S")
                    duration = datetime.now() - entry_dt
                    hours, remainder = divmod(int(duration.total_seconds()), 3600)
                    minutes, _ = divmod(remainder, 60)
                    time_in_trade = f"{hours}h {minutes}m"
                except Exception:
                    pass

                # Write exit columns
                ws.cell(row=row_num, column=21, value=exit_data.get('exit_price', 0))   # U
                ws.cell(row=row_num, column=22, value=datetime.now().strftime("%H:%M:%S"))  # V
                ws.cell(row=row_num, column=23, value=exit_data.get('exit_reason', ''))  # W
                ws.cell(row=row_num, column=24, value=pnl)                               # X
                ws.cell(row=row_num, column=25, value=exit_data.get('pnl_percent', 0))   # Y
                ws.cell(row=row_num, column=26, value=actual_rr)                          # Z
                ws.cell(row=row_num, column=27, value=mfe_pts)                            # AA
                ws.cell(row=row_num, column=28, value=mae_pts)                            # AB
                ws.cell(row=row_num, column=29, value=mfe_r)                              # AC
                ws.cell(row=row_num, column=30, value=mae_r)                              # AD
                ws.cell(row=row_num, column=31, value=time_in_trade)                      # AE
                ws.cell(row=row_num, column=32, value=exit_data.get('be_hit', ''))        # AF
                ws.cell(row=row_num, column=33, value=exit_data.get('balance_after', 0))  # AG
                ws.cell(row=row_num, column=34, value=exit_data.get('drawdown', 0))       # AH
                ws.cell(row=row_num, column=37, value=exit_data.get('spread_exit', ''))   # AK
                ws.cell(row=row_num, column=38, value=exit_data.get('exit_slippage', '')) # AL
                ws.cell(row=row_num, column=41, value=exit_data.get('commission', ''))    # AO
                ws.cell(row=row_num, column=42, value=exit_data.get('swap', ''))          # AP
                ws.cell(row=row_num, column=43, value=exit_data.get('risk_tier', ''))     # AQ
                
                # Color code P&L
                if pnl > 0:
                    ws.cell(row=row_num, column=24).fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                elif pnl < 0:
                    ws.cell(row=row_num, column=24).fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                
                break
        
        wb.save(self.journal_path)
        logger.info(f"Trade {trade_id} closed in journal (P&L: ${exit_data.get('pnl', 0):+.2f}, RR: {actual_rr})")
    
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
                pnl = ws.cell(row=row_num, column=24).value  # X: P&L
                if pnl is not None and pnl != '':
                    trades.append({
                        'symbol': ws.cell(row=row_num, column=4).value,
                        'direction': ws.cell(row=row_num, column=5).value,
                        'pnl': float(pnl) if pnl else 0,
                        'exit_reason': ws.cell(row=row_num, column=23).value,  # W
                        'slippage_pts': ws.cell(row=row_num, column=8).value,  # H
                        'slippage_usd': ws.cell(row=row_num, column=9).value,  # I
                        'actual_rr': ws.cell(row=row_num, column=26).value,    # Z
                        'mfe_r': ws.cell(row=row_num, column=29).value,        # AC
                        'mae_r': ws.cell(row=row_num, column=30).value,        # AD
                        'time_in_trade': ws.cell(row=row_num, column=31).value, # AE
                        'be_hit': ws.cell(row=row_num, column=32).value,       # AF
                        'spread_entry': ws.cell(row=row_num, column=36).value,  # AJ
                        'session': ws.cell(row=row_num, column=39).value,       # AM
                        'weekday': ws.cell(row=row_num, column=40).value,       # AN
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
                        pnl = ws.cell(row=row_num, column=24).value  # X: P&L
                        if pnl is not None and pnl != '':
                            trades.append({
                                'date': row_date_str,
                                'symbol': ws.cell(row=row_num, column=4).value,
                                'pnl': float(pnl) if pnl else 0,
                                'slippage_pts': ws.cell(row=row_num, column=8).value,
                                'actual_rr': ws.cell(row=row_num, column=26).value,
                                'mfe_r': ws.cell(row=row_num, column=29).value,
                                'mae_r': ws.cell(row=row_num, column=30).value,
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
        slippages = [float(t['slippage_pts']) for t in trades if t.get('slippage_pts') and t['slippage_pts'] != '']
        rr_vals = [float(t['actual_rr']) for t in trades if t.get('actual_rr') and t['actual_rr'] != '']
        mfe_vals = [float(t['mfe_r']) for t in trades if t.get('mfe_r') and t['mfe_r'] != '']
        mae_vals = [float(t['mae_r']) for t in trades if t.get('mae_r') and t['mae_r'] != '']
        
        return {
            'week_start': week_start,
            'week_end': end_date.strftime("%Y-%m-%d"),
            'trades': len(trades),
            'wins': wins,
            'losses': losses,
            'win_rate': (wins / len(trades) * 100) if trades else 0,
            'gross_pnl': sum(pnls),
            'avg_trade': sum(pnls) / len(pnls) if pnls else 0,
            'avg_slippage': sum(slippages) / len(slippages) if slippages else 0,
            'avg_rr': sum(rr_vals) / len(rr_vals) if rr_vals else 0,
            'avg_mfe_r': sum(mfe_vals) / len(mfe_vals) if mfe_vals else 0,
            'avg_mae_r': sum(mae_vals) / len(mae_vals) if mae_vals else 0,
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
        all_slippages = []
        all_rr = []
        for row_num in range(2, ws_trades.max_row + 1):
            pnl = ws_trades.cell(row=row_num, column=24).value  # X: P&L
            if pnl is not None and pnl != '':
                all_trades.append(float(pnl))
                slip = ws_trades.cell(row=row_num, column=8).value  # H: Slippage Pts
                if slip is not None and slip != '':
                    all_slippages.append(float(slip))
                rr = ws_trades.cell(row=row_num, column=26).value  # Z: Actual RR
                if rr is not None and rr != '':
                    all_rr.append(float(rr))
        
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
        
        ws["A20"] = "📉 EXECUTION QUALITY"
        ws["A20"].font = Font(size=14, bold=True)
        
        avg_slip = sum(all_slippages) / len(all_slippages) if all_slippages else 0
        ws["A22"] = "Avg Slippage:"
        ws["B22"] = f"{avg_slip:+.2f} pts"
        
        avg_rr = sum(all_rr) / len(all_rr) if all_rr else 0
        ws["A23"] = "Avg R-Multiple:"
        ws["B23"] = f"{avg_rr:.2f}R"
        
        ws["A25"] = f"Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ws["A25"].font = Font(italic=True, color="666666")
        
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
        
        # Weekly report on Friday at 8:00 PM (bot is still running, unlike Saturday)
        if now.weekday() == 4 and now.hour == 20 and now.minute < 10 and self.last_weekly_report != today:
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
        
        # Trade breakdown with slippage and RR
        trade_lines = ""
        total_slip_pts = []
        rr_list = []
        if stats.get('trade_details'):
            for t in stats['trade_details']:
                t_emoji = "✅" if t['pnl'] > 0 else "❌"
                rr_str = f" ({t['actual_rr']}R)" if t.get('actual_rr') else ""
                slip_str = f" slip:{t['slippage_pts']:+.1f}" if t.get('slippage_pts') else ""
                trade_lines += f"\n   {t_emoji} {t['symbol']} {t['direction']}: ${t['pnl']:+,.2f}{rr_str}{slip_str}"
                if t.get('slippage_pts'):
                    total_slip_pts.append(float(t['slippage_pts']))
                if t.get('actual_rr'):
                    rr_list.append(float(t['actual_rr']))
        
        # Execution quality block
        exec_block = ""
        if total_slip_pts or rr_list:
            avg_slip = sum(total_slip_pts) / len(total_slip_pts) if total_slip_pts else 0
            avg_rr = sum(rr_list) / len(rr_list) if rr_list else 0
            exec_block = f"""\n<b>⚡ Execution Quality:</b>
├─ Avg Slippage: {avg_slip:+.2f} pts
└─ Avg R-Multiple: {avg_rr:.2f}R"""
        
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
{exec_block}
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

<b>⚡ Execution Quality:</b>
├─ Avg Slippage: {stats.get('avg_slippage', 0):+.2f} pts
├─ Avg R-Multiple: {stats.get('avg_rr', 0):.2f}R
├─ Avg MFE: {stats.get('avg_mfe_r', 0):.2f}R
└─ Avg MAE: {stats.get('avg_mae_r', 0):.2f}R

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
