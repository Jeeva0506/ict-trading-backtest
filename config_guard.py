"""Fail-fast guards for trading configuration. Changes no values.

WHY THIS EXISTS
    Two divergent copies of config_fundednext.py exist:

        backtest_engine/config_fundednext.py
        backtest_engine/live/config_fundednext.py     (58 lines differ)

    and which one loads depends on the ENTRY POINT and on import order, because
    Python caches by module name:

        run_live.py       inserts live/ last            -> live copy
        mt5_connector.py  inserts backtest_engine/ last -> backtest_engine copy

    In a live process run_live.py imports first, so its choice silently wins for
    every module loaded afterwards. Any script importing mt5_connector directly
    -- i.e. most backtests -- gets the other copy.

    The copies disagree on `risk_per_trade_percent` (1.0 vs 1.5), so a backtest
    can size at 1.0% while the live account sizes at 1.5% on identical signals.
    That is a 50% risk difference with nothing in the logs to reveal it.

WHAT THIS MODULE DOES
    Nothing implicitly. It provides checks you call:

        require(cfg, "disp_mult", symbol="GER30")
            -> returns the value, or raises. Never substitutes a default.

        assert_symbol_complete(cfg, symbol)
            -> every behaviour key must be present.

        diff_config_copies()
            -> reports value-level divergence between the two copies.

        assert_configs_agree()
            -> raises if they diverge on any behaviour-relevant key.

    It deliberately does NOT unify the copies or pick a winner. Choosing which
    risk value is canonical is a trading decision, not an engineering one, and
    silently adopting either would violate "do not silently change account
    rules".

RULE THIS ENFORCES
    Never `config.get("some_behaviour_key", default)`. A missing behaviour
    parameter must raise. Two code defaults currently CONTRADICT the configured
    values, which is worse than having none:

        daily_drawdown_stop   config 2.0   code fallback 3.0
        max_drawdown_percent  config 10.0  code fallback 8.0
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Tuple

_HERE = Path(__file__).resolve().parent
COPY_BACKTEST = _HERE / "config_fundednext.py"
COPY_LIVE = _HERE / "live" / "config_fundednext.py"

# Per-symbol keys that change what is traded or how it is sized. A missing one
# must be an error, never a default.
SYMBOL_BEHAVIOUR_KEYS: Tuple[str, ...] = (
    "point_value", "sl_buffer", "min_sl_distance", "max_sl_distance",
    "orb_minutes", "entry_hours", "breakout_window_minutes",
    "disp_mult", "body_min", "close_strength", "breakout_margin",
    "orb_range_min_mult", "min_sl_points", "max_sl_points",
    "timezone", "market_open_hour", "market_open_minute",
    "market_close_hour", "market_close_minute",
    "mt5_open_hour", "mt5_open_minute", "mt5_close_hour", "mt5_close_minute",
)

# Account-level rules. These govern failure of the funded account.
RULE_KEYS: Tuple[str, ...] = (
    "risk_per_trade_percent", "max_trades_per_day", "max_open_positions",
    "max_drawdown_percent", "daily_drawdown_percent", "daily_drawdown_stop",
    "news_buffer_before_minutes", "news_buffer_after_minutes",
)


class ConfigError(RuntimeError):
    """Raised instead of silently substituting a default."""


def require(cfg: Dict[str, Any], key: str, symbol: str = "") -> Any:
    """Fetch a behaviour parameter or raise. The replacement for cfg.get(k, d)."""
    if key not in cfg:
        where = f" for {symbol}" if symbol else ""
        raise ConfigError(
            f"missing behaviour parameter {key!r}{where}. "
            f"Behaviour parameters must be explicit -- a default would change "
            f"what is traded without appearing anywhere in the logs."
        )
    v = cfg[key]
    if v is None:
        raise ConfigError(f"behaviour parameter {key!r} is None{' for ' + symbol if symbol else ''}")
    return v


def assert_symbol_complete(cfg: Dict[str, Any], symbol: str,
                           keys: Tuple[str, ...] = SYMBOL_BEHAVIOUR_KEYS) -> None:
    missing = [k for k in keys if k not in cfg]
    if missing:
        raise ConfigError(
            f"{symbol}: missing behaviour keys {missing}. "
            f"Add them explicitly rather than relying on code defaults."
        )


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def diff_config_copies() -> List[str]:
    """Value-level divergence between the two copies. Empty list == agree."""
    if not (COPY_BACKTEST.exists() and COPY_LIVE.exists()):
        return [f"one copy missing: {COPY_BACKTEST.exists()=} {COPY_LIVE.exists()=}"]
    a = _load(COPY_BACKTEST, "_cfg_backtest")
    b = _load(COPY_LIVE, "_cfg_live")
    out: List[str] = []

    ra, rb = getattr(a, "FUNDEDNEXT_RULES", {}), getattr(b, "FUNDEDNEXT_RULES", {})
    for k in sorted(set(ra) | set(rb)):
        if ra.get(k) != rb.get(k):
            out.append(f"FUNDEDNEXT_RULES[{k!r}]: backtest={ra.get(k)!r} live={rb.get(k)!r}")

    for flag in ("EDGE_GUARD_ENABLED", "ATR_SL_ENABLED", "USE_LIMIT_ORDERS",
                 "MARKETABLE_CAP_R", "ACCOUNT_LABEL"):
        va, vb = getattr(a, flag, "<absent>"), getattr(b, flag, "<absent>")
        if va != vb:
            out.append(f"{flag}: backtest={va!r} live={vb!r}")

    pa = getattr(a, "PROFITABLE_INDICES", {})
    pb = getattr(b, "PROFITABLE_INDICES", {})
    if set(pa) != set(pb):
        out.append(f"PROFITABLE_INDICES symbols differ: {set(pa) ^ set(pb)}")
    for s in sorted(set(pa) & set(pb)):
        for k in sorted(set(pa[s]) | set(pb[s])):
            if pa[s].get(k) != pb[s].get(k):
                out.append(f"{s}.{k}: backtest={pa[s].get(k)!r} live={pb[s].get(k)!r}")
    return out


def assert_configs_agree() -> None:
    d = diff_config_copies()
    if d:
        raise ConfigError(
            "the two config_fundednext.py copies disagree on behaviour:\n  "
            + "\n  ".join(d)
            + "\n\nResolve by choosing ONE canonical file. Which value is "
              "correct is a trading decision and must not be guessed."
        )


# ---------------------------------------------------------------------------
#  .env duplication
# ---------------------------------------------------------------------------
#  Both config copies contain the SAME line:
#
#      env_path = Path(__file__).parent.parent / '.env'
#
#  but `parent.parent` is depth-dependent and the file exists at two depths:
#
#      backtest_engine/config_fundednext.py      -> ROOT/.env
#      backtest_engine/live/config_fundednext.py -> backtest_engine/.env
#
#  so identical code loads different files. That is the whole mechanism behind
#  risk_per_trade_percent being 1.0 in backtests and 1.5 live.
_ROOT = _HERE.parent

ENV_BEHAVIOUR_KEYS = ("RISK_PER_TRADE", "ACCOUNT_SIZE", "ACCOUNT_LABEL",
                      "ADAPTIVE_RISK", "MT5_LOGIN", "MT5_SERVER",
                      "USE_LIMIT_ORDERS", "MARKETABLE_CAP_R")


def _read_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def diff_env_copies(profile: str = "") -> List[str]:
    """Divergence between the root and backtest_engine copies of one .env."""
    name = f".env.{profile}" if profile else ".env"
    a, b = _ROOT / name, _HERE / name
    if not a.exists() and not b.exists():
        return [f"{name}: neither copy exists"]
    if not (a.exists() and b.exists()):
        return []                      # only one copy -> unambiguous, fine
    ea, eb = _read_env(a), _read_env(b)
    out = []
    for k in sorted(set(ENV_BEHAVIOUR_KEYS) & (set(ea) | set(eb))):
        if ea.get(k) != eb.get(k):
            out.append(f"{name}[{k}]: root={ea.get(k)!r} backtest_engine={eb.get(k)!r}")
    return out


def assert_env_copies_agree(profiles=("", "stellar2", "stellar3", "stellar15k")) -> None:
    bad: List[str] = []
    for p in profiles:
        bad += diff_env_copies(p)
    if bad:
        raise ConfigError(
            "duplicated .env files disagree on behaviour keys:\n  "
            + "\n  ".join(bad)
            + "\n\nDelete one copy and have both configs resolve to it. Which "
              "value is correct is a trading decision and must not be guessed."
        )


if __name__ == "__main__":
    print("config copies:")
    print("  backtest:", COPY_BACKTEST)
    print("  live    :", COPY_LIVE)
    d = diff_config_copies()
    if not d:
        print("\nAGREE on all behaviour-relevant values.")
    else:
        print(f"\nDIVERGENT on {len(d)} behaviour value(s):")
        for line in d:
            print("  " + line)

    print("\nper-symbol behaviour-key completeness:")
    for path, tag in ((COPY_BACKTEST, "backtest"), (COPY_LIVE, "live")):
        mod = _load(path, "_cfg_" + tag)
        bad = 0
        for s, cfg in getattr(mod, "PROFITABLE_INDICES", {}).items():
            try:
                assert_symbol_complete(cfg, s)
            except ConfigError as e:
                bad += 1
                print(f"  [{tag}] {e}")
        if not bad:
            print(f"  [{tag}] all symbols complete")

    print("\n.env duplication check:")
    any_bad = False
    for prof in ("", "stellar2", "stellar3", "stellar15k"):
        d = diff_env_copies(prof)
        for line in d:
            any_bad = True
            print("  DIVERGENT " + line)
    if not any_bad:
        print("  all duplicated .env copies agree on behaviour keys")
