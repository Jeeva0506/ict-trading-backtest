"""
NR7 Paper Tracker — NSE Positional Strategy
============================================
Run daily at 4:15 PM IST (after NSE close) to:
  1. Enter any pending signals at today's actual open price
  2. Trail SL (50 EMA) and exit positions that hit SL today
  3. Scan for new NR7 breakout signals (will enter tomorrow)
  4. Send Telegram summary

Capital : Rs 2,00,000 (fixed sizing)
Risk    : 2% = Rs 4,000 per trade (fixed)
Max pos : 5 concurrent positions
Max cap : 25% = Rs 50,000 per position
Trail   : SMA50 (10yr backtest winner — CAGR 30%, MaxDD 8%, PF 4.71)
Filter  : Pure NR7 breakout, vol_surge prioritised when slots are full

Usage:
    python live/nr7_tracker.py              # full daily run
    python live/nr7_tracker.py --status     # print positions, no fetch
"""
import sys, json, logging, time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.nr7_positional import (
    compute_indicators, generate_signals, FILTERS_NONE, FILTER_NAMES,
)
from costs.zerodha_equity import equity_round_trip_cost
from run_nr7_backtest import _universe, NIFTY50_TOKEN
from live.telegram_alert import IndianTelegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nr7_tracker")

IST = timezone(timedelta(hours=5, minutes=30))

# ── Constants (match the backtest config) ─────────────────────────────────────
CAPITAL       = 200_000   # Rs 2L fixed sizing base
RISK_RS       = 4_000     # 2% of Rs 2L (never compounds)
MAX_POSITIONS = 5
MAX_CAP_RS    = 50_000    # 25% of Rs 2L
MIN_CASH      = 10_000    # keep this much free at all times

# Vol surge as priority scorer (not hard filter) — same as winning backtest config
VOL_SURGE_FILTER = {k: (k == 'vol_surge') for k in FILTER_NAMES}

STATE_FILE = ROOT / "data" / "nr7_paper_state.json"
EOD_CACHE  = ROOT / "data" / "nr7_eod_cache"
EOD_CACHE.mkdir(parents=True, exist_ok=True)


# ── Env + auth ────────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env = {}
    candidates = [
        ROOT / ".env",
        ROOT / ".env.indian",
        ROOT.parent / "backtest_engine" / ".env.zerodha",  # Trading_Automation/backtest_engine/
        ROOT / "backtest_engine" / ".env.zerodha",
    ]
    for fpath in candidates:
        if fpath.exists():
            for line in fpath.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def _get_kite(env: dict):
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=env["ZERODHA_API_KEY"])
    kite.set_access_token(env["ZERODHA_ACCESS_TOKEN"])
    return kite


# ── State ─────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"capital": float(CAPITAL), "positions": {}, "pending": {}, "trades": []}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Data fetching ─────────────────────────────────────────────────────────────
def _fetch_daily_kite(kite, token: int, days: int) -> pd.DataFrame:
    end   = datetime.now()
    start = end - timedelta(days=days)
    try:
        rows = kite.historical_data(token, start, end, "day",
                                    continuous=False, oi=False)
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date").sort_index()[["Open", "High", "Low", "Close", "Volume"]]


def update_eod_cache(kite, universe: list):
    """Append today's data to each stock's cache. Fast — only fetches last 7 days."""
    log.info(f"Updating EOD cache ({len(universe)} stocks)...")
    ok = 0
    for i, stock in enumerate(universe):
        sym   = stock["symbol"]
        fpath = EOD_CACHE / f"{sym}.csv"
        if fpath.exists():
            old = pd.read_csv(fpath, index_col="Date", parse_dates=True)
            fresh = _fetch_daily_kite(kite, stock["token"], days=7)
            if fresh.empty:
                continue
            df = pd.concat([old, fresh])
        else:
            df = _fetch_daily_kite(kite, stock["token"], days=220)
        df = df[~df.index.duplicated(keep="last")].sort_index().tail(300)
        if not df.empty:
            df.to_csv(fpath); ok += 1
        time.sleep(0.04)
        if (i + 1) % 50 == 0:
            log.info(f"  {i+1}/{len(universe)} done")
    log.info(f"Cache updated: {ok}/{len(universe)}")


def load_eod_data(universe: list) -> dict:
    out = {}
    for stock in universe:
        sym   = stock["symbol"]
        fpath = EOD_CACHE / f"{sym}.csv"
        if not fpath.exists():
            continue
        df = pd.read_csv(fpath, index_col="Date", parse_dates=True)
        if len(df) >= 120:
            out[sym] = df
    return out


