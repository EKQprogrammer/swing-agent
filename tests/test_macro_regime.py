from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_agent.agents.macro_regime import MacroRegimeError, get_macro_regime
from swing_agent.storage.db import upsert_prices

WINDOW = 200  # matches config.yaml's macro_regime.spy_trend_ma_days


def _price_df(dates: pd.DatetimeIndex, closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        },
        index=dates,
    )


def _seed_spy_trend(conn: sqlite3.Connection, ascending: bool) -> None:
    """Seeds exactly WINDOW rows of a linear SPY close series. With a linear
    series and a window covering all seeded rows, the last close sits above
    the trailing mean when ascending and below it when descending."""
    dates = pd.date_range(start="2023-01-01", periods=WINDOW, freq="D")
    if ascending:
        closes = [100.0 + i for i in range(WINDOW)]
    else:
        closes = [100.0 + (WINDOW - i) for i in range(WINDOW)]
    upsert_prices(conn, "SPY", _price_df(dates, closes))


def _seed_vix(conn: sqlite3.Connection, vix_close: float) -> None:
    last_date = pd.date_range(start="2023-01-01", periods=WINDOW, freq="D")[-1:]
    upsert_prices(conn, "^VIX", _price_df(last_date, [vix_close]))


def test_bullish_spy_above_ma_vix_calm(memory_conn: sqlite3.Connection) -> None:
    _seed_spy_trend(memory_conn, ascending=True)
    _seed_vix(memory_conn, 14.0)
    result = get_macro_regime(memory_conn)
    assert result["regime"] == "BULLISH"
    assert result["position_size_modifier"] == 1.0


def test_cautious_spy_above_ma_vix_elevated(memory_conn: sqlite3.Connection) -> None:
    _seed_spy_trend(memory_conn, ascending=True)
    _seed_vix(memory_conn, 28.0)
    result = get_macro_regime(memory_conn)
    assert result["regime"] == "CAUTIOUS"
    assert result["position_size_modifier"] == 0.5


def test_bearish_spy_below_ma_vix_calm(memory_conn: sqlite3.Connection) -> None:
    _seed_spy_trend(memory_conn, ascending=False)
    _seed_vix(memory_conn, 20.0)
    result = get_macro_regime(memory_conn)
    assert result["regime"] == "BEARISH"
    assert result["position_size_modifier"] == 0.0


def test_bearish_spy_above_ma_vix_panic(memory_conn: sqlite3.Connection) -> None:
    _seed_spy_trend(memory_conn, ascending=True)
    _seed_vix(memory_conn, 40.0)
    result = get_macro_regime(memory_conn)
    assert result["regime"] == "BEARISH"
    assert result["position_size_modifier"] == 0.0


def test_missing_spy_data_raises_clear_error(memory_conn: sqlite3.Connection) -> None:
    _seed_vix(memory_conn, 14.0)
    with pytest.raises(MacroRegimeError):
        get_macro_regime(memory_conn)
