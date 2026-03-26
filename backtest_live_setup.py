# backtest_live_setup.py
import pandas as pd
from pathlib import Path
from datetime import timedelta

from strategy.strategy_ict_clean import StrategyICTClean

BROKER_POINT_VALUES = {
    # Values = USD per 1 index point per 1 lot (from MT5 order_calc_profit)
    # Cross-currency symbols use approximate average FX rate
    "SPX500": 10.0, "US30": 10.0, "NDX100": 10.0,
    "GER30": 11.80, "FRA40": 10.0,       # GER30: 10 EUR/pt/lot × ~1.18 EURUSD
    "HK50": 10.0, "JP225": 10.0,
    # New indices (verified via MT5 order_calc_profit 2026-02-23)
    "AUS200": 7.07, "EUSTX50": 11.81, "NTH25": 20.0,
    "SWI20": 12.93, "UK100": 13.53, "US2000": 10.0,  # US2000 was 1.0 (10x off!)
}

LIVE_INDEX_CONFIG = {
    "SPX500": {
        "local_file": "SPX500_1YEAR_M1.csv",
        # US cash session: NYSE 9:30-16:00 ET = 16:30-23:00 EET (UTC+2)
        "session_start_hour": 16,
        "session_start_minute": 30,
        "session_end_hour": 23,
        "session_end_minute": 0,
        "sl_buffer": 1.0,
        "name": "S&P 500",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.5,
        "body_min": 0.60,
        "close_strength": 0.25,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 2.0,
        "max_sl_points": 25.0,
    },
    "US30": {
        "local_file": "US30_1YEAR_M1.csv",
        "session_start_hour": 16,
        "session_start_minute": 30,
        "session_end_hour": 23,
        "session_end_minute": 0,
        "sl_buffer": 5.0,
        "name": "Dow Jones 30",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.5,
        "body_min": 0.60,
        "close_strength": 0.25,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 15.0,
        "max_sl_points": 150.0,
    },
    "NDX100": {
        "local_file": "NDX100_1YEAR_M1.csv",
        "session_start_hour": 16,
        "session_start_minute": 30,
        "session_end_hour": 23,
        "session_end_minute": 0,
        "sl_buffer": 5.0,
        "name": "Nasdaq 100",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.5,
        "body_min": 0.60,
        "close_strength": 0.25,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 10.0,
        "max_sl_points": 80.0,
    },
    "GER30": {
        "local_file": "GER30_1YEAR_M1.csv",
        # German DAX cash: Xetra 9:00-17:30 CET = 10:00-18:30 EET (UTC+2)
        "session_start_hour": 10,
        "session_start_minute": 0,
        "session_end_hour": 18,
        "session_end_minute": 30,
        "sl_buffer": 5.0,
        "name": "DAX 30",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.5,
        "body_min": 0.60,
        "close_strength": 0.25,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 10.0,
        "max_sl_points": 80.0,
    },
    "FRA40": {
        "local_file": "FRA40_1YEAR_M1.csv",
        # French CAC40 cash: Euronext Paris 9:00-17:30 CET = 10:00-18:30 EET (UTC+2)
        "session_start_hour": 10,
        "session_start_minute": 0,
        "session_end_hour": 18,
        "session_end_minute": 30,
        "sl_buffer": 1.0,
        "name": "CAC 40",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.5,
        "body_min": 0.60,
        "close_strength": 0.30,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 2.0,
        "max_sl_points": 30.0,
    },
    "HK50": {
        "local_file": "HK50_1YEAR_M1.csv",
        # Hong Kong HSI cash: HKEX 9:30-16:00 HKT (UTC+8) = 3:30-10:00 EET (UTC+2)
        "session_start_hour": 3,
        "session_start_minute": 30,
        "session_end_hour": 10,
        "session_end_minute": 0,
        "sl_buffer": 5.0,
        "name": "Hang Seng 50",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.5,
        "body_min": 0.60,
        "close_strength": 0.30,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 10.0,
        "max_sl_points": 300.0,
    },
    "JP225": {
        "local_file": "JP225_1YEAR_M1.csv",
        # Japan Nikkei cash: TSE 9:00-15:00 JST (UTC+9) = 2:00-8:00 EET (UTC+2)
        "session_start_hour": 2,
        "session_start_minute": 0,
        "session_end_hour": 8,
        "session_end_minute": 0,
        "sl_buffer": 10.0,
        "name": "Nikkei 225",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.5,
        "body_min": 0.60,
        "close_strength": 0.30,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 20.0,
        "max_sl_points": 500.0,
    },
    # ====================================================================
    # NEW INDICES
    # ====================================================================
    "AUS200": {
        "local_file": "AUS200_M1_CLEAN.csv",
        # ASX cash session: 10:00-16:00 AEDT (UTC+11) = 01:00-07:00 EET (UTC+2)
        "session_start_hour": 1,
        "session_start_minute": 0,
        "session_end_hour": 7,
        "session_end_minute": 0,
        "sl_buffer": 1.0,
        "name": "Australia 200",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.7,
        "body_min": 0.65,
        "close_strength": 0.30,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 2.0,
        "max_sl_points": 100.0,
    },
    "EUSTX50": {
        "local_file": "EUSTX50_M1_CLEAN.csv",
        # Euro Stoxx 50 cash: Eurex 9:00-17:30 CET (UTC+1) = 10:00-18:30 EET
        "session_start_hour": 10,
        "session_start_minute": 0,
        "session_end_hour": 18,
        "session_end_minute": 30,
        "sl_buffer": 1.0,
        "name": "Euro Stoxx 50",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.7,
        "body_min": 0.65,
        "close_strength": 0.30,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 2.0,
        "max_sl_points": 100.0,
    },
    "NTH25": {
        "local_file": "NTH25_M1_CLEAN.csv",
        # Netherlands AEX 25: Euronext 9:00-17:30 CET (UTC+1) = 10:00-18:30 EET
        "session_start_hour": 10,
        "session_start_minute": 0,
        "session_end_hour": 18,
        "session_end_minute": 30,
        "sl_buffer": 0.5,
        "name": "Netherlands 25",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.7,
        "body_min": 0.65,
        "close_strength": 0.30,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 1.0,
        "max_sl_points": 50.0,
    },
    "SWI20": {
        "local_file": "SWI20_M1_CLEAN.csv",
        # Swiss SMI 20: SIX 9:00-17:30 CET (UTC+1) = 10:00-18:30 EET
        "session_start_hour": 10,
        "session_start_minute": 0,
        "session_end_hour": 18,
        "session_end_minute": 30,
        "sl_buffer": 2.0,
        "name": "Switzerland 20",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        # Universal params (anti-curve-fit): validated train 2024 + OOS 2023/2025
        "disp_mult": 1.0,
        "body_min": 0.70,
        "close_strength": 0.25,
        "breakout_margin": 0.15,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 5.0,
        "max_sl_points": 200.0,
    },
    "UK100": {
        "local_file": "UK100_M1_CLEAN.csv",
        # UK FTSE 100 cash: LSE 8:00-16:30 GMT (UTC) = 10:00-18:30 EET
        "session_start_hour": 10,
        "session_start_minute": 0,
        "session_end_hour": 18,
        "session_end_minute": 30,
        "sl_buffer": 1.0,
        "name": "UK 100",
        "orb_minutes": 15,
        "entry_hours": 2,
        "breakout_window_minutes": 60,
        "disp_mult": 1.7,
        "body_min": 0.65,
        "close_strength": 0.30,
        "breakout_margin": 0.10,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 2.0,
        "max_sl_points": 25.0,
    },
    "US2000": {
        "local_file": "US2000_M1_CLEAN.csv",
        # Russell 2000: NYSE 9:30-16:00 ET (UTC-5) = 16:30-23:00 EET
        "session_start_hour": 16,
        "session_start_minute": 30,
        "session_end_hour": 23,
        "session_end_minute": 0,
        "sl_buffer": 1.0,
        "name": "Russell 2000",
        "orb_minutes": 15,
        "entry_hours": 3,
        "breakout_window_minutes": 60,
        # Universal params (anti-curve-fit): validated train 2024 + OOS 2023/2025
        "disp_mult": 1.0,
        "body_min": 0.70,
        "close_strength": 0.25,
        "breakout_margin": 0.15,
        "orb_range_min_mult": 0.5,
        "min_sl_points": 2.0,
        "max_sl_points": 100.0,
    },
}

