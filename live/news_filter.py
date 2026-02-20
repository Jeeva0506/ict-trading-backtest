"""
Economic News Filter for High-Impact Events
============================================
Fetches news from Forex Factory and blocks trades around high-impact events.

TIMEZONE FIX (2026-02-20): Forex Factory dates are in EST/EDT with offset.
Previous code stripped the offset and compared raw EST hours against UTC,
causing events to appear ~5 hours early. Now we properly convert to UTC.

BUFFER FIX (2026-02-20): Changed from symmetric 30min/30min to asymmetric
5 minutes BEFORE news and 15 minutes AFTER news. This avoids blocking
the entire ORB entry window when multiple events cluster.
"""
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
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

    # Symbol to currency mapping — only backtested indices
    SYMBOL_CURRENCIES = {
        'SPX500': ['USD'],
        'NDX100': ['USD'],
        'US30': ['USD'],
        'GER30': ['EUR', 'USD'],  # Also affected by USD news
    }

    def __init__(self, buffer_before_minutes: int = 5,
                 buffer_after_minutes: int = 15,
                 cache_minutes: int = 15,
                 # Legacy kwarg kept so existing callers don't break
                 buffer_minutes: int = None):
        # New asymmetric buffers: 5 min before, 15 min after
        self.buffer_before = buffer_before_minutes
        self.buffer_after = buffer_after_minutes
        self.cache_minutes = cache_minutes
        self.cache = []
        self.last_fetch = None
        self.cache_file = Path(__file__).parent / 'logs' / 'news_cache.json'
        # C4 FIX: Load cached news from disk on startup (survives restarts + API downtime)
        self._load_cache_from_disk()

    # ── Helper: parse event time to UTC ─────────────────────────────────
    @staticmethod
    def _parse_event_utc(event_time_str: str) -> Optional[datetime]:
        """
        Parse Forex Factory date string and return a naive UTC datetime.
        FF dates look like '2026-02-20T08:30:00-05:00' (EST offset).
        We convert to UTC so all comparisons use one clock.
        """
        try:
            aware = datetime.strptime(event_time_str, '%Y-%m-%dT%H:%M:%S%z')
            utc_naive = aware.astimezone(timezone.utc).replace(tzinfo=None)
            return utc_naive
        except Exception:
            return None

    def _load_cache_from_disk(self):
        """Load news cache from disk if available and recent (< 6 hours old)."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                fetch_time_str = data.get('fetch_time', '')
                events = data.get('events', [])
                if events and fetch_time_str:
                    fetch_time = datetime.strptime(fetch_time_str[:19], '%Y-%m-%d %H:%M:%S')
                    age_hours = (datetime.now() - fetch_time).total_seconds() / 3600
                    if age_hours < 6:  # Only use cache if < 6 hours old
                        self.cache = events
                        self.last_fetch = fetch_time
                        logger.info(f"Loaded {len(events)} cached news events from disk ({age_hours:.1f}h old)")
                    else:
                        logger.info(f"Disk news cache too old ({age_hours:.1f}h), will fetch fresh")
        except Exception as e:
            logger.warning(f"Could not load news cache from disk: {e}")

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
            except Exception:
                pass

            logger.info(f"Fetched {len(events)} economic events")

        return self.cache

    def get_upcoming_high_impact(self, symbol: str) -> List[dict]:
        """
        Get high-impact events within the no-trade window for a symbol.

        Window: [event_time - buffer_before, event_time + buffer_after]
        Default: 5 min before → 15 min after.
        Only HIGH impact events from Forex Factory are considered.
        """
        events = self.fetch_news()
        currencies = self.SYMBOL_CURRENCIES.get(symbol, ['USD'])
        now_utc = datetime.utcnow()

        high_impact = []

        for event in events:
            try:
                # Parse event time and convert to UTC (TIMEZONE FIX)
                event_time_str = event.get('date', '')
                event_utc = self._parse_event_utc(event_time_str)
                if event_utc is None:
                    continue

                # Only high impact
                impact = event.get('impact', '').lower()
                if impact != 'high':
                    continue

                event_currency = event.get('country', '')
                event_title = event.get('title', '')

                if event_currency not in currencies:
                    continue

                # Minutes until event (positive = future, negative = past)
                minutes_to_event = (event_utc - now_utc).total_seconds() / 60

                # Asymmetric window: block from buffer_before mins BEFORE
                # to buffer_after mins AFTER the event
                if -self.buffer_after <= minutes_to_event <= self.buffer_before:
                    high_impact.append({
                        'title': event_title,
                        'time': event_utc.strftime('%Y-%m-%d %H:%M UTC'),
                        'currency': event_currency,
                        'minutes_away': minutes_to_event,
                        'impact': impact
                    })

            except Exception:
                continue

        return high_impact

    def is_news_time(self, symbol: str) -> Tuple[bool, Optional[dict]]:
        """
        Check if it's news time for a symbol.

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
        """Get all high-impact events for today (UTC date)"""
        events = self.fetch_news()
        today_utc = datetime.utcnow().date()

        today_events = []
        for event in events:
            try:
                event_time_str = event.get('date', '')
                event_utc = self._parse_event_utc(event_time_str)
                if event_utc is None:
                    continue

                if event_utc.date() == today_utc and event.get('impact', '').lower() == 'high':
                    today_events.append({
                        'time': event_utc.strftime('%H:%M UTC'),
                        'currency': event.get('country', ''),
                        'title': event.get('title', ''),
                    })
            except Exception:
                continue

        return sorted(today_events, key=lambda x: x['time'])


def test_news_filter():
    """Test the news filter"""
    calendar = EconomicCalendar(buffer_before_minutes=5, buffer_after_minutes=15)

    print("Testing Economic Calendar (5min before / 15min after)")
    print("=" * 60)

    # Fetch news
    events = calendar.fetch_news()
    print(f"Total events fetched: {len(events)}")

    # Check each symbol
    for symbol in ['SPX500', 'GER30', 'NDX100', 'US30']:
        is_news, event = calendar.is_news_time(symbol)
        if is_news:
            print(f"\n{symbol}: NEWS TIME")
            print(f"  Event: {event['title']}")
            print(f"  Time: {event['time']}")
            print(f"  Minutes away: {event['minutes_away']:.0f}")
        else:
            print(f"\n{symbol}: No high-impact news nearby")

    # Show today's events
    print("\n\nToday's High-Impact Events (UTC):")
    print("-" * 60)
    today_events = calendar.get_all_today_events()
    if today_events:
        for event in today_events:
            print(f"  {event['time']} | {event['currency']} | {event['title']}")
    else:
        print("  No high-impact events today")


if __name__ == '__main__':
    test_news_filter()
