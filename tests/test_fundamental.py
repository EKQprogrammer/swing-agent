from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_agent.agents.fundamental import (
    FundamentalError,
    calculate_relative_strength,
    get_fundamental_verdict,
    get_fundamental_verdict_as_of,
)
from swing_agent.storage.db import upsert_fundamentals, upsert_prices

FILED_DATE = "2024-06-01"
LOOKBACK = 252


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


def _seed_return_series(
    conn: sqlite3.Connection, ticker: str, total_return: float, end_date: str = FILED_DATE
) -> None:
    """Seeds LOOKBACK+1 daily rows ending on end_date with an exact total
    return from the oldest to the newest close (linear interpolation)."""
    days = LOOKBACK + 1
    dates = pd.date_range(end=end_date, periods=days, freq="D")
    start_price = 100.0
    end_price = start_price * (1 + total_return)
    step = (end_price - start_price) / (days - 1)
    closes = [start_price + i * step for i in range(days)]
    upsert_prices(conn, ticker, _price_df(dates, closes))


PASSING_METRICS = {
    "filed_date": FILED_DATE,
    "roic": 15.0,
    "fcf": 1_000_000.0,
    "fcf_margin": 10.0,
    "revenue_growth_yoy": 8.0,
    "earnings_growth_yoy": 12.0,
    "price": 50.0,
    "avg_daily_volume": 1_000_000,
}


def _fake_fetch(call_log: list[str], metrics: dict | None = None):
    def fetch_fn(conn: sqlite3.Connection, ticker: str, api_key: str) -> dict:
        call_log.append(ticker)
        row = {**(metrics or PASSING_METRICS), "ticker": ticker}
        upsert_fundamentals(conn, row)
        return row

    return fetch_fn


# --- calculate_relative_strength -------------------------------------------------


def test_relative_strength_top_performer_is_100(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=0.50)
    _seed_return_series(memory_conn, "SPY", total_return=0.10)
    _seed_return_series(memory_conn, "AAPL", total_return=0.02)
    rs = calculate_relative_strength(memory_conn, "ACME", ["SPY", "AAPL"], FILED_DATE, LOOKBACK)
    assert rs == pytest.approx(100.0)


def test_relative_strength_worst_performer_is_0(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=-0.10)
    _seed_return_series(memory_conn, "SPY", total_return=0.10)
    _seed_return_series(memory_conn, "AAPL", total_return=0.02)
    rs = calculate_relative_strength(memory_conn, "ACME", ["SPY", "AAPL"], FILED_DATE, LOOKBACK)
    assert rs == pytest.approx(0.0)


def test_relative_strength_insufficient_history_raises(memory_conn: sqlite3.Connection) -> None:
    with pytest.raises(FundamentalError):
        calculate_relative_strength(memory_conn, "ACME", ["SPY"], FILED_DATE, LOOKBACK)


# --- get_fundamental_verdict -------------------------------------------------


def test_verdict_pass_when_all_checks_clear(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=0.50)
    _seed_return_series(memory_conn, "SPY", total_return=0.05)
    _seed_return_series(memory_conn, "AAPL", total_return=0.01)
    call_log: list[str] = []
    result = get_fundamental_verdict(memory_conn, "ACME", fetch_fn=_fake_fetch(call_log))
    assert result["verdict"] == "PASS"
    assert result["failed_checks"] == []
    assert call_log == ["ACME"]


def test_verdict_reject_on_weak_roic(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=0.50)
    _seed_return_series(memory_conn, "SPY", total_return=0.05)
    weak = {**PASSING_METRICS, "roic": 4.0}
    call_log: list[str] = []
    result = get_fundamental_verdict(memory_conn, "ACME", fetch_fn=_fake_fetch(call_log, weak))
    assert result["verdict"] == "REJECT"
    assert "roic" in result["failed_checks"]


def test_verdict_reject_on_weak_relative_strength(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=-0.20)
    _seed_return_series(memory_conn, "SPY", total_return=0.10)
    _seed_return_series(memory_conn, "AAPL", total_return=0.05)
    call_log: list[str] = []
    result = get_fundamental_verdict(memory_conn, "ACME", fetch_fn=_fake_fetch(call_log))
    assert result["verdict"] == "REJECT"
    assert "relative_strength" in result["failed_checks"]


def test_fresh_cache_skips_refetch(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=0.50)
    _seed_return_series(memory_conn, "SPY", total_return=0.05)
    call_log: list[str] = []
    fetch_fn = _fake_fetch(call_log)
    get_fundamental_verdict(memory_conn, "ACME", fetch_fn=fetch_fn)
    get_fundamental_verdict(memory_conn, "ACME", fetch_fn=fetch_fn)
    assert call_log == ["ACME"]  # second call hit the cache, no re-fetch


def test_stale_cache_triggers_refetch(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=0.50)
    _seed_return_series(memory_conn, "SPY", total_return=0.05)
    call_log: list[str] = []
    fetch_fn = _fake_fetch(call_log)
    get_fundamental_verdict(memory_conn, "ACME", fetch_fn=fetch_fn)

    memory_conn.execute(
        "UPDATE fundamentals SET fetched_at = '2000-01-01 00:00:00' WHERE ticker='ACME'"
    )
    memory_conn.commit()

    get_fundamental_verdict(memory_conn, "ACME", fetch_fn=fetch_fn)
    assert call_log == ["ACME", "ACME"]  # stale cache forced a re-fetch


# --- get_fundamental_verdict_as_of (point-in-time, backtest path) -----------------


def test_as_of_pass_when_fundamentals_row_exists_and_clears_thresholds(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=0.50)
    upsert_fundamentals(memory_conn, {**PASSING_METRICS, "ticker": "ACME"})
    result = get_fundamental_verdict_as_of(memory_conn, "ACME", FILED_DATE, universe=[])
    assert result["verdict"] == "PASS"
    assert result["failed_checks"] == []


def test_as_of_reject_on_weak_metric(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=0.50)
    weak = {**PASSING_METRICS, "roic": 2.0, "ticker": "ACME"}
    upsert_fundamentals(memory_conn, weak)
    result = get_fundamental_verdict_as_of(memory_conn, "ACME", FILED_DATE, universe=[])
    assert result["verdict"] == "REJECT"
    assert "roic" in result["failed_checks"]


def test_as_of_no_data_yet_rejects_without_raising(memory_conn: sqlite3.Connection) -> None:
    # No fundamentals row at all for this ticker (e.g. pre-IPO) -- must be a
    # clean REJECT, not an exception, so a backtest loop can just skip it.
    result = get_fundamental_verdict_as_of(memory_conn, "NOPE", FILED_DATE, universe=[])
    assert result["verdict"] == "REJECT"
    assert result["failed_checks"] == ["no_fundamentals_data"]


def test_as_of_uses_only_filings_on_or_before_as_of_date(memory_conn: sqlite3.Connection) -> None:
    _seed_return_series(memory_conn, "ACME", total_return=0.50, end_date="2024-06-01")
    # an OLDER, weak filing and a NEWER, strong filing that hasn't "happened" yet
    upsert_fundamentals(memory_conn, {**PASSING_METRICS, "ticker": "ACME", "filed_date": "2023-01-01", "roic": 2.0})
    upsert_fundamentals(memory_conn, {**PASSING_METRICS, "ticker": "ACME", "filed_date": "2025-06-01", "roic": 20.0})

    # as of mid-2024, only the 2023 (weak) filing should be visible
    result = get_fundamental_verdict_as_of(memory_conn, "ACME", "2024-06-01", universe=[])
    assert result["roic"] == 2.0
    assert result["verdict"] == "REJECT"
