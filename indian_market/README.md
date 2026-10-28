# Indian Market Bot: Nifty ORB Sweep (Zerodha)

Paper-trading system for Nifty 50 options using Zerodha Kite Connect. **Paper mode is the default.**

The full documentation is in the repo's `docs/` folder:

- [Setup & usage](../docs/05-indian-setup-and-usage.md): Kite app, `.env.zerodha`, daily token, running, config
- [Technical architecture](../docs/06-indian-architecture.md): trader loop, strategy rules, exits, safety layer
- [Build your own strategy](../docs/07-indian-custom-strategy.md): `scan_bar` contract, plugging in, backtesting
- [Backtesting & pitfalls](../docs/08-backtesting-and-pitfalls.md)
- [Troubleshooting](../docs/09-troubleshooting.md)

Quick start:
```bat
pip install kiteconnect pyotp pandas numpy openpyxl requests pytz
mkdir ..\backtest_engine
copy .env.example ..\backtest_engine\.env.zerodha     :: fill in API key + secret
python get_token.py <request_token>                    :: every morning
python start_paper_trading.py
```
