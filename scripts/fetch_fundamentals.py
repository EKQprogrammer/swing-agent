"""Fetches and caches Layer 2 fundamentals (EODHD) for the configured universe.

Usage:
    python scripts/fetch_fundamentals.py [--tickers AAPL MSFT] [--historical] [--db data/swing.db] [--config config.yaml]

--historical writes ALL available annual filings (point-in-time history, for
backtesting) instead of just the latest year (for the live 7-day TTL cache).

Requires EODHD_API_KEY in .env (EODHD Fundamentals Data Feed plan).
"""
from __future__ import annotations

import argparse
import os

from swing_agent.config import load_config
from swing_agent.data.eodhd import (
    fetch_and_store_fundamentals,
    fetch_and_store_historical_fundamentals,
)
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_connection

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--historical", action="store_true", help="Write all available years (point-in-time history) instead of just the latest")
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.tickers:
        tickers = args.tickers
    elif args.historical:
        tickers = cfg.backtest.tickers  # the 30-ticker backtest universe
    else:
        tickers = cfg.fundamental.universe  # the smaller live-scan watchlist
    db_path = args.db or cfg.data.db_path
    api_key = os.environ.get("EODHD_API_KEY", "")

    if not api_key:
        print("EODHD_API_KEY not set in .env — cannot fetch live fundamentals.")
        raise SystemExit(1)

    conn = get_connection(db_path)
    try:
        for ticker in tickers:
            if args.historical:
                n = fetch_and_store_historical_fundamentals(conn, ticker, api_key)
                print(f"{ticker}: wrote {n} year(s) of historical fundamentals")
            else:
                metrics = fetch_and_store_fundamentals(conn, ticker, api_key)
                print(
                    f"{ticker}: roic={metrics.get('roic')} fcf_margin={metrics.get('fcf_margin')} "
                    f"revenue_growth_yoy={metrics.get('revenue_growth_yoy')}"
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
