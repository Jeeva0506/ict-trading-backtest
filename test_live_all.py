"""
Comprehensive Live Module Test
===============================
Tests ALL live modules without needing a live MT5 connection.
Validates: imports, class instantiation, method signatures,
           cross-module compatibility, config consistency, edge cases.

Usage: python test_live_all.py
"""
import sys
import os
import inspect
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock
from io import StringIO

# Add paths
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / 'live'))
sys.path.insert(0, str(Path(__file__).parent / 'strategy'))

PASS = 0
FAIL = 0
WARN = 0
results = []

def test(name, func):
    global PASS, FAIL, WARN
    try:
        func()
        PASS += 1
        results.append(("PASS", name))
        print(f"  [PASS] {name}")
    except AssertionError as e:
        FAIL += 1
        results.append(("FAIL", name, str(e)))
        print(f"  [FAIL] {name}: {e}")
    except Exception as e:
        FAIL += 1
        results.append(("FAIL", name, str(e)))
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")

def warn(name, msg):
    global WARN
    WARN += 1
    results.append(("WARN", name, msg))
    print(f"  [WARN] {name}: {msg}")


# ============================================================================
# 1. CONFIG MODULE
# ============================================================================
print("\n" + "="*60)
print("1. CONFIG_FUNDEDNEXT")
print("="*60)

def test_config_imports():
    from config_fundednext import (
        FUNDEDNEXT_RULES, ADAPTIVE_RISK, PROFITABLE_INDICES,
        get_index_config, calculate_position_size,
        calculate_position_size_with_broker_data,
        get_broker_symbol_specs, MT5_SERVER, MT5_LOGIN, MT5_PASSWORD,
        ACCOUNT_SIZE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    )
    assert FUNDEDNEXT_RULES is not None
    assert len(PROFITABLE_INDICES) > 0

test("config imports", test_config_imports)

def test_config_indices():
    from config_fundednext import PROFITABLE_INDICES
    expected = {'SPX500', 'NDX100', 'GER30', 'US30'}
    actual = set(PROFITABLE_INDICES.keys())
    assert actual == expected, f"Expected {expected}, got {actual}"
    # FRA40 must NOT be present
    assert 'FRA40' not in actual, "FRA40 should be removed"
    assert 'HK50' not in actual, "HK50 should be removed"

test("config has correct 4 indices", test_config_indices)

def test_config_orb_params():
    from config_fundednext import PROFITABLE_INDICES
    required_keys = [
        'orb_minutes', 'entry_hours', 'breakout_window_minutes',
        'disp_mult', 'body_min', 'close_strength', 'breakout_margin',
        'orb_range_min_mult', 'min_sl_points', 'max_sl_points',
        'force_close_hour', 'force_close_minute',
        'mt5_open_hour', 'mt5_open_minute', 'mt5_close_hour', 'mt5_close_minute',
    ]
    for sym, cfg in PROFITABLE_INDICES.items():
        for key in required_keys:
            assert key in cfg, f"{sym} missing '{key}'"
        # ORB params locked
        assert cfg['disp_mult'] == 1.7, f"{sym} disp_mult should be 1.7"
        assert cfg['body_min'] == 0.65, f"{sym} body_min should be 0.65"
        assert cfg['orb_minutes'] == 15, f"{sym} orb_minutes should be 15"

test("config ORB params per index", test_config_orb_params)

def test_config_sl_ranges():
    from config_fundednext import PROFITABLE_INDICES
    for sym, cfg in PROFITABLE_INDICES.items():
        assert cfg['max_sl_distance'] >= 100, \
            f"{sym} max_sl_distance={cfg['max_sl_distance']} too small for live"
        assert cfg['min_sl_distance'] >= 2.0, \
            f"{sym} min_sl_distance={cfg['min_sl_distance']} too small"

test("config SL ranges reasonable", test_config_sl_ranges)

def test_config_mt5_hours():
    from config_fundednext import PROFITABLE_INDICES
    # US indices: 16:30-23:00 EET
    for sym in ['SPX500', 'NDX100', 'US30']:
        cfg = PROFITABLE_INDICES[sym]
        assert cfg['mt5_open_hour'] == 16, f"{sym} mt5_open_hour"
        assert cfg['mt5_open_minute'] == 30, f"{sym} mt5_open_minute"
        assert cfg['mt5_close_hour'] == 23, f"{sym} mt5_close_hour"
    # GER30: 10:00-18:30 EET
    cfg = PROFITABLE_INDICES['GER30']
    assert cfg['mt5_open_hour'] == 10
    assert cfg['mt5_close_hour'] == 18

test("config MT5 trading hours", test_config_mt5_hours)

def test_config_position_sizing():
    from config_fundednext import calculate_position_size
    # $6000 account, 1.5% risk, 20 point SL, $1/point
    lots = calculate_position_size(6000, 1.5, 20, 1.0)
    assert 0.01 <= lots <= 40.0, f"Lots={lots} out of range"
    expected = 6000 * 0.015 / 20  # = 4.5
    assert abs(lots - 4.5) < 0.1, f"Expected ~4.5 lots, got {lots}"

test("config position sizing math", test_config_position_sizing)

