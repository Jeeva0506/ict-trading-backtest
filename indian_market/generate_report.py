"""Generate full portfolio audit report as Word document."""
import sys, os, io
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT

CAPITAL = 300_000
DATA_DIR = Path(__file__).resolve().parent / "data"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
OUT_PATH = REPORTS_DIR / "Portfolio_Audit_Report.docx"

# ── Load & prepare data ──────────────────────────────────────────────────────
nst = pd.read_csv(REPORTS_DIR / "straddle_nifty_trades.csv")
sst = pd.read_csv(REPORTS_DIR / "straddle_sensex_optimized_trades.csv")
nsw = pd.read_csv(REPORTS_DIR / "put_sweep_nifty_trades.csv")

ST_SCALE = (CAPITAL * 0.03) / (1_000_000 * 0.01)
ST_SLIP = 0.85
SW_SCALE = (CAPITAL * 0.01) / (1_000_000 * 0.01)
SW_SLIP = 0.92

nst["pnl_net"] = nst["pnl_net"] * ST_SCALE * ST_SLIP
nst["component"] = "NIFTY Straddle"
sst["pnl_net"] = sst["pnl_net"] * ST_SCALE * ST_SLIP
sst["component"] = "SENSEX Straddle"
nsw["pnl_net"] = nsw["pnl_net"] * SW_SCALE * SW_SLIP
nsw["component"] = "NIFTY PUT Sweep"

cols = ["entry_date", "exit_date", "pnl_net", "component", "exit_type"]
portfolio = pd.concat([nst[cols], sst[cols], nsw[cols]]).sort_values("entry_date").reset_index(drop=True)
portfolio["entry_dt"] = pd.to_datetime(portfolio["entry_date"])
portfolio["year"] = portfolio["entry_dt"].dt.year
portfolio["month"] = portfolio["entry_dt"].dt.to_period("M")
portfolio["yw"] = portfolio["entry_dt"].dt.strftime("%Y-W%U")

nd = pd.read_csv(DATA_DIR / "zerodha_nifty_day.csv", index_col=0, parse_dates=True)

pnls = portfolio["pnl_net"].values
equity = CAPITAL + np.cumsum(pnls)
peak = np.maximum.accumulate(equity)
dd_pct = (equity - peak) / peak * 100

first = portfolio["entry_dt"].min()
last = portfolio["entry_dt"].max()
years = (last - first).days / 365.25
strat_final = equity[-1]
strat_cagr = ((strat_final / CAPITAL) ** (1 / years) - 1) * 100
strat_dd = dd_pct.min()
wins = pnls[pnls > 0]; losses = pnls[pnls <= 0]
wr = len(wins) / len(pnls) * 100
pf = wins.sum() / abs(losses.sum()) if len(losses) else float("inf")

nbh = nd[(nd.index >= first) & (nd.index <= last)]
n0 = float(nbh.iloc[0]["Close"]); n1 = float(nbh.iloc[-1]["Close"])
bh_cagr = ((n1 / n0) ** (1 / years) - 1) * 100
bh_eq = nbh["Close"] / n0 * CAPITAL
bh_dd = ((bh_eq - bh_eq.cummax()) / bh_eq.cummax() * 100).min()
bh_sh = (nbh["Close"].pct_change().dropna().mean() - 0.065/252) / nbh["Close"].pct_change().dropna().std() * np.sqrt(252)
bh_final = CAPITAL * n1 / n0

dpnl = portfolio.groupby("entry_date")["pnl_net"].sum()
sharpe = dpnl.mean() / dpnl.std() * np.sqrt(252) if dpnl.std() > 0 else 0

# Monte Carlo
np.random.seed(42)
mc_dd = []; mc_f = []; mc_s = []
for _ in range(10000):
    sh = np.random.permutation(pnls)
    eq = CAPITAL + np.cumsum(sh); pk = np.maximum.accumulate(eq)
    mc_dd.append(((eq - pk) / pk * 100).min()); mc_f.append(eq[-1])
    st = 0; mx = 0
    for p in sh:
        if p <= 0: st += 1; mx = max(mx, st)
        else: st = 0
    mc_s.append(mx)
