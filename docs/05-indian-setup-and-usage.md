# 5. Indian Market Bot: Setup & Usage

[← Back to README](../README.md)

The production system is **ORB Sweep + Straddle** in `indian_market/orb_sweep/`, trading **Nifty 50 weekly options**. It runs in **paper mode by default**: it reads real live prices from Zerodha but sends no orders.

## Requirements

| Need | Notes |
|---|---|
| Python 3.10+ | Windows, macOS or Linux |
| Zerodha trading account | |
| **Kite Connect subscription** | Paid API plan from Zerodha. Required **even for paper mode**, because the bot needs live 1-minute data. |
| Kite Connect app | Create one at <https://developers.kite.trade/apps>. Set its **Redirect URL** to `https://127.0.0.1/` |
| Telegram bot | Optional |

## 1. Install

```bat
git clone https://github.com/Jeeva0506/ict-trading-backtest.git
cd ict-trading-backtest
python -m venv .venv
.venv\Scripts\activate
pip install kiteconnect pyotp pandas numpy openpyxl requests pytz
```

## 2. Credentials

The code reads credentials from two files. Both are git-ignored.

### a) Zerodha keys → `<repo>/backtest_engine/.env.zerodha`
The location is required by the code. Note that it's a folder **named `backtest_engine` inside the repo root**:
```bat
mkdir backtest_engine
copy indian_market\.env.example backtest_engine\.env.zerodha
```
Fill in:

| Key | Where from |
|---|---|
| `ZERODHA_API_KEY` | Your Kite Connect app page |
| `ZERODHA_API_SECRET` | Your Kite Connect app page |
| `ZERODHA_ACCESS_TOKEN` | Leave blank. `get_token.py` fills it in every day |
| `ZERODHA_USER_ID` | Optional, for auto-login. Your client ID (e.g. `AB1234`) |
| `ZERODHA_PASSWORD` | Optional, for auto-login |
| `ZERODHA_TOTP_SECRET` | Optional, for auto-login. The base32 secret shown when you enable 2FA TOTP in Kite (the text under the QR code) |

### b) Telegram → `indian_market/.env.indian` (optional)
```
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

## 3. Daily access token (every trading morning)

Zerodha tokens **expire every day** (around 6 AM IST). Get a new one before 9:15:

1. Open `https://kite.zerodha.com/connect/login?api_key=<YOUR_API_KEY>&v=3` and log in.
2. The browser goes to `https://127.0.0.1/?request_token=XXXX&action=login&status=success`. The page fails to load, **which is expected**. Copy the `request_token` value.
3. Run:
   ```bat
   cd indian_market
   python get_token.py XXXX
   ```
   This writes `ZERODHA_ACCESS_TOKEN` into `.env.zerodha`.

`python login.py` does the same thing interactively. With `USER_ID`/`PASSWORD`/`TOTP_SECRET` set, `live/auto_auth.py` can log in automatically.

## 4. Configure the strategy (optional)

Every parameter is in `indian_market/orb_sweep/config.py` (`TradingConfig`). The most important ones:

| Parameter | Default | Meaning |
|---|---|---|
| `capital` | 300000 | Rs, used for sizing and DD limits |
| `risk_per_trade_pct` | 0.01 | 1% risk per sweep trade |
| `straddle_risk_pct` | 0.03 | 3% for expiry straddles |
| `daily_dd_limit_pct` / `weekly_dd_limit_pct` | 0.02 / 0.05 | Kill switch thresholds |
| `max_trades_per_day` | 3 | |
| `lot_size` | 75 | **Check the current NSE Nifty lot size**, as NSE changes it |
| `vix_halt_threshold` | 25 | No new entries above this India VIX |
| `no_new_entry_after` | 14:00 | |
| `force_exit` | 15:15 | Everything is closed by then |
| `paper_mode` | True | Keep it True |

Straddle settings are in `LiveStraddleConfig` in `orb_sweep/trader.py`.

> **Expiry day.** `is_nifty_expiry()` in `orb_sweep/expiry_straddle.py` treats **Tuesday** as Nifty weekly expiry from Sep 2025 (Thursday before that, so backtests on older data stay correct). Expiries moved by an NSE holiday (e.g. to Monday) are **not** detected. Stop the straddle on those weeks yourself. If NSE changes the expiry day again, update `NIFTY_TUESDAY_EXPIRY_START` and the weekday in that function.

## 5. Run

```bat
cd indian_market
python start_paper_trading.py
```
You can also double-click `START_PAPER_TRADER.bat`. Start it any time before 9:15 IST and leave it running until after 15:30.

What happens during the day (IST):

| Time | Action |
|---|---|
| < 9:15 | Waits |
| 9:15–9:30 | Builds the opening range (ORB) |
| 9:30 | Computes the ORB high/low, fetches India VIX, runs the **regime filter** (range % and 5-minute ATR must be large enough) |
| ~9:31 on expiry day | **Short ATM straddle**, if the ORB range ≤ 0.8% and VIX is between 11 and 25 |
| 9:30–14:00 | Scans every closed 1-minute bar for a **CALL breakout** or **PUT sweep** signal and buys the ATM option |
| During trade | Premium-based SL / TP / trailing stop, checked every 15 seconds |
| 15:05–15:15 | Hard exits: avoids the STT trap and the broker's auto square-off |
| After close | EOD summary to the journal and Telegram |

The bot skips weekends. It does not know about NSE holidays, so stop it yourself on those days.

### Other commands (from `indian_market/`)
```bat
python -m orb_sweep.run live            :: same as start_paper_trading.py
python -m orb_sweep.run live --live     :: ⚠️ REAL ORDERS
python -m orb_sweep.run backtest --start 2024-01-01
python -m orb_sweep.run walkforward
python -m orb_sweep.run metrics         :: stats from the SQLite trade DB
```

## 6. Output files

| Path | Content |
|---|---|
| `indian_market/logs/orb_sweep.log` | Full log |
| `indian_market/data/trades.db` | SQLite trade DB |
| `indian_market/data/state.json` | Crash-recovery state: open position, day flags |
| `indian_market/data/trade_journal.xlsx` | Human-readable trade journal |

## 7. Going live (only after months of paper trading)
1. Paper trade for **at least 2–3 months** and compare the paper results with the backtest.
2. Run with `python -m orb_sweep.run live --live`, which sets `paper_mode=False`.
3. Keep enough margin for the straddle (short options need margin; `margin_buffer_pct` keeps 40% spare).