# ── Core daily logic ──────────────────────────────────────────────────────────
def run_eod(kite, tg: IndianTelegram) -> dict:
    """
    Full daily EOD run. Returns summary dict with exits, entries, signals.
    Called at ~4:15 PM IST each trading day.
    """
    state = _load_state()
    today = datetime.now(IST).date()
    log.info(f"NR7 EOD run for {today}")

    # ── 1. Update all stock data ──────────────────────────────────────────────
    universe = _universe(kite)
    update_eod_cache(kite, universe)
    stock_data = load_eod_data(universe)
    log.info(f"Loaded {len(stock_data)} stocks")

    # Nifty for trend filter
    nifty_raw = _fetch_daily_kite(kite, NIFTY50_TOKEN, days=220)
    nifty_ind = compute_indicators(nifty_raw) if not nifty_raw.empty else pd.DataFrame()

    # Pre-compute indicators (add SMA50 for trail — backtest winner over EMA50)
    def _with_sma50(df: pd.DataFrame) -> pd.DataFrame:
        df = compute_indicators(df)
        df["sma50"] = df["Close"].rolling(50, min_periods=25).mean()
        return df

    stock_ind = {sym: _with_sma50(df) for sym, df in stock_data.items()}

    exits   = []
    entries = []

    # ── 2. Enter pending signals (from yesterday) ─────────────────────────────
    for sym, sig in list(state["pending"].items()):
        if sym not in stock_ind:
            continue
        df  = stock_ind[sym]
        # Today's row
        today_rows = df[df.index.date == today]
        if today_rows.empty:
            continue
        row   = today_rows.iloc[-1]
        entry = float(row["Open"])
        sl    = float(sig["stop_loss"])

        if entry <= sl:
            log.info(f"SKIP entry {sym}: open {entry:.2f} <= SL {sl:.2f}")
            state["pending"].pop(sym, None)
            continue

        # Already at max positions or already holding this stock
        if len(state["positions"]) >= MAX_POSITIONS or sym in state["positions"]:
            state["pending"].pop(sym, None)
            continue

        distance = entry - sl
        if distance <= 0:
            state["pending"].pop(sym, None)
            continue

        qty = min(int(RISK_RS / distance), int(MAX_CAP_RS / entry))
        if qty < 1:
            state["pending"].pop(sym, None)
            continue

        cap_needed = qty * entry
        if cap_needed > state["capital"] - MIN_CASH:
            log.info(f"SKIP entry {sym}: need Rs{cap_needed:.0f}, "
                     f"have Rs{state['capital']-MIN_CASH:.0f}")
            state["pending"].pop(sym, None)
            continue

        state["capital"] -= cap_needed
        state["positions"][sym] = {
            "entry_date":       str(today),
            "entry_price":      round(entry, 2),
            "sl":               round(sl, 2),
            "qty":              qty,
            "capital_deployed": round(cap_needed, 2),
            "risk_rs":          RISK_RS,
            "score":            sig.get("score", 0),
            "score_detail":     sig.get("score_detail", "base"),
            "nr7_high":         sig.get("nr7_high", 0),
            "nr7_low":          sig.get("nr7_low", 0),
            "atr":              sig.get("atr", 0),
        }
        state["pending"].pop(sym, None)
        entries.append({"symbol": sym, "entry_price": entry, "sl": sl,
                        "qty": qty, "risk_rs": RISK_RS})
        log.info(f"ENTERED {sym}: {qty} shares @ Rs{entry:.2f}  SL={sl:.2f}")

    # ── 3. Trail SL and check exits ───────────────────────────────────────────
    for sym in list(state["positions"].keys()):
        if sym not in stock_ind:
            continue
        pos = state["positions"][sym]
        df  = stock_ind[sym]
        today_rows = df[df.index.date == today]
        if today_rows.empty:
            continue
        row = today_rows.iloc[-1]

        # SMA50 trail — 10yr backtest winner (CAGR 30%, DD 8%, PF 4.71)
        sma50 = row.get("sma50", float("nan"))
        new_sl = float(sma50) if not pd.isna(sma50) else float(row["ema50"])
        if new_sl > pos["sl"]:
            pos["sl"] = round(new_sl, 2)

        if float(row["Low"]) <= pos["sl"]:
            exit_p = pos["sl"]
            qty    = pos["qty"]
            entry  = pos["entry_price"]
            gross  = (exit_p - entry) * qty
            cost   = equity_round_trip_cost(entry, max(exit_p, 0.01), qty)
            net    = gross - cost
            hold_d = (today - pd.Timestamp(pos["entry_date"]).date()).days

            state["capital"] += pos["capital_deployed"] + net
            r_mult = round(net / pos["risk_rs"], 2)

            trade = {
                "symbol":      sym,
                "entry_date":  pos["entry_date"],
                "exit_date":   str(today),
                "entry_price": entry,
                "exit_price":  round(exit_p, 2),
                "qty":         qty,
                "net_rs":      round(net, 2),
                "r_multiple":  r_mult,
                "hold_days":   hold_d,
                "win":         net > 0,
            }
            state["trades"].append(trade)
            exits.append(trade)
            del state["positions"][sym]
            log.info(f"EXIT {sym}: Rs{net:+,.0f}  {r_mult:+.2f}R  hold={hold_d}d")

    # ── 4. Scan for new NR7 signals (for logging only — entries happen at 3:05 PM) ─
    # New entries are handled by run_305pm() called at 3:05 PM with live LTP.
    # This step just reports which signals fired today so EOD summary is complete.
    new_signals = []
    for sym, df_ind in stock_ind.items():
        if sym in state["positions"] or sym in state["pending"]:
            continue
        sigs = generate_signals(df_ind, nifty_df=nifty_ind, filters=VOL_SURGE_FILTER)
        today_sigs = [s for s in sigs if pd.Timestamp(s["date"]).date() == today]
        if not today_sigs:
            continue
        sig = today_sigs[-1]
        sig["symbol"] = sym
        new_signals.append(sig)

    new_signals.sort(key=lambda s: (-s.get("score", 0), s["symbol"]))

    _save_state(state)

    # ── 5. Send Telegram summary ──────────────────────────────────────────────
    _send_eod_summary(tg, state, exits, entries, new_signals, today)

    return {"exits": exits, "entries": entries, "new_signals": new_signals,
            "capital": state["capital"], "positions": state["positions"]}


