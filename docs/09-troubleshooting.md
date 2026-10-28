# 9. Troubleshooting & FAQ

[← Back to README](../README.md)

## Forex / MT5

| Symptom | Cause / fix |
|---|---|
| `Cannot connect to MT5` | The terminal isn't running or logged in, or `MT5_PATH`, `MT5_LOGIN` or `MT5_SERVER` is wrong. Open MT5 by hand once and log in. |
| Orders rejected, Telegram says AutoTrading is disabled | Turn on the **Algo Trading** button in the MT5 toolbar |
| `Missing .env.<profile>` / config error at start | The `--account X` you passed has no matching `.env.X` file |
| A symbol never trades | Wrong symbol name for your broker (see doc 2, step 2), it's listed in `EXCLUDE_INDICES`, or its market is closed |
| `CALENDAR UNAVAILABLE`, nothing trades | The news calendar fetch failed and the bot blocks trading as a fail-safe. Check your internet connection. It retries automatically. If it's stuck, restart the watchdog. |
| `Signal drift … > 1.0R` in the logs | The price moved too far from the signal before entry, so the trade was skipped on purpose |
| `stale_signal` | The signal was more than 15 bars old when it was seen (normal after a restart) |
| Watchdog says the bot stopped, or heartbeat is stale | Normal outside trading windows |
| `too many restarts` | The bot keeps crashing. Read `live/logs/<label>/` for the traceback. |
| A setting change has no effect | You edited only one of the two `config_fundednext.py` files |
| Lot size looks wrong | Check `point_value` for that symbol against `mt5.order_calc_profit` |
| `--strategy orb15/smc` fails with `ModuleNotFoundError: strategy_v3` | Those research modules aren't in this repo. Use the default `orb`, or your own strategy (doc 4). |
| Unicode or encoding errors in the console | Run `chcp 65001` first. The bot forces UTF-8 output itself. |

## Indian / Zerodha

| Symptom | Cause / fix |
|---|---|
| `FileNotFoundError … backtest_engine/.env.zerodha` | Create `<repo>/backtest_engine/.env.zerodha` (doc 5, step 2) |
| `TokenException` / `Incorrect api_key or access_token` | The daily token expired. Run `get_token.py` again. |
| `request_token` rejected | Each one works once, for a few minutes. Log in again and use the new one. |
| `Insufficient permission` / no historical data | The Kite Connect subscription isn't active, or the historical data add-on isn't enabled |
| `ORB computation failed -- insufficient bars` | You started after 9:30 on a holiday, or the data feed is behind. It retries. |
| `Regime filter FAILED` | Not an error. The day's range was too quiet, so no sweep trades today. |
| Straddle on the wrong day | The expiry moved because of a holiday, or NSE changed the weekday. See `is_nifty_expiry()` (doc 5, step 4) |
| Wrong quantity | Update `lot_size` in `orb_sweep/config.py` to the current NSE lot size |
| Telegram silent | `indian_market/.env.indian` is missing or has the wrong token or chat ID |

## FAQ

**Can I run it on Linux or Mac?** The Indian bot, yes. The Forex bot needs Windows, because of the MT5 Python package. Use a Windows VPS.

**Does it hold positions overnight?** No. Both bots close everything before their session ends.

**How much capital do I need?** Forex: whatever your broker's minimum lot allows at your risk %. Small accounts may not be able to size index CFDs at 0.5% risk. Indian: Rs 3 lakh is the default. Short straddles need a lot more margin than option buying.

**Is this financial advice?** No. It's research code. You're responsible for anything you run with real money.
