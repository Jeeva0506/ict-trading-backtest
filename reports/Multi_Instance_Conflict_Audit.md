# Multi-Instance Conflict Audit Report
## Stellar-2 vs Stellar-15K — Dual Bot on Same Machine
**Date:** 2026-03-18  
**Auditor:** Automated codebase analysis  

---

## Executive Summary

The codebase has been **well-architected for multi-account isolation**. Most subsystems correctly namespace files by `ACCOUNT_LABEL`. However, there are **3 CRITICAL issues** and **4 WARNING-level issues** that need attention.

**Verdict:** The two bots CAN run simultaneously, but the **MT5 Python API is a single-connection-per-process library** — the architectural decision to use **separate MT5 terminal instances** (via `MT5_PATH`) is the correct solution and is properly implemented. The remaining issues are in position filtering and stale shared files.

---

## 1. MT5 Connection (`mt5_connector.py`, `sl_protection.py`)

### 1A. `mt5.initialize()` — Separate terminal binding  
**Rating: OK** ✅

Each bot passes `MT5_PATH` to `mt5.initialize()`:
- Stellar-2: uses default `.env` → `MT5_PATH` empty → binds to default terminal
- Stellar-15K: uses `.env.stellar15k` → `MT5_PATH=D:\trading\terminal64.exe`