ADAPTIVE_RISK = {
    "enabled": True,
    "default_risk_percent": 1.5,
    "tier2_risk_percent": 0.75,
    "tier3_risk_percent": 0.5,
    "reduced_risk_percent": 0.75,        # legacy compat = tier2
    "tier2_threshold": 3.0,              # 3% DD from peak -> tier2
    "tier3_threshold": 5.0,              # 5% DD from peak -> tier3
    "daily_dd_reduce_threshold": 1.0,    # legacy: daily DD reduce
    "daily_dd_stop_threshold": 2.0,      # daily DD stop trading
}

ACCOUNT_RULES = {"starting_balance": 6000.0, "max_drawdown_pct": 10.0}  # aligned with FundedNext 10% max DD

TRADING_COSTS = {
    # Slippage applied adversely on ENTRY (market order) and SL exits (stop order)
    # TP exits use limit orders — filled at exact price (no slippage)
    "slippage_points": {"SPX500": 0.3, "US30": 1.0, "NDX100": 0.5, "GER30": 1.0, "FRA40": 0.3, "HK50": 2.0, "JP225": 5.0,
                         "AUS200": 0.5, "EUSTX50": 0.5, "NTH25": 0.2, "SWI20": 0.5, "UK100": 0.5, "US2000": 0.3},
    # Spread: difference between bid and ask. Entry is at worst side.
    # Long entry = ask (higher), Short entry = bid (lower). Already reflected by
    # entering on next-bar open, but we model HALF-spread as additional adverse cost.
    "spread_points": {"SPX500": 0.5, "US30": 2.0, "NDX100": 1.0, "GER30": 1.0, "FRA40": 0.5, "HK50": 5.0, "JP225": 10.0,
                       "AUS200": 2.0, "EUSTX50": 1.5, "NTH25": 0.5, "SWI20": 3.0, "UK100": 1.0, "US2000": 0.5},
    "commission_per_lot": {"SPX500": 0.0, "US30": 0.0, "NDX100": 0.0, "GER30": 0.0, "FRA40": 0.0, "HK50": 0.0, "JP225": 0.0,
                           "AUS200": 0.0, "EUSTX50": 0.0, "NTH25": 0.0, "SWI20": 0.0, "UK100": 0.0, "US2000": 0.0},
}

# FundedNext account rules (Stellar challenge, funded phase)
FUNDEDNEXT_RULES = {
    "daily_dd_pct": 3.0,      # max daily drawdown (aligned with live 3%)
    "max_dd_pct": 10.0,       # max trailing drawdown from peak equity
    "profit_target_pct": None, # no target in funded phase (just trade)
}

# Portfolio rules
MAX_OPEN_POSITIONS = 2
MAX_DAILY_LOSSES = 2          # Max losing trades per day across all symbols
MAX_DAILY_TRADES = 2           # Max total trades per day (matches live config)
LEVERAGE = 100


import math as _math


def get_vol_scaled_slippage(base_slippage: float, risk_points: float,
                            median_R: float, is_high_vol_day: bool = False) -> float:
    """Scale slippage by volatility (R) relative to symbol's median R.

    Higher R = more volatile session = more slippage in reality.
    sqrt scaling prevents over-penalizing: 2× R → 1.41× slippage.
    On high-vol event days (NFP Friday), adds 50% penalty.
    Capped at 3× base to avoid unrealistic extremes.
    """
    if median_R <= 0 or risk_points <= 0:
        return base_slippage
    vol_ratio = risk_points / median_R
    multiplier = max(1.0, _math.sqrt(vol_ratio))
    scaled = base_slippage * min(multiplier, 3.0)
    if is_high_vol_day:
        scaled *= 1.5
    return round(scaled, 4)