mc_dd = np.array(mc_dd); mc_f = np.array(mc_f); mc_s = np.array(mc_s)


# ── Chart helpers ─────────────────────────────────────────────────────────────
def chart_to_bytes(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def make_equity_chart():
    fig, ax = plt.subplots(figsize=(8, 3.5))
    dates = portfolio["entry_dt"].values
    ax.plot(dates, equity / 100000, color="#1a73e8", linewidth=1.2, label="Strategy")
    bh_dates = nbh.index.values
    bh_vals = bh_eq.values / 100000
    ax.plot(bh_dates, bh_vals, color="#999999", linewidth=0.8, linestyle="--", label="Nifty B&H")
    ax.set_ylabel("Equity (Rs Lakhs)")
    ax.set_title("Equity Curve: Strategy vs Nifty Buy & Hold", fontsize=10)
    ax.legend(fontsize=8)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.1f}L"))
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return chart_to_bytes(fig)


def make_drawdown_chart():
    fig, ax = plt.subplots(figsize=(8, 2.5))
    dates = portfolio["entry_dt"].values
    ax.fill_between(dates, dd_pct, 0, color="#e74c3c", alpha=0.4)
    ax.set_ylabel("Drawdown %")
    ax.set_title("Strategy Drawdown", fontsize=10)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return chart_to_bytes(fig)


