from __future__ import annotations

import sqlite3

import requests

from swing_agent.logging_setup import get_logger

logger = get_logger(__name__)

EODHD_BASE_URL = "https://eodhd.com/api"


class EodhdFetchError(RuntimeError):
    """Raised when EODHD_API_KEY is missing or the API returns an
    incomplete/unexpected response for a ticker."""


def _eodhd_get(endpoint: str, api_key: str, **params) -> dict:
    url = f"{EODHD_BASE_URL}/{endpoint}"
    resp = requests.get(url, params={**params, "api_token": api_key, "fmt": "json"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_raw_fundamentals(ticker: str, api_key: str, exchange: str = "US") -> dict:
    """Full EODHD fundamentals payload for ticker.exchange (default US),
    including yearly Income_Statement/Balance_Sheet/Cash_Flow -- genuine
    point-in-time historical data (unlike FMP's free tier, which is
    current/TTM only), each year tagged with both the fiscal period-end
    ("date") and the actual SEC filing date ("filing_date")."""
    if not api_key:
        raise EodhdFetchError("EODHD_API_KEY is not set; cannot fetch fundamentals.")
    symbol = f"{ticker}.{exchange}"
    data = _eodhd_get(f"fundamentals/{symbol}", api_key)
    if not data or "Financials" not in data:
        raise EodhdFetchError(f"Incomplete EODHD data for ticker={ticker!r}")
    return data


def _num(value) -> float | None:
    """EODHD frequently returns numeric fields as strings (or 'None' /
    null); coerce safely, treating anything unparseable as missing."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # filter NaN


def _lookup_price_and_volume(
    conn: sqlite3.Connection, ticker: str, as_of_date: str, volume_window: int = 20
) -> tuple[float | None, float | None]:
    """Point-in-time price/volume from OUR OWN prices table (not EODHD's
    payload) as of `as_of_date` -- more point-in-time-correct than trusting
    a vendor "current" snapshot for a historical filing, and avoids a
    second, less certain field-name dependency on EODHD's schema."""
    price_row = conn.execute(
        "SELECT close FROM prices WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        (ticker, as_of_date),
    ).fetchone()
    price = price_row[0] if price_row else None

    vol_row = conn.execute(
        """
        SELECT AVG(volume) FROM (
            SELECT volume FROM prices WHERE ticker = ? AND date <= ?
            ORDER BY date DESC LIMIT ?
        )
        """,
        (ticker, as_of_date, volume_window),
    ).fetchone()
    avg_volume = vol_row[0] if vol_row and vol_row[0] is not None else None
    return price, avg_volume


def compute_fundamental_metrics_series(conn: sqlite3.Connection, ticker: str, raw: dict) -> list[dict]:
    """Derives one fundamentals-table row PER available annual filing,
    oldest to newest -- the point-in-time historical series that makes
    genuine (no-look-ahead) Layer 2 backtesting possible.

    `filed_date` uses EODHD's "filing_date" (actual public disclosure date)
    rather than "date" (fiscal period end) -- filing_date is what point-in-
    time discipline actually cares about (a fiscal year ending Dec 31 isn't
    knowable to the market until the 10-K is filed, often 2-3 months
    later). Falls back to "date" only if filing_date is absent.

    Field names follow EODHD's documented Fundamentals API schema as of
    this writing. Any single missing/unparseable field yields None for
    that metric rather than dropping the whole year (an empty-frame-style
    guard, not a hard failure) -- a missing metric fails its threshold
    check downstream rather than crashing the fetch.
    """
    financials = raw.get("Financials", {})
    income_yearly = financials.get("Income_Statement", {}).get("yearly", {}) or {}
    cash_flow_yearly = financials.get("Cash_Flow", {}).get("yearly", {}) or {}
    balance_yearly = financials.get("Balance_Sheet", {}).get("yearly", {}) or {}

    # Years present in the income statement drive the series (revenue/
    # earnings growth need a prior year to diff against).
    years = sorted(income_yearly.keys())
    results: list[dict] = []

    for i, year_key in enumerate(years):
        income = income_yearly.get(year_key) or {}
        cash_flow = cash_flow_yearly.get(year_key) or {}
        balance = balance_yearly.get(year_key) or {}

        filed_date = income.get("filing_date") or income.get("date") or year_key
        period_end = income.get("date") or year_key

        revenue = _num(income.get("totalRevenue"))
        net_income = _num(income.get("netIncome"))

        revenue_growth_yoy = None
        earnings_growth_yoy = None
        if i > 0:
            prior_income = income_yearly.get(years[i - 1]) or {}
            prior_revenue = _num(prior_income.get("totalRevenue"))
            prior_net_income = _num(prior_income.get("netIncome"))
            if revenue is not None and prior_revenue:
                revenue_growth_yoy = (revenue - prior_revenue) / abs(prior_revenue) * 100
            if net_income is not None and prior_net_income:
                earnings_growth_yoy = (net_income - prior_net_income) / abs(prior_net_income) * 100

        fcf = _num(cash_flow.get("freeCashFlow"))
        if fcf is None:
            ocf = _num(cash_flow.get("totalCashFromOperatingActivities"))
            capex = _num(cash_flow.get("capitalExpenditures"))
            if ocf is not None and capex is not None:
                # EODHD stores capitalExpenditures as a POSITIVE magnitude
                # (confirmed against real AAPL data: ocf - capex == their
                # own reported freeCashFlow exactly), unlike FMP's
                # convention of storing it as a negative outflow.
                fcf = ocf - capex
        fcf_margin = fcf / revenue * 100 if fcf is not None and revenue else None

        ebit = _num(income.get("ebit"))
        income_before_tax = _num(income.get("incomeBeforeTax"))
        income_tax_expense = _num(income.get("incomeTaxExpense"))
        equity = _num(balance.get("totalStockholderEquity"))
        debt = _num(balance.get("shortLongTermDebtTotal"))
        cash = _num(balance.get("cash"))

        roic = None
        if ebit is not None and equity is not None and debt is not None and cash is not None:
            invested_capital = debt + equity - cash
            tax_rate = 0.21  # US statutory rate fallback
            if income_before_tax and income_tax_expense is not None and income_before_tax != 0:
                implied = income_tax_expense / income_before_tax
                if 0 <= implied <= 1:
                    tax_rate = implied
            nopat = ebit * (1 - tax_rate)
            if invested_capital > 0:
                roic = nopat / invested_capital * 100

        price, avg_daily_volume = _lookup_price_and_volume(conn, ticker, filed_date)

        results.append(
            {
                "ticker": ticker,
                "filed_date": filed_date,
                "roic": roic,
                "fcf": fcf,
                "fcf_margin": fcf_margin,
                "revenue_growth_yoy": revenue_growth_yoy,
                "earnings_growth_yoy": earnings_growth_yoy,
                "price": price,
                "avg_daily_volume": avg_daily_volume,
                "period_end": period_end,
            }
        )

    return results


def compute_latest_fundamental_metrics(conn: sqlite3.Connection, ticker: str, raw: dict) -> dict:
    """Most recent year's metrics dict -- for the live TTL-cache path,
    matching agents/fundamental.py's fetch_fn(conn, ticker, api_key) -> dict
    contract."""
    series = compute_fundamental_metrics_series(conn, ticker, raw)
    if not series:
        raise EodhdFetchError(f"No computable fundamentals for ticker={ticker!r}")
    return series[-1]


def fetch_and_store_fundamentals(conn: sqlite3.Connection, ticker: str, api_key: str) -> dict:
    """Live-path fetch_fn: matches agents/fundamental.py's injectable
    fetch_fn contract. Writes and returns only the latest year's row."""
    from swing_agent.storage.db import upsert_fundamentals

    logger.info("Fetching fundamentals for %s from EODHD", ticker)
    raw = fetch_raw_fundamentals(ticker, api_key)
    metrics = compute_latest_fundamental_metrics(conn, ticker, raw)
    upsert_fundamentals(conn, metrics)
    return metrics


def extract_earnings_report_dates(raw: dict) -> list[str]:
    """Pulls every historical reportDate out of EODHD's Earnings.History
    (already present in fetch_raw_fundamentals's payload -- no separate API
    call). Dates with a null/missing reportDate (upcoming, not-yet-reported
    quarters) are skipped. Returns ascending, deduplicated dates."""
    history = (raw.get("Earnings") or {}).get("History") or {}
    dates = {
        entry.get("reportDate")
        for entry in history.values()
        if isinstance(entry, dict) and entry.get("reportDate")
    }
    return sorted(dates)


def fetch_and_store_historical_fundamentals(conn: sqlite3.Connection, ticker: str, api_key: str) -> int:
    """Backtest-path: writes ALL available annual fundamentals rows (one per
    filed_date), enabling genuine point-in-time Layer 2 backtesting, AND all
    known historical earnings report dates (Tier 1 earnings filter) from the
    same already-fetched payload. Returns the number of fundamentals rows
    written (earnings row count is logged separately)."""
    from swing_agent.storage.db import upsert_earnings_dates, upsert_fundamentals

    logger.info("Fetching historical fundamentals for %s from EODHD", ticker)
    raw = fetch_raw_fundamentals(ticker, api_key)
    series = compute_fundamental_metrics_series(conn, ticker, raw)
    for metrics in series:
        upsert_fundamentals(conn, metrics)
    logger.info("Wrote %d historical fundamentals rows for %s", len(series), ticker)

    report_dates = extract_earnings_report_dates(raw)
    n_earnings = upsert_earnings_dates(conn, ticker, report_dates)
    logger.info("Wrote %d earnings report dates for %s", n_earnings, ticker)

    return len(series)