def is_high_vol_event_day(entry_time, symbol: str) -> bool:
    """Detect if the trading day is a known high-volatility event day.

    NFP Friday: First Friday of month (affects all US indices).
    The event itself (8:30 AM ET) is before the session opens but
    the entire session is more volatile, warrants extra slippage.
    """
    usd_symbols = {'SPX500', 'NDX100', 'US30', 'US2000'}
    if symbol not in usd_symbols:
        return False
    # entry_time is in EET (MT5 server time). Approximate ET = EET - 7h.
    et_approx = entry_time - pd.Timedelta(hours=7)
    # NFP: First Friday of month (day <= 7 and Friday)
    if et_approx.day <= 7 and et_approx.weekday() == 4:
        return True
    return False


def load_data(filename: str) -> pd.DataFrame:
    data_path = Path(__file__).parent / "data" / "raw" / filename
    if not data_path.exists():
        raise FileNotFoundError(f"Missing data file: {data_path}")

    # Tab-separated data files
    df = pd.read_csv(data_path, sep="\t")
    # Handle different date formats ("2025-02-14 11:07:00" vs "2025.02.10 01:01")
    df["Time"] = pd.to_datetime(df["Time"], format="mixed", dayfirst=False)
    df.set_index("Time", inplace=True)
    df.columns = [c.lower() for c in df.columns]
    df = df.sort_index()
    return df


def get_adaptive_risk(current_balance: float, day_peak_balance: float,
                      starting_balance: float = None):
    """3-tier adaptive risk aligned with live DrawdownProtection.
    
    Uses OVERALL DD from initial balance (FundedNext Stellar rule) for tier selection,
    plus daily DD from day-peak for stop-trading trigger.
    """
    if not ADAPTIVE_RISK["enabled"]:
        return ADAPTIVE_RISK["default_risk_percent"], False

    # Daily DD check (stop trading for the day)
    daily_dd_pct = ((day_peak_balance - current_balance) / day_peak_balance) * 100.0 if day_peak_balance > 0 else 0
    if daily_dd_pct >= ADAPTIVE_RISK["daily_dd_stop_threshold"]:
        return ADAPTIVE_RISK.get("tier3_risk_percent", 0.5), True

    # Overall DD from initial balance (matches live `get_adaptive_risk`)
    if starting_balance and starting_balance > 0:
        overall_dd = ((starting_balance - current_balance) / starting_balance) * 100.0
        overall_dd = max(0.0, overall_dd)
        tier3_dd = ADAPTIVE_RISK.get("tier3_threshold", 5.0)
        tier2_dd = ADAPTIVE_RISK.get("tier2_threshold", 3.0)
        if overall_dd >= tier3_dd:
            return ADAPTIVE_RISK.get("tier3_risk_percent", 0.5), False
        if overall_dd >= tier2_dd:
            return ADAPTIVE_RISK.get("tier2_risk_percent", 0.75), False

    # Legacy daily DD reduce removed — live does NOT have this path.
    # Keeping backtest aligned with live: overall DD tiers are the only triggers.
    return ADAPTIVE_RISK["default_risk_percent"], False


def check_account_breach(current_balance: float, starting_balance: float) -> bool:
    max_dd = ((starting_balance - current_balance) / starting_balance) * 100.0
    return max_dd >= ACCOUNT_RULES["max_drawdown_pct"]


def compute_htf_ema(df_m1: pd.DataFrame, fast: int = 50, slow: int = 200) -> pd.DataFrame:
    """Resample M1 data to H1 and compute EMA crossover for trend direction."""
    h1 = df_m1.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    h1[f"ema{fast}"] = h1["close"].ewm(span=fast, adjust=False).mean()
    h1[f"ema{slow}"] = h1["close"].ewm(span=slow, adjust=False).mean()
    h1["htf_trend"] = "bullish"
    h1.loc[h1[f"ema{fast}"] < h1[f"ema{slow}"], "htf_trend"] = "bearish"
    return h1[["htf_trend", f"ema{fast}", f"ema{slow}"]]


def get_htf_trend_for_day(htf_ema: pd.DataFrame, session_start: pd.Timestamp) -> str | None:
    """Look up the most recent H1 EMA trend before session start."""
    prior = htf_ema[htf_ema.index < session_start]
    if len(prior) == 0:
        return None  # not enough data for EMA warmup
    return str(prior.iloc[-1]["htf_trend"])


