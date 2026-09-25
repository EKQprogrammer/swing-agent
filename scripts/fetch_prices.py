"""Fetches OHLCV price history for the configured tickers into SQLite.

Usage:
    python scripts/fetch_prices.py [--tickers SPY ^VIX] [--period 3y]
        [--db data/swing.db] [--config config.yaml]
"""
from __future__ import annotations

import argparse

from swing_agent.config import load_config
from swing_agent.data.prices import fetch_and_store_prices
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_connection

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--period", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tickers = args.tickers or cfg.data.tickers
    period = args.period or f"{cfg.data.price_history_years}y"
    db_path = args.db or cfg.data.db_path

    conn = get_connection(db_path)
    try:
        for ticker in tickers:
            rows = fetch_and_store_prices(conn, ticker, period=period)
            print(f"{ticker}: {rows} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