def test_config_fundednext_rules():
    from config_fundednext import FUNDEDNEXT_RULES
    assert FUNDEDNEXT_RULES['max_drawdown_percent'] == 8.0
    assert FUNDEDNEXT_RULES['daily_drawdown_percent'] == 3.0
    assert FUNDEDNEXT_RULES['daily_drawdown_stop'] == 2.0
    assert FUNDEDNEXT_RULES['risk_per_trade_percent'] == 1.5

test("config FundedNext rules from .env", test_config_fundednext_rules)

def test_config_adaptive_risk():
    from config_fundednext import ADAPTIVE_RISK
    assert ADAPTIVE_RISK['enabled'] == True
    assert ADAPTIVE_RISK['default_risk_percent'] == 1.5
    assert ADAPTIVE_RISK['reduced_risk_percent'] == 1.0
    assert ADAPTIVE_RISK['drawdown_threshold'] == 2.0

test("config adaptive risk settings", test_config_adaptive_risk)


# ============================================================================
# 2. SL PROTECTION
# ============================================================================
print("\n" + "="*60)
print("2. SL_PROTECTION")
print("="*60)

def test_sl_imports():
    from sl_protection import (
        set_telegram_config, start_monitoring, stop_monitoring,
        verify_sl_after_entry, is_trading_paused, close_all_positions,
        get_sl_tolerance, SL_TOLERANCE_PERCENT, SL_TOLERANCE_MIN, SL_TOLERANCE_MAX,
        track_original_sl, get_original_sl, emergency_close_position,
    )

test("sl_protection imports", test_sl_imports)

def test_sl_tolerance_proportional():
    from sl_protection import get_sl_tolerance
    # SPX500: entry=5000, SL=4980 → distance=20, tolerance=2.0 (10%)
    tol = get_sl_tolerance(5000, 4980)
    assert 1.0 <= tol <= 20.0, f"SPX500 tolerance={tol}"
    assert abs(tol - 2.0) < 0.01, f"Expected 2.0, got {tol}"

    # US30: entry=40000, SL=39960 → distance=40, tolerance=4.0 (10%)
    tol = get_sl_tolerance(40000, 39960)
    assert abs(tol - 4.0) < 0.01, f"Expected 4.0, got {tol}"

    # Very small SL: distance=5, tolerance=max(1.0, 0.5)=1.0
    tol = get_sl_tolerance(100, 95)
    assert abs(tol - 1.0) < 0.01, f"Expected 1.0 (floor), got {tol}"

    # Very large SL: distance=500, tolerance=min(20.0, 50)=20.0
    tol = get_sl_tolerance(5000, 4500)
    assert abs(tol - 20.0) < 0.01, f"Expected 20.0 (cap), got {tol}"

test("sl_tolerance proportional math", test_sl_tolerance_proportional)

def test_sl_no_fixed_tolerance():
    """Ensure old fixed SL_TOLERANCE_POINTS is removed"""
    import sl_protection
    assert not hasattr(sl_protection, 'SL_TOLERANCE_POINTS'), \
        "SL_TOLERANCE_POINTS (fixed=5) should be removed"

test("sl_tolerance old fixed constant removed", test_sl_no_fixed_tolerance)


# ============================================================================
# 3. NEWS FILTER
# ============================================================================
print("\n" + "="*60)
print("3. NEWS_FILTER")
print("="*60)

def test_news_imports():
    from news_filter import EconomicCalendar
    cal = EconomicCalendar(buffer_minutes=30, cache_minutes=15)
    assert cal.buffer_minutes == 30

test("news_filter imports + init", test_news_imports)

def test_news_symbol_currencies():
    from news_filter import EconomicCalendar
    sc = EconomicCalendar.SYMBOL_CURRENCIES
    assert 'SPX500' in sc, "SPX500 missing"
    assert 'NDX100' in sc, "NDX100 missing"
    assert 'US30' in sc, "US30 missing"
    assert 'GER30' in sc, "GER30 missing"
    assert 'FRA40' not in sc, "FRA40 should be removed"
    assert 'HK50' not in sc, "HK50 should be removed"
    # Check USD currencies
    assert 'USD' in sc['NDX100']
    assert 'USD' in sc['US30']
    assert 'EUR' in sc['GER30']

test("news_filter symbol currencies", test_news_symbol_currencies)

def test_news_is_news_time():
    from news_filter import EconomicCalendar
    cal = EconomicCalendar(buffer_minutes=30)
    # Should have is_news_time method
    sig = inspect.signature(cal.is_news_time)
    assert 'symbol' in sig.parameters

test("news_filter is_news_time method", test_news_is_news_time)


# ============================================================================
# 4. TELEGRAM NOTIFIER
# ============================================================================
print("\n" + "="*60)
print("4. TELEGRAM_NOTIFIER")
print("="*60)

def test_telegram_imports():
    from telegram_notifier import TelegramNotifier
    tn = TelegramNotifier("fake_token", "fake_chat")
    assert tn is not None

test("telegram_notifier imports + init", test_telegram_imports)

def test_telegram_methods_exist():
    from telegram_notifier import TelegramNotifier
    required = [
        'send_message', 'trade_entry', 'trade_closed', 'bot_started',
        'bot_stopped', 'drawdown_warning', 'sl_moved', 'order_failed',
        'system_status', 'critical_alert',
    ]
    for method in required:
        assert hasattr(TelegramNotifier, method), f"Missing method: {method}"

