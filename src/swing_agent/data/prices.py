from __future__ import annotations

import sqlite3

import pandas as pd
import yfinance as yf

from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import upsert_prices

logger = get_logger(__name__)


def fetch_price_history(ticker: str, period: str = "3y") -> pd.DataFrame:
    """Fetches OHLCV history for a single ticker via yfinance.

    auto_adjust is pinned explicitly to True (rather than relying on the
    installed yfinance version's default) so closes are consistently
    split/dividend-adjusted, which matters for the 200-day SMA calculation.
    Returns an empty DataFrame (with a warning logged) for a bad/delisted
    ticker rather than raising.
    """
    logger.info("Fetching price history for %s (period=%s)", ticker, period)
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    if df.empty:
        logger.warning("No price data returned for ticker=%s", ticker)
    return df


def fetch_and_store_prices(conn: sqlite3.Connection, ticker: str, period: str = "3y") -> int:
    df = fetch_price_history(ticker, period=period)
    return upsert_prices(conn, ticker, df)
