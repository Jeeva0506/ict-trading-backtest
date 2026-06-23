"""
FundedNext Stellar-2 Live Trading Configuration
================================================
Account Rules loaded from .env file:
- Max Drawdown: 10%
- Daily Drawdown: 3% (Stop at 2% to be safe)
- Risk per trade: Adaptive (2% default, 1% in drawdown)

Adaptive Risk Strategy:
- Evaluation phase: Start 2% risk, switch to 1% at 3% DD
- Funded account: Set ADAPTIVE_RISK['enabled'] = False for fixed 1%
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# ── Multi-account support ──────────────────────────────────────────────
# Set ACCOUNT_PROFILE env var BEFORE importing this module to select profile.
#   ACCOUNT_PROFILE=stellar15k  → loads .env.stellar15k
#   ACCOUNT_PROFILE=  (default) → loads .env (original account)
_profile = os.environ.get('ACCOUNT_PROFILE', '').strip()
if _profile:
    env_path = Path(__file__).parent.parent / f'.env.{_profile}'
    if not env_path.exists():
        raise FileNotFoundError(
            f"Account profile '{_profile}' requested but {env_path} not found.\n"
            f"Create the file or check the ACCOUNT_PROFILE env var."
        )
else:
    env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path, override=True)  # override=True so profile env beats any stale vars

# ============================================================================
# MT5 SERVER CREDENTIALS (from .env)
# ============================================================================
MT5_SERVER = os.getenv('MT5_SERVER', 'FundedNext-Server')
MT5_LOGIN = int(os.getenv('MT5_LOGIN', '0'))
MT5_PASSWORD = os.getenv('MT5_PASSWORD', '')

# ============================================================================
# ACCOUNT CONFIGURATION (from .env)
# ============================================================================
ACCOUNT_SIZE = float(os.getenv('ACCOUNT_SIZE', '100000'))
ACCOUNT_CURRENCY = os.getenv('ACCOUNT_CURRENCY', 'USD')

# ============================================================================
# FUNDEDNEXT STELLAR-2 RULES (from .env with defaults)
# ============================================================================
FUNDEDNEXT_RULES = {
    'max_drawdown_percent': float(os.getenv('MAX_DRAWDOWN', '10.0')),
    'daily_drawdown_percent': float(os.getenv('DAILY_DRAWDOWN', '3.0')),
    'daily_drawdown_stop': float(os.getenv('DAILY_DRAWDOWN_STOP', '2.0')),
    'risk_per_trade_percent': float(os.getenv('RISK_PER_TRADE', '1.0')),
    'max_trades_per_day': 2,           # REDUCED from 3: limits correlated loss clusters on bad days
    'max_open_positions': 2,           # Max concurrent positions across all symbols (aligned with backtest)
    'news_buffer_before_minutes': 5,    # Block 5 min BEFORE high-impact news
    'news_buffer_after_minutes': 15,    # Block 15 min AFTER high-impact news
}

# ============================================================================
# ATR SL CONFIGURATION
# ============================================================================
ATR_SL_ENABLED = False              # Disabled - using fixed buffers instead

# ============================================================================
# EDGE GUARD CONFIGURATION (default enabled for most accounts)
# ============================================================================
EDGE_GUARD_ENABLED = False          # Disabled — trading freely without edge guard

# ============================================================================
# ADAPTIVE RISK STRATEGY - ACCOUNT SPECIFIC
# ============================================================================
# STELLAR-15K (Primary account on FundedNext-Server2):
#   Tier 1: 1.0% risk (DD < 4%)
#   Tier 2: 0.5% risk (DD >= 4%) ← ACTIVE NOW (DD=4.98%)
#
# STELLAR-2 (Secondary account, ultra-conservative):
#   Tier 1: 0.25% risk (DD < 4%)
#   Tier 2: 0.125% risk (DD >= 4%)
#
# Both accounts locked for 5-10 trade monitoring period (2026-03-26)

# Determine account profile and apply correct risk tiers
_account_profile = os.environ.get('ACCOUNT_PROFILE', '').strip().lower()

if _account_profile == 'stellar15k':
    # STELLAR-15K: Higher tier baseline (1.0% → 0.5%)
    ADAPTIVE_RISK = {
        'enabled': True,
        'default_risk_percent': 1.0,    # Tier 1: DD < 4% (normal trading)
        'tier2_risk_percent': 0.5,      # Tier 2: DD >= 4% (drawdown protection)
        'tier2_threshold': 4.0,         # Switch to tier2 at 4% DD
        'recovery_threshold': 1.0,      # Restore to tier1 when DD < 1%
    }
    ATR_SL_ENABLED = False            # Disabled - reverting to fixed buffers
    EDGE_GUARD_ENABLED = False        # Disabled - let it trade freely
elif _account_profile == 'stellar3':
    # STELLAR-3: Conservative (1.0% → 0.5%)
    ADAPTIVE_RISK = {
        'enabled': True,
        'default_risk_percent': 1.5,    # Tier 1: DD < 4% (conservative risk)
        'tier2_risk_percent': 1,      # Tier 2: DD >= 4% (drawdown protection)
        'tier2_threshold': 4.0,         # Switch to tier2 at 4% DD
        'recovery_threshold': 1.0,      # Restore to tier1 when DD < 1%
    }
    ATR_SL_ENABLED = False            # Fixed buffers
    EDGE_GUARD_ENABLED = False        # Disabled — trading freely
else:
    # STELLAR-2: Standard (1.5% → 1.0%)
    ADAPTIVE_RISK = {
        'enabled': True,
        'default_risk_percent': 1.5,    # Tier 1: DD < 4% (standard risk)
        'tier2_risk_percent': 1.0,      # Tier 2: DD >= 4% (drawdown protection)
        'tier2_threshold': 4.0,         # Switch to tier2 at 4% DD
        'recovery_threshold': 1.0,      # Restore to tier1 when DD < 1%
    }
    ATR_SL_ENABLED = False            # Fixed buffers
    EDGE_GUARD_ENABLED = False        # Disabled — trading freely

# ============================================================================
# NOTIFICATION SETTINGS (from .env)
# ============================================================================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')

# ============================================================================
# EXTERNAL HEARTBEAT MONITOR (healthchecks.io)
# ============================================================================
# Free service that alerts via Telegram if the bot stops pinging.
# Detects: laptop power-off, internet loss, full system crash.
#
# Setup (one-time, 2 minutes):
#   1. Sign up at https://healthchecks.io (free, no card)
#   2. Create a check: Period=5min, Grace=10min
#   3. Copy the ping URL and paste in .env as HEARTBEAT_URL
#   4. In healthchecks.io → Integrations → Add Telegram → follow bot link
#
# The watchdog pings this URL every 5 minutes. If no ping for 10 minutes,
# healthchecks.io sends YOU a Telegram alert — even if this laptop is dead.
HEARTBEAT_URL = os.getenv('HEARTBEAT_URL', '')  # e.g. https://hc-ping.com/your-uuid-here
HEARTBEAT_INTERVAL_SECONDS = 300  # Ping every 5 minutes

# ============================================================================
# MULTI-ACCOUNT SETTINGS (from .env)
# ============================================================================
ACCOUNT_LABEL = os.getenv('ACCOUNT_LABEL', 'DEFAULT')
MT5_PATH = os.getenv('MT5_PATH', '')           # path to terminal64.exe (empty = default)
# Comma-separated list of symbols to EXCLUDE from this account
# e.g. EXCLUDE_INDICES=US2000,FRA40
_EXCLUDE_RAW = os.getenv('EXCLUDE_INDICES', '')
EXCLUDE_INDICES = [s.strip().upper() for s in _EXCLUDE_RAW.split(',') if s.strip()]

# ============================================================================
# PROFITABLE INDICES ONLY - With correct MT5 specifications
# ============================================================================
PROFITABLE_INDICES = {
    'SPX500': {
        'symbol': 'SPX500',
        'description': 'S&P 500 CFD',
        'contract_size': 10,
        'tick_size': 0.01,
        'tick_value': 0.01,         # $ per tick per lot
        'currency': 'USD',
        'point_value': 10.0,        # $ per point per lot (verified via MT5 order_calc_profit)
        'sl_buffer': 1.0,           # SL buffer in points
        'min_sl_distance': 2.0,     # Min SL distance for valid trade
        'max_sl_distance': 25.0,    # Max SL distance (aligned with backtest)
        
        # ORB strategy params — Backtest-validated (Mar 24 2026 fix: 1.5/0.60/0.10/2h)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.5,
        'body_min': 0.60,
        'close_strength': 0.25,
        'breakout_margin': 0.10,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 2.0,
        'max_sl_points': 25.0,
        
        # Trading Hours (Local market time)
        'market_open_hour': 9,
        'market_open_minute': 30,
        'market_close_hour': 16,
        'market_close_minute': 0,
        'force_close_hour': 15,
        'force_close_minute': 45,
        
        # MT5 Time (EET/EEST - FundedNext server time)
        'mt5_open_hour': 16,        # 9:30 ET = 16:30 EET (winter)
        'mt5_open_minute': 30,
        'mt5_close_hour': 23,       # 16:00 ET = 23:00 EET
        'mt5_close_minute': 0,
        
        'timezone': 'America/New_York',
    },
    
    'GER30': {
        'symbol': 'GER30',
        'description': 'DAX 30 CFD',
        'contract_size': 10,
        'tick_size': 0.01,
        'tick_value': 0.01,
        'currency': 'EUR',
        'point_value': 11.80,       # $ per point per lot (10 EUR × ~1.18 EURUSD, verified MT5)
        'sl_buffer': 5.0,           # Buffer for ~24000 index (matched to backtest)
        'min_sl_distance': 10.0,
        'max_sl_distance': 80.0,    # Max SL distance (aligned with backtest)
        
        # ORB strategy params — Backtest-validated (Mar 24 2026 fix: 1.5/0.60/0.10/2h)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.5,
        'body_min': 0.60,
        'close_strength': 0.25,
        'breakout_margin': 0.10,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 10.0,
        'max_sl_points': 80.0,
        
        'market_open_hour': 9,
        'market_open_minute': 0,
        'market_close_hour': 17,
        'market_close_minute': 30,
        'force_close_hour': 17,
        'force_close_minute': 15,
        
        # MT5 Time (EET - same as Frankfurt basically)
        'mt5_open_hour': 10,
        'mt5_open_minute': 0,
        'mt5_close_hour': 18,
        'mt5_close_minute': 30,
        
        'timezone': 'Europe/Berlin',
    },
    
    'FRA40': {
        'symbol': 'FRA40',
        'description': 'CAC 40 CFD',
        'contract_size': 10,
        'tick_size': 0.01,
        'tick_value': 0.01,
        'currency': 'EUR',
        'point_value': 10.0,        # $ per point per lot (10 EUR × ~1.0 — backtest-validated)
        'sl_buffer': 1.0,           # Aligned with backtest
        'min_sl_distance': 2.0,
        'max_sl_distance': 30.0,    # Max SL distance (aligned with backtest)
        
        # ORB strategy params — FRA40 custom (backtest-validated, combined PF 1.54)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.5,
        'body_min': 0.60,
        'close_strength': 0.30,
        'breakout_margin': 0.10,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 2.0,
        'max_sl_points': 30.0,
        
        # Trading Hours — Euronext Paris 9:00-17:30 CET
        'market_open_hour': 9,
        'market_open_minute': 0,
        'market_close_hour': 17,
        'market_close_minute': 30,
        'force_close_hour': 17,
        'force_close_minute': 15,
        
        # MT5 Time (EET — Paris 9:00-17:30 CET = 10:00-18:30 EET)
        'mt5_open_hour': 10,
        'mt5_open_minute': 0,
        'mt5_close_hour': 18,
        'mt5_close_minute': 30,
        
        'timezone': 'Europe/Paris',
    },
    
    # HK50 removed - negative P&L after costs in 1-year backtest (-$4,018)
    # 'HK50': { ... },
    
    'NDX100': {
        'symbol': 'NDX100',
        'description': 'Nasdaq 100 CFD',
        'contract_size': 10,
        'tick_size': 0.01,
        'tick_value': 0.01,
        'currency': 'USD',
        'point_value': 10.0,        # $ per point per lot (verified via MT5 order_calc_profit)
        'sl_buffer': 5.0,
        'min_sl_distance': 10.0,
        'max_sl_distance': 80.0,    # Max SL distance (aligned with backtest)
        
        # ORB strategy params — Backtest-validated (Mar 24 2026 fix: 1.5/0.60/0.10/2h)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.5,
        'body_min': 0.60,
        'close_strength': 0.25,
        'breakout_margin': 0.10,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 10.0,
        'max_sl_points': 80.0,
        
        'market_open_hour': 9,
        'market_open_minute': 30,
        'market_close_hour': 16,
        'market_close_minute': 0,
        'force_close_hour': 15,
        'force_close_minute': 45,
        
        # MT5 Time (EET/EEST - same as SPX500)
        'mt5_open_hour': 16,
        'mt5_open_minute': 30,
        'mt5_close_hour': 23,
        'mt5_close_minute': 0,
        
        'timezone': 'America/New_York',
    },
    
    'US30': {
        'symbol': 'US30',
        'description': 'Dow Jones 30 CFD',
        'contract_size': 10,
        'tick_size': 0.01,
        'tick_value': 0.01,
        'currency': 'USD',
        'point_value': 10.0,        # $ per point per lot (verified via MT5 order_calc_profit)
        'sl_buffer': 5.0,           # Dow has larger point movements
        'min_sl_distance': 15.0,    # Aligned with backtest
        'max_sl_distance': 150.0,   # Aligned with backtest
        
        # ORB strategy params — Backtest-validated (Mar 24 2026 fix: 1.5/0.60/0.10/2h)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.5,
        'body_min': 0.60,
        'close_strength': 0.25,
        'breakout_margin': 0.10,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 15.0,
        'max_sl_points': 150.0,
        
        'market_open_hour': 9,
        'market_open_minute': 30,
        'market_close_hour': 16,
        'market_close_minute': 0,
        'force_close_hour': 15,
        'force_close_minute': 45,
        
        # MT5 Time (EET/EEST - same as SPX500)
        'mt5_open_hour': 16,
        'mt5_open_minute': 30,
        'mt5_close_hour': 23,
        'mt5_close_minute': 0,
        
        'timezone': 'America/New_York',
    },
    
    'SWI20': {
        'symbol': 'SWI20',
        'description': 'Swiss SMI 20 CFD',
        'contract_size': 10,
        'tick_size': 0.01,
        'tick_value': 0.01,
        'currency': 'CHF',
        'point_value': 12.93,       # $ per point per lot (verified via MT5 order_calc_profit)
        'sl_buffer': 2.0,
        'min_sl_distance': 5.0,
        'max_sl_distance': 200.0,

        # ORB strategy params — backtest-validated (PF 2.83 with limit orders)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.0,
        'body_min': 0.70,
        'close_strength': 0.25,
        'breakout_margin': 0.15,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 5.0,
        'max_sl_points': 200.0,

        # Trading Hours — SIX Swiss Exchange 9:00-17:30 CET
        'market_open_hour': 9,
        'market_open_minute': 0,
        'market_close_hour': 17,
        'market_close_minute': 30,
        'force_close_hour': 17,
        'force_close_minute': 15,

        # MT5 Time (EET — CET+1: 9:00-17:30 CET = 10:00-18:30 EET)
        'mt5_open_hour': 10,
        'mt5_open_minute': 0,
        'mt5_close_hour': 18,
        'mt5_close_minute': 30,

        'timezone': 'Europe/Zurich',
    },

    'JP225': {
        'symbol': 'JP225',
        'description': 'Nikkei 225 CFD',
        'contract_size': 10,
        'tick_size': 0.01,
        'tick_value': 0.01,
        'currency': 'JPY',
        'point_value': 10.0,        # $ per point per lot (verified via MT5 order_calc_profit)
        'sl_buffer': 10.0,
        'min_sl_distance': 20.0,
        'max_sl_distance': 500.0,

        # ORB strategy params — backtest-validated (PF 1.77 with limit orders)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.5,
        'body_min': 0.60,
        'close_strength': 0.30,
        'breakout_margin': 0.10,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 20.0,
        'max_sl_points': 500.0,

        # Trading Hours — TSE 9:00-15:00 JST
        'market_open_hour': 9,
        'market_open_minute': 0,
        'market_close_hour': 15,
        'market_close_minute': 0,
        'force_close_hour': 14,
        'force_close_minute': 45,

        # MT5 Time (EET — JST-7: 9:00-15:00 JST = 2:00-8:00 EET)
        'mt5_open_hour': 2,
        'mt5_open_minute': 0,
        'mt5_close_hour': 8,
        'mt5_close_minute': 0,

        'timezone': 'Asia/Tokyo',
    },

    'US2000': {
        'symbol': 'US2000',
        'description': 'Russell 2000 CFD',
        'contract_size': 10,
        'tick_size': 0.1,            # MT5 actual: digits=1, tick_size=0.1
        'tick_value': 0.1,           # MT5 actual: tick_value=0.1 USD
        'currency': 'USD',
        'point_value': 10.0,        # $ per point per lot (verified via MT5 order_calc_profit)
        'sl_buffer': 1.0,
        'min_sl_distance': 2.0,
        'max_sl_distance': 100.0,
        
        # ORB strategy params — Backtest-validated (original params for US2000)
        'orb_minutes': 15,
        'entry_hours': 3,
        'breakout_window_minutes': 60,
        'disp_mult': 1.0,
        'body_min': 0.70,
        'close_strength': 0.25,
        'breakout_margin': 0.15,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 2.0,
        'max_sl_points': 100.0,
        
        # Trading Hours (Local market time — ET)
        'market_open_hour': 9,
        'market_open_minute': 30,
        'market_close_hour': 16,
        'market_close_minute': 0,
        'force_close_hour': 15,
        'force_close_minute': 45,
        
        # MT5 Time (EET/EEST — NYSE 9:30-16:00 ET = 16:30-23:00 EET)
        'mt5_open_hour': 16,
        'mt5_open_minute': 30,
        'mt5_close_hour': 23,
        'mt5_close_minute': 0,
        
        'timezone': 'America/New_York',
    },
}

# ── Apply per-account exclusions ────────────────────────────────────────
if EXCLUDE_INDICES:
    for _sym in EXCLUDE_INDICES:
        if _sym in PROFITABLE_INDICES:
            del PROFITABLE_INDICES[_sym]
    # Log at import time so it's visible in the startup log
    import logging as _logging
    _logging.getLogger("config").info(
        f"[{ACCOUNT_LABEL}] Excluded indices: {EXCLUDE_INDICES} "
        f"→ active: {list(PROFITABLE_INDICES.keys())}"
    )

# ============================================================================
# HIGH IMPACT NEWS EVENTS TO AVOID
# ============================================================================
HIGH_IMPACT_NEWS = {
    'USD': [
        'Non-Farm Payrolls',
        'NFP',
        'FOMC',
        'Fed Interest Rate Decision',
        'CPI',
        'Core CPI',
        'GDP',
        'Unemployment Claims',
        'Retail Sales',
        'ISM Manufacturing PMI',
        'ISM Services PMI',
    ],
    'EUR': [
        'ECB Interest Rate Decision',
        'ECB Press Conference',
        'German CPI',
        'German GDP',
        'Eurozone CPI',
        'Eurozone GDP',
        'German ZEW Economic Sentiment',
    ],
    'GBP': [
        'BOE Interest Rate Decision',
        'UK CPI',
        'UK GDP',
        'UK Retail Sales',
    ],
    'HKD': [
        'Hong Kong GDP',
        'Hong Kong CPI',
    ],
    'CHF': [
        'SNB Interest Rate Decision',
        'SNB Press Conference',
        'Swiss CPI',
        'Swiss GDP',
        'Swiss Trade Balance',
    ],
}


def get_index_config(symbol: str) -> dict:
    """Get configuration for a specific index"""
    return PROFITABLE_INDICES.get(symbol.upper())


def get_all_profitable_indices() -> list:
    """Get list of all profitable index symbols"""
    return list(PROFITABLE_INDICES.keys())


def get_broker_symbol_specs(symbol: str) -> dict:
    """
    Get live symbol specifications from broker (MT5)
    This ensures we use actual broker data for calculations
    
    Returns:
        dict with contract_size, tick_size, tick_value, point, 
        volume_min, volume_max, volume_step, margin_per_lot, point_value
    """
    try:
        import MetaTrader5 as mt5
        
        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            return None
        
        # Calculate margin required for 1 lot
        margin_per_lot = mt5.order_calc_margin(
            mt5.ORDER_TYPE_BUY, 
            symbol, 
            1.0, 
            sym_info.ask
        ) or 0
        
        # Calculate ACTUAL point value using order_calc_profit
        # This is the most accurate way - ask MT5 directly what profit 
        # we get for a 1 full index point move with 1 lot
        point_value = mt5.order_calc_profit(
            mt5.ORDER_TYPE_BUY,
            symbol,
            1.0,  # 1 lot
            sym_info.ask,
            sym_info.ask + 1.0  # 1 full index point move
        ) or 0
        
        # Fallback if order_calc_profit fails
        if point_value == 0 and sym_info.trade_tick_size > 0:
            ticks_per_point = 1.0 / sym_info.trade_tick_size
            point_value = sym_info.trade_tick_value * ticks_per_point
        
        return {
            'contract_size': sym_info.trade_contract_size,
            'tick_size': sym_info.trade_tick_size,
            'tick_value': sym_info.trade_tick_value,
            'point': sym_info.point,
            'point_value': point_value,  # ACTUAL profit per 1 index point per lot
            'volume_min': sym_info.volume_min,
            'volume_max': sym_info.volume_max,
            'volume_step': sym_info.volume_step,
            'margin_per_lot': margin_per_lot,
            'currency_profit': sym_info.currency_profit,
            'currency_margin': sym_info.currency_margin,
        }
    except Exception as e:
        return None


def get_account_leverage() -> int:
    """Get account leverage from broker"""
    try:
        import MetaTrader5 as mt5
        info = mt5.account_info()
        return info.leverage if info else 100
    except:
        return 100


def calculate_position_size(account_balance: float, risk_percent: float, 
                           sl_points: float, point_value: float,
                           volume_min: float = 0.01, volume_max: float = 40.0,
                           volume_step: float = 0.01) -> float:
    """
    Calculate position size based on risk using broker specifications
    
    Args:
        account_balance: Current account balance
        risk_percent: Risk percentage (e.g., 1.0 for 1%)
        sl_points: Stop loss distance in points
        point_value: Value per point per lot (from broker data)
        volume_min: Minimum lot size (from broker)
        volume_max: Maximum lot size (from broker)
        volume_step: Lot step size (from broker)
    
    Returns:
        Position size in lots (respecting broker limits)
    """
    risk_amount = account_balance * (risk_percent / 100)
    risk_per_lot = sl_points * point_value
    
    if risk_per_lot <= 0:
        return volume_min
    
    lots = risk_amount / risk_per_lot
    
    # Round down to volume_step (use round() to avoid float imprecision
    # e.g. int(113) * 0.01 = 1.1300000000000001 instead of 1.13)
    lots = int(lots / volume_step) * volume_step
    # Determine precision from volume_step and round to eliminate float noise
    _vs_str = f"{volume_step:.10f}".rstrip('0')
    _vs_decimals = len(_vs_str.split('.')[-1]) if '.' in _vs_str else 0
    lots = round(lots, _vs_decimals)
    
    # Apply broker limits
    lots = max(volume_min, min(lots, volume_max))
    
    return lots


def calculate_position_size_with_broker_data(symbol: str, account_balance: float, 
                                              risk_percent: float, sl_points: float,
                                              free_margin: float = None,
                                              max_margin_percent: float = 90.0) -> dict:
    """
    Calculate position size using live broker data
    
    Args:
        symbol: Trading symbol
        account_balance: Current account balance
        risk_percent: Risk percentage
        sl_points: Stop loss distance in points
        free_margin: Available margin (if None, uses account_balance)
        max_margin_percent: Max % of free margin to use (default 90%)
    
    Returns:
        dict with lots, risk_amount, margin_required, etc.
    """
    specs = get_broker_symbol_specs(symbol)
    if not specs:
        # Fallback to config values
        config = get_index_config(symbol)
        if not config:
            return {'lots': 0.01, 'error': 'Symbol not found'}
        
        lots = calculate_position_size(
            account_balance, risk_percent, sl_points, 
            config.get('point_value', 1.0)
        )
        return {
            'lots': lots,
            'risk_amount': account_balance * (risk_percent / 100),
            'margin_required': 0,
            'source': 'config_fallback'
        }
    
    # Use broker data
    point_value = specs['point_value']
    risk_amount = account_balance * (risk_percent / 100)
    margin_per_lot = specs['margin_per_lot']
    
    # Calculate lots based on risk
    lots = calculate_position_size(
        account_balance, risk_percent, sl_points, point_value,
        specs['volume_min'], specs['volume_max'], specs['volume_step']
    )
    
    margin_required = margin_per_lot * lots
    
    # Use free_margin if provided, else use account_balance
    available_margin = free_margin if free_margin is not None else account_balance
    max_margin_allowed = available_margin * (max_margin_percent / 100)
    
    # Check if margin exceeds limit - reduce lots if needed
    margin_reduced = False
    original_lots = lots
    
    if margin_required > max_margin_allowed and margin_per_lot > 0:
        # Reduce lots to fit within margin limit
        max_lots_by_margin = max_margin_allowed / margin_per_lot
        
        # Round down to volume_step
        max_lots_by_margin = int(max_lots_by_margin / specs['volume_step']) * specs['volume_step']
        
        # Apply minimum
        max_lots_by_margin = max(specs['volume_min'], max_lots_by_margin)
        
        if max_lots_by_margin < lots:
            lots = max_lots_by_margin
            margin_required = margin_per_lot * lots
            margin_reduced = True
            
            # Recalculate actual risk with reduced lots
            actual_risk = lots * sl_points * point_value
            actual_risk_percent = (actual_risk / account_balance) * 100
    
    return {
        'lots': lots,
        'risk_amount': risk_amount,
        'risk_per_lot': sl_points * point_value,
        'point_value': point_value,
        'margin_required': margin_required,
        'margin_per_lot': margin_per_lot,
        'contract_size': specs['contract_size'],
        'volume_min': specs['volume_min'],
        'volume_max': specs['volume_max'],
        'source': 'broker_live',
        'margin_reduced': margin_reduced,
        'original_lots': original_lots if margin_reduced else lots,
        'max_margin_percent': max_margin_percent,
        'available_margin': available_margin
    }


if __name__ == '__main__':
    print("FundedNext Stellar-2 Configuration")
    print("=" * 50)
    print(f"\nAccount Rules:")
    for key, value in FUNDEDNEXT_RULES.items():
        print(f"  {key}: {value}")
    
    print(f"\nProfitable Indices:")
    for symbol, config in PROFITABLE_INDICES.items():
        print(f"\n  {symbol} ({config['description']}):")
        print(f"    Contract Size: {config['contract_size']}")
        print(f"    SL Buffer: {config['sl_buffer']} pts")
        print(f"    Market Hours: {config['market_open_hour']:02d}:{config['market_open_minute']:02d} - {config['market_close_hour']:02d}:{config['market_close_minute']:02d}")