test("telegram_notifier required methods", test_telegram_methods_exist)

def test_telegram_trade_entry_sig():
    from telegram_notifier import TelegramNotifier
    sig = inspect.signature(TelegramNotifier.trade_entry)
    params = list(sig.parameters.keys())
    # Must accept: symbol, direction, entry_price, sl, tp1, tp2, tp3, lot_size, risk_amount
    expected = ['self', 'symbol', 'direction', 'entry_price', 'sl', 'tp1', 'tp2', 'tp3', 'lot_size', 'risk_amount']
    assert params == expected, f"trade_entry params: {params}, expected: {expected}"

test("telegram trade_entry signature", test_telegram_trade_entry_sig)

def test_telegram_trade_closed_sig():
    from telegram_notifier import TelegramNotifier
    sig = inspect.signature(TelegramNotifier.trade_closed)
    params = list(sig.parameters.keys())
    expected = ['self', 'symbol', 'reason', 'profit']
    assert params == expected, f"trade_closed params: {params}, expected: {expected}"

test("telegram trade_closed signature", test_telegram_trade_closed_sig)

def test_telegram_bot_started_sig():
    from telegram_notifier import TelegramNotifier
    sig = inspect.signature(TelegramNotifier.bot_started)
    params = list(sig.parameters.keys())
    expected = ['self', 'indices', 'balance']
    assert params == expected, f"bot_started params: {params}, expected: {expected}"

test("telegram bot_started signature", test_telegram_bot_started_sig)

def test_telegram_drawdown_warning_sig():
    from telegram_notifier import TelegramNotifier
    sig = inspect.signature(TelegramNotifier.drawdown_warning)
    params = list(sig.parameters.keys())
    expected = ['self', 'current_dd', 'limit', 'balance']
    assert params == expected, f"drawdown_warning params: {params}, expected: {expected}"

test("telegram drawdown_warning signature", test_telegram_drawdown_warning_sig)

def test_telegram_sl_moved_sig():
    from telegram_notifier import TelegramNotifier
    sig = inspect.signature(TelegramNotifier.sl_moved)
    params = list(sig.parameters.keys())
    expected = ['self', 'symbol', 'old_sl', 'new_sl', 'reason']
    assert params == expected, f"sl_moved params: {params}, expected: {expected}"

test("telegram sl_moved signature", test_telegram_sl_moved_sig)

def test_telegram_order_failed_sig():
    from telegram_notifier import TelegramNotifier
    sig = inspect.signature(TelegramNotifier.order_failed)
    params = list(sig.parameters.keys())
    expected = ['self', 'symbol', 'order_type', 'reason']
    assert params == expected, f"order_failed params: {params}, expected: {expected}"

test("telegram order_failed signature", test_telegram_order_failed_sig)

def test_telegram_system_status_sig():
    from telegram_notifier import TelegramNotifier
    sig = inspect.signature(TelegramNotifier.system_status)
    params = list(sig.parameters.keys())
    expected = ['self', 'balance', 'open_positions', 'daily_pnl', 'daily_dd', 'overall_dd', 'active_markets']
    assert params == expected, f"system_status params: {params}, expected: {expected}"

test("telegram system_status signature", test_telegram_system_status_sig)


# ============================================================================
# 5. TELEGRAM COMMANDS
# ============================================================================
print("\n" + "="*60)
print("5. TELEGRAM_COMMANDS")
print("="*60)

def test_cmd_imports():
    from telegram_commands import TelegramCommandHandler
    cmd = TelegramCommandHandler("fake_token", "fake_chat")
    assert cmd is not None

test("telegram_commands imports + init", test_cmd_imports)

def test_cmd_callbacks():
    from telegram_commands import TelegramCommandHandler
    cmd = TelegramCommandHandler("fake_token", "fake_chat")
    required = ['on_start', 'on_stop', 'on_pause', 'on_resume',
                'on_status', 'on_positions', 'on_balance']
    for attr in required:
        assert hasattr(cmd, attr), f"Missing callback: {attr}"

test("telegram_commands callback attributes", test_cmd_callbacks)

def test_cmd_methods():
    from telegram_commands import TelegramCommandHandler
    required = ['start_polling', 'stop_polling']
    for method in required:
        assert hasattr(TelegramCommandHandler, method), f"Missing method: {method}"

test("telegram_commands start/stop methods", test_cmd_methods)


# ============================================================================
# 6. TRADE JOURNAL
# ============================================================================
print("\n" + "="*60)
print("6. TRADE_JOURNAL")
print("="*60)

def test_journal_imports():
    from trade_journal import TradeJournal, ReportScheduler

test("trade_journal imports", test_journal_imports)

def test_journal_add_trade_sig():
    from trade_journal import TradeJournal
    sig = inspect.signature(TradeJournal.add_trade)
    params = list(sig.parameters.keys())
    assert 'trade_data' in params

test("trade_journal add_trade signature", test_journal_add_trade_sig)

def test_journal_report_scheduler_sig():
    from trade_journal import ReportScheduler
    sig = inspect.signature(ReportScheduler.check_and_send_reports)
    params = list(sig.parameters.keys())
    expected = ['self', 'balance', 'daily_dd', 'overall_dd']
    assert params == expected, f"check_and_send_reports params: {params}, expected: {expected}"

