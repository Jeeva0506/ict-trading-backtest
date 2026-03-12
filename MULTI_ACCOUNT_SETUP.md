# Multi-Account Setup Guide
## Running Two FundedNext Accounts in Parallel

### Architecture Overview

Each account runs as a **completely independent process tree** sharing one codebase:

```
start_trading.bat          → watchdog.py             → run_live.py             (STELLAR-2, .env)
start_trading_stellar15k.bat → watchdog.py --account stellar15k → run_live.py --account stellar15k (.env.stellar15k)
```

**Isolated per account:**
- MT5 terminal instance (separate installation)
- Log directory (`live/logs/stellar-2/` vs `live/logs/stellar-15k/`)
- Trade journal (`reports/trade_journal_stellar-2.xlsx` vs `reports/trade_journal_stellar-15k.xlsx`)
- State files (position_state.json, edge_guard.json, telegram_state.json)
- Lock files (bot.lock, watchdog.lock)
- Telegram bot (separate BotFather bot per account)
- BOT_MAGIC number (derived from MT5 login, unique per account)

**Shared (safe):**
- Strategy code (read-only)
- news_cache.json (read-only calendar data)

---

### Step 1: Install a Second MT5 Terminal (Portable Mode)

The MetaTrader 5 Python library (`MetaTrader5`) can only connect to **one terminal per process**. You need a separate MT5 installation for the second account.

#### Option A: Portable Install (Recommended)

1. Download the FundedNext MT5 installer from their portal
2. When the installer asks for a location, choose a **new folder**:
   ```
   C:\MT5_Stellar15K\
   ```
3. Complete the installation
4. Open the new terminal and log in with your **15K account credentials**
5. Enable **Auto Trading** (click the "Auto Trading" button in the toolbar)
6. Keep this terminal open whenever you want to trade the 15K account

#### Option B: Copy an Existing Installation

1. Copy your existing MT5 folder:
   ```
   xcopy "C:\Program Files\FundedNext MT5 Terminal" "C:\MT5_Stellar15K\" /E /I
   ```
2. Open `C:\MT5_Stellar15K\terminal64.exe`
3. File → Login to Trade Account → enter your 15K credentials
4. Enable Auto Trading

**Important:** Both MT5 terminals must be open simultaneously for both accounts to trade.

---

### Step 2: Create a Telegram Bot for the Second Account

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Name it something like `ORB Trading 15K`
4. Copy the **bot token** (e.g., `1234567890:AAFxxx...`)
5. Start a chat with your new bot, send `/start`
6. Get your chat ID:
   - Send a message to the bot
   - Visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
   - Find `"chat":{"id":XXXXXXXX}` — that's your chat ID

---

### Step 3: Fill in .env.stellar15k

Edit `backtest_engine/.env.stellar15k`:

```env
MT5_SERVER=FundedNext-Server3          # Usually same server
MT5_LOGIN=<your_15k_login>             # Your 15K account number
MT5_PASSWORD=<your_15k_password>       # Your 15K account password
MT5_PATH=C:\MT5_Stellar15K\terminal64.exe   # Path to the SECOND MT5 terminal

ACCOUNT_SIZE=15000
RISK_PER_TRADE=1.5
MAX_DRAWDOWN=8.0
DAILY_DRAWDOWN=3.0
DAILY_DRAWDOWN_STOP=2.0

TELEGRAM_BOT_TOKEN=<new_bot_token>     # From Step 2
TELEGRAM_CHAT_ID=<your_chat_id>        # From Step 2

ACCOUNT_LABEL=STELLAR-15K
EXCLUDE_INDICES=US2000
```

---

### Step 4: Start Both Accounts

**Account 1 (existing $6K):**
```
Double-click: start_trading.bat
```

**Account 2 (new $15K):**
```
Double-click: start_trading_stellar15k.bat
```

Or from command line:
```bash
# Terminal 1:
python watchdog.py

# Terminal 2:
python watchdog.py --account stellar15k
```

---

### Step 5: Verify Isolation

Check that each account has its own log directory:
```
live/logs/stellar-2/
    trading.log
    watchdog.log
    bot.lock
    position_state.json
    edge_guard.json
    telegram_state.json
    sl_protection.log

live/logs/stellar-15k/
    trading.log
    watchdog.log
    bot.lock
    position_state.json
    edge_guard.json
    telegram_state.json
    sl_protection.log
```

And separate journals:
```
reports/trade_journal_stellar-2.xlsx
reports/trade_journal_stellar-15k.xlsx
```

---

### Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| "MT5 init failed" | Second MT5 terminal not open | Open `C:\MT5_Stellar15K\terminal64.exe` |
| "Another watchdog is running" | Lock file collision | Check that lock files are in separate dirs |
| Same trades on both accounts | Both pointing to same .env | Verify `ACCOUNT_PROFILE` is set correctly |
| Telegram commands go to wrong bot | Same bot token | Create a separate bot via @BotFather |
| "Symbol not found" | MT5 terminal not logged in | Log in to the correct account in the second terminal |

---

### Adding More Accounts

To add a third account:
1. Create `.env.account3` with the new credentials
2. Install a third MT5 terminal at `C:\MT5_Account3\`
3. Create `start_trading_account3.bat` with `python watchdog.py --account account3`
4. Everything else is automatic — logs, journals, state files all namespace by `ACCOUNT_LABEL`
