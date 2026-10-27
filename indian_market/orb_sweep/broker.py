"""Zerodha Kite Connect broker abstraction."""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from orb_sweep.config import TradingConfig, PROJECT_ROOT

logger = logging.getLogger(__name__)

ENV_ZERODHA = PROJECT_ROOT.parent / "backtest_engine" / ".env.zerodha"
ENV_INDIAN = PROJECT_ROOT / ".env.indian"


def _load_env() -> dict:
    env = {}
    for fpath in (ENV_ZERODHA, ENV_INDIAN):
        if fpath.exists():
            for line in fpath.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


class ZerodhaBroker:
    """Wrapper around KiteConnect for data fetching and order management."""

    def __init__(self, config: TradingConfig):
        self.cfg = config
        self._kite = None
        self._instruments_cache: Optional[pd.DataFrame] = None

    # ── Connection ─────────────────────────────────────────────────────────
    def connect(self):
        from kiteconnect import KiteConnect
        env = _load_env()
        api_key = env.get("ZERODHA_API_KEY", "")
        access_token = env.get("ZERODHA_ACCESS_TOKEN", "")
        if not api_key or not access_token:
            raise ValueError(
                "ZERODHA_API_KEY / ZERODHA_ACCESS_TOKEN missing. "
                "Run zerodha_auth.py first."
            )
        self._kite = KiteConnect(api_key=api_key)
        self._kite.set_access_token(access_token)
        profile = self._kite.profile()
        logger.info(f"Connected to Zerodha as {profile['user_name']}")

    @property
    def kite(self):
        if self._kite is None:
            self.connect()
        return self._kite

    # ── Historical Data ────────────────────────────────────────────────────
    def fetch_bars(self, token: int, interval: str,
                   days_back: int = 10) -> pd.DataFrame:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days_back)
        try:
            rows = self.kite.historical_data(
                token, start_dt, end_dt, interval,
                continuous=False, oi=False,
            )
        except Exception as e:
            logger.error(f"historical_data({token}, {interval}) failed: {e}")
            return pd.DataFrame()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).rename(columns={
            "date": "datetime", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        })
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime")[["Open", "High", "Low", "Close", "Volume"]]
        df.index = df.index.tz_localize(None)
        return df.sort_index()

    def fetch_1m(self, days_back: int = 5) -> pd.DataFrame:
        return self.fetch_bars(self.cfg.nifty_token, "minute", days_back)

    def fetch_5m(self, days_back: int = 10) -> pd.DataFrame:
        return self.fetch_bars(self.cfg.nifty_token, "5minute", days_back)

    def fetch_vix(self) -> float:
        try:
            rows = self.kite.historical_data(
                self.cfg.vix_token,
                datetime.now() - timedelta(days=5),
                datetime.now(),
                "day", continuous=False, oi=False,
            )
            if rows:
                return float(rows[-1]["close"])
        except Exception as e:
            logger.warning(f"VIX fetch failed: {e}")
        return 15.0

    # ── LTP ────────────────────────────────────────────────────────────────
    def ltp(self, exchange: str, tradingsymbol: str) -> float:
        key = f"{exchange}:{tradingsymbol}"
        try:
            data = self.kite.ltp(key)
            return float(data[key]["last_price"])
        except Exception as e:
            logger.error(f"LTP fetch failed for {key}: {e}")
            return 0.0

    def spot_ltp(self) -> float:
        return self.ltp("NSE", "NIFTY 50")

    # ── Instrument Lookup ──────────────────────────────────────────────────
    def _load_instruments(self):
        if self._instruments_cache is None:
            instruments = self.kite.instruments(self.cfg.exchange)
            self._instruments_cache = pd.DataFrame(instruments)
        return self._instruments_cache

    def find_option_token(self, strike: int, option_type: str,
                          expiry_date=None) -> dict:
        """
        Find the instrument token for a NIFTY weekly option.
        option_type: 'CE' or 'PE'
        Returns dict with tradingsymbol, instrument_token, lot_size.
        """
        df = self._load_instruments()
        mask = (
            (df["name"] == "NIFTY") &
            (df["strike"] == strike) &
            (df["instrument_type"] == option_type) &
            (df["segment"] == "NFO-OPT")
        )
        matches = df[mask].copy()
        if matches.empty:
            raise ValueError(f"No option found: NIFTY {strike} {option_type}")

        matches["expiry"] = pd.to_datetime(matches["expiry"])
        if expiry_date:
            matches = matches[matches["expiry"] >= pd.Timestamp(expiry_date)]

        matches = matches.sort_values("expiry")
        row = matches.iloc[0]
        return {
            "tradingsymbol": row["tradingsymbol"],
            "instrument_token": int(row["instrument_token"]),
            "lot_size": int(row["lot_size"]),
            "expiry": row["expiry"],
        }

    # ── Order Placement ────────────────────────────────────────────────────
    def place_buy(self, tradingsymbol: str, quantity: int) -> str:
        if self.cfg.paper_mode:
            logger.info(f"[PAPER] BUY {quantity} x {tradingsymbol}")
            return "PAPER_ORDER"
        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.cfg.exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=self.kite.TRANSACTION_TYPE_BUY,
            quantity=quantity,
            product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET,
        )
        logger.info(f"BUY order placed: {order_id} | {quantity} x {tradingsymbol}")
        return str(order_id)

    def place_sell(self, tradingsymbol: str, quantity: int) -> str:
        if self.cfg.paper_mode:
            logger.info(f"[PAPER] SELL {quantity} x {tradingsymbol}")
            return "PAPER_ORDER"
        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.cfg.exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=quantity,
            product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET,
        )
        logger.info(f"SELL order placed: {order_id} | {quantity} x {tradingsymbol}")
        return str(order_id)

    # ── Position / Order Queries (used by safety module) ──────────────────
    def _get_positions(self) -> list:
        """Fetch net positions from broker. Returns list of position dicts."""
        if self.cfg.paper_mode:
            return []
        try:
            positions = self.kite.positions()
            return positions.get("net", [])
        except Exception as e:
            logger.error(f"Positions fetch failed: {e}")
            raise

    def _get_orders(self) -> list:
        """Fetch today's orders from broker."""
        if self.cfg.paper_mode:
            return []
        try:
            return self.kite.orders()
        except Exception as e:
            logger.error(f"Orders fetch failed: {e}")
            raise

    def _cancel_order(self, order_id: str):
        """Cancel a pending order."""
        if self.cfg.paper_mode:
            logger.info(f"[PAPER] CANCEL order {order_id}")
            return
        try:
            self.kite.cancel_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id,
            )
            logger.info(f"Order {order_id} cancelled")
        except Exception as e:
            logger.error(f"Cancel order {order_id} failed: {e}")
            raise