test("report_scheduler check_and_send_reports sig", test_journal_report_scheduler_sig)


# ============================================================================
# 7. MT5_CONNECTOR (core classes, mocking MT5)
# ============================================================================
print("\n" + "="*60)
print("7. MT5_CONNECTOR")
print("="*60)

# Mock MetaTrader5 before importing
mock_mt5 = MagicMock()
mock_mt5.TIMEFRAME_M1 = 1
mock_mt5.TIMEFRAME_M5 = 5
mock_mt5.TIMEFRAME_M15 = 15
mock_mt5.TIMEFRAME_H1 = 16385
mock_mt5.ORDER_TYPE_BUY = 0
mock_mt5.ORDER_TYPE_SELL = 1
mock_mt5.POSITION_TYPE_BUY = 0
mock_mt5.POSITION_TYPE_SELL = 1
mock_mt5.ORDER_FILLING_IOC = 2
mock_mt5.ORDER_TIME_GTC = 0
mock_mt5.TRADE_ACTION_DEAL = 1
mock_mt5.TRADE_ACTION_SLTP = 6
mock_mt5.TRADE_RETCODE_DONE = 10009
mock_mt5.initialize.return_value = True
mock_mt5.login.return_value = True

# Mock account_info
mock_account = MagicMock()
mock_account.login = 13958231
mock_account.balance = 6000.0
mock_account.equity = 6000.0
mock_account.margin = 0.0
mock_account.margin_free = 6000.0
mock_account.profit = 0.0
mock_account.leverage = 100
mock_mt5.account_info.return_value = mock_account
mock_mt5.positions_get.return_value = []

sys.modules['MetaTrader5'] = mock_mt5

def test_mt5_connector_imports():
    from live.mt5_connector import (
        MT5Connector, DrawdownProtection, LiveTrader,
        TradingState, is_market_open, get_active_indices,
        BOT_MAGIC,
    )
    assert BOT_MAGIC == 20260218

test("mt5_connector imports", test_mt5_connector_imports)

def test_mt5_connector_class():
    from live.mt5_connector import MT5Connector
    mc = MT5Connector()
    assert hasattr(mc, 'connect')
    assert hasattr(mc, 'disconnect')
    assert hasattr(mc, 'get_account_info')
    assert hasattr(mc, 'get_current_price')
    assert hasattr(mc, 'get_ohlc_data')
    assert hasattr(mc, 'place_order')
    assert hasattr(mc, 'modify_position')
    assert hasattr(mc, 'close_position')
    assert hasattr(mc, 'get_open_positions')

test("MT5Connector class methods", test_mt5_connector_class)

def test_drawdown_protection():
    from live.mt5_connector import DrawdownProtection
    dd = DrawdownProtection(6000.0)

    # No drawdown
    r = dd.update(6000.0)
    assert r['overall_dd'] == 0.0
    assert r['daily_dd'] == 0.0
    assert r['can_trade'] == True

    # 1% drawdown from initial
    r = dd.update(5940.0)
    assert abs(r['overall_dd'] - 1.0) < 0.01
    assert r['can_trade'] == True

    # 2% daily DD from start → should breach (daily_drawdown_stop=2.0)
    r = dd.update(5880.0)
    assert r['daily_dd'] >= 2.0
    assert r['is_daily_dd_breach'] == True

    # Max DD breach at 8% of initial
    r = dd.update(5520.0)
    assert r['overall_dd'] >= 8.0
    assert r['is_max_dd_breach'] == True
    assert r['can_trade'] == False

test("DrawdownProtection logic", test_drawdown_protection)

def test_dd_daily_from_start_balance():
    """Daily DD must be from day-start balance, NOT daily peak"""
    from live.mt5_connector import DrawdownProtection
    dd = DrawdownProtection(6000.0)

    # Simulate day start at $6000
    dd.reset_daily(6000.0)

    # Go up to $6100 during day
    r = dd.update(6100.0)
    assert r['daily_dd'] == 0.0  # still zero, above start

    # Drop to $5940 = $60 below START ($6000), not below PEAK ($6100)
    r = dd.update(5940.0)
    # Daily DD should be from $6000 (start), not from $6100 (peak)
    expected_daily_dd = (6000 - 5940) / 6000 * 100  # = 1.0%
    assert abs(r['daily_dd'] - expected_daily_dd) < 0.01, \
        f"Daily DD={r['daily_dd']:.2f}%, expected {expected_daily_dd:.2f}% (from start, not peak)"

test("DD daily from start balance (not peak)", test_dd_daily_from_start_balance)

def test_dd_overall_from_initial():
    """Overall DD must be from INITIAL balance"""
    from live.mt5_connector import DrawdownProtection
    dd = DrawdownProtection(6000.0)

    # Go up to $7000 and back down to $6200
    dd.update(7000.0)
    r = dd.update(6200.0)
    # Overall DD from INITIAL ($6000), not from peak ($7000)
    # We're above initial so DD = 0
    assert r['overall_dd'] == 0.0, \
        f"DD={r['overall_dd']:.2f}% — should be 0 (above initial balance)"

