"""
FundedNext Stellar-2 Live Trading Configuration
================================================
Account Rules loaded from .env file:
- Max Drawdown: 10%
- Daily Drawdown: 3% (Stop at 2% to be safe)
- Risk per trade: 1%
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
        'point_value': 1.0,         # $ per point per lot
        'sl_buffer': 1.0,           # SL buffer in points
        'min_sl_distance': 2.0,     # Min SL distance for valid trade
        'max_sl_distance': 25.0,    # Max SL distance for valid trade
        
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
        'sl_buffer': 2.0,           # Buffer for ~24000 index
        'min_sl_distance': 2.0,
        'max_sl_distance': 25.0,
        
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
        'point_value': 1.0,
        'sl_buffer': 1.0,           # Buffer for ~8000 index
        'min_sl_distance': 2.0,
        'max_sl_distance': 25.0,
        
        'market_open_hour': 9,
        'market_open_minute': 0,
        'market_close_hour': 17,
        'market_close_minute': 30,
        'force_close_hour': 17,
        'force_close_minute': 15,
        
        'mt5_open_hour': 10,
        'mt5_open_minute': 0,
        'mt5_close_hour': 18,
        'mt5_close_minute': 30,
        
        'timezone': 'Europe/Paris',
    },
    
    'HK50': {
        'symbol': 'HK50',
        'description': 'Hang Seng 50 CFD',
        'contract_size': 10,
        'tick_size': 1.0,
        'tick_value': 0.01,
        'currency': 'HKD',
        'point_value': 1.0,
        'sl_buffer': 2.5,           # Buffer for ~26000 index
        'min_sl_distance': 2.0,
        'max_sl_distance': 25.0,
        
        'market_open_hour': 9,
        'market_open_minute': 30,
        'market_close_hour': 16,
        'market_close_minute': 0,
        'force_close_hour': 15,
        'force_close_minute': 50,
        
        # Lunch break
        'lunch_start_hour': 12,
        'lunch_start_minute': 0,
        'lunch_end_hour': 13,
        'lunch_end_minute': 0,
        
        # MT5 Time (HK is UTC+8, EET is UTC+2/3)
        'mt5_open_hour': 3,         # 9:30 HKT = 3:30 EET
        'mt5_open_minute': 30,
        'mt5_close_hour': 10,       # 16:00 HKT = 10:00 EET
        'mt5_close_minute': 0,
        
        'timezone': 'Asia/Hong_Kong',
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


def calculate_position_size(account_balance: float, risk_percent: float, 
                           sl_points: float, point_value: float) -> float:
    """
    Calculate position size based on risk
    
    Args:
        account_balance: Current account balance
        risk_percent: Risk percentage (e.g., 1.0 for 1%)
        sl_points: Stop loss distance in points
        point_value: Value per point per lot
    
    Returns:
        Position size in lots
    """
    risk_amount = account_balance * (risk_percent / 100)
    risk_per_lot = sl_points * point_value
    
    if risk_per_lot <= 0:
        return 0
    
    lots = risk_amount / risk_per_lot
    
    # Round down to 2 decimal places (0.01 lot minimum)
    lots = int(lots * 100) / 100
    
    return max(0.01, lots)


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