def run_305pm(kite, tg: IndianTelegram) -> dict:
    """
    3:05 PM live-LTP entry scan. Called during market hours — fetches real-time
    prices via kite.quote() so entries are at true 3:05 PM market price.

    Workflow:
      1. Load cached EOD data (yesterday's OHLCV — no API calls, fast)
      2. Compute NR7 high from yesterday's data for each stock
      3. Pre-filter: only keep stocks where yesterday_close >= nr7_h * 0.95
         (within 5% of breakout — avoids fetching LTP for 450 stocks)
      4. Batch fetch live LTP: kite.quote(["NSE:SYM1", ...]) — ONE API call
      5. Enter if ltp > nr7_high (breakout confirmed at 3:05 PM)
      6. Save state, send Telegram
    """
    state  = _load_state()
    today  = datetime.now(IST).date()
    universe = _universe(kite)

    log.info(f"NR7 3:05 PM scan — {today}")

    stock_data = load_eod_data(universe)
    if not stock_data:
        log.warning("3:05 PM scan: no cached EOD data found, skipping")
        return {"entries": []}

    # Pre-compute indicators (need NR7 high + SMA50 for each stock)
    def _with_sma50(df: pd.DataFrame) -> pd.DataFrame:
        df = compute_indicators(df)
        df["sma50"] = df["Close"].rolling(50, min_periods=25).mean()
        return df

    stock_ind = {sym: _with_sma50(df) for sym, df in stock_data.items()}

    # Build token lookup
    token_map = {s["symbol"]: s["token"] for s in universe}

    # Nifty trend filter
    nifty_raw = _fetch_daily_kite(kite, NIFTY50_TOKEN, days=220)
    nifty_ind = compute_indicators(nifty_raw) if not nifty_raw.empty else pd.DataFrame()

    # ── Scan signals using yesterday's data ─────────────────────────────────
    # generate_signals uses shift(1) so today's signal is based on yesterday's bar.
    # We use yesterday's close to check how close we are to nr7_high.
    candidates = []  # (sym, sig, nr7_high)
    for sym, df_ind in stock_ind.items():
        if sym in state["positions"] or sym in state["pending"]:
            continue
        sigs = generate_signals(df_ind, nifty_df=nifty_ind, filters=VOL_SURGE_FILTER)
        today_sigs = [s for s in sigs if pd.Timestamp(s["date"]).date() == today]
        if not today_sigs:
            continue
        sig      = today_sigs[-1]
        nr7_high = float(sig.get("nr7_high", 0))
        if nr7_high <= 0:
            continue

        # Pre-filter: only stocks within 5% below breakout (avoids quoting 450 stocks)
        yest_rows = df_ind[df_ind.index.date < today]
        if yest_rows.empty:
            continue
        yest_close = float(yest_rows.iloc[-1]["Close"])
        if yest_close < nr7_high * 0.95:
            continue

        sig["symbol"] = sym
        candidates.append((sym, sig, nr7_high))

    if not candidates:
        log.info("3:05 PM scan: no candidates near breakout level")
        tg._send(f"[NR7-PAPER] 3:05 PM — no breakout candidates today ({today})")
        return {"entries": []}

    log.info(f"3:05 PM candidates: {[c[0] for c in candidates]}")

    # ── Batch fetch live LTP — single API call ───────────────────────────────
    quote_keys = [f"NSE:{sym}" for sym, _, _ in candidates]
    try:
        quotes = kite.quote(quote_keys)
    except Exception as e:
        log.error(f"3:05 PM kite.quote() failed: {e}")
        tg._send(f"[NR7-PAPER] 3:05 PM LTP fetch failed: {e}")
        return {"entries": []}

    # ── Enter breakouts ──────────────────────────────────────────────────────
    candidates.sort(key=lambda x: (-x[1].get("score", 0), x[0]))
    entries  = []
    slots_free = MAX_POSITIONS - len(state["positions"])

    for sym, sig, nr7_high in candidates:
        if len(entries) >= slots_free:
            break
        if sym in state["positions"]:
            continue

        ltp = quotes.get(f"NSE:{sym}", {}).get("last_price")
        if ltp is None:
            log.warning(f"3:05 PM: no LTP for {sym}")
            continue

        ltp = float(ltp)
        if ltp <= nr7_high:
            log.info(f"3:05 PM: {sym} ltp={ltp:.2f} <= nr7_h={nr7_high:.2f} — no breakout")
            continue

        sl = float(sig["stop_loss"])
        if ltp <= sl or ltp <= 0:
            continue

        distance = ltp - sl
        if distance <= 0:
            continue

        qty = min(int(RISK_RS / distance), int(MAX_CAP_RS / ltp))
        if qty < 1:
            continue

        cap_needed = qty * ltp
        if cap_needed > state["capital"] - MIN_CASH:
            log.info(f"3:05 PM: skip {sym} — need Rs{cap_needed:.0f}, "
                     f"have Rs{state['capital'] - MIN_CASH:.0f}")
            continue

        # Get SMA50 for initial SL (use yesterday's SMA50 as starting trail)
        df_ind  = stock_ind[sym]
        yest_rows = df_ind[df_ind.index.date < today]
        sma50_sl = float(yest_rows.iloc[-1]["sma50"]) if not yest_rows.empty else sl
        initial_sl = max(sl, sma50_sl) if not pd.isna(sma50_sl) else sl
        # Never let trail push SL above ltp (can happen if SMA50 > ltp on thin stocks)
        initial_sl = min(initial_sl, ltp - distance * 0.5)

        state["capital"] -= cap_needed
        state["positions"][sym] = {
            "entry_date":       str(today),
            "entry_price":      round(ltp, 2),
            "sl":               round(initial_sl, 2),
            "qty":              qty,
            "capital_deployed": round(cap_needed, 2),
            "risk_rs":          RISK_RS,
            "score":            int(sig.get("score", 0)),
            "score_detail":     sig.get("score_detail", "base"),
            "nr7_high":         round(nr7_high, 2),
            "nr7_low":          round(float(sig.get("nr7_low", 0)), 2),
            "atr":              round(float(sig.get("atr", 0)), 2),
            "entry_type":       "live_3:05pm",
        }
        entries.append({
            "symbol":       sym,
            "entry_price":  round(ltp, 2),
            "sl":           round(initial_sl, 2),
            "qty":          qty,
            "risk_rs":      RISK_RS,
            "score":        int(sig.get("score", 0)),
            "entry_type":   "live_3:05pm",
        })
        log.info(f"3:05 PM ENTER {sym}: {qty}sh @ Rs{ltp:.2f}  "
                 f"nr7_h={nr7_high:.2f}  SL={initial_sl:.2f}  "
                 f"score={sig.get('score', 0)}")

    _save_state(state)

    # ── Telegram notification ─────────────────────────────────────────────────
    lines = [f"[NR7-PAPER] 3:05 PM Entry — {today}"]
    if entries:
        lines.append(f"\nBREAKOUT ENTRIES ({len(entries)}):")
        for e in entries:
            lines.append(
                f"  {e['symbol']}: {e['qty']}sh @ Rs{e['entry_price']:.2f}"
                f"  SL={e['sl']:.2f}  score={e['score']}"
            )
    else:
        no_break = [sym for sym, sig, nr7_h in candidates
                    if quotes.get(f"NSE:{sym}", {}).get("last_price", 0) <= nr7_h]
        lines.append(f"\nCandidates scanned: {len(candidates)} — none broke out yet")
        if no_break:
            lines.append(f"Below nr7_high: {', '.join(no_break[:8])}")
    lines.append(f"\nOpen positions: {len(state['positions'])}/{MAX_POSITIONS}")
    lines.append(f"Available capital: Rs{state['capital']:,.0f}")
    tg._send("\n".join(lines))

    return {"entries": entries, "candidates": len(candidates)}


