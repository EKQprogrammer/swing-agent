from __future__ import annotations

import os
import sqlite3

import pandas as pd
import pytest

from swing_agent.data.eodhd import (
    EodhdFetchError,
    compute_fundamental_metrics_series,
    compute_latest_fundamental_metrics,
    fetch_and_store_fundamentals,
    fetch_and_store_historical_fundamentals,
    fetch_raw_fundamentals,
)
from swing_agent.storage.db import get_cached_fundamentals, upsert_prices


def _price_df(dates, closes) -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": [1_000_000] * len(closes)},
        index=pd.to_datetime(dates),
    )


def _raw_payload() -> dict:
    return {
        "General": {"Code": "ACME"},
        "Financials": {
            "Income_Statement": {
                "yearly": {
                    "2021-12-31": {
                        "date": "2021-12-31", "filing_date": "2022-02-15",
                        "totalRevenue": "1000000000", "netIncome": "100000000",
                        "ebit": "160000000", "incomeBeforeTax": "150000000", "incomeTaxExpense": "30000000",
                    },
                    "2022-12-31": {
                        "date": "2022-12-31", "filing_date": "2023-02-15",
                        "totalRevenue": "1100000000", "netIncome": "120000000",
                        "ebit": "180000000", "incomeBeforeTax": "170000000", "incomeTaxExpense": "35000000",
                    },
                    "2023-12-31": {
                        "date": "2023-12-31", "filing_date": "2024-02-15",
                        "totalRevenue": "1210000000", "netIncome": "150000000",
                        "ebit": "200000000", "incomeBeforeTax": "190000000", "incomeTaxExpense": "40000000",
                    },
                }
            },
            "Cash_Flow": {
                "yearly": {
                    "2021-12-31": {"freeCashFlow": "90000000"},
                    "2022-12-31": {  # no direct freeCashFlow -> exercises the OCF-capex fallback
                        "totalCashFromOperatingActivities": "150000000",
                        "capitalExpenditures": "30000000",  # EODHD stores capex as a positive magnitude
                    },
                    "2023-12-31": {"freeCashFlow": "130000000"},
                }
            },
            "Balance_Sheet": {
                "yearly": {
                    "2021-12-31": {"totalStockholderEquity": "700000000", "shortLongTermDebtTotal": "150000000", "cash": "80000000"},
                    "2022-12-31": {"totalStockholderEquity": "750000000", "shortLongTermDebtTotal": "180000000", "cash": "90000000"},
                    "2023-12-31": {"totalStockholderEquity": "800000000", "shortLongTermDebtTotal": "200000000", "cash": "100000000"},
                }
            },
        },
    }


def _seed_prices(conn: sqlite3.Connection) -> None:
    dates = pd.date_range("2022-01-01", periods=800, freq="D")
    closes = [50.0 + i * 0.01 for i in range(len(dates))]
    upsert_prices(conn, "ACME", _price_df(dates, closes))


def test_compute_metrics_series_growth_and_fcf(memory_conn: sqlite3.Connection) -> None:
    _seed_prices(memory_conn)
    series = compute_fundamental_metrics_series(memory_conn, "ACME", _raw_payload())
    assert len(series) == 3

    y2021, y2022, y2023 = series
    assert y2021["filed_date"] == "2022-02-15"
    assert y2021["revenue_growth_yoy"] is None  # no prior year to diff against
    assert y2021["fcf"] == pytest.approx(90_000_000)

    # 2022: (1100-1000)/1000*100 = 10%, (120-100)/100*100 = 20%
    assert y2022["revenue_growth_yoy"] == pytest.approx(10.0)
    assert y2022["earnings_growth_yoy"] == pytest.approx(20.0)
    # FCF fallback: OCF - capex(positive magnitude) = 150M - 30M = 120M
    assert y2022["fcf"] == pytest.approx(120_000_000)
    assert y2022["fcf_margin"] == pytest.approx(120_000_000 / 1_100_000_000 * 100)

    # 2023: (1210-1100)/1100*100 = 10%, (150-120)/120*100 = 25%
    assert y2023["revenue_growth_yoy"] == pytest.approx(10.0)
    assert y2023["earnings_growth_yoy"] == pytest.approx(25.0)
    assert y2023["fcf"] == pytest.approx(130_000_000)


def test_compute_metrics_series_roic() -> None:
    conn = sqlite3.connect(":memory:")
    from swing_agent.storage.db import init_schema
    init_schema(conn)
    series = compute_fundamental_metrics_series(conn, "ACME", _raw_payload())
    y2023 = series[-1]
    # ebit=200M, tax_rate=40/190=0.21053, nopat=200M*0.78947=157.895M
    # invested_capital = 200M + 800M - 100M = 900M
    # roic = 157.895M / 900M * 100 = 17.544%
    assert y2023["roic"] == pytest.approx(17.5438, abs=0.01)
    conn.close()


