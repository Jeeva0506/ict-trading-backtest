"""Full portfolio audit — Rs 3L capital, realistic slippage."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
from pathlib import Path

CAPITAL = 300_000
DATA_DIR = Path(__file__).resolve().parent / "data"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"

# Load pre-computed trades and rescale
nst = pd.read_csv(REPORTS_DIR / "straddle_nifty_trades.csv")
sst = pd.read_csv(REPORTS_DIR / "straddle_sensex_optimized_trades.csv")
nsw = pd.read_csv(REPORTS_DIR / "put_sweep_nifty_trades.csv")

# Straddles were at 1% risk on Rs 10L = Rs 10K risk/trade
# At 3% on Rs 3L = Rs 9K risk/trade → scale = 0.9x
# PLUS realistic slippage haircut: original 1%/1%, realistic 3%/5%
# Avg straddle entry ~110, extra slip = (0.02+0.04)*110*qty ≈ 15% of gross P&L
ST_SCALE = (CAPITAL * 0.03) / (1_000_000 * 0.01)  # = 0.9
ST_SLIP_HAIRCUT = 0.85  # 15% haircut for realistic slippage

nst["pnl_net"] = nst["pnl_net"] * ST_SCALE * ST_SLIP_HAIRCUT
nst["component"] = "nifty_straddle"

sst["pnl_net"] = sst["pnl_net"] * ST_SCALE * ST_SLIP_HAIRCUT
sst["component"] = "sensex_straddle"

# Sweep at 1% on Rs 10L = Rs 10K. At 1% on Rs 3L = Rs 3K → scale = 0.3
# Extra slippage: 8% haircut
SW_SCALE = (CAPITAL * 0.01) / (1_000_000 * 0.01)  # = 0.3
SW_SLIP_HAIRCUT = 0.92  # 8% haircut

nsw["pnl_net"] = nsw["pnl_net"] * SW_SCALE * SW_SLIP_HAIRCUT
nsw["component"] = "nifty_sweep"

portfolio = pd.concat([nst[["entry_date","exit_date","pnl_net","component","exit_type"]],
                       sst[["entry_date","exit_date","pnl_net","component","exit_type"]],
                       nsw[["entry_date","exit_date","pnl_net","component","exit_type"]]
                      ]).sort_values("entry_date").reset_index(drop=True)

# Nifty B&H
nd = pd.read_csv(DATA_DIR / "zerodha_nifty_day.csv", index_col=0, parse_dates=True)

pnls = portfolio["pnl_net"].values
equity = CAPITAL + np.cumsum(pnls)
peak = np.maximum.accumulate(equity)
dd = (equity - peak) / peak * 100

first = pd.to_datetime(portfolio["entry_date"].min())
last = pd.to_datetime(portfolio["entry_date"].max())
years = (last - first).days / 365.25
strat_final = equity[-1]
strat_cagr = ((strat_final / CAPITAL) ** (1 / years) - 1) * 100
strat_dd = dd.min()
wins = pnls[pnls > 0]
losses = pnls[pnls <= 0]
wr = len(wins) / len(pnls) * 100
pf = wins.sum() / abs(losses.sum()) if len(losses) else float("inf")

nbh = nd[(nd.index >= first) & (nd.index <= last)]
n0 = float(nbh.iloc[0]["Close"])
n1 = float(nbh.iloc[-1]["Close"])
bh_cagr = ((n1 / n0) ** (1 / years) - 1) * 100
bh_eq = nbh["Close"] / n0 * CAPITAL
bh_dd = ((bh_eq - bh_eq.cummax()) / bh_eq.cummax() * 100).min()
bh_sh = (nbh["Close"].pct_change().dropna().mean() - 0.065 / 252) / nbh["Close"].pct_change().dropna().std() * np.sqrt(252)
bh_final = CAPITAL * n1 / n0

dpnl = portfolio.groupby("entry_date")["pnl_net"].sum()
sharpe = dpnl.mean() / dpnl.std() * np.sqrt(252) if dpnl.std() > 0 else 0

sep = "=" * 80
line = "-" * 80

print(f"\n{sep}")
print(f"  FULL PORTFOLIO AUDIT")
print(f"  Capital: Rs {CAPITAL:,} | Slippage: 3%/5% (realistic) | Costs: Full Zerodha")
print(f"  Period: {first.date()} to {last.date()} ({years:.1f} years)")
print(f"  Pricing: Synthetic BS (NOT real option chain)")
print(f"{sep}")

print(f"\n  {'Metric':<28} {'Strategy':>18} {'Nifty B&H':>18}")
print(f"  {line}")
print(f"  {'Trades':<28} {len(pnls):>18,} {'1':>18}")
print(f"  {'Win Rate':<28} {f'{wr:.1f}%':>18} {'N/A':>18}")
print(f"  {'Profit Factor':<28} {f'{pf:.3f}':>18} {'N/A':>18}")
print(f"  {'CAGR':<28} {f'{strat_cagr:.1f}%':>18} {f'{bh_cagr:.1f}%':>18}")
print(f"  {'Sharpe':<28} {f'{sharpe:.3f}':>18} {f'{bh_sh:.3f}':>18}")
print(f"  {'Max Drawdown':<28} {f'{strat_dd:.1f}%':>18} {f'{bh_dd:.1f}%':>18}")
print(f"  {'Final Equity':<28} {'Rs '+format(int(strat_final),','):>18} {'Rs '+format(int(bh_final),','):>18}")
print(f"  {'Total Return':<28} {f'{(strat_final/CAPITAL-1)*100:.1f}%':>18} {f'{(bh_final/CAPITAL-1)*100:.1f}%':>18}")
print(f"  {'Calmar (CAGR/DD)':<28} {f'{abs(strat_cagr/strat_dd):.2f}':>18} {f'{abs(bh_cagr/bh_dd):.2f}':>18}")
print(f"  {line}")

# Components
print(f"\n  COMPONENTS (risk-adjusted, slippage-adjusted):")
print(f"  {line}")
for comp in ["nifty_straddle", "sensex_straddle", "nifty_sweep"]:
    c = portfolio[portfolio["component"] == comp]
    cw = (c["pnl_net"] > 0).mean() * 100
    risk = "3%" if "straddle" in comp else "1%"
    print(f"    {comp:<25} N={len(c):>4}  WR={cw:>5.1f}%  Risk={risk}  Net=Rs{c['pnl_net'].sum():>10,.0f}")
print(f"    {'TOTAL':<25} N={len(portfolio):>4}                Net=Rs{pnls.sum():>10,.0f}")
print(f"  {line}")

# YEARLY
portfolio["year"] = pd.to_datetime(portfolio["entry_date"]).dt.year
print(f"\n  YEARLY BREAKDOWN:")
print(f"  {line}")
print(f"  {'Year':>6} {'N':>5} {'WR':>7} {'PF':>7} {'Net P&L':>11} {'Strat%':>8} {'Nifty%':>8} {'Beat':>5}")
print(f"  {line}")
for yr in sorted(portfolio["year"].unique()):
    yd = portfolio[portfolio["year"] == yr]
    p = yd["pnl_net"]
    n = len(yd)
    w = (p > 0).sum()
    gp = p[p > 0].sum()
    gl = abs(p[p <= 0].sum()) if (p <= 0).any() else 1e-9
    s_pct = p.sum() / CAPITAL * 100
    yn = nd[nd.index.year == yr]
    n_pct = (float(yn.iloc[-1]["Close"]) / float(yn.iloc[0]["Close"]) - 1) * 100 if len(yn) > 1 else 0
    beat = "YES" if s_pct > n_pct else "no"
    print(f"  {yr:>6} {n:>5} {w/n*100:>6.1f}% {gp/gl:>6.2f} Rs{p.sum():>9,.0f} {s_pct:>+7.1f}% {n_pct:>+7.1f}% {beat:>5}")
print(f"  {line}")
beat_count = sum(1 for yr in sorted(portfolio["year"].unique())
    if portfolio[portfolio["year"] == yr]["pnl_net"].sum() / CAPITAL * 100 >
    ((float(nd[nd.index.year == yr].iloc[-1]["Close"]) / float(nd[nd.index.year == yr].iloc[0]["Close"]) - 1) * 100
     if len(nd[nd.index.year == yr]) > 1 else 0))
print(f"  Beat Nifty: {beat_count}/{len(portfolio['year'].unique())} years")

# MONTHLY (last 24)
portfolio["month"] = pd.to_datetime(portfolio["entry_date"]).dt.to_period("M")
mo = portfolio.groupby("month").agg(n=("pnl_net", "count"), net=("pnl_net", "sum"),
    wins=("pnl_net", lambda x: (x > 0).sum()))
mo["wr"] = mo["wins"] / mo["n"] * 100
print(f"\n  MONTHLY (last 24):")
print(f"  {line}")
for idx, r in mo.tail(24).iterrows():
    bar = "+" * min(int(max(r["net"] / 200, 0)), 40) + "-" * min(int(max(-r["net"] / 200, 0)), 40)
    print(f"    {str(idx):>7}  N={int(r['n']):>3}  WR={r['wr']:>5.1f}%  Rs{r['net']:>7,.0f}  {bar}")
print(f"  {line}")
print(f"  Profitable months: {(mo['net'] > 0).sum()}/{len(mo)} ({(mo['net'] > 0).mean()*100:.1f}%)")

# WEEKLY
portfolio["yw"] = pd.to_datetime(portfolio["entry_date"]).dt.strftime("%Y-W%U")
wk = portfolio.groupby("yw")["pnl_net"].sum()
print(f"\n  WEEKLY STATS:")
print(f"    Weeks traded: {len(wk)} | Profitable: {(wk > 0).sum()} ({(wk > 0).mean()*100:.1f}%)")
print(f"    Avg: Rs{wk.mean():>6,.0f} | Best: Rs{wk.max():>6,.0f} | Worst: Rs{wk.min():>7,.0f}")

# EXIT TYPE BREAKDOWN
print(f"\n  EXIT TYPES:")
for comp in sorted(portfolio["component"].unique()):
    c = portfolio[portfolio["component"] == comp]
    exits = c["exit_type"].value_counts().to_dict()
    print(f"    {comp}: {exits}")

# MONTE CARLO
print(f"\n  MONTE CARLO (10,000 shuffles):")
print(f"  {line}")
np.random.seed(42)
mc_dd = []
mc_f = []
mc_s = []
for _ in range(10000):
    sh = np.random.permutation(pnls)
    eq = CAPITAL + np.cumsum(sh)
    pk = np.maximum.accumulate(eq)
    mc_dd.append(((eq - pk) / pk * 100).min())
    mc_f.append(eq[-1])
    st = 0
    mx = 0
    for p in sh:
        if p <= 0:
            st += 1
            mx = max(mx, st)
        else:
            st = 0
    mc_s.append(mx)
mc_dd = np.array(mc_dd)
mc_f = np.array(mc_f)
mc_s = np.array(mc_s)
print(f"    Max DD:  median={np.median(mc_dd):.1f}%  |  95th={np.percentile(mc_dd, 5):.1f}%  |  worst={mc_dd.min():.1f}%")
print(f"    Final:   median=Rs{np.median(mc_f):>8,.0f}  |  5th=Rs{np.percentile(mc_f, 5):>8,.0f}")
print(f"    Streak:  median={np.median(mc_s):.0f}  |  95th={np.percentile(mc_s, 95):.0f}  |  worst={mc_s.max():.0f}")
print(f"    P(ruin < Rs 1.5L): {(mc_f < 150000).mean()*100:.2f}%")
print(f"    P(profit):         {(mc_f > CAPITAL).mean()*100:.1f}%")
print(f"  {line}")

# CAVEATS
print(f"\n  CAVEATS & KNOWN LIMITATIONS:")
print(f"  {line}")
print(f"  1. Option pricing: Synthetic BS, NOT real historical option chain")
print(f"     - CE premiums overpriced ~22-26% (conservative for shorts)")
print(f"     - PE premiums off ~4-6% (acceptable)")
print(f"     - 0DTE gamma/theta behavior is approximate")
print(f"  2. Slippage: 3% entry / 5% exit applied as haircut")
print(f"     - Real 0DTE bid-ask can be Rs 4-10 per leg")
print(f"     - SL exits in fast moves get worse fills")
print(f"  3. No gap handling: BS assumes continuous prices")
print(f"  4. VIX used as daily value (no intraday IV changes)")
print(f"  5. Walk-forward showed 2023-2024 were near-zero years")
print(f"  6. SENSEX straddle optimized IS: 2019-2021, OOS: rest")
print(f"  7. NIFTY straddle NOT optimized (default params)")
print(f"  {line}")
print(f"{sep}\n")
