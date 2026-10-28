# 2. Forex / Indices Bot: Setup & Usage

[← Back to README](../README.md)

## Requirements

| Need | Notes |
|---|---|
| **Windows 10/11** | The `MetaTrader5` Python package is Windows-only. A Windows VPS is fine. |
| **Python 3.10+** | Developed on 3.12 |
| **MetaTrader 5 terminal** | From your broker. Log in once by hand, then turn on **Algo Trading** (toolbar button must be green). |
| **MT5 account** | Use a **demo** account first. Built for FundedNext; any MT5 broker with index CFDs works if the symbol names match. |
| Telegram bot | Optional, but strongly recommended for alerts and remote control |

## 1. Install

```bat
git clone https://github.com/Jeeva0506/ict-trading-backtest.git
cd ict-trading-backtest
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Check symbol names in your MT5

Open **Market Watch → Symbols** in MT5 and check what your broker calls each index. This bot expects:

`SPX500, GER30, FRA40, NDX100, US30, SWI20, JP225, US2000`

If your broker uses other names (for example `US100.cash`, `DE40`, `USTEC`), either:
- change the `'symbol'` field of that entry in **both** `config_fundednext.py` files, or
- turn that symbol off with `EXCLUDE_INDICES` (below).

Also check `point_value` for each symbol, which is $ per 1 point per 1 lot. Position sizing depends on it, so a wrong value means wrong risk. To check it in Python:
```python
import MetaTrader5 as mt5; mt5.initialize()
print(mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, "NDX100", 1.0, 20000, 20001))  # = $ per point per lot
```

## 3. Configure `.env`

```bat
copy .env.example .env
```

| Key | Meaning | Example |
|---|---|---|
| `MT5_SERVER` | Broker server name, as shown in the MT5 login dialog | `FundedNext-Server2` |
| `MT5_LOGIN` | Account number | `12345678` |
| `MT5_PASSWORD` | Trading password | |
| `MT5_PATH` | Full path to `terminal64.exe` (needed if you run several terminals) | `C:\Program Files\MetaTrader 5\terminal64.exe` |
| `ACCOUNT_SIZE` | Starting balance, used as the reference for drawdown | `15000` |
| `ACCOUNT_CURRENCY` | | `USD` |
| `RISK_PER_TRADE` | **Percent** of balance risked per trade | `0.5` (= 0.5%) |
| `MAX_DRAWDOWN` | **Percent**. Hitting it closes everything and stops the bot | `10.0` |
| `DAILY_DRAWDOWN` | **Percent**. Your broker's / prop firm's daily limit | `3.0` |
| `DAILY_DRAWDOWN_STOP` | **Percent**. The bot stops new trades for the day here, before the hard limit | `2.0` |
| `NEWS_BUFFER_MINUTES` | Minutes to avoid around high-impact news | `30` |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) | |
| `TELEGRAM_CHAT_ID` | Your chat ID (message [@userinfobot](https://t.me/userinfobot)) | |
| `DISCORD_WEBHOOK_URL` | Optional | |
| `HEARTBEAT_URL` | Optional uptime-monitor URL to ping | |
| `ACCOUNT_LABEL` | Name used in Telegram messages and log folders | `DEMO` |
| `EXCLUDE_INDICES` | Comma-separated symbols to skip | `US2000,FRA40` |
| `USE_LIMIT_ORDERS` | `true` = enter with limit orders | `false` |
| `USE_MARKETABLE_LIMIT` | `true` = marketable limit: fills at market but caps slippage | `true` |
| `MARKETABLE_CAP_R` | Max entry slippage as a fraction of R | `0.05` |

> **All risk values are percentages**: `1.0` means 1%, not 100%.

> **Prop-firm accounts:** `config_fundednext.py` also has an `ADAPTIVE_RISK` block that lowers risk during drawdown (tier 1 → tier 2). It's chosen by account profile name. Read it before going live.

### Multiple accounts
Create one file per account, named `.env.<profile>` (for example `.env.demo` or `.env.funded1`), and pass the profile name:
```bat
python watchdog.py --account demo
```
Each account needs its **own MT5 terminal installation** (set `MT5_PATH`) and runs in its own console window.

## 4. Test (no orders)

```bat
python run_live.py --config    :: prints the loaded configuration and active symbols
python run_live.py --test      :: connects to MT5, checks market hours, news, signals; places nothing
python run_live.py --status    :: current state
```

## 5. Run live

**Always run through the watchdog:**
```bat
python watchdog.py                  :: uses .env
python watchdog.py --account demo   :: uses .env.demo
```

The watchdog:
- starts `run_live.py` and restarts it if it crashes, with rate limiting
- kills and restarts it if the heartbeat stops (a freeze)
- starts the MT5 terminal if it isn't running
- stops the bot outside trading windows. **This is normal**, so don't panic when it goes quiet.
- stops a second watchdog from starting for the same account (lock file)

Running `python run_live.py` directly also works, but nothing restarts it if it crashes.

### Run at Windows startup
Create a `.bat` file next to the repo:
```bat
@echo off
cd /d "%~dp0ict-trading-backtest"
".venv\Scripts\python.exe" watchdog.py --account demo
pause
```
Then add it to **Task Scheduler** (trigger: "At log on").

## 6. Daily operation

Once a day:
1. Make sure the MT5 terminal is open and **Algo Trading is on**. The bot sends a Telegram alert if it's off.
2. Glance at the Telegram summary.

**When it trades.** Each symbol is traded only during its own cash session: US indices from the 9:30 ET open, GER30/FRA40/SWI20 from the European open, JP225 from the Tokyo open. Session times are set in the index's **local timezone**, so DST changes are handled automatically.

**Trade rules:**
- at most 1 trade per symbol per day
- at most 2 trades per day in total
- at most 2 positions open at once
- every position is force-closed 15 minutes before its session ends, so nothing is held overnight

## 7. Telegram commands

| Command | Action |
|---|---|
| `/status` | Bot state, balance, DD, open positions |
| `/positions` | Open positions with P&L |
| `/balance` | Balance / equity |
| `/trades` | Today's trades |
| `/risk` | Current risk % and DD tiers |
| `/performance` (`/perf`) | Rolling stats |
| `/pause` / `/resume` | Stop / restart taking new trades (open positions are still managed) |
| `/emergency` | **Close all positions immediately** |
| `/uptime` | Process uptime |
| `/help` | List commands |

## 8. Where things are written

| Path | Content |
|---|---|
| `live/logs/<account_label>/` | Bot logs, `bot.lock`, heartbeat |
| `live/reports/trade_journal.xlsx` | Every trade: entry, exit, R, slippage, balance after |
| `live/logs/news_cache.json` | Cached economic calendar |

> The journal's per-trade P&L column can be wrong when two trades close at the same time. Trust **account-level** balance totals.

## 9. Stop the bot
Press Ctrl+C in the watchdog window, or close the window. Open positions keep their broker-side SL/TP. Send `/emergency` first if you also want them closed.
