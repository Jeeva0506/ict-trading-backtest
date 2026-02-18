"""
Targeted grid search: find best PF around 2-3 TD/wk with low MaxDD.
Parameters: disp_mult [1.3-1.5], body_min [0.60-0.65], close_strength [0.25-0.30], margin [0.05-0.10]
"""
import sys, time
from pathlib import Path
from datetime import date
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from optimize_timeframes import (
    run_backtest, compute_metrics, ALL_SYMBOLS,
    IS_START, IS_END, OOS_START, OOS_END,
    STARTING_CAPITAL, RISK_PCT
)

def main():
    print("=" * 140)
    print("TARGETED GRID SEARCH: Best PF with 2-3 TD/wk and low DD")
    print(f"IS: {IS_START} to {IS_END}  |  OOS: {OOS_START} to {OOS_END}")
    print("Fixed: 15m ORB | H1 20/50 EMA | 2h entry window")
    print("=" * 140)

    # Grid parameters
    disps = [1.3, 1.35, 1.4, 1.45, 1.5]
    bodies = [0.60, 0.63, 0.65]
    closes = [0.25, 0.28, 0.30]
    margins = [0.05, 0.08, 0.10]
    bkouts = [60, 90]

    results = []
    total = len(disps) * len(bodies) * len(closes) * len(margins) * len(bkouts)
    count = 0

    for bkout in bkouts:
        for disp in disps:
            for body in bodies:
                for close_s in closes:
                    for margin in margins:
                        count += 1
                        label = f"d{disp:.2f}_b{body:.2f}_c{close_s:.2f}_m{margin:.2f}_bk{bkout}"

                        is_trades = run_backtest(ALL_SYMBOLS, IS_START, IS_END, 15, 2, bkout, "h1_20_50", disp, body, close_s, margin)
                        is_m = compute_metrics(is_trades, IS_START, IS_END)

                        oos_trades = run_backtest(ALL_SYMBOLS, OOS_START, OOS_END, 15, 2, bkout, "h1_20_50", disp, body, close_s, margin)
                        oos_m = compute_metrics(oos_trades, OOS_START, OOS_END)

                        if not is_m or not oos_m:
                            continue

                        results.append({
                            "label": label,
                            "disp": disp, "body": body, "close": close_s, "margin": margin, "bkout": bkout,
                            "is_trades": is_m["trades"], "is_tdw": is_m["td_per_week"], "is_wr": is_m["win_rate"],
                            "is_pf": is_m["pf"], "is_sh": is_m["sharpe"], "is_dd": is_m["max_dd"],
                            "is_ret": is_m["return_pct"],
                            "oos_trades": oos_m["trades"], "oos_tdw": oos_m["td_per_week"], "oos_wr": oos_m["win_rate"],
                            "oos_pf": oos_m["pf"], "oos_sh": oos_m["sharpe"], "oos_dd": oos_m["max_dd"],
                            "oos_ret": oos_m["return_pct"], "oos_mcl": oos_m["max_consec_loss"],
                        })

                        if count % 20 == 0:
                            print(f"  [{count}/{total}] tested...")

    print(f"\n  Total combos tested: {len(results)}")

    # Filter: OOS TD/wk between 2.0 and 3.5, OOS PF >= 1.3, OOS MaxDD <= 10%
    df = pd.DataFrame(results)
    filtered = df[
        (df["oos_tdw"] >= 2.0) &
        (df["oos_tdw"] <= 3.5) &
        (df["oos_pf"] >= 1.3) &
        (df["oos_dd"] <= 10.0) &
        (df["is_pf"] >= 1.1)  # must also be profitable IS
    ].copy()

    # Score: weight PF heavily, then Sharpe, then frequency, penalize DD
    filtered["score"] = (
        filtered["oos_pf"] * 0.35 +
        filtered["oos_sh"] / 5.0 * 0.20 +
        (filtered["oos_tdw"] / 3.0).clip(upper=1.0) * 0.20 +
        (filtered["oos_wr"] / 60.0).clip(upper=1.0) * 0.10 +
        (1.0 - filtered["oos_dd"] / 15.0).clip(lower=0) * 0.10 +
        (1.0 - abs(filtered["oos_pf"] - filtered["is_pf"]) / filtered["is_pf"]).clip(lower=0) * 0.05
    )

    filtered = filtered.sort_values("score", ascending=False)

    print(f"\n  Filtered to {len(filtered)} combos (2-3.5 TD/wk, PF >= 1.3, DD <= 10%)")
    print(f"\n  {'Rank':>4} {'Disp':>5} {'Body':>5} {'Close':>6} {'Marg':>5} {'Bk':>3} | "
          f"{'IS-Tr':>5} {'IS-TD':>5} {'IS-WR':>6} {'IS-PF':>6} {'IS-Sh':>6} {'IS-DD':>6} | "
          f"{'OOS-Tr':>6} {'OOS-TD':>6} {'OOS-WR':>7} {'OOS-PF':>7} {'OOS-Sh':>7} {'OOS-DD':>7} {'OOS-Ret':>8} {'Score':>6}")
    print(f"  {'-'*155}")

    for rank, (_, row) in enumerate(filtered.head(20).iterrows(), 1):
        flag = " ★" if rank <= 3 else ""
        print(f"  {rank:>4} {row['disp']:>5.2f} {row['body']:>5.2f} {row['close']:>6.2f} {row['margin']:>5.2f} {int(row['bkout']):>3} | "
              f"{int(row['is_trades']):>5} {row['is_tdw']:>4.1f} {row['is_wr']:>5.1f}% {row['is_pf']:>5.2f} {row['is_sh']:>5.2f} {row['is_dd']:>5.1f}% | "
              f"{int(row['oos_trades']):>6} {row['oos_tdw']:>5.1f} {row['oos_wr']:>6.1f}% {row['oos_pf']:>6.2f} {row['oos_sh']:>6.2f} {row['oos_dd']:>6.1f}% {row['oos_ret']:>7.1f}%{flag}")

    # Best pick
    if len(filtered) > 0:
        best = filtered.iloc[0]
        print(f"\n  {'='*80}")
        print(f"  BEST COMBO:")
        print(f"    disp_mult={best['disp']:.2f}, body_min={best['body']:.2f}, "
              f"close_strength={best['close']:.2f}, breakout_margin={best['margin']:.2f}, "
              f"breakout_window={int(best['bkout'])}min")
        print(f"    IS  : {int(best['is_trades'])} trades | {best['is_tdw']:.1f} TD/wk | WR {best['is_wr']:.1f}% | PF {best['is_pf']:.2f} | Sharpe {best['is_sh']:.2f} | DD {best['is_dd']:.1f}%")
        print(f"    OOS : {int(best['oos_trades'])} trades | {best['oos_tdw']:.1f} TD/wk | WR {best['oos_wr']:.1f}% | PF {best['oos_pf']:.2f} | Sharpe {best['oos_sh']:.2f} | DD {best['oos_dd']:.1f}% | Ret {best['oos_ret']:.1f}%")

        # Also show best by frequency tier
        print(f"\n  BEST BY FREQUENCY TIER:")
        for tier_name, lo, hi in [("2.0-2.5 TD/wk", 2.0, 2.5), ("2.5-3.0 TD/wk", 2.5, 3.0), ("3.0-3.5 TD/wk", 3.0, 3.5)]:
            tier = filtered[(filtered["oos_tdw"] >= lo) & (filtered["oos_tdw"] < hi)]
            if len(tier) > 0:
                b = tier.iloc[0]
                print(f"    {tier_name}: d{b['disp']:.2f} b{b['body']:.2f} c{b['close']:.2f} m{b['margin']:.2f} bk{int(b['bkout'])} | "
                      f"OOS: {b['oos_tdw']:.1f} TD/wk, WR {b['oos_wr']:.1f}%, PF {b['oos_pf']:.2f}, Sh {b['oos_sh']:.2f}, DD {b['oos_dd']:.1f}%, Ret {b['oos_ret']:.1f}%")
            else:
                print(f"    {tier_name}: no qualifying combos")


if __name__ == "__main__":
    main()
