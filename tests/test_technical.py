from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_agent.agents.technical import TechnicalError, get_technical_signal
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


def test_no_setup_on_flat_thin_history(memory_conn: sqlite3.Connection) -> None:
    _seed_flat(memory_conn)
    result = get_technical_signal(memory_conn, "FLT")
    assert result["verdict"] == "NO_SETUP"
    assert result["setup"] is None
    assert result["entry"] is None


def test_missing_ticker_raises_clear_error(memory_conn: sqlite3.Connection) -> None:
    with pytest.raises(TechnicalError):
        get_technical_signal(memory_conn, "NOPE")