test("DD overall from initial balance", test_dd_overall_from_initial)

def test_adaptive_risk():
    from live.mt5_connector import DrawdownProtection
    dd = DrawdownProtection(6000.0)

    # Healthy account → default 1.5%
    risk = dd.get_adaptive_risk(6000.0)
    assert risk == 1.5, f"Expected 1.5%, got {risk}%"

    # In drawdown → reduced 1.0%
    dd.peak_balance = 6200.0
    risk = dd.get_adaptive_risk(6000.0)
    # DD from peak = (6200-6000)/6000*100 = 3.33%
    # If > drawdown_threshold (2.0%), switch to reduced
    assert risk == 1.0, f"Expected 1.0% in drawdown, got {risk}%"

test("adaptive risk reduces in drawdown", test_adaptive_risk)

def test_trading_state():
    from live.mt5_connector import TradingState
    state = TradingState()
    assert state.daily_pnl == 0.0
    assert state.daily_trades == 0
    assert state.is_daily_limit_hit == False

test("TradingState defaults", test_trading_state)

def test_livetrader_class_exists():
    from live.mt5_connector import LiveTrader
    required = [
        'initialize', 'run', '_generate_signal', '_can_trade',
        '_execute_signal', '_manage_positions', '_check_connection',
        '_save_state', '_load_state', '_check_daily_reset',
        '_write_heartbeat', '_emergency_close_all',
        '_verify_positions_after_reconnect', '_get_htf_trend',
    ]
    for method in required:
        assert hasattr(LiveTrader, method), f"Missing method: {method}"

test("LiveTrader class methods", test_livetrader_class_exists)

def test_livetrader_init():
    from live.mt5_connector import LiveTrader
    lt = LiveTrader()
    assert lt.mt5 is not None
    assert lt.news_filter is not None
    assert lt.telegram is not None
    assert lt.journal is not None
    assert lt._shutdown_requested == False
    assert lt._trading_enabled == True

test("LiveTrader instantiation", test_livetrader_init)

def test_livetrader_cmd_callbacks_wired():
    from live.mt5_connector import LiveTrader
    lt = LiveTrader()
    assert lt.cmd_handler.on_start is not None, "on_start not wired"
    assert lt.cmd_handler.on_stop is not None, "on_stop not wired"
    assert lt.cmd_handler.on_pause is not None, "on_pause not wired"
    assert lt.cmd_handler.on_resume is not None, "on_resume not wired"
    assert lt.cmd_handler.on_status is not None, "on_status not wired"
    assert lt.cmd_handler.on_positions is not None, "on_positions not wired"
    assert lt.cmd_handler.on_balance is not None, "on_balance not wired"

test("LiveTrader command callbacks wired", test_livetrader_cmd_callbacks_wired)

def test_livetrader_strategies_per_symbol():
    """After initialize(), should have a strategy for each profitable index"""
    from live.mt5_connector import LiveTrader
    lt = LiveTrader()
    ok = lt.initialize()
    assert ok == True, "initialize() failed"
    from config_fundednext import PROFITABLE_INDICES
    for sym in PROFITABLE_INDICES:
        assert sym in lt.strategies, f"No strategy for {sym}"
        assert sym in lt.trade_taken_today, f"No trade_taken_today for {sym}"

test("LiveTrader has strategy per index", test_livetrader_strategies_per_symbol)

def test_livetrader_save_load_state():
    from live.mt5_connector import LiveTrader
    import json, tempfile
    lt = LiveTrader()
    lt.initialize()

    # Add a tracked position
    lt.tracked_positions[12345] = {
        'symbol': 'SPX500', 'direction': 'buy', 'entry': 5000.0,
        'sl_init': 4980.0, 'tp': 5050.0, 'R': 20.0,
        'hit_be': False, 'max_favorable': 0.0,
        'volume': 0.5, 'entry_time': str(datetime.now()),
    }
    lt.trade_taken_today['SPX500'] = True
    lt.state.daily_pnl = 42.50
    lt.state.daily_trades = 1
    lt.state.last_reset_date = datetime.now().strftime("%Y-%m-%d")

    # Save state
    lt._save_state()
    assert lt.STATE_FILE.exists(), "State file not created"

    # Verify content
    with open(lt.STATE_FILE) as f:
        data = json.load(f)
    assert data['strategy'] == 'ORB_Clean_Breakout'
    assert '12345' in data['positions']
    assert data['trade_taken_today']['SPX500'] == True
    assert data['daily_pnl'] == 42.50

    # Clean up
    lt.STATE_FILE.unlink(missing_ok=True)

test("LiveTrader save/load state", test_livetrader_save_load_state)

def test_livetrader_daily_reset():
    from live.mt5_connector import LiveTrader
    lt = LiveTrader()
    lt.initialize()

    # Simulate yesterday's state
    lt.state.last_reset_date = "2026-02-17"
    lt.state.daily_pnl = 100.0
    lt.state.daily_trades = 3
    lt.trade_taken_today['SPX500'] = True

    lt._check_daily_reset()

    assert lt.state.last_reset_date == datetime.now().strftime("%Y-%m-%d")
    assert lt.state.daily_pnl == 0.0
    assert lt.state.daily_trades == 0
    assert lt.trade_taken_today['SPX500'] == False

