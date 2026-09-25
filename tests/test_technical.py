from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_agent.agents.technical import (
    TechnicalError,
    _detect_gap_fade,
    compute_indicators,
    get_technical_signal,
    get_volatility_percentile,
)
from swing_agent.config import load_config
from swing_agent.storage.db import upsert_prices


def _price_df(dates, opens, highs, lows, closes, volumes) -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=dates,
    )


def _seed_pullback(conn: sqlite3.Connection, ticker: str = "PBK") -> None:
    n = 90
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    # A sawtooth uptrend (occasional small down-days), not a pure monotonic
    # ramp -- a monotonic ramp has zero losses so RSI pins near 100, which
    # is unrealistic and would now be rejected by the pullback_rsi_max gate.
    closes = []
    price = 100.0
    for i in range(n - 2):
        price += -0.6 if i % 3 == 2 else 1.0
        closes.append(price)
    dip = closes[-1] - 6
    closes.append(dip)
    closes.append(dip + 8)

    opens = list(closes)
    highs = list(closes)
    lows = list(closes)
    highs[-2] = dip + 1
    lows[-2] = dip - 1
    volumes = [1_000_000] * (n - 2) + [600_000, 1_800_000]
    upsert_prices(conn, ticker, _price_df(dates, opens, highs, lows, closes, volumes))


def _seed_breakout(conn: sqlite3.Connection, ticker: str = "BRK") -> None:
    lead, consolidation = 60, 25
    n = lead + consolidation + 1
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    closes = [100 + i * 0.5 for i in range(lead)]
    base = closes[-1]
    cons_low, cons_high = base, base * 1.09
    cons_closes = [cons_low + (cons_high - cons_low) * (0.3 + 0.4 * ((i % 5) / 4)) for i in range(consolidation)]
    closes.extend(cons_closes)
    closes.append(cons_high * 1.05)

    opens = list(closes)
    highs = list(closes)
    lows = list(closes)
    highs[lead:lead + consolidation] = [c + 0.2 for c in cons_closes]
    lows[lead:lead + consolidation] = [c - 0.2 for c in cons_closes]
    volumes = [1_000_000] * lead + [500_000] * consolidation + [1_600_000]
    upsert_prices(conn, ticker, _price_df(dates, opens, highs, lows, closes, volumes))


def _seed_failed_breakdown(conn: sqlite3.Connection, ticker: str = "FBD") -> None:
    lead = 40
    n = lead + 3
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    closes = [100 + (i % 5) * 0.2 for i in range(lead)]
    support = min(closes[-20:])
    closes.extend([support - 3, support - 1, support + 1])

    opens = list(closes)
    highs = list(closes)
    lows = list(closes)
    lows[lead] = support - 4
    volumes = [800_000] * lead + [2_000_000, 900_000, 1_000_000]
    upsert_prices(conn, ticker, _price_df(dates, opens, highs, lows, closes, volumes))


def _seed_flat(conn: sqlite3.Connection, ticker: str = "FLT", n: int = 10) -> None:
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    closes = [100.0] * n
    volumes = [1_000_000] * n
    upsert_prices(conn, ticker, _price_df(dates, closes, closes, closes, closes, volumes))


def test_pullback_setup_detected(memory_conn: sqlite3.Connection) -> None:
    _seed_pullback(memory_conn)
    result = get_technical_signal(memory_conn, "PBK")
    assert result["verdict"] == "TRIGGER"
    assert result["setup"] == "PULLBACK"
    assert result["entry"] > result["stop"]
    assert result["half_size"] is False


def test_breakout_setup_detected(memory_conn: sqlite3.Connection) -> None:
    _seed_breakout(memory_conn)
    result = get_technical_signal(memory_conn, "BRK")
    assert result["verdict"] == "TRIGGER"
    assert result["setup"] == "BREAKOUT"
    assert result["entry"] > result["stop"]


def test_failed_breakdown_setup_detected(memory_conn: sqlite3.Connection) -> None:
    _seed_failed_breakdown(memory_conn)
    result = get_technical_signal(memory_conn, "FBD")
    assert result["verdict"] == "TRIGGER"
    assert result["setup"] == "FAILED_BREAKDOWN"
    assert result["half_size"] is True