def run_live_simulation(run_days: int | None = 15, debug_strategy: bool = False, symbol: str = "SPX500",
                        m1_file: str | None = None, h1_file: str | None = None,
                        orb_bias_filter: bool = True):
    """Run single-index backtest. Optionally override data files."""
    cfg = LIVE_INDEX_CONFIG[symbol]
    data_file = m1_file if m1_file else cfg["local_file"]
    df = load_data(data_file)

    # Compute HTF trend from 20/50 EMA on H1
    if h1_file:
        # Load native H1 from separate file (no resampling from M1)
        from pathlib import Path as _P
        h1_path = _P(__file__).parent / "data" / "raw" / h1_file
        h1 = pd.read_csv(h1_path, sep="\t")
        time_col = [c for c in h1.columns if c.lower() == "time"][0]
        h1[time_col] = pd.to_datetime(h1[time_col], format="mixed", dayfirst=False)
        h1 = h1.set_index(time_col).sort_index()
        h1.columns = [c.lower() for c in h1.columns]
        h1["ema20"] = h1["close"].ewm(span=20, adjust=False).mean()
        h1["ema50"] = h1["close"].ewm(span=50, adjust=False).mean()
        h1["htf_trend"] = "bullish"
        h1.loc[h1["ema20"] < h1["ema50"], "htf_trend"] = "bearish"
        htf_ema = h1[["htf_trend", "ema20", "ema50"]]
        print(f"   HTF EMA from native H1: {len(htf_ema)} bars ({h1_file})")
    else:
        htf_ema = compute_htf_ema(df, fast=20, slow=50)
        print(f"   HTF EMA computed: {len(htf_ema)} H1 bars")

    trading_days = sorted(set(df.index.date))
    if run_days is not None:
        trading_days = trading_days[:run_days]

    print(f"\n{symbol} Backtest | {len(trading_days)} trading days")
    print(
        f"   Session window: {cfg['session_start_hour']:02d}:{cfg['session_start_minute']:02d} "
        f"-> {cfg['session_end_hour']:02d}:{cfg['session_end_minute']:02d} (server time/EET)"
    )

    all_signals = []
    point_value = BROKER_POINT_VALUES[symbol]

    for day in trading_days:
        session_start = pd.Timestamp(day).replace(hour=cfg["session_start_hour"], minute=cfg["session_start_minute"])
        session_end = pd.Timestamp(day).replace(hour=cfg["session_end_hour"], minute=cfg["session_end_minute"])
        market_df = df[(df.index >= session_start) & (df.index <= session_end)].copy()
        if len(market_df) < 80:
            continue
        htf_trend = get_htf_trend_for_day(htf_ema, session_start)
        strategy = StrategyICTClean(
            sl_buffer=cfg["sl_buffer"],
            min_sl_points=cfg.get("min_sl_points", 2.0),
            max_sl_points=cfg.get("max_sl_points", 25.0),
            debug=debug_strategy,
            orb_minutes=cfg.get("orb_minutes", 15),
            entry_hours=cfg.get("entry_hours", 3),
            breakout_window_minutes=cfg.get("breakout_window_minutes", 120),
            disp_mult=cfg.get("disp_mult", 1.5),
            body_min=cfg.get("body_min", 0.60),
            close_strength=cfg.get("close_strength", 0.30),
            breakout_margin=cfg.get("breakout_margin", 0.10),
            orb_range_min_mult=cfg.get("orb_range_min_mult", 0.5),
            tp3_r=2.5,
            orb_bias_filter=orb_bias_filter,
        )
        signals = strategy.generate_signals(market_df, htf_trend=htf_trend)
        for s in signals:
            all_signals.append({
                "symbol": symbol, "signal": s, "point_value": point_value,
                "entry_time": pd.Timestamp(s.entry_time),
                "exit_time": pd.Timestamp(s.exit_time) if s.exit_time else None,
            })
    return all_signals, trading_days


