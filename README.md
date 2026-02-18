# FundedNext ORB Trading Bot

Automated **Opening Range Breakout (ORB)** trading bot for FundedNext Stellar-2 prop firm accounts.  
Trades 4 index CFDs on MetaTrader 5 with full safety systems built in.

---

## Strategy

| Parameter | Value |
|---|---|
| Strategy | 15-min ORB with pullback retest entry |
| Indices | SPX500, NDX100, GER30, US30 |
| HTF Filter | 20/50 EMA crossover on H1 |
| TP | 2.5R single target |
| BE Trigger | 1.0R |
| Trail Buffer | 1.0R after BE |
| Trades/Day | Max 1 per symbol, 3 total |

### Backtest Results (1-Year, 4 Indices)

| Metric | Value |
|---|---|
| Total Trades | 253 |
| Win Rate | 55.3% |
| Profit Factor | 1.78 |
| Net Profit | $10,838 |
| Sharpe Ratio | 3.56 |
| Max Drawdown | 11.9% |

---

## Project Structure

```
backtest_engine/
├── .env                    # Credentials (never commit)
├── .env.example            # Template for .env setup
├── requirements.txt        # Python dependencies
├── README.md               # This file
│
├── run_live.py             # Live bot entry point
├── watchdog.py             # Process monitor + auto-restart
├── start_trading.bat       # Windows startup script
│
├── run_ict_clean.py        # Run ORB backtest
├── simulate_fundednext.py  # Run FundedNext simulation
├── backtest_live_setup.py  # Backtest framework
├── test_live_all.py        # 72-test validation suite
│
├── strategy/
│   └── strategy_ict_clean.py   # ORB strategy (494 lines, locked params)
│
├── live/
│   ├── config_fundednext.py    # Index configs, DD rules, position sizing
│   ├── mt5_connector.py        # MT5 ops, DrawdownProtection, LiveTrader
│   ├── sl_protection.py        # SL failsafe (verify/monitor/emergency)
│   ├── news_filter.py          # Forex Factory high-impact filter
│   ├── telegram_notifier.py    # Trade alerts to Telegram
│   ├── telegram_commands.py    # /start /stop /status /positions
│   ├── trade_journal.py        # Trade logging + daily reports
│   └── logs/                   # Runtime logs (gitignored)
│
├── backtest/
│   └── engine.py               # Backtest engine
│
├── data/
│   ├── loader.py               # CSV data loader
│   ├── validator.py            # Data quality checks
│   └── raw/                    # M1/H1 CSVs (gitignored)
│
├── indicators/
│   └── indicators.py           # Technical indicators
│
├── reports/
│   ├── charts.py               # Chart generation
│   ├── metrics.py              # Backtest metrics
│   └── trade_log.py            # Trade logging
│
├── config/
│   ├── config.yaml             # Backtest configuration
│   └── index_specifications.yaml
│
├── pinescript/                  # TradingView PineScript (reference)
│
└── archive/                    # Old scripts, forex data, removed indices
```

---

## Setup

### Prerequisites
- Python 3.10+
- MetaTrader 5 terminal (FundedNext)
- Windows (MT5 only runs on Windows)

### Installation

```bash
# Clone and setup
cd backtest_engine
pip install -r requirements.txt

# Configure credentials
copy .env.example .env
# Edit .env with your MT5 login, Telegram token, etc.
```

### Running

```bash
# Backtest
python run_ict_clean.py

# FundedNext simulation
python simulate_fundednext.py

# Live trading (via watchdog — recommended)
python watchdog.py

# Live trading (direct — no auto-restart)
python run_live.py

# Windows startup (use this for Task Scheduler)
start_trading.bat

# Run test suite
python test_live_all.py

# Check status
python run_live.py --status
```

---

## Safety Systems

| System | Protection |
|---|---|
| **Drawdown Protection** | Overall DD from initial balance, daily DD from day-start equity |
| **SL Failsafe** | Verify SL after entry (3 retries), monitor for SL disappearance, emergency close at 130% risk |
| **News Filter** | Block trades within 30min of Forex Factory high-impact events |
| **Watchdog** | Auto-restart on crash/freeze, rate-limited to 5/hour |
| **State Persistence** | Atomic save every 5 min, survives power loss |
| **Position Reconciliation** | Re-sync with MT5 after disconnect, re-apply missing SL |
| **DST-Safe Hours** | Market hours use local timezone conversion (no DST gaps) |
| **Telegram Commands** | `/start` `/stop` `/pause` `/resume` `/status` `/positions` `/balance` |

---

## Risk Parameters (.env)

| Setting | Default | Description |
|---|---|---|
| `RISK_PER_TRADE` | 1.5% | Risk per trade ($90 on $6,000) |
| `MAX_DRAWDOWN` | 8.0% | Overall DD limit (FundedNext = 10%) |
| `DAILY_DRAWDOWN` | 3.0% | Daily DD tracking threshold |
| `DAILY_DRAWDOWN_STOP` | 2.0% | Stop new trades at this daily DD |
| `NEWS_BUFFER_MINUTES` | 30 | Block trades near high-impact news |

---

## Adaptive Risk

Controlled by `ADAPTIVE_RISK` in `config_fundednext.py`:
- **Healthy account**: Uses `RISK_PER_TRADE` (1.5%)
- **Drawdown > 2%**: Reduces to 1.0%
- **Drawdown < 0.5%**: Returns to 1.5%
- Set `enabled: False` for funded phase (fixed risk)

---

## Locked Strategy Parameters

These are optimized via 7 backtest iterations and must not be changed:

```
disp_mult      = 1.7     # Displacement multiplier
body_min       = 0.65    # Minimum body %
close_strength = 0.30    # Close in top/bottom 30%
breakout_margin= 0.10    # Close must exceed ORB by 10% of range
orb_minutes    = 15      # Opening range: first 15 minutes
entry_hours    = 2       # Entry window: 2 hours after ORB
```