def _send_eod_summary(tg, state, exits, entries, new_sigs, today):
    lines = [f"[NR7-PAPER] EOD Summary -- {today}"]

    if exits:
        lines.append(f"\nEXITS ({len(exits)}):")
        for t in exits:
            em = "WIN" if t["win"] else "LOSS"
            lines.append(
                f"  {em} {t['symbol']}: Rs{t['net_rs']:+,.0f}"
                f" ({t['r_multiple']:+.2f}R) | {t['hold_days']}d"
            )

    if entries:
        lines.append(f"\nENTERED TODAY ({len(entries)}):")
        for e in entries:
            lines.append(
                f"  {e['symbol']}: {e['qty']}sh @ Rs{e['entry_price']:.2f}"
                f"  SL={e['sl']:.2f}"
            )

    open_pos = state.get("positions", {})
    if open_pos:
        lines.append(f"\nOPEN POSITIONS ({len(open_pos)}/{MAX_POSITIONS}):")
        for sym, pos in open_pos.items():
            lines.append(
                f"  {sym}: entry={pos['entry_price']:.2f}"
                f"  SL={pos['sl']:.2f}"
                f"  qty={pos['qty']}"
            )

    # new_sigs = signals that fired today (entries were handled at 3:05 PM)
    if new_sigs:
        already_in = [s["symbol"] for s in new_sigs if s["symbol"] in state["positions"]]
        not_in     = [s["symbol"] for s in new_sigs if s["symbol"] not in state["positions"]]
        if already_in:
            lines.append(f"\nSIGNALS ENTERED AT 3:05 PM: {', '.join(already_in)}")
        if not_in:
            lines.append(f"\nSIGNALS (not entered — slots full or capital): {', '.join(not_in[:5])}")

    # P&L summary from all closed trades
    trades = state.get("trades", [])
    if trades:
        wins = [t for t in trades if t["win"]]
        total_net = sum(t["net_rs"] for t in trades)
        wr = len(wins) / len(trades) * 100
        lines.append(
            f"\nPORTFOLIO: Rs{state['capital']:,.0f} | "
            f"{len(trades)} trades | WR={wr:.0f}% | "
            f"Net=Rs{total_net:+,.0f}"
        )
    else:
        lines.append(f"\nCapital: Rs{state['capital']:,.0f}")

    tg._send("\n".join(lines))