def make_monthly_pnl_chart():
    mo = portfolio.groupby("month")["pnl_net"].sum()
    fig, ax = plt.subplots(figsize=(8, 3))
    colors = ["#2ecc71" if v > 0 else "#e74c3c" for v in mo.values]
    x = range(len(mo))
    ax.bar(x, mo.values / 1000, color=colors, width=0.8)
    tick_step = max(1, len(mo) // 12)
    ax.set_xticks(x[::tick_step])
    ax.set_xticklabels([str(m) for m in mo.index[::tick_step]], rotation=45, fontsize=6)
    ax.set_ylabel("P&L (Rs '000)")
    ax.set_title("Monthly P&L", fontsize=10)
    ax.axhline(0, color="black", linewidth=0.5)
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    return chart_to_bytes(fig)


def make_mc_dd_chart():
    fig, ax = plt.subplots(figsize=(5, 3))
    ax.hist(mc_dd, bins=80, color="#3498db", alpha=0.7, edgecolor="white")
    ax.axvline(np.median(mc_dd), color="green", linestyle="--", label=f"Median: {np.median(mc_dd):.1f}%")
    ax.axvline(np.percentile(mc_dd, 5), color="orange", linestyle="--", label=f"95th: {np.percentile(mc_dd, 5):.1f}%")
    ax.axvline(strat_dd, color="red", linestyle="-", label=f"Actual: {strat_dd:.1f}%")
    ax.set_xlabel("Max Drawdown %")
    ax.set_title("Monte Carlo — Drawdown Distribution (10K sims)", fontsize=9)
    ax.legend(fontsize=7)
    fig.tight_layout()
    return chart_to_bytes(fig)


# ── Document ──────────────────────────────────────────────────────────────────
doc = Document()
style = doc.styles["Normal"]
style.font.name = "Calibri"
style.font.size = Pt(10)

# Title
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run("PORTFOLIO AUDIT REPORT")
r.bold = True; r.font.size = Pt(18); r.font.color.rgb = RGBColor(0x1a, 0x73, 0xe8)
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run("Combined Short Straddle + PUT Sweep Strategy")
r.font.size = Pt(12); r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = p.add_run(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Capital: Rs {CAPITAL:,}")
r.font.size = Pt(9); r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

doc.add_page_break()

# ── Section 1: Executive Summary ─────────────────────────────────────────────
doc.add_heading("1. Executive Summary", level=1)
doc.add_paragraph(
    f"This report audits a combined options strategy backtested over {years:.1f} years "
    f"({first.date()} to {last.date()}) on NIFTY and SENSEX indices using Zerodha's "
    f"historical spot data. Option premiums are synthetic (Black-Scholes) with "
    f"India VIX as implied volatility input."
)
doc.add_paragraph(
    f"Realistic assumptions applied: 3%/5% entry/exit slippage (vs 1%/1% in raw backtest), "
    f"full Zerodha transaction costs (brokerage, STT, exchange, SEBI, GST, stamp duty), "
    f"and capital-appropriate position sizing for Rs {CAPITAL:,}."
)

# Strategy vs B&H table
doc.add_heading("Strategy vs Nifty Buy & Hold", level=2)
t = doc.add_table(rows=11, cols=3, style="Light Shading Accent 1")
t.alignment = WD_TABLE_ALIGNMENT.CENTER
headers = ["Metric", "Strategy", "Nifty B&H"]
for i, h in enumerate(headers):
    t.rows[0].cells[i].text = h
    t.rows[0].cells[i].paragraphs[0].runs[0].bold = True

data = [
    ("Total Trades", f"{len(pnls):,}", "1"),
    ("Win Rate", f"{wr:.1f}%", "N/A"),
    ("Profit Factor", f"{pf:.3f}", "N/A"),
    ("CAGR", f"{strat_cagr:.1f}%", f"{bh_cagr:.1f}%"),
    ("Sharpe Ratio", f"{sharpe:.3f}", f"{bh_sh:.3f}"),
    ("Max Drawdown", f"{strat_dd:.1f}%", f"{bh_dd:.1f}%"),
    ("Final Equity", f"Rs {int(strat_final):,}", f"Rs {int(bh_final):,}"),
    ("Total Return", f"{(strat_final/CAPITAL-1)*100:.1f}%", f"{(bh_final/CAPITAL-1)*100:.1f}%"),
    ("Calmar Ratio", f"{abs(strat_cagr/strat_dd):.2f}", f"{abs(bh_cagr/bh_dd):.2f}"),
    ("Expectancy/Trade", f"Rs {int(pnls.mean()):,}", "N/A"),
]
for i, (m, s, b) in enumerate(data):
    t.rows[i+1].cells[0].text = m
    t.rows[i+1].cells[1].text = s
    t.rows[i+1].cells[2].text = b

# Equity chart
doc.add_heading("Equity Curve", level=2)
doc.add_picture(make_equity_chart(), width=Inches(6))

# Drawdown chart
doc.add_heading("Drawdown", level=2)
doc.add_picture(make_drawdown_chart(), width=Inches(6))

doc.add_page_break()

# ── Section 2: Component Breakdown ───────────────────────────────────────────
doc.add_heading("2. Strategy Components", level=1)
doc.add_paragraph(
    "The portfolio combines three independent alpha sources, trading on different "
    "days to minimize overlap:"
)

t = doc.add_table(rows=5, cols=6, style="Light Shading Accent 1")
for i, h in enumerate(["Component", "Trades", "Win Rate", "Risk/Trade", "When", "Net P&L"]):
    t.rows[0].cells[i].text = h
    t.rows[0].cells[i].paragraphs[0].runs[0].bold = True

comp_data = [
    ("NIFTY Straddle", "343", "83.4%", "3%", "Tue (expiry)", f"Rs {int(portfolio[portfolio['component']=='NIFTY Straddle']['pnl_net'].sum()):,}"),
    ("SENSEX Straddle", "159", "85.5%", "3%", "Thu (expiry)", f"Rs {int(portfolio[portfolio['component']=='SENSEX Straddle']['pnl_net'].sum()):,}"),
    ("NIFTY PUT Sweep", "178", "58.4%", "1%", "Non-expiry", f"Rs {int(portfolio[portfolio['component']=='NIFTY PUT Sweep']['pnl_net'].sum()):,}"),
    ("TOTAL", "680", "77.4%", "", "", f"Rs {int(pnls.sum()):,}"),
]
for i, row_data in enumerate(comp_data):
    for j, val in enumerate(row_data):
        t.rows[i+1].cells[j].text = val

doc.add_heading("Strategy Logic", level=2)
doc.add_paragraph("Short Straddle (expiry days):", style="List Bullet")
doc.add_paragraph("Sell ATM Call + ATM Put at 9:31 IST on 0DTE expiry day. "
    "Profit from theta decay when spot stays near strike. "
    "SL if straddle value rises 50-65%, TP if it decays 40-60%, trailing stop locks gains.", style="List Bullet 2")
doc.add_paragraph("PUT Sweep (non-expiry days):", style="List Bullet")
doc.add_paragraph("Detect ORB (9:15-9:30) liquidity sweep above range high, "
    "enter PUT when price reverses back below. Premium-based SL/TP/trail exits.", style="List Bullet 2")

doc.add_page_break()

# ── Section 3: Yearly Breakdown ──────────────────────────────────────────────
doc.add_heading("3. Yearly Performance", level=1)

yearly_data = []
for yr in sorted(portfolio["year"].unique()):
    yd = portfolio[portfolio["year"] == yr]
    p = yd["pnl_net"]; n = len(yd)
    w = (p > 0).sum()
    gp = p[p > 0].sum(); gl = abs(p[p <= 0].sum()) if (p <= 0).any() else 1e-9
    s_pct = p.sum() / CAPITAL * 100
    yn = nd[nd.index.year == yr]
    n_pct = (float(yn.iloc[-1]["Close"]) / float(yn.iloc[0]["Close"]) - 1) * 100 if len(yn) > 1 else 0
    yearly_data.append((yr, n, w/n*100, gp/gl, p.sum(), s_pct, n_pct, "YES" if s_pct > n_pct else "No"))

t = doc.add_table(rows=len(yearly_data)+1, cols=8, style="Light Shading Accent 1")
for i, h in enumerate(["Year", "Trades", "WR%", "PF", "Net P&L", "Strategy%", "Nifty%", "Beat?"]):
    t.rows[0].cells[i].text = h
    t.rows[0].cells[i].paragraphs[0].runs[0].bold = True
for i, (yr, n, wr_y, pf_y, net, sp, np_, beat) in enumerate(yearly_data):
    t.rows[i+1].cells[0].text = str(yr)
    t.rows[i+1].cells[1].text = str(n)
    t.rows[i+1].cells[2].text = f"{wr_y:.1f}%"
    t.rows[i+1].cells[3].text = f"{min(pf_y, 999):.2f}"
    t.rows[i+1].cells[4].text = f"Rs {int(net):,}"
    t.rows[i+1].cells[5].text = f"{sp:+.1f}%"
    t.rows[i+1].cells[6].text = f"{np_:+.1f}%"
    t.rows[i+1].cells[7].text = beat

doc.add_paragraph(f"\nBeat Nifty in {sum(1 for d in yearly_data if d[7]=='YES')}/{len(yearly_data)} years.")

doc.add_page_break()

# ── Section 4: Monthly ──────────────────────────────────────────────────────
doc.add_heading("4. Monthly Performance", level=1)
doc.add_picture(make_monthly_pnl_chart(), width=Inches(6))

mo = portfolio.groupby("month").agg(n=("pnl_net", "count"), net=("pnl_net", "sum"),
    wins=("pnl_net", lambda x: (x > 0).sum()))
mo["wr"] = mo["wins"] / mo["n"] * 100

doc.add_paragraph(f"Profitable months: {(mo['net'] > 0).sum()}/{len(mo)} ({(mo['net'] > 0).mean()*100:.1f}%)")
doc.add_paragraph(f"Best month: Rs {int(mo['net'].max()):,}  |  Worst month: Rs {int(mo['net'].min()):,}")

# Last 24 months table
doc.add_heading("Last 24 Months Detail", level=2)
recent = mo.tail(24)
t = doc.add_table(rows=len(recent)+1, cols=4, style="Light Shading Accent 1")
for i, h in enumerate(["Month", "Trades", "WR%", "Net P&L"]):
    t.rows[0].cells[i].text = h
    t.rows[0].cells[i].paragraphs[0].runs[0].bold = True
for i, (idx, r) in enumerate(recent.iterrows()):
    t.rows[i+1].cells[0].text = str(idx)
    t.rows[i+1].cells[1].text = str(int(r["n"]))
    t.rows[i+1].cells[2].text = f"{r['wr']:.1f}%"
    t.rows[i+1].cells[3].text = f"Rs {int(r['net']):,}"

doc.add_page_break()

# ── Section 5: Weekly Stats ──────────────────────────────────────────────────
doc.add_heading("5. Weekly Statistics", level=1)
wk = portfolio.groupby("yw")["pnl_net"].sum()
doc.add_paragraph(f"Total weeks with trades: {len(wk)}")
doc.add_paragraph(f"Profitable weeks: {(wk > 0).sum()} ({(wk > 0).mean()*100:.1f}%)")
doc.add_paragraph(f"Average weekly P&L: Rs {int(wk.mean()):,}")
doc.add_paragraph(f"Best week: Rs {int(wk.max()):,}")
doc.add_paragraph(f"Worst week: Rs {int(wk.min()):,}")

# ── Section 6: Monte Carlo ──────────────────────────────────────────────────
doc.add_heading("6. Monte Carlo Stress Test", level=1)
doc.add_paragraph(
    "10,000 simulations with randomized trade order to test robustness "
    "independent of actual sequence."
)
doc.add_picture(make_mc_dd_chart(), width=Inches(4.5))

t = doc.add_table(rows=6, cols=4, style="Light Shading Accent 1")
for i, h in enumerate(["Metric", "Median", "95th Percentile", "Worst"]):
    t.rows[0].cells[i].text = h
    t.rows[0].cells[i].paragraphs[0].runs[0].bold = True
mc_data = [
    ("Max Drawdown", f"{np.median(mc_dd):.1f}%", f"{np.percentile(mc_dd, 5):.1f}%", f"{mc_dd.min():.1f}%"),
    ("Final Equity", f"Rs {int(np.median(mc_f)):,}", f"Rs {int(np.percentile(mc_f, 5)):,}", f"Rs {int(mc_f.min()):,}"),
    ("Losing Streak", f"{np.median(mc_s):.0f}", f"{np.percentile(mc_s, 95):.0f}", f"{mc_s.max():.0f}"),
    ("P(Ruin < Rs 1.5L)", "", "", f"{(mc_f < 150000).mean()*100:.2f}%"),
    ("P(Profit)", "", "", f"{(mc_f > CAPITAL).mean()*100:.1f}%"),
]
for i, (m, med, p95, worst) in enumerate(mc_data):
    t.rows[i+1].cells[0].text = m
    t.rows[i+1].cells[1].text = med
    t.rows[i+1].cells[2].text = p95
    t.rows[i+1].cells[3].text = worst

doc.add_page_break()

# ── Section 7: Risk Analysis ────────────────────────────────────────────────
doc.add_heading("7. Risk Analysis & Caveats", level=1)

doc.add_heading("Known Limitations", level=2)
caveats = [
    ("Synthetic Option Pricing", "Black-Scholes premiums used instead of real historical option chain data. "
     "Validation showed CE premiums overpriced by 22-26% (conservative for short selling) "
     "and PE premiums off by 4-6%. 0DTE gamma/theta behavior is approximated."),
    ("Slippage Model", "3% entry / 5% exit slippage applied as haircut on gross P&L. "
     "Real 0DTE bid-ask spreads can be Rs 4-10 per leg. SL exits during fast moves "
     "typically get worse fills than modeled."),
    ("No Gap Handling", "BS assumes continuous price paths. Real markets can gap, "
     "especially at market open and during news events."),
    ("Daily VIX", "Intraday IV changes are not captured. VIX is used as a daily snapshot. "
     "Real 0DTE IV can swing 20-50% intraday."),
    ("Walk-Forward Weakness", "NIFTY straddle walk-forward showed 2023-2024 were near-zero years "
     "(PF 1.05-1.07). The edge is thinner in recent markets."),
    ("Sharpe Caveat", f"Reported Sharpe of {sharpe:.1f} is likely to be 2-3 in live trading "
     "due to execution friction, psychological factors, and regime changes."),
]
for title, desc in caveats:
    doc.add_paragraph(title, style="List Bullet")
    doc.add_paragraph(desc, style="List Bullet 2")

doc.add_heading("What Would Improve Confidence", level=2)
improvements = [
    "Backtest with real historical option chain data (not BS synthetic)",
    "Paper trade for 3 months before deploying capital",
    "Start with 1% risk (not 3%) and scale up after verifying live results",
    "Monitor bid-ask spreads on 0DTE options for actual slippage data",
    "Implement proper walk-forward optimization for NIFTY straddle parameters",
    "Track live fills vs backtest expected fills for slippage calibration",
]
for imp in improvements:
    doc.add_paragraph(imp, style="List Bullet")

doc.add_page_break()

# ── Section 8: Strategy Parameters ──────────────────────────────────────────
doc.add_heading("8. Strategy Parameters", level=1)

doc.add_heading("NIFTY Short Straddle", level=2)
t = doc.add_table(rows=8, cols=2, style="Light Shading Accent 1")
nifty_params = [
    ("Stop Loss", "50% (straddle value rises 50%)"),
    ("Take Profit", "40% (straddle decays 40%)"),
    ("Trail Activate", "25% (after 25% decay)"),
    ("Trail %", "15% from trough"),
    ("VIX Range", "11-25"),
    ("Entry Time", "9:31 IST"),
    ("Force Exit", "15:10 IST"),
]
t.rows[0].cells[0].text = "Parameter"; t.rows[0].cells[1].text = "Value"
t.rows[0].cells[0].paragraphs[0].runs[0].bold = True
t.rows[0].cells[1].paragraphs[0].runs[0].bold = True
for i, (k, v) in enumerate(nifty_params):
    t.rows[i+1].cells[0].text = k; t.rows[i+1].cells[1].text = v

doc.add_heading("SENSEX Short Straddle (Optimized)", level=2)
t = doc.add_table(rows=8, cols=2, style="Light Shading Accent 1")
sensex_params = [
    ("Stop Loss", "65% (wider — SENSEX is more volatile)"),
    ("Take Profit", "60% (deeper decay target)"),
    ("Trail Activate", "20%"),
    ("Trail %", "18% from trough"),
    ("VIX Range", "12-30"),
    ("Optimization", "IS: 2019-2021, OOS: 2018 + 2022-2026"),
    ("OOS Result", "N=119, WR=81.5%, PF=2.580"),
]
t.rows[0].cells[0].text = "Parameter"; t.rows[0].cells[1].text = "Value"
t.rows[0].cells[0].paragraphs[0].runs[0].bold = True
t.rows[0].cells[1].paragraphs[0].runs[0].bold = True
for i, (k, v) in enumerate(sensex_params):
    t.rows[i+1].cells[0].text = k; t.rows[i+1].cells[1].text = v

# ── Save ─────────────────────────────────────────────────────────────────────
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
doc.save(str(OUT_PATH))
print(f"Report saved: {OUT_PATH}")
print(f"  Pages: ~10 | Trades: {len(pnls)} | Period: {years:.1f} years")