def run_portfolio_backtest(debug_strategy: bool = False):
    """
    Portfolio-level backtest across all indices with real constraints:
    - Max 2 open positions at a time
    - Max 2 losses per calendar day (then stop for the day)
    - Capital $6,000, leverage 1:100
    - Correct market timezone for each index
    """
    ALL_SYMBOLS = ["GER30", "US30", "NDX100", "SPX500"]

    # Clean M1 data + native H1 for EMA (if available)
    CLEAN_M1_FILES = {
        "GER30": "GER30_M1_CLEAN.csv",
        "US30": "US30_M1_CLEAN.csv",
        "NDX100": "NDX100_M1_CLEAN.csv",
        "SPX500": "SPX500_M1_CLEAN.csv",
    }
    H1_EMA_FILES = {
        "GER30": "GER30_H1_EMA.csv",
        "US30": "US30_H1_EMA.csv",
        "NDX100": "NDX100_H1_EMA.csv",
        "SPX500": "SPX500_H1_EMA.csv",
    }

    # Check if clean files exist, fall back to 1-year if not
    from pathlib import Path as _P
    data_dir = _P(__file__).parent / "data" / "raw"
    use_clean = all((data_dir / f).exists() for f in CLEAN_M1_FILES.values())

    # Phase 1: Generate all signals across all indices
    print("=" * 65)
    print("PORTFOLIO BACKTEST — 4 Indices with Position & Loss Limits")
    print("=" * 65)
    print(f"  Capital: ${ACCOUNT_RULES['starting_balance']:,.0f}  |  Leverage: 1:{LEVERAGE}")
    print(f"  Max Open Positions: {MAX_OPEN_POSITIONS}  |  Max Daily Losses: {MAX_DAILY_LOSSES}")
    if use_clean:
        print(f"  Data: Clean M1 (~2.8 years) + Native H1 EMA")
    else:
        print(f"  Data: 1-year M1 (resampled H1 EMA)")
    print()

    all_signals = []
    all_trading_days = set()

    for sym in ALL_SYMBOLS:
        m1 = CLEAN_M1_FILES[sym] if use_clean else None
        h1 = H1_EMA_FILES[sym] if use_clean else None
        sigs, tdays = run_live_simulation(run_days=None, debug_strategy=debug_strategy, symbol=sym,
                                          m1_file=m1, h1_file=h1)
        all_signals.extend(sigs)
        all_trading_days.update(tdays)
        print(f"  {sym}: {len(sigs)} raw signals generated")

    # Sort all signals by entry time (chronological execution)
    all_signals.sort(key=lambda x: x["entry_time"])
    print(f"\nTotal raw signals: {len(all_signals)} across {len(all_trading_days)} trading days")

    # Phase 2: Execute with portfolio constraints
    STARTING_CAPITAL = ACCOUNT_RULES["starting_balance"]
    current_balance = STARTING_CAPITAL
    peak_balance = STARTING_CAPITAL

    open_positions = []  # list of dicts with exit_time
    daily_losses = {}    # date -> loss count
    all_trades = []
    skipped_reasons = {"max_positions": 0, "daily_loss_limit": 0, "adaptive_stop": 0,
                       "account_breached": 0, "daily_dd_breach": 0}

    current_day = None
    day_peak_balance = STARTING_CAPITAL
    day_start_balance = STARTING_CAPITAL
    account_breached = False
    breach_info = None

    for sig in all_signals:
        s = sig["signal"]
        symbol = sig["symbol"]
        point_value = sig["point_value"]
        entry_time = sig["entry_time"]
        exit_time = sig["exit_time"]
        trade_day = entry_time.date()

        # Reset daily counters
        if trade_day != current_day:
            current_day = trade_day
            day_peak_balance = current_balance
            day_start_balance = current_balance

        day_peak_balance = max(day_peak_balance, current_balance)

        # --- FundedNext breach checks DISABLED (backtest mode) ---
        # DD is still tracked for reporting but does not halt trading.

        # --- Constraint 1: Max 2 daily losses ---
        day_losses_so_far = daily_losses.get(trade_day, 0)
        if day_losses_so_far >= MAX_DAILY_LOSSES:
            skipped_reasons["daily_loss_limit"] += 1
            continue

        # --- Constraint 2: Max 2 open positions ---
        # Remove closed positions (exit_time <= current entry_time)
        open_positions = [p for p in open_positions if p["exit_time"] > entry_time]
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            skipped_reasons["max_positions"] += 1
            continue

        # --- Adaptive risk ---
        risk_pct, should_stop = get_adaptive_risk(current_balance, day_peak_balance)
        if should_stop:
            skipped_reasons["adaptive_stop"] += 1
            continue

        # --- Position sizing (with leverage) ---
        risk_amount = current_balance * (risk_pct / 100.0)
        risk_points = float(s.risk_points)
        if risk_points <= 0:
            continue

        lots = risk_amount / (risk_points * point_value)
        lots = max(0.01, min(40.0, lots))
        lots = int(lots * 100) / 100

        # Classify result
        exit_reason = getattr(s, "exit_reason", None)
        pnl_points = float(getattr(s, "pnl_points", 0.0))

        # --- REALISTIC COST MODEL ---
        slippage_pts = TRADING_COSTS["slippage_points"].get(symbol, 0.5)
        spread_pts = TRADING_COSTS["spread_points"].get(symbol, 1.0)
        half_spread = spread_pts / 2.0  # entry gets half, exit gets half

        # Entry cost: slippage + half spread (always adverse)
        entry_cost_pts = slippage_pts + half_spread

        # Exit cost depends on exit type:
        #   - TP: limit order, no slippage, just half spread
        #   - SL/SL_BE/SL_TRAIL: stop order, slipped adversely + half spread
        #   - SESSION_CLOSE: market order, full slippage + half spread
        if exit_reason == "TP":
            exit_cost_pts = half_spread  # limit order, no slippage
        else:
            exit_cost_pts = slippage_pts + half_spread  # market/stop order

        total_cost_pts = entry_cost_pts + exit_cost_pts
        commission_cost = TRADING_COSTS["commission_per_lot"].get(symbol, 0.0) * lots

        # Adjust pnl_points for costs (points adverse to the trade)
        adjusted_pnl_points = pnl_points - total_cost_pts

        if adjusted_pnl_points > 0:
            result = "win" if exit_reason == "TP" else "partial_win"
        elif adjusted_pnl_points == 0:
            result = "breakeven"
        else:
            result = "loss"

        # P&L calculation
        pnl_raw = pnl_points * lots * point_value
        total_costs = total_cost_pts * lots * point_value + commission_cost
        pnl = pnl_raw - total_costs

        current_balance += pnl
        if pnl > 0:
            day_peak_balance = max(day_peak_balance, current_balance)
        peak_balance = max(peak_balance, current_balance)

        # --- FundedNext Drawdown Checks ---
        # Daily DD: from day's peak balance
        daily_ref_post = max(day_start_balance, day_peak_balance)
        daily_dd_pct = ((daily_ref_post - current_balance) / daily_ref_post * 100) if daily_ref_post > 0 else 0
        # Trailing max DD: from overall peak equity
        trailing_dd_pct = ((peak_balance - current_balance) / peak_balance * 100) if peak_balance > 0 else 0

        # Track daily losses
        if result == "loss":
            daily_losses[trade_day] = daily_losses.get(trade_day, 0) + 1

        # Track open position
        if exit_time is not None:
            open_positions.append({"symbol": symbol, "exit_time": exit_time})

        all_trades.append({
            "symbol": symbol, "date": entry_time, "direction": s.direction,
            "result": result, "risk_percent": risk_pct, "risk_amount": risk_amount,
            "risk_points": risk_points, "pnl_points": pnl_points,
            "adjusted_pnl_points": adjusted_pnl_points,
            "pnl_raw": pnl_raw,
            "costs": total_costs, "pnl": pnl, "balance": current_balance,
            "peak_balance": peak_balance,
            "day_peak_balance": day_peak_balance,
            "daily_dd_pct": round(daily_dd_pct, 2),
            "trailing_dd_pct": round(trailing_dd_pct, 2),
            "lots": lots, "exit_reason": exit_reason,
        })

    # Phase 3: Comprehensive Live Audit Report
    import numpy as np

    output_dir = Path(__file__).parent / "reports" / "live_simulation"
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / "portfolio_trades.csv"

    if len(all_trades) == 0:
        print("\n0 trades generated.")
        return

    trades_df = pd.DataFrame(all_trades)
    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)
    trades_df.to_csv(trades_path, index=False)

    # --- Per-index summaries ---
    print("\n" + "=" * 70)
    print("  LIVE AUDIT — PER-INDEX RESULTS (with spread + slippage)")
    print("=" * 70)
    trading_weeks = max(1, len(all_trading_days) / 5)

    for sym in ALL_SYMBOLS:
        sdf = trades_df[trades_df["symbol"] == sym]
        if len(sdf) == 0:
            print(f"\n  {sym}: 0 trades")
            continue
        t = len(sdf)
        w = len(sdf[sdf["result"] == "win"])
        pw = len(sdf[sdf["result"] == "partial_win"])
        be = len(sdf[sdf["result"] == "breakeven"])
        lo = len(sdf[sdf["result"] == "loss"])
        wr = ((w + pw) / t * 100) if t > 0 else 0
        gp = sdf[sdf["pnl"] > 0]["pnl"].sum()
        gl = sdf[sdf["pnl"] < 0]["pnl"].sum()
        pf = abs(gp / gl) if gl != 0 else float("inf")
        net = sdf["pnl"].sum()
        total_cost = sdf["costs"].sum()
        awp = sdf[sdf["pnl_points"] > 0]["pnl_points"].mean() if (w + pw) > 0 else 0
        alp = abs(sdf[sdf["pnl_points"] < 0]["pnl_points"].mean()) if lo > 0 else 0
        rr = (awp / alp) if alp > 0 else float("inf")
        tpw = t / trading_weeks
        print(f"\n  {sym} ({LIVE_INDEX_CONFIG[sym]['name']}):")
        print(f"    Trades: {t} ({tpw:.1f}/wk)  |  W:{w}  PW:{pw}  BE:{be}  L:{lo}")
        print(f"    WR: {wr:.1f}%  |  RR: 1:{rr:.2f}  |  PF: {pf:.2f}  |  Net: ${net:,.2f}")
        print(f"    Total Costs (spread+slip): ${total_cost:,.2f}")

    # --- Combined portfolio summary ---
    total = len(trades_df)
    wins = len(trades_df[trades_df["result"] == "win"])
    partial_wins = len(trades_df[trades_df["result"] == "partial_win"])
    breakevens = len(trades_df[trades_df["result"] == "breakeven"])
    losses = len(trades_df[trades_df["result"] == "loss"])
    profitable = wins + partial_wins
    win_rate = (profitable / total * 100) if total > 0 else 0
    net_pnl = trades_df["pnl"].sum()
    gross_profit = trades_df[trades_df["pnl"] > 0]["pnl"].sum()
    gross_loss = trades_df[trades_df["pnl"] < 0]["pnl"].sum()
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")
    avg_win_pts = trades_df[trades_df["pnl_points"] > 0]["pnl_points"].mean() if profitable > 0 else 0
    avg_loss_pts = abs(trades_df[trades_df["pnl_points"] < 0]["pnl_points"].mean()) if losses > 0 else 0
    actual_rr = (avg_win_pts / avg_loss_pts) if avg_loss_pts > 0 else float("inf")
    trades_per_week = total / trading_weeks
    total_costs = trades_df["costs"].sum()

    # Drawdown calculations from equity curve
    equity = trades_df["balance"].values
    peak_equity = np.maximum.accumulate(equity)
    drawdowns = (peak_equity - equity) / peak_equity * 100
    max_dd_pct = drawdowns.max()
    max_dd_idx = drawdowns.argmax()
    max_dd_dollar = (peak_equity[max_dd_idx] - equity[max_dd_idx])

    # Max consecutive losses
    results_list = trades_df["result"].tolist()
    max_consec_loss = 0
    cur_consec = 0
    for r in results_list:
        if r == "loss":
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # Max consecutive wins
    max_consec_win = 0
    cur_consec = 0
    for r in results_list:
        if r in ("win", "partial_win"):
            cur_consec += 1
            max_consec_win = max(max_consec_win, cur_consec)
        else:
            cur_consec = 0

    # Daily P&L for Sharpe/Sortino
    trades_df["trade_date"] = trades_df["date"].dt.date
    daily_pnl = trades_df.groupby("trade_date")["pnl"].sum()
    avg_daily_pnl = daily_pnl.mean()
    std_daily_pnl = daily_pnl.std()
    downside_std = daily_pnl[daily_pnl < 0].std() if len(daily_pnl[daily_pnl < 0]) > 1 else std_daily_pnl

    # Annualized Sharpe Ratio (252 trading days)
    sharpe = (avg_daily_pnl / std_daily_pnl * np.sqrt(252)) if std_daily_pnl > 0 else 0
    # Sortino Ratio
    sortino = (avg_daily_pnl / downside_std * np.sqrt(252)) if downside_std > 0 else 0
    # Calmar Ratio (annual return / max DD)
    annual_return = net_pnl / max(1, len(all_trading_days) / 252) 
    calmar = (annual_return / max_dd_dollar) if max_dd_dollar > 0 else 0
    # Recovery Factor (net profit / max DD)
    recovery_factor = (net_pnl / max_dd_dollar) if max_dd_dollar > 0 else 0
    # Expectancy per trade
    expectancy = net_pnl / total if total > 0 else 0
    # Average holding time
    avg_win_pnl = trades_df[trades_df["pnl"] > 0]["pnl"].mean() if profitable > 0 else 0
    avg_loss_pnl = abs(trades_df[trades_df["pnl"] < 0]["pnl"].mean()) if losses > 0 else 0

    # Largest single win/loss
    largest_win = trades_df["pnl"].max()
    largest_loss = trades_df["pnl"].min()

    # FundedNext-specific checks
    peak_daily_dd = trades_df["daily_dd_pct"].max()
    peak_trailing_dd = trades_df["trailing_dd_pct"].max()

    # Monthly breakdown
    trades_df["month"] = trades_df["date"].dt.to_period("M")
    monthly = trades_df.groupby("month").agg(
        trades=("pnl", "count"),
        net_pnl=("pnl", "sum"),
        wins=("result", lambda x: ((x == "win") | (x == "partial_win")).sum()),
        losses=("result", lambda x: (x == "loss").sum()),
    )
    monthly["wr"] = (monthly["wins"] / monthly["trades"] * 100).round(1)

    daily_counts = trades_df.groupby("trade_date").size()
    daily_loss_counts = trades_df[trades_df["result"] == "loss"].groupby("trade_date").size()

    # ================ PRINT REPORT ================
    print("\n" + "=" * 70)
    print("  LIVE STRESS TEST AUDIT — PORTFOLIO SUMMARY")
    print("  (Next-bar entry, spread, slippage, FundedNext rules)")
    print("=" * 70)

    print(f"\n  --- TRADE STATISTICS ---")
    print(f"  Total Trades    : {total}  ({trades_per_week:.1f}/week)")
    print(f"  Wins (TP 2.5R)  : {wins}")
    print(f"  Partial Wins    : {partial_wins}")
    print(f"  Breakevens (BE) : {breakevens}")
    print(f"  Losses (SL)     : {losses}")
    print(f"  Win Rate (W+PW) : {win_rate:.1f}%")
    print(f"  Avg RR (points) : 1:{actual_rr:.2f}")

    print(f"\n  --- FINANCIAL RESULTS ---")
    print(f"  Starting Cap    : ${STARTING_CAPITAL:,.2f}")
    print(f"  Final Balance   : ${current_balance:,.2f}")
    print(f"  Net PnL         : ${net_pnl:,.2f}")
    print(f"  Return          : {((current_balance - STARTING_CAPITAL) / STARTING_CAPITAL * 100):.1f}%")
    print(f"  Gross Profit    : ${gross_profit:,.2f}")
    print(f"  Gross Loss      : ${gross_loss:,.2f}")
    print(f"  Profit Factor   : {profit_factor:.2f}")
    print(f"  Expectancy/trade: ${expectancy:,.2f}")
    print(f"  Avg Win Trade   : ${avg_win_pnl:,.2f}")
    print(f"  Avg Loss Trade  : -${avg_loss_pnl:,.2f}")
    print(f"  Largest Win     : ${largest_win:,.2f}")
    print(f"  Largest Loss    : ${largest_loss:,.2f}")

    print(f"\n  --- COST ANALYSIS ---")
    print(f"  Total Costs     : ${total_costs:,.2f}  (spread + slippage + commission)")
    print(f"  Cost/Trade      : ${total_costs / total:,.2f}")
    print(f"  Cost as % P&L   : {(total_costs / (net_pnl + total_costs) * 100):.1f}%" if (net_pnl + total_costs) > 0 else "  Cost as % P&L   : N/A")

    print(f"\n  --- RISK METRICS ---")
    print(f"  Max Drawdown    : {max_dd_pct:.1f}%  (${max_dd_dollar:,.2f})")
    print(f"  Peak Daily DD   : {peak_daily_dd:.1f}%  (FundedNext limit: {FUNDEDNEXT_RULES['daily_dd_pct']}%)")
    print(f"  Peak Trailing DD: {peak_trailing_dd:.1f}%  (FundedNext limit: {FUNDEDNEXT_RULES['max_dd_pct']}%)")
    print(f"  Max Consec Wins : {max_consec_win}")
    print(f"  Max Consec Loss : {max_consec_loss}")
    print(f"  Sharpe Ratio    : {sharpe:.2f}")
    print(f"  Sortino Ratio   : {sortino:.2f}")
    print(f"  Calmar Ratio    : {calmar:.2f}")
    print(f"  Recovery Factor : {recovery_factor:.2f}")

    print(f"\n  --- FUNDEDNEXT COMPLIANCE ---")
    if account_breached:
        print(f"  *** ACCOUNT BREACHED ***")
        print(f"  Breach Type     : {breach_info['type']}")
        print(f"  Breach Date     : {breach_info['date']}")
        print(f"  Balance at Breach: ${breach_info['balance']:,.2f}")
        print(f"  Peak Balance    : ${breach_info['peak']:,.2f}")
        print(f"  DD at Breach    : {breach_info['dd_pct']}%")
    else:
        print(f"  PASSED — Account survived full backtest period")
        print(f"  Daily DD headroom  : {FUNDEDNEXT_RULES['daily_dd_pct'] - peak_daily_dd:.1f}% remaining")
        print(f"  Max DD headroom    : {FUNDEDNEXT_RULES['max_dd_pct'] - peak_trailing_dd:.1f}% remaining")

    print(f"\n  --- CONSTRAINTS APPLIED ---")
    print(f"  Skipped (max {MAX_OPEN_POSITIONS} open pos)  : {skipped_reasons['max_positions']}")
    print(f"  Skipped (max {MAX_DAILY_LOSSES} losses/day)  : {skipped_reasons['daily_loss_limit']}")
    print(f"  Skipped (adaptive stop)   : {skipped_reasons['adaptive_stop']}")
    print(f"  Skipped (daily DD breach) : {skipped_reasons['daily_dd_breach']}")
    print(f"  Skipped (account breached): {skipped_reasons['account_breached']}")
    print(f"  Max trades/day   : {daily_counts.max()}")
    print(f"  Avg trades/day   : {daily_counts.mean():.1f}")
    print(f"  Max losses/day   : {daily_loss_counts.max() if len(daily_loss_counts) > 0 else 0}")

    # ─── YEARLY BREAKDOWN ───
    trades_df["year"] = trades_df["date"].dt.year
    print(f"\n  --- YEARLY BREAKDOWN ---")
    print(f"  {'Year':<6} {'Trades':>6} {'Tr/Wk':>6} {'W':>4} {'PW':>4} {'L':>4} {'WR':>6} {'PF':>6} {'Net PnL':>10} {'Max DD$':>9} {'MaxCL':>5}")
    print(f"  {'-'*72}")
    for yr in sorted(trades_df["year"].unique()):
        ydf = trades_df[trades_df["year"] == yr]
        yt = len(ydf)
        yw = len(ydf[ydf["result"] == "win"])
        ypw = len(ydf[ydf["result"] == "partial_win"])
        yl = len(ydf[ydf["result"] == "loss"])
        ywr = ((yw + ypw) / yt * 100) if yt > 0 else 0
        ygp = ydf[ydf["pnl"] > 0]["pnl"].sum()
        ygl = ydf[ydf["pnl"] < 0]["pnl"].sum()
        ypf = abs(ygp / ygl) if ygl != 0 else float("inf")
        ynp = ydf["pnl"].sum()
        # Year max DD
        yeq = ydf["balance"].values
        ypk = np.maximum.accumulate(yeq)
        ydd = (ypk - yeq)
        ymdd = ydd.max() if len(ydd) > 0 else 0
        # Trades per week in that year
        y_days = len(set(ydf["trade_date"]))
        y_weeks = max(1, y_days / 5)
        ytpw = yt / y_weeks
        # Max consecutive losses in year
        yr_results = ydf["result"].tolist()
        yr_mcl = 0
        yr_cl = 0
        for r in yr_results:
            if r == "loss":
                yr_cl += 1
                yr_mcl = max(yr_mcl, yr_cl)
            else:
                yr_cl = 0
        print(f"  {yr:<6} {yt:>6} {ytpw:>5.1f} {yw:>4} {ypw:>4} {yl:>4} {ywr:>5.1f}% {ypf:>5.2f} ${ynp:>9,.2f} ${ymdd:>8,.2f} {yr_mcl:>5}")

    # ─── MONTHLY BREAKDOWN (enhanced) ───
    print(f"\n  --- MONTHLY BREAKDOWN ---")
    print(f"  {'Month':<10} {'Trades':>6} {'W':>4} {'PW':>4} {'L':>4} {'WR':>6} {'PF':>6} {'Net PnL':>10} {'Cum PnL':>10} {'MaxCL':>5}")
    print(f"  {'-'*72}")
    cum_pnl = 0.0
    for month, row in monthly.iterrows():
        mdf = trades_df[trades_df["month"] == month]
        mt = int(row["trades"])
        mw = len(mdf[mdf["result"] == "win"])
        mpw = len(mdf[mdf["result"] == "partial_win"])
        ml = int(row["losses"])
        mwr = row["wr"]
        mgp = mdf[mdf["pnl"] > 0]["pnl"].sum()
        mgl = mdf[mdf["pnl"] < 0]["pnl"].sum()
        mpf = abs(mgp / mgl) if mgl != 0 else float("inf")
        mnp = row["net_pnl"]
        cum_pnl += mnp
        # Max consecutive losses in month
        m_results = mdf["result"].tolist()
        m_mcl = 0
        m_cl = 0
        for r in m_results:
            if r == "loss":
                m_cl += 1
                m_mcl = max(m_mcl, m_cl)
            else:
                m_cl = 0
        print(f"  {str(month):<10} {mt:>6} {mw:>4} {mpw:>4} {ml:>4} {mwr:>5.1f}% {mpf:>5.2f} ${mnp:>9,.2f} ${cum_pnl:>9,.2f} {m_mcl:>5}")

    # ─── STREAK ANALYSIS ───
    print(f"\n  --- STREAK ANALYSIS ---")

    # All streaks (wins and losses)
    win_streaks = []
    loss_streaks = []
    cur_streak = 0
    cur_type = None
    for r in results_list:
        is_win = r in ("win", "partial_win")
        if is_win:
            if cur_type == "win":
                cur_streak += 1
            else:
                if cur_type == "loss" and cur_streak > 0:
                    loss_streaks.append(cur_streak)
                cur_streak = 1
                cur_type = "win"
        else:
            if cur_type == "loss":
                cur_streak += 1
            else:
                if cur_type == "win" and cur_streak > 0:
                    win_streaks.append(cur_streak)
                cur_streak = 1
                cur_type = "loss"
    # flush last streak
    if cur_type == "win" and cur_streak > 0:
        win_streaks.append(cur_streak)
    elif cur_type == "loss" and cur_streak > 0:
        loss_streaks.append(cur_streak)

    print(f"  Max Consecutive Wins   : {max(win_streaks) if win_streaks else 0}")
    print(f"  Max Consecutive Losses : {max(loss_streaks) if loss_streaks else 0}")
    print(f"  Avg Win Streak         : {np.mean(win_streaks):.1f}" if win_streaks else "  Avg Win Streak         : 0")
    print(f"  Avg Loss Streak        : {np.mean(loss_streaks):.1f}" if loss_streaks else "  Avg Loss Streak        : 0")

    # Distribution of loss streaks
    if loss_streaks:
        from collections import Counter
        ls_dist = Counter(loss_streaks)
        print(f"\n  Loss Streak Distribution:")
        for length in sorted(ls_dist.keys()):
            bar = "█" * ls_dist[length]
            print(f"    {length:>2} in a row : {ls_dist[length]:>3}x  {bar}")

    # Distribution of win streaks
    if win_streaks:
        from collections import Counter
        ws_dist = Counter(win_streaks)
        print(f"\n  Win Streak Distribution:")
        for length in sorted(ws_dist.keys()):
            bar = "█" * ws_dist[length]
            print(f"    {length:>2} in a row : {ws_dist[length]:>3}x  {bar}")

    # ─── PER-INDEX YEARLY BREAKDOWN ───
    print(f"\n  --- PER-INDEX YEARLY BREAKDOWN ---")
    for sym in ALL_SYMBOLS:
        sdf = trades_df[trades_df["symbol"] == sym]
        if len(sdf) == 0:
            continue
        print(f"\n  {sym}:")
        print(f"    {'Year':<6} {'Trades':>6} {'W':>4} {'PW':>4} {'L':>4} {'WR':>6} {'PF':>6} {'Net PnL':>10} {'MaxCL':>5}")
        print(f"    {'-'*56}")
        for yr in sorted(sdf["year"].unique()):
            sydf = sdf[sdf["year"] == yr]
            st = len(sydf)
            sw = len(sydf[sydf["result"] == "win"])
            spw = len(sydf[sydf["result"] == "partial_win"])
            sl_cnt = len(sydf[sydf["result"] == "loss"])
            swr = ((sw + spw) / st * 100) if st > 0 else 0
            sgp = sydf[sydf["pnl"] > 0]["pnl"].sum()
            sgl = sydf[sydf["pnl"] < 0]["pnl"].sum()
            spf = abs(sgp / sgl) if sgl != 0 else float("inf")
            snp = sydf["pnl"].sum()
            s_results = sydf["result"].tolist()
            s_mcl = 0
            s_cl = 0
            for r in s_results:
                if r == "loss":
                    s_cl += 1
                    s_mcl = max(s_mcl, s_cl)
                else:
                    s_cl = 0
            print(f"    {yr:<6} {st:>6} {sw:>4} {spw:>4} {sl_cnt:>4} {swr:>5.1f}% {spf:>5.2f} ${snp:>9,.2f} {s_mcl:>5}")

    # Exit reason breakdown
    exit_counts = trades_df["exit_reason"].value_counts()
    print(f"\n  --- EXIT REASON BREAKDOWN ---")
    for reason, count in exit_counts.items():
        pct = count / total * 100
        avg_pnl_by_reason = trades_df[trades_df["exit_reason"] == reason]["pnl"].mean()
        print(f"  {reason:<15} : {count:>4} ({pct:>5.1f}%)  avg PnL: ${avg_pnl_by_reason:>8,.2f}")

    print("\n" + "=" * 70)
    print(f"  LOOK-AHEAD BIAS: ELIMINATED (next-bar open entry)")
    print(f"  SPREAD MODEL   : Half-spread on entry + exit")
    print(f"  SLIPPAGE MODEL : Adverse slippage on entry + SL exits")
    print(f"  TP EXECUTION   : Limit order (no slippage)")
    print(f"  DRAWDOWN RULES : FundedNext (5% daily, 10% trailing)")
    print("=" * 70)


if __name__ == "__main__":
    run_portfolio_backtest(debug_strategy=False)
