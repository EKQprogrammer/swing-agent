from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_agent.agents.orchestrator import get_verdict
from swing_agent.storage.db import upsert_fundamentals, upsert_prices

FILED_DATE = "2024-06-01"
STRONG_METRICS = {
    "filed_date": FILED_DATE,
    "roic": 15.0,
    "fcf": 1_000_000.0,
    "fcf_margin": 10.0,
    "revenue_growth_yoy": 8.0,
    "earnings_growth_yoy": 12.0,
    "price": 50.0,
    "avg_daily_volume": 1_000_000,
}
WEAK_METRICS = {**STRONG_METRICS, "roic": 4.0}


def _price_df(dates, opens, highs, lows, closes, volumes) -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=dates,
    )


def _seed_spy(conn: sqlite3.Connection, bullish: bool) -> None:
    dates = pd.date_range(start="2023-01-01", periods=200, freq="D")
    if bullish:
        closes = [100.0 + i for i in range(200)]
    else:
        closes = [100.0 + (200 - i) for i in range(200)]
    upsert_prices(conn, "SPY", _price_df(dates, closes, closes, closes, closes, [1_000_000] * 200))
    last_date = dates[-1:]
    upsert_prices(conn, "^VIX", _price_df(last_date, [14.0], [14.0], [14.0], [14.0], [1_000_000]))


def _seed_ticker(conn: sqlite3.Connection, ticker: str, trigger_pullback: bool, n: int = 300) -> None:
    """>=253 rows so the fundamental layer's relative-strength calc has
    enough point-in-time history, ending on FILED_DATE so it lines up with
    the fake fundamentals fetch's filed_date."""
    dates = pd.date_range(end=FILED_DATE, periods=n, freq="D")
    if trigger_pullback:
        # Sawtooth uptrend (occasional small down-days), not a pure
        # monotonic ramp -- a monotonic ramp has zero losses so RSI pins
        # near 100, which is unrealistic and now fails pullback_rsi_max.
        closes = []
        price = 100.0
        for i in range(n - 2):
            price += -0.6 if i % 3 == 2 else 1.0
            closes.append(price)
        dip = closes[-1] - 6
        closes.append(dip)
        closes.append(dip + 8)
        opens, highs, lows = list(closes), list(closes), list(closes)
        highs[-2] = dip + 1
        lows[-2] = dip - 1
        volumes = [1_000_000] * (n - 2) + [600_000, 1_800_000]
    else:
        closes = [100 + i * 1.0 for i in range(n)]  # smooth rise, flat volume -> no setup fires
        opens, highs, lows = list(closes), list(closes), list(closes)
        volumes = [1_000_000] * n
    upsert_prices(conn, ticker, _price_df(dates, opens, highs, lows, closes, volumes))


def _fake_fetch(metrics: dict):
    def fetch_fn(conn: sqlite3.Connection, ticker: str, api_key: str) -> dict:
        row = {**metrics, "ticker": ticker}
        upsert_fundamentals(conn, row)
        return row

    return fetch_fn


def test_reject_at_macro_layer(memory_conn: sqlite3.Connection) -> None:
    _seed_spy(memory_conn, bullish=False)
    result = get_verdict(memory_conn, "XYZ", fetch_fn=_fake_fetch(STRONG_METRICS))
    assert result["verdict"] == "REJECT"
    assert result["reject_layer"] == "macro"
    assert result["fundamental"] is None
    assert result["technical"] is None


def test_reject_at_fundamental_layer(memory_conn: sqlite3.Connection) -> None:
    _seed_spy(memory_conn, bullish=True)
    _seed_ticker(memory_conn, "XYZ", trigger_pullback=True)
    result = get_verdict(memory_conn, "XYZ", fetch_fn=_fake_fetch(WEAK_METRICS))
    assert result["verdict"] == "REJECT"
    assert result["reject_layer"] == "fundamental"
    assert result["technical"] is None


def test_reject_at_technical_layer(memory_conn: sqlite3.Connection) -> None:
    _seed_spy(memory_conn, bullish=True)
    _seed_ticker(memory_conn, "XYZ", trigger_pullback=False)
    result = get_verdict(memory_conn, "XYZ", fetch_fn=_fake_fetch(STRONG_METRICS))
    assert result["verdict"] == "REJECT"
    assert result["reject_layer"] == "technical"
    assert result["technical"]["verdict"] == "NO_SETUP"


def test_approve_full_chain(memory_conn: sqlite3.Connection) -> None:
    _seed_spy(memory_conn, bullish=True)
    _seed_ticker(memory_conn, "XYZ", trigger_pullback=True)
    result = get_verdict(memory_conn, "XYZ", fetch_fn=_fake_fetch(STRONG_METRICS))
    assert result["verdict"] == "APPROVE"
    assert result["reject_layer"] is None
    assert result["technical"]["setup"] == "PULLBACK"
    assert result["risk"]["shares"] > 0


def test_reject_at_risk_layer_when_max_positions_reached(memory_conn: sqlite3.Connection) -> None:
    _seed_spy(memory_conn, bullish=True)
    _seed_ticker(memory_conn, "XYZ", trigger_pullback=True)
    result = get_verdict(
        memory_conn, "XYZ", fetch_fn=_fake_fetch(STRONG_METRICS),
        portfolio_state={"open_positions": 999},
    )
    assert result["verdict"] == "REJECT"
    assert result["reject_layer"] == "risk"