**Code:** [mt5_connector.py](backtest_engine/live/mt5_connector.py#L222-L225)
```python
init_kwargs = {'timeout': 30000}
if MT5_PATH:
    init_kwargs['path'] = MT5_PATH
if not mt5.initialize(**init_kwargs):
```

Since the Python `MetaTrader5` library communicates via IPC (named pipe) with a **specific terminal process**, each `initialize(path=...)` call binds to its own terminal. Two Python processes calling `mt5.initialize()` with **different paths** connect to **different MT5 terminals** — no conflict.

### 1B. `mt5.login()` — Same or different account?
**Rating: OK** ✅

The `.env.stellar15k` file shows different credentials:
- Stellar-2: separate `.env` (different `MT5_LOGIN`)
- Stellar-15K: `MT5_LOGIN=13998357`, `MT5_SERVER=FundedNext-Server2`

Since they connect to **different MT5 terminal instances**, one `mt5.login()` does NOT kick the other out. Each terminal maintains its own session.

**However:** If `MT5_PATH` is **empty for Stellar-2**, it binds to the *default* terminal. If only one MT5 terminal is installed, **both bots would fight over the same terminal**. Ensure Stellar-2's `.env` also has an explicit `MT5_PATH` pointing to its own terminal installation.

### 1C. `sl_protection.py` reconnect logic
**Rating: OK** ✅

The `reconnect_mt5()` function at [sl_protection.py](backtest_engine/live/sl_protection.py#L962-L968) also correctly reads `MT5_PATH` from config and passes it to `mt5.initialize()`. Both bots reconnect to their own terminal.

---

## 2. File Lock Mechanisms

### 2A. Watchdog lock file  
**Rating: OK** ✅

[watchdog.py](backtest_engine/watchdog.py#L87):
```python
LOCK_FILE = log_dir / 'watchdog.lock'
```
Where `log_dir` = `live/logs/<account_label>/`. Each watchdog gets its own lock:
- Stellar-2: `live/logs/stellar-2/watchdog.lock`
- Stellar-15K: `live/logs/stellar-15k/watchdog.lock`

### 2B. Bot lock file  
**Rating: OK** ✅

[run_live.py](backtest_engine/run_live.py#L183):
```python
lock = Path(__file__).parent / 'live' / 'logs' / _label / 'bot.lock'
```
Namespaced by account. Each bot gets its own lock.

### 2C. Telegram poll lock  
**Rating: WARNING** ⚠️ (see Section 4 below)

---

## 3. Shared State Files

### 3A. Position state (`position_state.json`)
**Rating: OK** ✅

[mt5_connector.py](backtest_engine/live/mt5_connector.py#L599):
```python
STATE_FILE = _log_dir / "position_state.json"
```
Where `_log_dir` = `live/logs/<account_label>/`. Each bot has its own state file.

### 3B. Heartbeat files
**Rating: OK** ✅

[mt5_connector.py](backtest_engine/live/mt5_connector.py#L600):
```python
HEARTBEAT_FILE = _log_dir / "heartbeat.txt"
```
Namespaced. Watchdog reads from the same `log_dir`:
[watchdog.py](backtest_engine/watchdog.py#L88):
```python
HEARTBEAT_FILE = log_dir / 'heartbeat.txt'
```

### 3C. Trade journal files
**Rating: OK** ✅

[mt5_connector.py](backtest_engine/live/mt5_connector.py#L615):
```python
journal_path = str(_log_dir.parent.parent / "reports" / f"trade_journal_{ACCOUNT_LABEL.lower()...}.xlsx")
```
Results in:
- `reports/trade_journal_stellar-2.xlsx`
- `reports/trade_journal_stellar-15k.xlsx`

Both confirmed to exist on disk.

### 3D. EdgeGuard data file
**Rating: OK** ✅

[edge_guard.py](backtest_engine/live/edge_guard.py#L52) + [mt5_connector.py](backtest_engine/live/mt5_connector.py#L617):
```python
self.edge_guard = EdgeGuard(data_dir=_log_dir, ...)
self.data_file = data_dir / "edge_guard.json"
```
Each bot writes to `live/logs/<account_label>/edge_guard.json`.

### 3E. News cache
**Rating: OK** ✅

[mt5_connector.py](backtest_engine/live/mt5_connector.py#L608):
```python
self.news_filter = EconomicCalendar(cache_dir=_log_dir, ...)
```
Cache written to `live/logs/<account_label>/news_cache.json`. Correctly namespaced (FIX B8 comment in code).

### 3F. SL Protection log
**Rating: OK** ✅

[sl_protection.py](backtest_engine/live/sl_protection.py#L210-L211): Log directory passed via `start_monitoring(log_dir=_log_dir)` from [mt5_connector.py](backtest_engine/live/mt5_connector.py#L1085).

### 3G. Telegram command state
**Rating: OK** ✅

[telegram_commands.py](backtest_engine/live/telegram_commands.py#L92):
```python
self.state_file = state_dir / 'telegram_state.json'
```
`state_dir` is `_log_dir` passed from [mt5_connector.py](backtest_engine/live/mt5_connector.py#L649). Each bot has its own `telegram_state.json`.

### 3H. STALE FILES IN SHARED `live/logs/` ROOT
**Rating: WARNING** ⚠️

The directory listing of `live/logs/` shows **orphaned files from before multi-account isolation was implemented**:
```
live/logs/
  bot_stderr.log          ← STALE (not namespaced)
  bot_stdout.log          ← STALE
  edge_guard.json         ← STALE
  heartbeat.txt           ← STALE
  news_cache.json         ← STALE
  position_state.json     ← STALE
  sl_protection.log       ← STALE
  telegram_poll.lock      ← SHARED (intentional)
  telegram_state.json     ← STALE
  trading.log             ← STALE
  watchdog.log            ← STALE
  stellar-2/              ← Active (namespaced)
  stellar-15k/            ← Active (namespaced)
```

These stale root-level files are NOT currently used by either bot (all paths now point to `logs/<account_label>/`), but they could cause confusion. They should be cleaned up.

**Severity:** LOW — No functional impact, just cleanup needed.

---

## 4. Telegram Poll Lock

**Rating: WARNING** ⚠️

[telegram_commands.py](backtest_engine/live/telegram_commands.py#L720-L748):
```python
# Lock file lives in a SHARED location (not per-account).
self._poll_lock_file = Path(__file__).parent / 'logs' / 'telegram_poll.lock'
```

**Design:** Both bots share the same Telegram bot token. Telegram's `getUpdates` API can only have ONE consumer per token. The code correctly uses a **shared** lock file so only one bot polls. The second bot becomes "notification-only" (can send messages, but won't process `/status`, `/stop` commands).

**Issue #1:** The lock file lives at `live/logs/telegram_poll.lock` — **correct** for a shared resource, BUT:
- When the polling bot dies/restarts, the **stale lock may prevent the other bot from picking up polling**.
- The PID check uses `psutil.pid_exists()` which is reasonable, but there's a **TOCTOU race**: between checking the PID and writing the new lock, both bots could see the stale PID and both acquire the lock.

**Issue #2:** Commands like `/stop`, `/pause`, `/positions` are dispatched by whichever bot owns the poll lock. If Stellar-2 owns the lock, sending `/status` shows Stellar-2's status even when you intended Stellar-15K. There's no account-routing mechanism for commands.

**Recommendation:** 
1. Use separate Telegram bots (different tokens) for each account, OR
2. Add `/status stellar15k` command routing, OR
3. Accept that commands always go to whichever bot acquired the lock first.

---

## 5. MT5 Position Management — CRITICAL ISSUES

### 5A. Magic number isolation
**Rating: OK** ✅

[mt5_connector.py](backtest_engine/live/mt5_connector.py#L100):
```python
BOT_MAGIC = 20260000 + (MT5_LOGIN % 10000)
```
Different MT5_LOGIN values → different magic numbers. Orders are tagged correctly.

[sl_protection.py](backtest_engine/live/sl_protection.py#L64-L68):
```python
def _get_bot_magic() -> int:
    from config_fundednext import MT5_LOGIN
    return 20260000 + (MT5_LOGIN % 10000)
```
Aligned with `mt5_connector.py`.

### 5B. `get_open_positions()` — NO MAGIC NUMBER FILTER
**Rating: CRITICAL** 🔴

[mt5_connector.py](backtest_engine/live/mt5_connector.py#L471-L493):
```python
def get_open_positions(self, symbol: str = None) -> List[dict]:
    if symbol:
        positions = mt5.positions_get(symbol=symbol)
    else:
        positions = mt5.positions_get()
    if positions is None:
        return []
    return [
        {
            "ticket": p.ticket,
            ...
            "magic": p.magic,
        }
        for p in positions
    ]
```

**This returns ALL positions regardless of magic number.** Since each bot connects to its OWN MT5 terminal (different account), positions from the other account are NOT visible. This is saved by the MT5 architecture — different accounts, different terminals, different positions.

**BUT** if someone ever configures both bots to use the **same MT5 terminal** (e.g., by leaving `MT5_PATH` empty for both), this would cause **catastrophic cross-account interference**.

### 5C. `close_all_positions()` in sl_protection.py — NO MAGIC FILTER
**Rating: CRITICAL** 🔴

[sl_protection.py](backtest_engine/live/sl_protection.py#L681):
```python
def close_all_positions(reason: str = "Unknown") -> int:
    positions = mt5.positions_get()  # ALL positions, no magic filter
    for position in positions:
        emergency_close_position(position.ticket, reason)
```

This closes **ALL positions on the connected terminal** regardless of magic number. If both bots somehow connect to the same terminal, one bot's emergency close would close the other bot's positions too.

**Same terminal risk also applies to:**
- `monitor_open_positions()` at [sl_protection.py](backtest_engine/live/sl_protection.py#L742): monitors ALL positions
- `_emergency_close_all()` at [mt5_connector.py](backtest_engine/live/mt5_connector.py#L2541): closes ALL positions from `get_open_positions()` (no magic filter)
- Max open position check at [mt5_connector.py](backtest_engine/live/mt5_connector.py#L1491): counts ALL positions

**Current safety:** Since the bots connect to different terminals (different accounts), each terminal only has its own positions. The lack of magic filtering is **not a live issue today** but is a **latent defect**.

### 5D. `_load_state()` position reconciliation — MAGIC FILTER EXISTS
**Rating: OK** ✅

[mt5_connector.py](backtest_engine/live/mt5_connector.py#L775):
```python
if p["ticket"] not in self.tracked_positions and p.get("magic") == BOT_MAGIC:
```

The state loader correctly filters by magic when adopting orphaned positions. Good.

---

## 6. Global Mutable State

### 6A. sl_protection module globals  
**Rating: OK** ✅ (separate processes)

[sl_protection.py](backtest_engine/live/sl_protection.py#L100-L113) has several module-level globals:
```python
trading_paused_until: Optional[datetime] = None
original_sl_tracker: Dict[int, float] = {}
failed_close_tracker: Dict[int, Dict] = {}
monitoring_active: bool = False
TELEGRAM_BOT_TOKEN: str = ""
```

These would be **dangerous if both bots ran in the same process** (e.g., imported as threads). However, since each bot is a **separate Python process** (launched by its own watchdog via `subprocess.Popen`), each process gets its own copy of these globals. **No conflict.**

### 6B. BOT_MAGIC as module-level constant
**Rating: OK** ✅

`BOT_MAGIC = 20260000 + (MT5_LOGIN % 10000)` is computed at import time. Since each process loads its own `.env`, each gets the correct magic number.

### 6C. `PROFITABLE_INDICES` exclusion filtering
**Rating: OK** ✅

[config_fundednext.py](backtest_engine/live/config_fundednext.py#L430-L436): `EXCLUDE_INDICES` removes symbols from the dict at import time. Stellar-15K excludes `US2000`, so it won't trade on that symbol. Each process does this independently at import.

---

## 7. Watchdog PID Tracking

### 7A. Process isolation
**Rating: OK** ✅

Each watchdog:
1. Acquires its own lock file (`live/logs/<label>/watchdog.lock`)
2. Starts its own subprocess (`run_live.py --account <profile>`)
3. Tracks only its own child process PID (`self.process.pid`)
4. Kills only its own child on freeze detection

[watchdog.py](backtest_engine/watchdog.py#L253-L285): `kill_bot()` uses `psutil.Process(pid)` where `pid` is the PID of the subprocess it spawned. Cannot accidentally kill the other bot.

### 7B. MT5 terminal process check
**Rating: WARNING** ⚠️

[watchdog.py](backtest_engine/watchdog.py#L371-L382):
```python
def check_mt5_process(self) -> bool:
    target_path = self._mt5_path_cache
    for proc in psutil.process_iter(['name', 'exe']):
        if 'terminal64' in name.lower():
            if target_path:
                # Multi-instance: match exact executable path
                exe = proc.info.get('exe', '')
                if Path(exe).resolve() == Path(str(target_path)).resolve():
                    return True
            else:
                # Single instance: any terminal64 counts
                return True
```

**Issue:** If Stellar-2's `MT5_PATH` is empty, `self._mt5_path_cache` is `None`, and the check falls through to "any terminal64 counts". This means Stellar-2's watchdog could mistakenly believe MT5 is running because Stellar-15K's terminal is up, even if Stellar-2's own terminal has crashed.

**Same for `start_mt5()`** at [watchdog.py](backtest_engine/watchdog.py#L393-L401): if `MT5_PATH` is empty, it searches common paths and may launch a **second instance of the wrong terminal**.

**Recommendation:** Ensure Stellar-2's `.env` has an explicit `MT5_PATH` pointing to its own terminal executable.

---

## Summary Table

| # | Area | File(s) | Severity | Status |
|---|------|---------|----------|--------|
| 1A | MT5 init with path | mt5_connector.py:222 | OK ✅ | Separate terminals |
| 1B | MT5 login credentials | .env / .env.stellar15k | OK ✅ | Different accounts |
| 2A | Watchdog lock | watchdog.py:87 | OK ✅ | Namespaced |
| 2B | Bot lock | run_live.py:183 | OK ✅ | Namespaced |
| 3A | Position state JSON | mt5_connector.py:599 | OK ✅ | Namespaced |
| 3B | Heartbeat files | mt5_connector.py:600 | OK ✅ | Namespaced |
| 3C | Trade journal Excel | mt5_connector.py:615 | OK ✅ | Namespaced |
| 3D | EdgeGuard JSON | edge_guard.py:52 | OK ✅ | Namespaced |
| 3E | News cache JSON | news_filter.py:57 | OK ✅ | Namespaced |
| 3F | SL Protection log | sl_protection.py:210 | OK ✅ | Namespaced |
| 3G | Telegram state JSON | telegram_commands.py:92 | OK ✅ | Namespaced |
| 3H | Stale root-level files | live/logs/*.* | LOW ⚪ | Cleanup needed |
| 4 | Telegram poll lock | telegram_commands.py:723 | WARNING ⚠️ | Shared by design; commands not routable |
| 5B | get_open_positions no magic filter | mt5_connector.py:471 | CRITICAL 🔴 | Latent defect — safe only because different terminals |
| 5C | close_all_positions no magic filter | sl_protection.py:681 | CRITICAL 🔴 | Would close other bot's positions if same terminal |
| 5D | Max position count no magic filter | mt5_connector.py:1491 | CRITICAL 🔴 | Would count other bot's positions if same terminal |
| 6 | Module-level globals | sl_protection.py:100 | OK ✅ | Separate processes |
| 7A | Watchdog PID tracking | watchdog.py:253 | OK ✅ | Tracks own child only |
| 7B | MT5 process check with empty path | watchdog.py:371 | WARNING ⚠️ | Falls back to "any terminal64" |

---

## Recommended Actions

### Must-Fix (CRITICAL)

1. **Ensure Stellar-2's `.env` has an explicit `MT5_PATH`** — Without this, the default terminal binding is fragile. Both bots MUST point to different `terminal64.exe` installations.

2. **Add magic number filter to `get_open_positions()`** — Even though the current setup uses separate terminals, a defensive filter prevents catastrophe if configuration changes:
   ```python
   def get_open_positions(self, symbol=None, filter_magic=True):
       ...
       results = [...]
       if filter_magic:
           results = [p for p in results if p["magic"] == BOT_MAGIC]
       return results
   ```

3. **Add magic number filter to `close_all_positions()` in sl_protection.py** — Only close positions with the bot's own magic number:
   ```python
   positions = mt5.positions_get()
   magic = _get_bot_magic()
   positions = [p for p in positions if p.magic == magic]
   ```

### Should-Fix (WARNING)

4. **Clean up stale files** in `live/logs/` root directory (the 11 orphaned files from pre-multi-account era).

5. **Add command routing to Telegram** — Either use separate bot tokens per account, or add account-targeting syntax (`/status stellar15k`).

6. **Harden watchdog MT5 process check** — When `MT5_PATH` is empty, the watchdog should log a warning rather than silently accepting any `terminal64` process.