def test_point_in_time_price_lookup(memory_conn: sqlite3.Connection) -> None:
    _seed_prices(memory_conn)
    series = compute_fundamental_metrics_series(memory_conn, "ACME", _raw_payload())
    # price as of each filed_date should be looked up from OUR prices table,
    # not the (nonexistent) EODHD price field
    assert all(row["price"] is not None for row in series)
    assert all(row["avg_daily_volume"] == pytest.approx(1_000_000) for row in series)
    # prices are monotonically increasing in the fixture, and filed_dates are ascending
    assert series[0]["price"] < series[1]["price"] < series[2]["price"]


def test_fcf_fallback_subtracts_positive_capex(memory_conn: sqlite3.Connection) -> None:
    """Discriminating regression test: OCF and capex chosen so ocf-capex !=
    ocf+capex, catching the sign-convention bug found against real EODHD
    data (capitalExpenditures is a positive magnitude, not a negative
    outflow like FMP's convention -- confirmed: real AAPL data satisfies
    ocf - capex == their own reported freeCashFlow exactly)."""
    raw = {
        "Financials": {
            "Income_Statement": {"yearly": {"2022-12-31": {"date": "2022-12-31", "filing_date": "2023-02-01", "totalRevenue": "500000000", "netIncome": "50000000"}}},
            "Cash_Flow": {"yearly": {"2022-12-31": {"totalCashFromOperatingActivities": "100000000", "capitalExpenditures": "40000000"}}},
            "Balance_Sheet": {"yearly": {"2022-12-31": {}}},
        }
    }
    series = compute_fundamental_metrics_series(memory_conn, "ACME", raw)
    # correct: 100M - 40M = 60M. bug would give 100M + 40M = 140M.
    assert series[0]["fcf"] == pytest.approx(60_000_000)


def test_missing_roic_components_yields_none(memory_conn: sqlite3.Connection) -> None:
    raw = _raw_payload()
    del raw["Financials"]["Balance_Sheet"]["yearly"]["2023-12-31"]["cash"]
    series = compute_fundamental_metrics_series(memory_conn, "ACME", raw)
    assert series[-1]["roic"] is None
    # other metrics for that year remain computable
    assert series[-1]["fcf"] is not None


def test_compute_latest_returns_most_recent_year(memory_conn: sqlite3.Connection) -> None:
    latest = compute_latest_fundamental_metrics(memory_conn, "ACME", _raw_payload())
    assert latest["filed_date"] == "2024-02-15"


def test_empty_income_statement_raises(memory_conn: sqlite3.Connection) -> None:
    raw = {"Financials": {"Income_Statement": {"yearly": {}}, "Cash_Flow": {"yearly": {}}, "Balance_Sheet": {"yearly": {}}}}
    with pytest.raises(EodhdFetchError):
        compute_latest_fundamental_metrics(memory_conn, "ACME", raw)


def test_fetch_and_store_historical_writes_all_years(memory_conn: sqlite3.Connection, monkeypatch) -> None:
    _seed_prices(memory_conn)
    monkeypatch.setattr("swing_agent.data.eodhd.fetch_raw_fundamentals", lambda ticker, api_key, exchange="US": _raw_payload())
    n = fetch_and_store_historical_fundamentals(memory_conn, "ACME", "fake-key")
    assert n == 3
    count = memory_conn.execute("SELECT COUNT(*) FROM fundamentals WHERE ticker='ACME'").fetchone()[0]
    assert count == 3


def test_fetch_and_store_live_writes_only_latest(memory_conn: sqlite3.Connection, monkeypatch) -> None:
    _seed_prices(memory_conn)
    monkeypatch.setattr("swing_agent.data.eodhd.fetch_raw_fundamentals", lambda ticker, api_key, exchange="US": _raw_payload())
    metrics = fetch_and_store_fundamentals(memory_conn, "ACME", "fake-key")
    assert metrics["filed_date"] == "2024-02-15"
    cached = get_cached_fundamentals(memory_conn, "ACME", ttl_days=7)
    assert cached is not None
    assert cached["filed_date"] == "2024-02-15"


def test_missing_api_key_raises(memory_conn: sqlite3.Connection) -> None:
    with pytest.raises(EodhdFetchError):
        fetch_raw_fundamentals("ACME", "")


@pytest.mark.live
def test_fetch_raw_fundamentals_live() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.environ.get("EODHD_API_KEY", "")
    if not api_key:
        pytest.skip("EODHD_API_KEY not set")
    raw = fetch_raw_fundamentals("AAPL", api_key)
    assert "Financials" in raw
