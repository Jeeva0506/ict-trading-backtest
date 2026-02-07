from pathlib import Path
from typing import Optional, Tuple
import pandas as pd
import yaml
from datetime import datetime
import pytz
from indicators.indicators import calculate_cvd

class DataLoader:
    def __init__(self, config_path: Path, mode: str = 'nifty'):
        """
        Initialize DataLoader with market mode support.
        
        Args:
            config_path: Path to config.yaml
            mode: 'nifty', 'forex', or 'spx500'
        """
        self.raw_config = self._load_config(config_path)
        self.mode = mode
        
        # Get mode-specific config
        mode_config = self.raw_config.get(mode, {})
        self.config = mode_config
        
        self.file_path = (Path(__file__).parent.parent / mode_config['data']['file_path']).resolve()
        self.date_column = 'Date'
        self.time_column = 'Time'
        self.start_date = mode_config['data']['start_date']
        self.end_date = mode_config['data']['end_date']
        self.timeframe = mode_config['data'].get('timeframe', '1min')
        self.timezone = mode_config.get('backtest', {}).get('timezone', None)

    def _load_config(self, config_path: Path) -> dict:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)

    def load_data(self) -> pd.DataFrame:
        """Load and preprocess market data based on mode."""
        data_config = self.config.get('data', {})
        has_header = data_config.get('has_header', True)
        separator = data_config.get('separator', 'comma')
        
        # Determine separator
        sep = '\t' if separator == 'tab' else ','
        
        if self.file_path.suffix in ['.xlsx', '.xls']:
            df = pd.read_excel(self.file_path, engine='openpyxl')
        elif self.file_path.suffix == '.csv':
            # Try different encodings
            encodings = ['utf-8', 'utf-16', 'latin-1', 'cp1252']
            df = None
            for encoding in encodings:
                try:
                    if has_header:
                        df = pd.read_csv(self.file_path, sep=sep, encoding=encoding)
                    else:
                        # No header - use predefined column names for FOREX data
                        # Format: Date Time Open High Low Close Volume
                        df = pd.read_csv(
                            self.file_path, 
                            sep=sep, 
                            header=None,
                            names=['datetime_str', 'open', 'high', 'low', 'close', 'volume'],
                            encoding=encoding
                        )
                    break
                except UnicodeDecodeError:
                    continue
            if df is None:
                raise ValueError(f"Could not decode file with any of: {encodings}")
        else:
            raise ValueError('Unsupported file format')
        
        # Standardize column names to lowercase for compatibility
        df.columns = [col.lower() for col in df.columns]
        
        # Handle datetime parsing based on data format
        if 'datetime_str' in df.columns:
            # FOREX format: "2024.01.02 00:00"
            df['datetime'] = pd.to_datetime(df['datetime_str'], format='%Y.%m.%d %H:%M')
        elif 'datetime' in df.columns:
            # Data already has datetime column
            df['datetime'] = pd.to_datetime(df['datetime'])
        elif 'date' in df.columns and 'time' in df.columns:
            # Separate date and time columns (NIFTY format)
            df['datetime'] = pd.to_datetime(df['date'] + ' ' + df['time'], dayfirst=True)
        elif 'date' in df.columns:
            # Only date column, might contain datetime (FOREX format)
            df['datetime'] = pd.to_datetime(df['date'])
        elif 'time' in df.columns:
            # SPX500/MT4 format: 'time' column contains full datetime like "2025.10.24 03:43"
            try:
                df['datetime'] = pd.to_datetime(df['time'], format='%Y.%m.%d %H:%M')
            except:
                df['datetime'] = pd.to_datetime(df['time'])
        else:
            raise ValueError("Could not find datetime column. Expected 'datetime', 'date', or 'date' + 'time'")
        
        # Convert timezone for MT4 data (EET/EEST) to local market time
        # Skip conversion if timezone is already "EET" (Option A: use broker time directly)
        mt4_markets = ['spx500', 'fra40', 'ger30', 'hk50', 'jp225', 'ndx100', 'uk100', 'us30']
        if self.mode in mt4_markets and self.timezone != 'EET':
            df = self._convert_mt4_to_local_time(df)
        elif self.mode in mt4_markets:
            print(f"   📍 Using EET broker time directly (no conversion)")
        
        # Filter by date range
        mask = (
            (df['datetime'] >= pd.to_datetime(self.start_date)) &
            (df['datetime'] <= pd.to_datetime(self.end_date))
        )
        df = df.loc[mask].reset_index(drop=True)
        
        # For FOREX, add previous day high/low
        if self.mode == 'forex':
            df = self._add_pdh_pdl(df)
        
        # For all MT4 markets, calculate CVD (Cumulative Volume Delta)
        mt4_markets = ['spx500', 'fra40', 'ger30', 'hk50', 'jp225', 'ndx100', 'uk100', 'us30']
        if self.mode in mt4_markets and 'volume' in df.columns:
            df['cvd'] = calculate_cvd(df, reset_daily=True)
            print(f"   📊 CVD calculated (resets daily)")
        
        return df
    
    def _convert_mt4_to_local_time(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Convert MT4/MT5 broker time (EET/EEST) to local market time.
        
        MT4 brokers typically use Eastern European Time:
        - EET (UTC+2) in winter
        - EEST (UTC+3) in summer
        """
        # Define timezones
        eet_tz = pytz.timezone('Europe/Helsinki')  # EET/EEST
        
        # Get target timezone from config
        target_tz_name = self.timezone or 'America/New_York'
        target_tz = pytz.timezone(target_tz_name)
        
        # Localize to EET (broker time) then convert to target timezone
        # This handles DST automatically for both timezones
        df['datetime'] = df['datetime'].apply(
            lambda x: eet_tz.localize(x).astimezone(target_tz).replace(tzinfo=None)
        )
        
        print(f"   📍 Converted timezone: EET/EEST → {target_tz_name}")
        
        return df
    
    def _add_pdh_pdl(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add Previous Day High (PDH) and Previous Day Low (PDL) for FOREX.
        These are used as targets for shorts (PDL) and longs (PDH).
        """
        df['date_only'] = df['datetime'].dt.date
        
        # Calculate daily high/low
        daily_hl = df.groupby('date_only').agg({
            'high': 'max',
            'low': 'min'
        }).reset_index()
        daily_hl.columns = ['date_only', 'day_high', 'day_low']
        
        # Shift to get previous day
        daily_hl['pdh'] = daily_hl['day_high'].shift(1)
        daily_hl['pdl'] = daily_hl['day_low'].shift(1)
        
        # Merge back to main dataframe
        df = df.merge(daily_hl[['date_only', 'pdh', 'pdl']], on='date_only', how='left')
        
        # Fill NaN (first day) with current day values
        df['pdh'] = df['pdh'].fillna(df.groupby('date_only')['high'].transform('max'))
        df['pdl'] = df['pdl'].fillna(df.groupby('date_only')['low'].transform('min'))
        
        return df
