"""
Economic News Filter for High-Impact Events
============================================
Fetches news from multiple sources and filters trades during high-impact events
"""
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class EconomicCalendar:
    """Fetches and manages economic calendar events"""
    
    # Multiple news sources for redundancy
    SOURCES = {
        'forex_factory': "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    }
    
    # Events to avoid for each currency
    HIGH_IMPACT_EVENTS = {
        'USD': [
            'Non-Farm Payrolls', 'NFP', 'FOMC', 'Fed Interest Rate Decision',
            'Fed Chair Powell Speaks', 'CPI', 'Core CPI', 'PPI',
            'GDP', 'Advance GDP', 'Unemployment Claims', 'Retail Sales',
            'ISM Manufacturing PMI', 'ISM Services PMI', 'Durable Goods Orders'
        ],
        'EUR': [
            'ECB Interest Rate Decision', 'ECB Press Conference', 'ECB President Lagarde Speaks',
            'German CPI', 'German GDP', 'German ZEW Economic Sentiment',
            'Eurozone CPI', 'Eurozone GDP', 'Eurozone Retail Sales'
        ],
        'GBP': [
            'BOE Interest Rate Decision', 'BOE Gov Bailey Speaks',
            'UK CPI', 'UK GDP', 'UK Retail Sales', 'UK Unemployment Rate'
        ],
        'JPY': [
            'BOJ Interest Rate Decision', 'BOJ Policy Rate',
            'Japanese CPI', 'Japanese GDP', 'Tankan Manufacturing Index'
        ],
        'HKD': [
            'Hong Kong GDP', 'Hong Kong CPI', 'Hong Kong Trade Balance'
        ],
        'CNY': [
            'Chinese CPI', 'Chinese GDP', 'Chinese Trade Balance',
            'Chinese Manufacturing PMI', 'PBOC Interest Rate Decision'
        ]
    }
    
    # Symbol to currency mapping
    SYMBOL_CURRENCIES = {
        'SPX500': ['USD'],
        'GER30': ['EUR', 'USD'],  # Also affected by USD news
        'FRA40': ['EUR', 'USD'],
        'HK50': ['HKD', 'CNY', 'USD'],  # China news affects HK
        'UK100': ['GBP', 'USD'],
        'JP225': ['JPY', 'USD'],
    }
    
    def __init__(self, buffer_minutes: int = 30, cache_minutes: int = 15):
        self.buffer_minutes = buffer_minutes
        self.cache_minutes = cache_minutes
        self.cache = []
        self.last_fetch = None
        self.cache_file = Path(__file__).parent / 'logs' / 'news_cache.json'
    
    def _fetch_forex_factory(self) -> List[dict]:
        """Fetch from Forex Factory"""
        try:
            response = requests.get(self.SOURCES['forex_factory'], timeout=10)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            logger.error(f"Forex Factory fetch failed: {e}")
        return []
    
    def fetch_news(self) -> List[dict]:
        """Fetch news from all sources"""
        # Check cache
        if self.last_fetch:
            cache_age = (datetime.now() - self.last_fetch).total_seconds() / 60
            if cache_age < self.cache_minutes:
                return self.cache
        
        # Fetch fresh data
        events = self._fetch_forex_factory()
        
        if events:
            self.cache = events
            self.last_fetch = datetime.now()
            
            # Save to file
            try:
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.cache_file, 'w') as f:
                    json.dump({
                        'fetch_time': str(self.last_fetch),
                        'events': events
                    }, f)
            except:
                pass
            
            logger.info(f"Fetched {len(events)} economic events")
        
        return self.cache
    
    def get_upcoming_high_impact(self, symbol: str, 
                                  minutes_ahead: int = None) -> List[dict]:
        """
        Get upcoming high-impact events for a symbol
        
        Args:
            symbol: Trading symbol (e.g., 'SPX500')
            minutes_ahead: How far ahead to look (default: buffer_minutes)
        """
        if minutes_ahead is None:
            minutes_ahead = self.buffer_minutes
        
        events = self.fetch_news()
        currencies = self.SYMBOL_CURRENCIES.get(symbol, ['USD'])
        now = datetime.utcnow()
        
        high_impact = []
        
        for event in events:
            try:
                # Parse event time
                event_time_str = event.get('date', '')
                event_time = datetime.strptime(event_time_str, '%Y-%m-%dT%H:%M:%S%z')
                event_time = event_time.replace(tzinfo=None)
                
                # Check if high impact
                impact = event.get('impact', '').lower()
                event_currency = event.get('country', '')
                event_title = event.get('title', '')
                
                if impact != 'high':
                    continue
                
                if event_currency not in currencies:
                    continue
                
                # Check time window (before and after)
                time_to_event = (event_time - now).total_seconds() / 60
                
                # Within buffer window (before or after)
                if -self.buffer_minutes <= time_to_event <= minutes_ahead:
                    high_impact.append({
                        'title': event_title,
                        'time': str(event_time),
                        'currency': event_currency,
                        'minutes_away': time_to_event,
                        'impact': impact
                    })
                    
            except Exception as e:
                continue
        
        return high_impact
    
    def is_news_time(self, symbol: str) -> tuple:
        """
        Check if it's news time for a symbol
        
        Returns:
            (is_news: bool, event_details: dict or None)
        """
        upcoming = self.get_upcoming_high_impact(symbol)
        
        if upcoming:
            # Get the nearest event
            nearest = min(upcoming, key=lambda x: abs(x['minutes_away']))
            return True, nearest
        
        return False, None
    
    def get_all_today_events(self) -> List[dict]:
        """Get all high-impact events for today"""
        events = self.fetch_news()
        today = datetime.utcnow().date()
        
        today_events = []
        for event in events:
            try:
                event_time_str = event.get('date', '')
                event_time = datetime.strptime(event_time_str, '%Y-%m-%dT%H:%M:%S%z')
                
                if event_time.date() == today and event.get('impact', '').lower() == 'high':
                    today_events.append({
                        'time': event_time.strftime('%H:%M'),
                        'currency': event.get('country', ''),
                        'title': event.get('title', ''),
                    })
            except:
                continue
        
        return sorted(today_events, key=lambda x: x['time'])


def test_news_filter():
    """Test the news filter"""
    calendar = EconomicCalendar(buffer_minutes=30)
    
    print("Testing Economic Calendar")
    print("=" * 50)
    
    # Fetch news
    events = calendar.fetch_news()
    print(f"Total events fetched: {len(events)}")
    
    # Check each symbol
    for symbol in ['SPX500', 'GER30', 'FRA40', 'HK50']:
        is_news, event = calendar.is_news_time(symbol)
        if is_news:
            print(f"\n{symbol}: ⚠️ NEWS TIME")
            print(f"  Event: {event['title']}")
            print(f"  Time: {event['time']}")
            print(f"  Minutes away: {event['minutes_away']:.0f}")
        else:
            print(f"\n{symbol}: ✅ No high-impact news")
    
    # Show today's events
    print("\n\nToday's High-Impact Events:")
    print("-" * 50)
    today_events = calendar.get_all_today_events()
    if today_events:
        for event in today_events:
            print(f"  {event['time']} | {event['currency']} | {event['title']}")
    else:
        print("  No high-impact events today")


if __name__ == '__main__':
    test_news_filter()