# ── Status print (no API call) ────────────────────────────────────────────────
def print_status():
    state = _load_state()
    print(f"\nNR7 Paper Trader Status")
    print(f"Capital: Rs{state['capital']:,.0f}")
    print(f"\nOpen Positions ({len(state['positions'])}/{MAX_POSITIONS}):")
    for sym, pos in state["positions"].items():
        print(f"  {sym:<14}  entry={pos['entry_price']:.2f}"
              f"  SL={pos['sl']:.2f}  qty={pos['qty']}"
              f"  since={pos['entry_date']}")
    print(f"\nPending (legacy AMO, if any):")
    for sym, sig in state["pending"].items():
        print(f"  {sym:<14}  SL={sig['stop_loss']:.2f}"
              f"  score={sig['score']}  ({sig.get('score_detail','base')})")
    trades = state.get("trades", [])
    if trades:
        wins = [t for t in trades if t["win"]]
        net  = sum(t["net_rs"] for t in trades)
        print(f"\nClosed trades: {len(trades)}"
              f"  WR={len(wins)/len(trades)*100:.0f}%"
              f"  Net=Rs{net:+,.0f}")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    if "--status" in sys.argv:
        print_status()
        return

    env = _load_env()
    kite = _get_kite(env)
    tg   = IndianTelegram(
        bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        label="NR7-PAPER",
    )

    result = run_eod(kite, tg)
    print(f"\nDone. Exits={len(result['exits'])}  "
          f"Entries={len(result['entries'])}  "
          f"New signals={len(result['new_signals'])}  "
          f"Capital=Rs{result['capital']:,.0f}")


if __name__ == "__main__":
    main()