def _gap_fade_indicator_df(n_lead: int = 45) -> pd.DataFrame:
    """_detect_gap_fade is intentionally NOT wired into get_technical_signal
    (train-period backtest testing showed consistently negative expectancy
    -- see get_technical_signal's docstring), so it's unit-tested directly
    against the function rather than through the live/backtest entry point."""
    dates = pd.date_range("2023-01-01", periods=n_lead + 1, freq="D")
    closes = [100.0] * n_lead
    opens = [100.0] * n_lead
    highs = [100.0] * n_lead
    lows = [100.0] * n_lead
    volumes = [1_000_000] * n_lead

    opens.append(96.0)   # 4% gap down vs yesterday's close (100)
    lows.append(95.0)
    highs.append(98.0)
    closes.append(97.5)  # green, closes in the upper half of [95, 98]
    volumes.append(2_000_000)  # panic volume, 2x avg

    df = pd.DataFrame(
        {
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes,
        }
    )
    cfg = load_config().technical
    return compute_indicators(df, cfg)


def test_gap_fade_detector_fires_on_intended_pattern() -> None:
    cfg = load_config().technical
    df = _gap_fade_indicator_df()
    match = _detect_gap_fade(df, cfg)
    assert match is not None
    assert match["setup"] == "GAP_FADE"
    assert match["half_size"] is True
    assert match["entry"] > match["stop"]


def test_gap_fade_detector_does_not_fire_on_normal_up_day() -> None:
    n = 46
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    closes = [100.0 + i * 0.1 for i in range(n)]  # no gap, mild drift, no volume spike
    df = pd.DataFrame(
        {
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "open": closes, "high": closes, "low": closes, "close": closes,
            "volume": [1_000_000] * n,
        }
    )
    cfg = load_config().technical
    indicator_df = compute_indicators(df, cfg)
    assert _detect_gap_fade(indicator_df, cfg) is None


def test_gap_fade_not_reachable_via_get_technical_signal(memory_conn: sqlite3.Connection) -> None:
    """Confirms the detector is disabled in the active chain (not just
    correct in isolation) -- feeding the exact same pattern through the
    live/backtest entry point must NOT trigger GAP_FADE."""
    n_lead = 45
    dates = pd.date_range("2023-01-01", periods=n_lead + 1, freq="D")
    closes = [100.0] * n_lead + [97.5]
    opens = [100.0] * n_lead + [96.0]
    highs = [100.0] * n_lead + [98.0]
    lows = [100.0] * n_lead + [95.0]
    volumes = [1_000_000] * n_lead + [2_000_000]
    upsert_prices(memory_conn, "GAP", _price_df(dates, opens, highs, lows, closes, volumes))
    result = get_technical_signal(memory_conn, "GAP")
    assert result["setup"] != "GAP_FADE"


def test_volatility_percentile_spikes_on_a_wide_range_day(memory_conn: sqlite3.Connection) -> None:
    n = 40
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    closes = [100.0] * n
    highs = [100.0] * n
    lows = [100.0] * n
    # a single, much-wider-range day near the end -> should rank near the top
    highs[-1] = 130.0
    lows[-1] = 70.0
    volumes = [1_000_000] * n
    upsert_prices(memory_conn, "VOL", _price_df(dates, closes, highs, lows, closes, volumes))
    pct = get_volatility_percentile(memory_conn, "VOL", lookback_days=252)
    assert pct is not None
    assert pct > 80


def test_volatility_percentile_none_without_enough_history(memory_conn: sqlite3.Connection) -> None:
    dates = pd.date_range("2023-01-01", periods=3, freq="D")
    closes = [100.0, 101.0, 102.0]
    upsert_prices(memory_conn, "SHORT", _price_df(dates, closes, closes, closes, closes, [1_000_000] * 3))
    assert get_volatility_percentile(memory_conn, "SHORT", lookback_days=252) is None


def test_volatility_percentile_none_for_unknown_ticker(memory_conn: sqlite3.Connection) -> None:
    assert get_volatility_percentile(memory_conn, "NOPE", lookback_days=252) is None


def test_no_setup_on_flat_thin_history(memory_conn: sqlite3.Connection) -> None:
    _seed_flat(memory_conn)
    result = get_technical_signal(memory_conn, "FLT")
    assert result["verdict"] == "NO_SETUP"
    assert result["setup"] is None
    assert result["entry"] is None


def test_missing_ticker_raises_clear_error(memory_conn: sqlite3.Connection) -> None:
    with pytest.raises(TechnicalError):
        get_technical_signal(memory_conn, "NOPE")
