# 1. Repository Overview & Structure

[← Back to README](../README.md)

## The big picture

```
ict-trading-backtest/
│
├── ── FOREX / INDICES BOT (MetaTrader 5) ─────────────────────────────
├── run_live.py              Entry point: starts the live trader (or --test / --status / --config)
├── watchdog.py              Supervisor: starts run_live.py, restarts it on crash/freeze,
│                            starts MT5 if needed, stops the bot outside trading windows
├── config_fundednext.py     Symbols, sessions, per-symbol ORB params, risk rules (reads .env)
├── config_guard.py          Fail-fast checks that the two config copies agree
├── backtest_live_setup.py   Portfolio backtester that replays the live strategy on M1 CSVs
├── requirements.txt         Python dependencies for the Forex bot
├── .env.example             Template for your MT5 / Telegram / risk settings
│
├── live/                    Live-trading runtime
│   ├── mt5_connector.py     MT5Connector (orders, data, positions) + LiveTrader (main bot)
│   ├── config_fundednext.py Second copy of the config, used by run_live.py (see note below)
│   ├── news_filter.py       EconomicCalendar: blocks trading around high-impact news
│   ├── edge_guard.py        Pauses trading if rolling performance degrades
│   ├── sl_protection.py     Verifies every position has a correct broker-side stop loss
│   ├── trade_journal.py     Excel trade journal + scheduled daily/weekly reports
│   ├── telegram_notifier.py Outgoing Telegram alerts
│   ├── telegram_commands.py Incoming Telegram commands (/status, /pause, /emergency …)
│   ├── atr_sl.py            Optional ATR-based stop-loss sizing (disabled by default)
│   ├── orb15_trader.py      Alternative strategy (needs research modules NOT in this repo)
│   ├── smc_trader.py        Alternative strategy (needs research modules NOT in this repo)
│   ├── logs/                Runtime logs, heartbeat, lock files (git-ignored)
│   └── reports/             trade_journal.xlsx (git-ignored)
│
├── strategy/
│   ├── strategy_ict_clean.py  StrategyICTClean: the ORB signal generator used live + in backtests
│   └── utils.py
├── indicators/indicators.py  Indicator helpers
├── config/                   YAML symbol specifications
├── data/                     Historical index M1 data (Dukascopy parquet chunks, HistData zips)
├── forexDATA/                3-year EURUSD / USDJPY / XAUUSD CSVs (M1, M15, H1)
│
├── ── INDIAN MARKET BOT (Zerodha) ────────────────────────────────────
└── indian_market/
    ├── start_paper_trading.py  Entry point: ORB Sweep + Straddle paper trader
    ├── START_PAPER_TRADER.bat  Double-click launcher
    ├── get_token.py / login.py Daily Zerodha access-token tools
    ├── .env.example            Template for Zerodha keys
    ├── orb_sweep/              ★ The production system (see doc 6)
    │   ├── config.py           TradingConfig dataclass: every parameter
    │   ├── strategy.py         ORBSweepStrategy: signal generation
    │   ├── trader.py           ORBSweepTrader: main loop, entries, exits, straddle
    │   ├── broker.py           ZerodhaBroker: data, LTP, option lookup, orders (paper-aware)
    │   ├── safety.py           API guard, outage detection, position reconciliation, kill switch
    │   ├── risk_manager.py     Daily/weekly loss limits, trade count
    │   ├── expiry_straddle.py  Expiry detection + 0-DTE Black-Scholes pricing
    │   ├── indicators.py       VWAP, ATR, volume SMA, swing highs/lows, 5m resample
    │   ├── excel_journal.py    Excel journal
    │   ├── trade_logger.py     SQLite trade DB
    │   ├── alerts.py           Telegram alerts
    │   ├── run.py              CLI: live / backtest / walkforward / metrics
    │   ├── backtest.py, walk_forward.py, optimize*.py, *_audit.py   Research tooling
    ├── strategies/             Research strategies (ORB variants, CPR, NR7, PO3, straddle, VRDOB)
    ├── backtest/               Backtest engines + Black-Scholes option pricer
    ├── costs/                  Zerodha brokerage, STT, GST, stamp duty model
    ├── data/                   Data download + Zerodha auth helpers
    ├── live/                   Older paper traders (NR7 positional, PO3) + auth/journal helpers
    └── run_*.py                Research backtest scripts
```

## How the two bots relate
They don't. The only thing they share is this repository. You can delete either half and the other still works.

## Things that will surprise you

1. **There are two `config_fundednext.py` files**, one at the root and one in `live/`. Which one Python loads depends on the entry point: `run_live.py` loads `live/`, while scripts that import `mt5_connector` directly load the root copy. **If you change a setting, change it in both files.** `config_guard.py` checks that they agree.
2. **The Indian bot looks for Zerodha keys in `<repo>/backtest_engine/.env.zerodha`**: a folder named `backtest_engine` inside the repo root. That path is a leftover from the original folder layout. Create the folder and it works.
3. **Only the default `orb` strategy works out of the box** for Forex. `--strategy orb15` and `--strategy smc` import research modules (`orb_v2/strategy_v3.py`, `strategy_smc.py`) that are not in this repo.
4. **Historical data is mostly not included** for the Indian bot. The download scripts are in `indian_market/data/`.

## Glossary

| Term | Meaning |
|---|---|
| **ORB** | Opening Range Breakout: the high/low of the first N minutes after the open, and the trade taken when price breaks out of that range |
| **R** | One unit of risk = distance from entry to stop loss. "+2.5R" = won 2.5× what was risked |
| **BE** | Break-even: stop moved to the entry price |
| **DD** | Drawdown: drop from the peak balance, daily or overall |
| **HTF** | Higher timeframe (H1 EMA 50/200 trend filter) |
| **Sweep** | Price pokes beyond a level, grabs liquidity, then closes back inside |
| **Straddle** | Selling an ATM call + ATM put together; profits if price stays still |
| **Lookahead bias** | A backtest using information that wasn't available yet at that moment. It makes results look better than reality |
| **PF / WR / MCL** | Profit factor / win rate / max consecutive losses |
