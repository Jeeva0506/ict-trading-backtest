# Trading Bots: Forex/Indices (MT5) + Indian Market (Zerodha)

This repo holds two **independent** automated trading systems that share no code at runtime:

| | **Forex / Indices bot** | **Indian market bot** |
|---|---|---|
| Market | Global index CFDs: NDX100, US30, SPX500, GER30, FRA40, SWI20, JP225, US2000 | Nifty 50 index options (NSE) |
| Broker / API | MetaTrader 5 (built for FundedNext prop accounts, works with any MT5 broker) | Zerodha Kite Connect |
| Strategy | Opening Range Breakout (ORB), 1 trade per symbol per day | ORB sweep (CALL breakout / PUT sweep reversal) + short straddle on expiry days |
| Mode | Live orders, with `--test` dry run | **Paper trading by default** |
| Folder | repo root | [`indian_market/`](indian_market/) |
| OS | Windows only (the MT5 Python API needs it) | Any OS with Python |

> ⚠️ **Read [docs/08-backtesting-and-pitfalls.md](docs/08-backtesting-and-pitfalls.md) before risking money.**
> These are personal research bots. Rigorous out-of-sample testing found that the default
> Forex ORB config has **no proven edge** after removing lookahead bias, and live fills have been
> worse than the backtests. Run demo / paper first and judge the bots on your own results.

---

## Documentation

| # | Doc | What's inside |
|---|---|---|
| 1 | [Repository overview & structure](docs/01-overview-and-structure.md) | What every folder and file is for |
| 2 | [Forex: setup & usage](docs/02-forex-setup-and-usage.md) | Install, `.env` reference, running, Telegram, multiple accounts |
| 3 | [Forex: technical architecture](docs/03-forex-architecture.md) | Main loop, signal pipeline, execution, risk, guards, watchdog |
| 4 | [Forex: build your own strategy](docs/04-forex-custom-strategy.md) | Step-by-step strategy plug-in with a full code template |
| 5 | [Indian: setup & usage](docs/05-indian-setup-and-usage.md) | Zerodha app, daily token, running the paper trader |
| 6 | [Indian: technical architecture](docs/06-indian-architecture.md) | Trader loop, strategy, broker, safety, risk, journals |
| 7 | [Indian: build your own strategy](docs/07-indian-custom-strategy.md) | Writing a new strategy, backtesting it, wiring it into the trader |
| 8 | [Backtesting, validation & pitfalls](docs/08-backtesting-and-pitfalls.md) | How to test honestly; lessons already paid for |
| 9 | [Troubleshooting & FAQ](docs/09-troubleshooting.md) | Common errors and fixes |

## Quick start

**Forex (Windows + MT5):**
```bat
git clone https://github.com/Jeeva0506/ict-trading-backtest.git
cd ict-trading-backtest
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          :: then fill in your MT5 login etc.
python run_live.py --test       :: dry run, no orders
python watchdog.py              :: production start
```

**Indian (Zerodha):**
```bat
pip install kiteconnect pyotp pandas numpy openpyxl requests pytz
mkdir backtest_engine
copy indian_market\.env.example backtest_engine\.env.zerodha   :: fill in API key/secret
cd indian_market
python get_token.py <request_token>     :: every morning
python start_paper_trading.py
```

Full details are in the docs above.

## Security
- Credentials live only in `.env*` files, which are git-ignored. **Never commit them.**
- Templates with blank values: [`.env.example`](.env.example) and [`indian_market/.env.example`](indian_market/.env.example).
