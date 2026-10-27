"""
Indian Options Paper Trading Journal
=====================================
Tracks paper trades in Excel with daily/weekly summaries.
Adapted from the forex TradeJournal for Indian options context.

Columns: trade metadata, option Greeks, exit analysis, MFE/MAE, P&L in Rs
"""
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

logger = logging.getLogger(__name__)

_GREEN  = "C6EFCE"
_RED    = "FFC7CE"
_HEADER = "1F4E79"
_SUBHDR = "2E75B6"

_SAVE_RETRIES    = 3
_SAVE_RETRY_DELAY = 1.0


class IndianPaperJournal:
    """Excel journal for Indian options paper trading."""

    TRADE_HEADERS = [
        "Trade ID",       # A
        "Date",           # B
        "Signal Time",    # C  - when breakout bar fired
        "Entry Time",     # D  - when retest confirmed
        "Instrument",     # E  - NIFTY / FINNIFTY
        "Signal",         # F  - call / put
        "Quality",        # G  - A+ / regular
        "ORB High",       # H
        "ORB Low",        # I
        "ORB Range",      # J  - pts
        "Entry Spot",     # K  - underlying price at entry
        "Strike",         # L
        "DTE",            # M  - days to expiry
        "IV Used %",      # N
        "Entry Premium",  # O  - Rs per unit
        "Option SL",      # P  - Rs per unit
        "Lots",           # Q
        "Qty",            # R  - Lots × lot_size
        "Risk Rs",        # S  - Lots × option_sl × qty
        "VIX at Entry",   # T
        "BEP Trigger",    # U  - premium level to activate BEP
        "Trail Trigger",  # V  - premium level to activate trail
        "Exit Time",      # W
        "Exit Spot",      # X
        "Exit Premium",   # Y
        "Exit Reason",    # Z  - SL / BEP / Trail SL / EOD
        "Peak Premium",   # AA
        "MFE Rs",         # AB - max favorable excursion (Rs)
        "MAE Rs",         # AC - max adverse excursion (Rs)
        "MFE R",          # AD - MFE / risk
        "MAE R",          # AE - MAE / risk
        "BE Hit?",        # AF
        "Trail Hit?",     # AG
        "Time in Trade",  # AH
        "Gross Rs",       # AI
        "Cost Rs",        # AJ - Zerodha transaction costs
        "Net Rs",         # AK
        "Return %",       # AL - Net / Capital
        "Cum Capital Rs", # AM - running capital
        "Monthly P&L Rs", # AN
        "Notes",          # AO
    ]

    def __init__(self, journal_path: str = None, capital: float = 1_000_000):
        if journal_path is None:
            journal_path = Path(__file__).parent / "reports" / "indian_paper_journal.xlsx"
        self.journal_path = Path(journal_path)
        self.capital      = capital
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.journal_path.exists():
            self._create_journal()

    # ── Safe I/O ──────────────────────────────────────────────────────────────
    def _safe_load(self) -> Workbook:
        last_err = None
        for attempt in range(_SAVE_RETRIES):
            try:
                return load_workbook(self.journal_path)
            except PermissionError as e:
                last_err = e
                logger.warning(f"Journal locked (attempt {attempt+1}): {e}")
                time.sleep(_SAVE_RETRY_DELAY)
            except Exception as e:
                last_err = e
                logger.error(f"Journal load failed: {e}")
                time.sleep(_SAVE_RETRY_DELAY)
        raise last_err

    def _safe_save(self, wb: Workbook):
        for attempt in range(_SAVE_RETRIES):
            try:
                wb.save(self.journal_path)
                return
            except PermissionError as e:
                logger.warning(f"Journal save locked (attempt {attempt+1}): {e}")
                time.sleep(_SAVE_RETRY_DELAY)
            except Exception as e:
                logger.error(f"Journal save failed: {e}")
                time.sleep(_SAVE_RETRY_DELAY)
        logger.critical("Journal save FAILED after retries — data may be lost!")

    def _style_header_row(self, ws, color: str = _HEADER):
        fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
        font = Font(color="FFFFFF", bold=True, size=9)
        for cell in ws[1]:
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(horizontal="center", wrap_text=True)

    # ── Journal creation ───────────────────────────────────────────────────────
    def _create_journal(self):
        wb = Workbook()

        # Trades sheet
        ws_t = wb.active
        ws_t.title = "Trades"
        ws_t.append(self.TRADE_HEADERS)
        self._style_header_row(ws_t)
        ws_t.freeze_panes = "A2"
        for col in ws_t.iter_cols(min_row=1, max_row=1):
            ws_t.column_dimensions[col[0].column_letter].width = 14

        # Daily Summary sheet
        ws_d = wb.create_sheet("Daily Summary")
        daily_headers = [
            "Date", "Weekday", "Nifty Trades", "FN Trades", "Total Trades",
            "Wins", "Losses", "Win Rate %", "A+ Trades", "Reg Trades",
            "Gross Rs", "Cost Rs", "Net Rs", "Return %",
            "Best Trade Rs", "Worst Trade Rs",
            "Capital Rs", "Daily DD %", "Cum DD %",
        ]
        ws_d.append(daily_headers)
        self._style_header_row(ws_d, _SUBHDR)
        ws_d.freeze_panes = "A2"

        # Weekly Summary sheet
        ws_w = wb.create_sheet("Weekly Summary")
        weekly_headers = [
            "Week", "Mon Date", "Fri Date", "Trades", "Wins", "Losses",
            "Win Rate %", "Net Rs", "Avg Trade Rs", "Best Day Rs", "Worst Day Rs",
            "Capital Start Rs", "Capital End Rs", "Weekly Return %",
        ]
        ws_w.append(weekly_headers)
        self._style_header_row(ws_w, _SUBHDR)
        ws_w.freeze_panes = "A2"

        # Dashboard sheet
        ws_dash = wb.create_sheet("Dashboard")
        ws_dash["A1"] = "Indian Options Paper Trader — Dashboard"
        ws_dash["A1"].font = Font(size=16, bold=True, color="1F4E79")
        ws_dash["A3"] = "Run: python live/paper_trader.py"
        ws_dash["A3"].font = Font(italic=True, color="666666")

        wb.save(self.journal_path)
        logger.info(f"Journal created: {self.journal_path}")

    # ── Add trade on entry ─────────────────────────────────────────────────────
    def add_trade(self, trade: dict) -> int:
        """
        Write a new paper trade row at entry time.
        Exit columns left blank — filled in by close_trade().
        Returns trade_id (row number).
        """
        wb  = self._safe_load()
        ws  = wb["Trades"]
        tid = ws.max_row   # 1-indexed; row 1 = header, so first trade = row 2

        risk_rs = trade.get("option_sl", 0) * trade.get("qty", 0)
        bep_trig   = trade.get("entry_prem", 0) * (1 + trade.get("bep_activate", 0.25))
        trail_trig = trade.get("entry_prem", 0) * (1 + trade.get("trail_activate", 0.50))

        row = [
            tid,
            trade.get("date", ""),
            trade.get("signal_time", ""),
            trade.get("entry_time", ""),
            trade.get("instrument", ""),
            trade.get("direction", ""),
            trade.get("quality", ""),
            round(trade.get("orb_h", 0), 2),
            round(trade.get("orb_l", 0), 2),
            round(trade.get("orb_h", 0) - trade.get("orb_l", 0), 2),
            round(trade.get("entry_spot", 0), 2),
            round(trade.get("strike", 0), 2),
            trade.get("dte", 0),
            round(trade.get("iv_used", 0), 2),
            round(trade.get("entry_prem", 0), 2),
            round(trade.get("option_sl", 0), 2),
            trade.get("lots", 0),
            trade.get("qty", 0),
            round(risk_rs, 0),
            round(trade.get("vix", 0), 2),
            round(bep_trig, 2),
            round(trail_trig, 2),
            # exit columns (W–AN) left blank
            "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
        ]
        ws.append(row)

        # Colour-code quality
        last_row = ws.max_row
        qual_fill = PatternFill(
            start_color="FFD700" if trade.get("quality") == "A+" else "DDEBF7",
            end_color="FFD700"   if trade.get("quality") == "A+" else "DDEBF7",
            fill_type="solid",
        )
        ws.cell(row=last_row, column=7).fill = qual_fill

        self._safe_save(wb)
        logger.info(f"Trade {tid} added: {trade.get('instrument')} {trade.get('direction')} "
                    f"entry={trade.get('entry_prem', 0):.2f} lots={trade.get('lots', 0)}")
        return tid

    # ── Close trade on exit ────────────────────────────────────────────────────
    def close_trade(self, trade_id: int, exit_data: dict, running_capital: float,
                    monthly_pnl: float):
        """Fill exit columns for an existing trade row."""
        wb = self._safe_load()
        ws = wb["Trades"]

        for row_num in range(2, ws.max_row + 1):
            if ws.cell(row=row_num, column=1).value != trade_id:
                continue

            # Read entry fields needed for derived calcs
            entry_prem = float(ws.cell(row=row_num, column=15).value or 0)
            risk_rs    = float(ws.cell(row=row_num, column=19).value or 0)
            qty        = int(ws.cell(row=row_num, column=18).value or 0)
            option_sl  = float(ws.cell(row=row_num, column=16).value or 0)

            gross = exit_data.get("gross_rs", 0)
            cost  = exit_data.get("cost_rs", 0)
            net   = exit_data.get("net_rs", 0)

            # MFE / MAE in R-multiples (R = risk_rs)
            mfe_rs = exit_data.get("mfe_rs", 0)
            mae_rs = exit_data.get("mae_rs", 0)
            mfe_r  = round(mfe_rs / risk_rs, 2) if risk_rs else ""
            mae_r  = round(mae_rs / risk_rs, 2) if risk_rs else ""

            # Time in trade
            entry_time_str = ws.cell(row=row_num, column=4).value or ""
            entry_date_str = ws.cell(row=row_num, column=2).value or ""
            time_in_trade  = ""
            try:
                entry_dt  = datetime.strptime(f"{entry_date_str} {entry_time_str}", "%Y-%m-%d %H:%M:%S")
                dur       = datetime.now() - entry_dt
                h, r      = divmod(int(dur.total_seconds()), 3600)
                m, _      = divmod(r, 60)
                time_in_trade = f"{h}h {m}m"
            except Exception:
                pass

            ret_pct = round(net / self.capital * 100, 4) if self.capital else 0

            # Column indices (1-based, matching TRADE_HEADERS order)
            # W=23, X=24, Y=25, Z=26, AA=27, AB=28, AC=29, AD=30, AE=31, AF=32, AG=33, AH=34
            # AI=35, AJ=36, AK=37, AL=38, AM=39, AN=40, AO=41
            ws.cell(row=row_num, column=23, value=exit_data.get("exit_time", ""))
            ws.cell(row=row_num, column=24, value=round(exit_data.get("exit_spot", 0), 2))
            ws.cell(row=row_num, column=25, value=round(exit_data.get("exit_prem", 0), 2))
            ws.cell(row=row_num, column=26, value=exit_data.get("exit_reason", ""))
            ws.cell(row=row_num, column=27, value=round(exit_data.get("peak_prem", 0), 2))
            ws.cell(row=row_num, column=28, value=round(mfe_rs, 0))
            ws.cell(row=row_num, column=29, value=round(mae_rs, 0))
            ws.cell(row=row_num, column=30, value=mfe_r)
            ws.cell(row=row_num, column=31, value=mae_r)
            ws.cell(row=row_num, column=32, value="Yes" if exit_data.get("be_hit") else "No")
            ws.cell(row=row_num, column=33, value="Yes" if exit_data.get("trail_hit") else "No")
            ws.cell(row=row_num, column=34, value=time_in_trade)
            ws.cell(row=row_num, column=35, value=round(gross, 0))
            ws.cell(row=row_num, column=36, value=round(cost, 0))
            ws.cell(row=row_num, column=37, value=round(net, 0))
            ws.cell(row=row_num, column=38, value=ret_pct)
            ws.cell(row=row_num, column=39, value=round(running_capital, 0))
            ws.cell(row=row_num, column=40, value=round(monthly_pnl, 0))
            ws.cell(row=row_num, column=41, value=exit_data.get("notes", ""))

            # Color-code P&L cell (AK = col 37)
            pnl_fill = PatternFill(
                start_color=_GREEN if net >= 0 else _RED,
                end_color=_GREEN   if net >= 0 else _RED,
                fill_type="solid",
            )
            ws.cell(row=row_num, column=37).fill = pnl_fill

            break

        self._safe_save(wb)
        logger.info(f"Trade {trade_id} closed: net=Rs{exit_data.get('net_rs', 0):+,.0f} "
                    f"reason={exit_data.get('exit_reason', '?')}")

    # ── Daily summary ──────────────────────────────────────────────────────────
    def update_daily_summary(self, date: str, capital: float, cum_dd_pct: float):
        """Aggregate all trades for the given date and write/update daily summary row."""
        wb      = self._safe_load()
        ws_t    = wb["Trades"]
        ws_d    = wb["Daily Summary"]

        day_trades = []
        for rn in range(2, ws_t.max_row + 1):
            if ws_t.cell(rn, 2).value != date:
                continue
            net = ws_t.cell(rn, 37).value   # AK
            if net is None or net == "":
                continue
            day_trades.append({
                "net":        float(net),
                "gross":      float(ws_t.cell(rn, 35).value or 0),
                "cost":       float(ws_t.cell(rn, 36).value or 0),
                "instrument": ws_t.cell(rn, 5).value or "",
                "quality":    ws_t.cell(rn, 7).value or "",
            })

        if not day_trades:
            self._safe_save(wb)
            return

        nets      = [t["net"] for t in day_trades]
        wins      = sum(1 for n in nets if n > 0)
        losses    = sum(1 for n in nets if n < 0)
        net_total = sum(nets)
        gross_total = sum(t["gross"] for t in day_trades)
        cost_total  = sum(t["cost"]  for t in day_trades)
        nifty_n  = sum(1 for t in day_trades if "NIFTY" == t["instrument"] and "FIN" not in t["instrument"])
        fn_n     = sum(1 for t in day_trades if "FINNIFTY" in t["instrument"])
        aplus_n  = sum(1 for t in day_trades if t["quality"] == "A+")
        reg_n    = sum(1 for t in day_trades if t["quality"] == "regular")
        ret_pct  = round(net_total / self.capital * 100, 2) if self.capital else 0
        prev_cap = capital - net_total
        daily_dd = round((net_total / prev_cap * 100) if net_total < 0 and prev_cap else 0, 2)

        row_data = [
            date,
            datetime.strptime(date, "%Y-%m-%d").strftime("%A"),
            nifty_n, fn_n, len(day_trades),
            wins, losses,
            round(wins / len(day_trades) * 100, 1),
            aplus_n, reg_n,
            round(gross_total, 0), round(cost_total, 0), round(net_total, 0),
            ret_pct,
            round(max(nets), 0), round(min(nets), 0),
            round(capital, 0), daily_dd, round(cum_dd_pct, 2),
        ]

        # Find existing row for date or append
        target_row = None
        for rn in range(2, ws_d.max_row + 1):
            if ws_d.cell(rn, 1).value == date:
                target_row = rn
                break

        if target_row:
            for col, val in enumerate(row_data, 1):
                ws_d.cell(row=target_row, column=col, value=val)
        else:
            ws_d.append(row_data)
            target_row = ws_d.max_row

        # Color net P&L column (col 13)
        ws_d.cell(target_row, 13).fill = PatternFill(
            start_color=_GREEN if net_total >= 0 else _RED,
            end_color=_GREEN   if net_total >= 0 else _RED,
            fill_type="solid",
        )

        self._safe_save(wb)
        logger.info(f"Daily summary updated: {date}  net=Rs{net_total:+,.0f}  "
                    f"trades={len(day_trades)}  WR={wins/len(day_trades)*100:.0f}%")

    # ── Dashboard ──────────────────────────────────────────────────────────────
    def update_dashboard(self, capital: float, paper_start_date: str = ""):
        """Refresh Dashboard sheet with all-time aggregate stats."""
        wb   = self._safe_load()
        ws_t = wb["Trades"]
        ws   = wb["Dashboard"]

        # Collect all closed trades
        nets, grosses, instruments = [], [], []
        qualities = []
        for rn in range(2, ws_t.max_row + 1):
            net = ws_t.cell(rn, 37).value
            if net is None or net == "":
                continue
            nets.append(float(net))
            grosses.append(float(ws_t.cell(rn, 35).value or 0))
            instruments.append(ws_t.cell(rn, 5).value or "")
            qualities.append(ws_t.cell(rn, 7).value or "")

        total  = len(nets)
        wins   = sum(1 for n in nets if n > 0)
        losses = sum(1 for n in nets if n < 0)
        pnl    = sum(nets)
        gross_w = sum(n for n in nets if n > 0)
        gross_l = abs(sum(n for n in nets if n < 0))
        pf = round(gross_w / gross_l, 3) if gross_l else 0

        # Clear old content (rows 3–40)
        for r in range(3, 41):
            for c in range(1, 5):
                ws.cell(r, c).value = None

        def _row(r, label, value):
            ws.cell(r, 1).value = label
            ws.cell(r, 1).font  = Font(bold=True, size=10)
            ws.cell(r, 2).value = value
            ws.cell(r, 2).font  = Font(size=10)

        ws.cell(3, 1).value = "ACCOUNT OVERVIEW"
        ws.cell(3, 1).font  = Font(size=13, bold=True, color="1F4E79")

        _row(5,  "Paper Start Date:",     paper_start_date or "—")
        _row(6,  "Capital (fixed):",      f"Rs {self.capital:,.0f}")
        _row(7,  "Running Capital:",      f"Rs {capital:,.0f}")
        _row(8,  "Total Net P&L:",        f"Rs {pnl:+,.0f}")
        _row(9,  "Return %:",             f"{pnl/self.capital*100:.2f}%")

        ws.cell(11, 1).value = "TRADING STATS"
        ws.cell(11, 1).font  = Font(size=13, bold=True, color="1F4E79")

        _row(13, "Total Trades:",         total)
        _row(14, "Wins / Losses:",        f"{wins} / {losses}")
        _row(15, "Win Rate:",             f"{wins/total*100:.1f}%" if total else "—")
        _row(16, "Profit Factor:",        pf)
        _row(17, "Avg Win Rs:",           f"Rs {gross_w/wins:,.0f}" if wins else "—")
        _row(18, "Avg Loss Rs:",          f"Rs {-gross_l/losses:,.0f}" if losses else "—")

        nifty_n = sum(1 for s in instruments if s == "NIFTY")
        fn_n    = sum(1 for s in instruments if s == "FINNIFTY")
        ap_n    = sum(1 for q in qualities if q == "A+")
        _row(19, "Nifty / FinNifty:",    f"{nifty_n} / {fn_n}")
        _row(20, "A+ / Regular:",         f"{ap_n} / {total - ap_n}")

        ws.cell(22, 1).value = f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ws.cell(22, 1).font  = Font(italic=True, color="888888")

        # Colour net P&L cell
        ws.cell(8, 2).font = Font(
            size=10, bold=True,
            color="007700" if pnl >= 0 else "CC0000",
        )

        self._safe_save(wb)
        logger.info("Dashboard refreshed")

    # ── Stats helpers ──────────────────────────────────────────────────────────
    def get_today_stats(self, date: str = None) -> dict:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        wb   = self._safe_load()
        ws   = wb["Trades"]
        nets = []
        for rn in range(2, ws.max_row + 1):
            if ws.cell(rn, 2).value != date:
                continue
            net = ws.cell(rn, 37).value
            if net not in (None, ""):
                nets.append(float(net))
        wins = sum(1 for n in nets if n > 0)
        return {
            "date":      date,
            "trades":    len(nets),
            "wins":      wins,
            "losses":    len(nets) - wins,
            "net_rs":    sum(nets),
            "best_rs":   max(nets) if nets else 0,
            "worst_rs":  min(nets) if nets else 0,
            "win_rate":  wins / len(nets) * 100 if nets else 0,
        }