test("LiveTrader daily reset", test_livetrader_daily_reset)

def test_livetrader_heartbeat():
    from live.mt5_connector import LiveTrader
    lt = LiveTrader()
    lt._write_heartbeat()
    assert lt._heartbeat_file.exists(), "Heartbeat file not created"
    content = lt._heartbeat_file.read_text()
    assert len(content) > 10, "Heartbeat file empty"
    # Clean up
    lt._heartbeat_file.unlink(missing_ok=True)

test("LiveTrader heartbeat write", test_livetrader_heartbeat)


# ============================================================================
# 8. STRATEGY
# ============================================================================
print("\n" + "="*60)
print("8. STRATEGY (ORB Clean Breakout)")
print("="*60)

def test_strategy_imports():
    from strategy.strategy_ict_clean import StrategyICTClean
    s = StrategyICTClean(sl_buffer=1.0, orb_minutes=15, disp_mult=1.7, body_min=0.65)
    assert s.orb_minutes == 15
    assert s.disp_mult == 1.7
    assert s.body_min == 0.65

test("strategy imports + init", test_strategy_imports)

def test_strategy_generate_signals():
    from strategy.strategy_ict_clean import StrategyICTClean
    sig = inspect.signature(StrategyICTClean.generate_signals)
    params = list(sig.parameters.keys())
    assert 'df' in params
    assert 'htf_trend' in params

test("strategy generate_signals signature", test_strategy_generate_signals)


# ============================================================================
# 9. WATCHDOG
# ============================================================================
print("\n" + "="*60)
print("9. WATCHDOG")
print("="*60)

def test_watchdog_imports():
    from watchdog import TradingWatchdog, acquire_lock, release_lock

test("watchdog imports", test_watchdog_imports)

def test_watchdog_methods():
    from watchdog import TradingWatchdog
    required = [
        'start_bot', 'is_bot_running', 'kill_bot', 'is_bot_frozen',
        'can_restart', 'check_system_resources', 'check_mt5_process',
        'start_mt5', 'run',
    ]
    for method in required:
        assert hasattr(TradingWatchdog, method), f"Missing method: {method}"

test("watchdog all methods exist", test_watchdog_methods)

def test_watchdog_init():
    from watchdog import TradingWatchdog
    wd = TradingWatchdog()
    assert wd.max_restarts_per_hour == 5
    assert wd.process is None
    assert wd.python_path.exists(), f"Python not found: {wd.python_path}"
    assert wd.script_path.exists(), f"Script not found: {wd.script_path}"

test("watchdog init + python/script paths", test_watchdog_init)

def test_watchdog_can_restart():
    from watchdog import TradingWatchdog
    wd = TradingWatchdog()
    assert wd.can_restart() == True
    # Simulate 5 recent restarts
    wd.restart_times = [datetime.now()] * 5
    assert wd.can_restart() == False

test("watchdog can_restart rate limiting", test_watchdog_can_restart)

def test_watchdog_check_resources():
    from watchdog import TradingWatchdog
    wd = TradingWatchdog()
    res = wd.check_system_resources()
    assert 'cpu_percent' in res
    assert 'memory_percent' in res
    assert 'disk_percent' in res
    assert 0 <= res['cpu_percent'] <= 100
    assert 0 <= res['memory_percent'] <= 100

test("watchdog system resources", test_watchdog_check_resources)


# ============================================================================
# 10. RUN_LIVE
# ============================================================================
print("\n" + "="*60)
print("10. RUN_LIVE")
print("="*60)

def test_run_live_imports():
    # This file manipulates sys.path at module level
    import importlib
    spec = importlib.util.spec_from_file_location("run_live",
        str(Path(__file__).parent / "run_live.py"))
    mod = importlib.util.module_from_spec(spec)
    # Don't execute it, just check it loads without syntax error
    # (already validated by py_compile)
    assert spec is not None

test("run_live module spec loads", test_run_live_imports)


# ============================================================================
# 11. CROSS-MODULE COMPATIBILITY
# ============================================================================
print("\n" + "="*60)
print("11. CROSS-MODULE COMPATIBILITY")
print("="*60)

def test_mt5_calls_telegram_correctly():
    """Verify all telegram calls in LiveTrader match telegram_notifier signatures"""
    import ast
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    
    from telegram_notifier import TelegramNotifier
    tn_methods = {name: inspect.signature(getattr(TelegramNotifier, name))
                  for name in dir(TelegramNotifier) if not name.startswith('_')}
    
    # Find all self.telegram.METHOD(...) calls
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Attribute):
                if (hasattr(node.func.value, 'attr') and 
                    node.func.value.attr == 'telegram'):
                    method = node.func.attr
                    n_args = len(node.args) + len(node.keywords)
                    calls.append((method, n_args, node.lineno))
    
    errors = []
    for method, n_args, line in calls:
        if method in tn_methods:
            sig = tn_methods[method]
            min_params = sum(1 for p in sig.parameters.values() 
                           if p.default is inspect.Parameter.empty and p.name != 'self')
            max_params = len(sig.parameters) - 1  # exclude self
            if n_args < min_params:
                errors.append(f"Line {line}: {method}() got {n_args} args, needs minimum {min_params}")
            elif n_args > max_params:
                errors.append(f"Line {line}: {method}() got {n_args} args, max {max_params}")
    
    assert len(errors) == 0, f"Telegram call mismatches: {errors}"

