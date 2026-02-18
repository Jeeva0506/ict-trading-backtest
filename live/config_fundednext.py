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

# Load environment variables from .env file
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

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
    'max_trades_per_day': 3,
    'news_buffer_minutes': int(os.getenv('NEWS_BUFFER_MINUTES', '30')),
}

# ============================================================================
# ADAPTIVE RISK STRATEGY (For Evaluation Phase - Fast Pass)
# ============================================================================
# Start with aggressive 1.5% risk, switch to 1% when drawdown exceeds threshold
# $6,000 Account with 8% DD limit: Use 2% DD threshold for safety
# Backtest Results: 50.4% return, 7.2% max DD, 8% target in 5 trades
ADAPTIVE_RISK = {
    'enabled': True,                # Set False for funded account (use fixed 1.5%)
    'default_risk_percent': 1.5,    # Risk when account is healthy
    'reduced_risk_percent': 1.0,    # Conservative risk when in drawdown
    'drawdown_threshold': 2.0,      # Switch to reduced risk at 2% DD (safer for 8% limit)
    'recovery_threshold': 0.5,      # Switch back to default when DD < 0.5%
}

# ============================================================================
# NOTIFICATION SETTINGS (from .env)
# ============================================================================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')

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
        'point_value': 1.0,         # $ per point per lot (fallback; live uses broker data)
        'sl_buffer': 1.0,           # SL buffer in points
        'min_sl_distance': 2.0,     # Min SL distance for valid trade
        'max_sl_distance': 100.0,   # Max SL distance for valid trade
        
        # ORB strategy params (Candidate #12 — optimized for frequency)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.3,
        'body_min': 0.60,
        'close_strength': 0.25,
        'breakout_margin': 0.05,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 2.0,
        'max_sl_points': 100.0,
        
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
        'point_value': 1.0,
        'sl_buffer': 5.0,           # Buffer for ~24000 index (matched to backtest)
        'min_sl_distance': 10.0,
        'max_sl_distance': 200.0,
        
        # ORB strategy params (Candidate #12 — optimized for frequency)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.3,
        'body_min': 0.60,
        'close_strength': 0.25,
        'breakout_margin': 0.05,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 10.0,
        'max_sl_points': 200.0,
        
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
    
    # FRA40 removed - not backtested with ORB strategy
    # 'FRA40': { ... },
    
    # HK50 removed - negative P&L after costs in 1-year backtest (-$4,018)
    # 'HK50': { ... },
    
    'NDX100': {
        'symbol': 'NDX100',
        'description': 'Nasdaq 100 CFD',
        'contract_size': 10,
        'tick_size': 0.01,
        'tick_value': 0.01,
        'currency': 'USD',
        'point_value': 1.0,
        'sl_buffer': 5.0,
        'min_sl_distance': 10.0,
        'max_sl_distance': 200.0,
        
        # ORB strategy params (Candidate #12 — optimized for frequency)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.3,
        'body_min': 0.60,
        'close_strength': 0.25,
        'breakout_margin': 0.05,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 10.0,
        'max_sl_points': 200.0,
        
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
        'point_value': 1.0,
        'sl_buffer': 5.0,           # Dow has larger point movements
        'min_sl_distance': 10.0,
        'max_sl_distance': 200.0,
        
        # ORB strategy params (Candidate #12 — optimized for frequency)
        'orb_minutes': 15,
        'entry_hours': 2,
        'breakout_window_minutes': 60,
        'disp_mult': 1.3,
        'body_min': 0.60,
        'close_strength': 0.25,
        'breakout_margin': 0.05,
        'orb_range_min_mult': 0.5,
        'min_sl_points': 10.0,
        'max_sl_points': 200.0,
        
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
}

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
    
    # Round down to volume_step
    lots = int(lots / volume_step) * volume_step
    
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
