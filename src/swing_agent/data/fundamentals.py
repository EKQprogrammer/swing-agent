from __future__ import annotations

import sqlite3

import requests

from swing_agent.logging_setup import get_logger

logger = get_logger(__name__)

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"


class FundamentalsFetchError(RuntimeError):
    """Raised when FMP_API_KEY is missing or the FMP API returns an
    incomplete/unexpected response for a ticker."""


def _fmp_get(endpoint: str, api_key: str, **params) -> list | dict:
    url = f"{FMP_BASE_URL}/{endpoint}"
    resp = requests.get(url, params={**params, "apikey": api_key}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_raw_fundamentals(ticker: str, api_key: str) -> dict:
    """Pulls the handful of FMP free-tier endpoints needed for Layer 2 metrics:
    company profile (price, average volume), two years of annual income
    statements (revenue/earnings YoY growth), the latest annual cash flow
    statement (free cash flow), and TTM key metrics (ROIC). Raises
    FundamentalsFetchError if the key is missing or any payload is empty."""
    if not api_key:
        raise FundamentalsFetchError("FMP_API_KEY is not set; cannot fetch fundamentals.")

    profile = _fmp_get(f"profile/{ticker}", api_key)
    income = _fmp_get(f"income-statement/{ticker}", api_key, period="annual", limit=2)
    cash_flow = _fmp_get(f"cash-flow-statement/{ticker}", api_key, period="annual", limit=1)
    key_metrics = _fmp_get(f"key-metrics-ttm/{ticker}", api_key)

    if not profile or not income or len(income) < 2 or not cash_flow or not key_metrics:
        raise FundamentalsFetchError(f"Incomplete FMP data for ticker={ticker!r}")

    return {
        "profile": profile[0],
        "income_current": income[0],
        "income_prior": income[1],
        "cash_flow": cash_flow[0],
        "key_metrics": key_metrics[0],
    }


def compute_fundamental_metrics(ticker: str, raw: dict) -> dict:
    """Derives a fundamentals-table row from fetch_raw_fundamentals's payload.

    Field names follow FMP API v3's documented schema as of this writing. If
    FMP renames/moves a field, the affected metric falls back to None rather
    than raising, so a single vendor field change doesn't hard-fail the whole
    fetch (the fundamental agent's verdict then treats a None metric as a
    failed threshold check, per the empty-frame/missing-data conventions)."""
    profile = raw["profile"]
    income_cur = raw["income_current"]
    income_prior = raw["income_prior"]
    cash_flow = raw["cash_flow"]
    key_metrics = raw["key_metrics"]

    revenue_cur = income_cur.get("revenue")
    revenue_prior = income_prior.get("revenue")
    revenue_growth_yoy = (
        (revenue_cur - revenue_prior) / abs(revenue_prior) * 100
        if revenue_cur is not None and revenue_prior
        else None
    )

    earnings_cur = income_cur.get("netIncome")
    earnings_prior = income_prior.get("netIncome")
    earnings_growth_yoy = (
        (earnings_cur - earnings_prior) / abs(earnings_prior) * 100
        if earnings_cur is not None and earnings_prior
        else None
    )

    fcf = cash_flow.get("freeCashFlow")
    fcf_margin = fcf / revenue_cur * 100 if fcf is not None and revenue_cur else None

    # FMP returns roicTTM as a decimal fraction (e.g. 0.15), not a percent.
    roic = key_metrics.get("roicTTM")
    if roic is not None:
        roic = roic * 100

    return {
        "ticker": ticker,
        "filed_date": income_cur.get("date"),
        "roic": roic,
        "fcf": fcf,
        "fcf_margin": fcf_margin,
        "revenue_growth_yoy": revenue_growth_yoy,
        "earnings_growth_yoy": earnings_growth_yoy,
        "price": profile.get("price"),
        "avg_daily_volume": profile.get("volAvg"),
    }


def fetch_and_store_fundamentals(conn: sqlite3.Connection, ticker: str, api_key: str) -> dict:
    from swing_agent.storage.db import upsert_fundamentals

    logger.info("Fetching fundamentals for %s from FMP", ticker)
    raw = fetch_raw_fundamentals(ticker, api_key)
    metrics = compute_fundamental_metrics(ticker, raw)
    upsert_fundamentals(conn, metrics)
    return metrics