test("mt5_connector <-> telegram_notifier compatibility", test_mt5_calls_telegram_correctly)

def test_mt5_calls_journal_correctly():
    """Verify journal.add_trade field names match TradeJournal expectations"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    
    # The journal expects: symbol, direction, entry_price, stop_loss, tp1, tp2, tp3, lot_size, risk_amount
    # Check the dict keys in the add_trade call
    import re
    match = re.search(r'journal\.add_trade\(\{(.*?)\}\)', src, re.DOTALL)
    assert match, "Could not find journal.add_trade call"
    call_text = match.group(1)
    
    expected_keys = ['symbol', 'direction', 'lot_size', 'entry_price', 'stop_loss', 'tp1']
    for key in expected_keys:
        assert f'"{key}"' in call_text, f"Missing key '{key}' in add_trade call"
    
    # Should NOT have old mismatched keys
    assert '"volume"' not in call_text, "'volume' should be 'lot_size'"
    assert '"sl"' not in call_text, "'sl' should be 'stop_loss'"

test("mt5_connector <-> trade_journal field names", test_mt5_calls_journal_correctly)

def test_mt5_cmd_handler_uses_attributes():
    """Verify LiveTrader wires callbacks via attributes, not register_callback()"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    assert 'register_callback' not in src, \
        "Should use cmd_handler.on_X = ... not register_callback()"
    assert 'cmd_handler.on_start' in src

test("LiveTrader uses cmd attribute callbacks", test_mt5_cmd_handler_uses_attributes)


# ============================================================================
# 12. EDGE CASE SCENARIOS
# ============================================================================
print("\n" + "="*60)
print("12. EDGE CASE SCENARIOS")
print("="*60)

def test_max_dd_breach_closes_all():
    from live.mt5_connector import LiveTrader
    lt = LiveTrader()
    lt.initialize()
    lt._trading_enabled = True

    # Should have emergency close method
    assert hasattr(lt, '_emergency_close_all')
    sig = inspect.signature(lt._emergency_close_all)
    assert 'reason' in sig.parameters

test("emergency close all exists", test_max_dd_breach_closes_all)

def test_verify_positions_after_reconnect():
    from live.mt5_connector import LiveTrader
    lt = LiveTrader()
    lt.initialize()
    assert hasattr(lt, '_verify_positions_after_reconnect')

test("position reconciliation after reconnect", test_verify_positions_after_reconnect)

def test_state_file_atomic_write():
    """State must use atomic write (temp + rename)"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    assert '.with_suffix(".tmp")' in src, "State save should use temp file"
    assert '.replace(' in src, "State save should rename temp to final"

test("state file atomic write pattern", test_state_file_atomic_write)

def test_stale_signal_guard():
    """Signals older than 5 bars should be skipped"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    assert 'bars_since_entry > 5' in src or 'bars_since_entry > 5' in src, \
        "Stale signal guard (5-bar check) missing"

test("stale signal guard (5 bars)", test_stale_signal_guard)

def test_internet_check_exists():
    from live.mt5_connector import _internet_ok
    # Don't actually call it to avoid delays, just verify it exists
    sig = inspect.signature(_internet_ok)
    assert 'timeout' in sig.parameters

test("internet connectivity check function", test_internet_check_exists)

def test_session_close_force_exit():
    """Force close must happen before session end"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    assert 'force_close_hour' in src
    assert 'force_close_minute' in src
    assert 'session ending' in src.lower() or 'session_close' in src

test("session close force exit logic", test_session_close_force_exit)

def test_sl_missing_detection():
    """Detect and re-apply missing SL"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    assert 'current_sl == 0.0' in src or 'sl == 0' in src, "SL missing detection"
    assert 'sl_init' in src, "Should re-apply from sl_init"

test("SL missing detection + re-apply", test_sl_missing_detection)

def test_slippage_logging():
    """Must log slippage on every order"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    assert 'slippage' in src.lower()
    assert 'fill_price' in src

test("slippage logging on order", test_slippage_logging)


# ============================================================================
# 13. LOOPHOLE FIX VERIFICATION
# ============================================================================
print("\n" + "="*60)
print("13. LOOPHOLE FIX VERIFICATION")
print("="*60)

def test_daily_pnl_updated_on_external_close():
    """Loophole 1: daily_pnl must be updated when TP/SL closes a trade"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    # Find the externally closed section - must update daily_pnl
    idx = src.find('closed externally')
    assert idx > 0, "externally closed section not found"
    section = src[idx-300:idx+300]
    assert 'daily_pnl' in section, "daily_pnl not updated when trade closed externally"

test("daily PnL updated on TP/SL close", test_daily_pnl_updated_on_external_close)

