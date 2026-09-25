from __future__ import annotations

import os

import pytest

from swing_agent.data.fundamentals import (
    compute_fundamental_metrics,
    fetch_raw_fundamentals,
)


def _raw_payload() -> dict:
    return {
        "profile": {"price": 150.0, "volAvg": 25_000_000},
        "income_current": {"date": "2025-12-31", "revenue": 1_100_000_000, "netIncome": 220_000_000},
        "income_prior": {"date": "2024-12-31", "revenue": 1_000_000_000, "netIncome": 200_000_000},
        "cash_flow": {"freeCashFlow": 150_000_000},
        "key_metrics": {"roicTTM": 0.18},
    }


def test_compute_fundamental_metrics_happy_path() -> None:
    metrics = compute_fundamental_metrics("ACME", _raw_payload())
    assert metrics["ticker"] == "ACME"
    assert metrics["filed_date"] == "2025-12-31"
    assert metrics["roic"] == pytest.approx(18.0)
    assert metrics["fcf"] == 150_000_000
    assert metrics["fcf_margin"] == pytest.approx(150_000_000 / 1_100_000_000 * 100)
    assert metrics["revenue_growth_yoy"] == pytest.approx(10.0)
    assert metrics["earnings_growth_yoy"] == pytest.approx(10.0)
    assert metrics["price"] == 150.0
    assert metrics["avg_daily_volume"] == 25_000_000


def test_compute_fundamental_metrics_missing_field_falls_back_to_none() -> None:
    raw = _raw_payload()
    del raw["key_metrics"]["roicTTM"]
    metrics = compute_fundamental_metrics("ACME", raw)
    assert metrics["roic"] is None
    # Other metrics still computed despite the missing field.
    assert metrics["revenue_growth_yoy"] == pytest.approx(10.0)


def test_compute_fundamental_metrics_zero_prior_revenue_guards_division() -> None:
    raw = _raw_payload()
    raw["income_prior"]["revenue"] = 0
    metrics = compute_fundamental_metrics("ACME", raw)
    assert metrics["revenue_growth_yoy"] is None


@pytest.mark.live
def test_fetch_raw_fundamentals_live() -> None:
    api_key = os.environ.get("FMP_API_KEY", "")
    if not api_key:
        pytest.skip("FMP_API_KEY not set")
    raw = fetch_raw_fundamentals("AAPL", api_key)
    assert "profile" in raw
