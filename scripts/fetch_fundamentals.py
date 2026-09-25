"""Fetches and caches Layer 2 fundamentals (FMP) for the configured universe.

Usage:
    python scripts/fetch_fundamentals.py [--tickers AAPL MSFT] [--db data/swing.db] [--config config.yaml]

Requires FMP_API_KEY in .env (Financial Modeling Prep free tier).
"""
from __future__ import annotations

import argparse
import os

from swing_agent.config import load_config
from swing_agent.data.fundamentals import fetch_and_store_fundamentals
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_connection

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tickers = args.tickers or cfg.fundamental.universe
    db_path = args.db or cfg.data.db_path
    api_key = os.environ.get("FMP_API_KEY", "")

    if not api_key:
        print("FMP_API_KEY not set in .env — cannot fetch live fundamentals.")
        raise SystemExit(1)

    conn = get_connection(db_path)
    try:
        for ticker in tickers:
            metrics = fetch_and_store_fundamentals(conn, ticker, api_key)
            print(
                f"{ticker}: roic={metrics.get('roic')} fcf_margin={metrics.get('fcf_margin')} "
                f"revenue_growth_yoy={metrics.get('revenue_growth_yoy')}"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