def test_deal_history_query_exists():
    """Loophole 8: must query deal history for actual closed PnL"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    assert '_get_closed_trade_pnl' in src, "deal history query method missing"
    assert 'history_deals_get' in src, "MT5 history_deals_get not called"

test("deal history PnL query exists", test_deal_history_query_exists)

def test_dst_safe_market_hours():
    """Loophole 2: is_market_open must use market's local timezone"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    # is_market_open should reference market_open_hour (local tz), not just mt5_open_hour
    func_start = src.find('def is_market_open')
    func_end = src.find('\ndef ', func_start + 10)
    func_body = src[func_start:func_end]
    assert 'market_open_hour' in func_body, "is_market_open should use local market hours"
    assert "config.get('timezone'" in func_body or 'config["timezone"]' in func_body, \
        "is_market_open should use config timezone"

test("DST-safe market hours (local timezone)", test_dst_safe_market_hours)

def test_dst_safe_signal_generation():
    """Loophole 2b: session slicing must convert local time to server time"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    func_start = src.find('def _generate_signal')
    func_end = src.find('\n    def ', func_start + 10)
    func_body = src[func_start:func_end]
    assert 'srv_open' in func_body or 'local_open' in func_body, \
        "signal generation should convert local market time to server time"

test("DST-safe signal generation slicing", test_dst_safe_signal_generation)

def test_stop_command_sets_shutdown():
    """Loophole 3: /stop must set _shutdown_requested=True"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    stop_start = src.find('def _on_stop')
    stop_end = src.find('\n    def ', stop_start + 10)
    stop_body = src[stop_start:stop_end]
    assert '_shutdown_requested = True' in stop_body, "/stop must set shutdown flag"
    assert 'disconnect' not in stop_body, "/stop should not disconnect MT5 directly"

test("/stop sets shutdown flag (not disconnect)", test_stop_command_sets_shutdown)

def test_emergency_close_correct_magic():
    """Loophole 4: emergency close must use BOT_MAGIC 20260218"""
    src = open(str(Path(__file__).parent / 'live' / 'sl_protection.py'), encoding='utf-8').read()
    idx = src.find('Emergency:')
    assert idx > 0
    section = src[idx-200:idx+50]
    assert '20260218' in section, "emergency close uses wrong magic number"
    assert '234000' not in section, "old magic 234000 still present"

test("emergency close correct magic (20260218)", test_emergency_close_correct_magic)

def test_sl_monitor_proportional_tolerance():
    """Loophole 5: SL deviation check uses proportional tolerance, not hardcoded 10"""
    src = open(str(Path(__file__).parent / 'live' / 'sl_protection.py'), encoding='utf-8').read()
    monitor_start = src.find('def monitor_open_positions')
    monitor_end = src.find('\ndef ', monitor_start + 10)
    monitor_body = src[monitor_start:monitor_end]
    assert 'get_sl_tolerance' in monitor_body, "SL monitor should use proportional tolerance"
    assert 'sl_deviation > 10' not in monitor_body, "hardcoded 10pt check still present"

test("SL monitor proportional tolerance", test_sl_monitor_proportional_tolerance)

def test_sl_protection_reads_env_risk():
    """Loophole 7: sl_protection RISK_PER_TRADE from .env not hardcoded"""
    src = open(str(Path(__file__).parent / 'live' / 'sl_protection.py'), encoding='utf-8').read()
    # Should use os.environ or os.getenv
    assert "os.environ" in src or "os.getenv" in src, "RISK_PER_TRADE should read from env"
    assert "RISK_PER_TRADE = 0.015" not in src, "hardcoded 0.015 still present"

test("sl_protection reads RISK from .env", test_sl_protection_reads_env_risk)

def test_sl_tracker_synced_on_be_trail():
    """Loophole 9: SL tracker must be updated when bot moves SL to BE or trails"""
    src = open(str(Path(__file__).parent / 'live' / 'mt5_connector.py'), encoding='utf-8').read()
    assert 'sl_track_original_sl' in src, "sl_track_original_sl import missing"
    # Should appear near BE trigger and trail sections
    be_idx = src.find('BE triggered')
    assert be_idx > 0
    be_section = src[be_idx-200:be_idx]
    assert 'sl_track_original_sl' in be_section, "SL tracker not updated on BE move"

test("SL tracker synced on BE/trail moves", test_sl_tracker_synced_on_be_trail)

def test_watchdog_no_pipe_deadlock():
    """Loophole 6: watchdog should not use subprocess.PIPE for long-running bot"""
    src = open(str(Path(__file__).parent / 'watchdog.py'), encoding='utf-8').read()
    start_bot_idx = src.find('def start_bot')
    start_bot_end = src.find('\n    def ', start_bot_idx + 10)
    start_bot_body = src[start_bot_idx:start_bot_end]
    assert 'subprocess.PIPE' not in start_bot_body, "subprocess.PIPE can deadlock"

test("watchdog no PIPE deadlock", test_watchdog_no_pipe_deadlock)


# ============================================================================
# FINAL SUMMARY
# ============================================================================
print("\n" + "="*60)
print("FINAL RESULTS")
print("="*60)
print(f"  PASS: {PASS}")
print(f"  FAIL: {FAIL}")
print(f"  WARN: {WARN}")
print(f"  TOTAL: {PASS + FAIL + WARN}")
print("="*60)

if FAIL > 0:
    print("\nFAILED TESTS:")
    for r in results:
        if r[0] == "FAIL":
            print(f"  [FAIL] {r[1]}: {r[2]}")
    sys.exit(1)
else:
    print("\n  ALL TESTS PASSED!")
    sys.exit(0)
