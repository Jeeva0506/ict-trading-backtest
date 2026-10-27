import pandas as pd

CAPITAL = 1_000_000

def metrics(df, label):
    if df.empty:
        print(f"  {label}: no trades")
        return
    n   = len(df)
    years = pd.to_datetime(df["entry_date"]).dt.year
    yrs = years.max() - years.min() + 1
    wr  = (df["pnl_net"] > 0).mean() * 100
    wins = df[df["pnl_net"] > 0]["pnl_net"].sum()
    loss = abs(df[df["pnl_net"] < 0]["pnl_net"].sum())
    pf  = wins / loss if loss > 0 else 9.99
    net = df["pnl_net"].sum()
    eb  = df["exit_type"].value_counts().to_dict()
    print(f"  {label:<44} n={n:>5} ({n/yrs:.0f}/yr)  WR={wr:.1f}%  PF={pf:.3f}  "
          f"Net=Rs{net:>10,.0f} ({net/CAPITAL/yrs*100:.1f}%/yr)")
    print(f"    Exits: {eb}")

nifty = pd.read_csv("results/zerodha_nifty_trades.csv")
bn    = pd.read_csv("results/zerodha_banknifty_trades.csv")
combo = pd.concat([nifty, bn], ignore_index=True)
combo["year"] = pd.to_datetime(combo["entry_date"]).dt.year

print("="*80)
print("  ZERODHA BACKTEST RESULTS  (Nifty + BankNifty, real data)")
print("="*80)

print("\n[Overall]")
metrics(nifty, "Nifty")
metrics(bn,    "BankNifty")
metrics(combo, "COMBINED")

print("\n[Nifty by quality]")
metrics(nifty[nifty["quality"] == "A+"],      "  A+ (2% risk)")
metrics(nifty[nifty["quality"] == "regular"],  "  Regular (1% risk)")

print("\n[Yearly — Nifty]")
print(f"  {'Year':>5}  {'n':>4}  {'WR':>6}  {'PF':>6}  {'Net P&L':>12}  {'Return%':>8}  Gate")
print("  " + "-"*70)
for yr, g in nifty.groupby(pd.to_datetime(nifty["entry_date"]).dt.year):
    n  = len(g)
    wr = (g["pnl_net"] > 0).mean() * 100
    w  = g[g["pnl_net"] > 0]["pnl_net"].sum()
    l  = abs(g[g["pnl_net"] < 0]["pnl_net"].sum())
    pf = w / l if l > 0 else 9.99
    net = g["pnl_net"].sum()
    ret = net / CAPITAL * 100
    gates = []
    if n >= 100: gates.append("100t")
    if ret >= 50: gates.append("50%")
    print(f"  {yr:>5}  {n:>4}  {wr:>5.1f}%  {pf:>5.3f}  Rs{net:>10,.0f}  {ret:>7.1f}%   {' '.join(gates)}")

print("\n[Yearly — BankNifty]")
print(f"  {'Year':>5}  {'n':>4}  {'WR':>6}  {'PF':>6}  {'Net P&L':>12}  {'Return%':>8}")
print("  " + "-"*60)
for yr, g in bn.groupby(pd.to_datetime(bn["entry_date"]).dt.year):
    n  = len(g)
    wr = (g["pnl_net"] > 0).mean() * 100
    w  = g[g["pnl_net"] > 0]["pnl_net"].sum()
    l  = abs(g[g["pnl_net"] < 0]["pnl_net"].sum())
    pf = w / l if l > 0 else 9.99
    net = g["pnl_net"].sum()
    ret = net / CAPITAL * 100
    print(f"  {yr:>5}  {n:>4}  {wr:>5.1f}%  {pf:>5.3f}  Rs{net:>10,.0f}  {ret:>7.1f}%")

print("\n[Yearly — COMBINED]")
print(f"  {'Year':>5}  {'n':>4}  {'WR':>6}  {'PF':>6}  {'Net P&L':>12}  {'Return%':>8}  Gate")
print("  " + "-"*70)
for yr, g in combo.groupby("year"):
    n  = len(g)
    wr = (g["pnl_net"] > 0).mean() * 100
    w  = g[g["pnl_net"] > 0]["pnl_net"].sum()
    l  = abs(g[g["pnl_net"] < 0]["pnl_net"].sum())
    pf = w / l if l > 0 else 9.99
    net = g["pnl_net"].sum()
    ret = net / CAPITAL * 100
    gates = []
    if n >= 100: gates.append("100t")
    if ret >= 50: gates.append("50%")
    print(f"  {yr:>5}  {n:>4}  {wr:>5.1f}%  {pf:>5.3f}  Rs{net:>10,.0f}  {ret:>7.1f}%   {' '.join(gates)}")

yearly_net = combo.groupby("year")["pnl_net"].sum()
avg_n   = combo.groupby("year").size().mean()
avg_ret = yearly_net.mean() / CAPITAL * 100
print(f"  {'AVG':>5}  {avg_n:>4.0f}                           "
      f"Rs{yearly_net.mean():>10,.0f}  {avg_ret:>7.1f}%")
yr100 = sum(1 for y, g in combo.groupby("year") if len(g) >= 100)
yr50  = sum(1 for y in yearly_net.index if yearly_net[y] / CAPITAL * 100 >= 50)
print(f"\n  Years with 100+ trades : {yr100}/{combo['year'].nunique()}")
print(f"  Years with 50%+ return : {yr50}/{combo['year'].nunique()}")
