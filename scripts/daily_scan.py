"""Daily automation entry point.

Refreshes price data for the macro tickers (SPY/^VIX) and the scan
watchlist via yfinance (free), then runs the full macro -> fundamental ->
technical -> risk chain over the watchlist and writes the day's results to
data/scans/scan_<date>.json in addition to printing them.

Fundamentals are NOT force-refreshed here -- get_fundamental_verdict's
existing 7-day TTL cache (see agents/fundamental.py) already handles that,
calling FMP only for tickers whose cached data has actually gone stale, so a
daily run stays well within FMP's free-tier quota (250 calls/day) without
any extra throttling logic here.

Usage:
    python scripts/daily_scan.py [--tickers T1 T2 ...] [--skip-fetch] [--json]

Intended to be invoked by Windows Task Scheduler -- see scripts/daily_scan.bat.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from swing_agent.agents.orchestrator import get_verdict
from swing_agent.config import load_config
from swing_agent.data.macro import fetch_and_store_macro_series
from swing_agent.data.prices import fetch_and_store_prices
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_connection

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickers", nargs="+", default=None, help="Watchlist to scan (default: config.yaml's fundamental.universe)")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip refreshing price/macro data; use what's already in the DB")
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    watchlist = args.tickers or cfg.fundamental.universe
    db_path = args.db or cfg.data.db_path

    conn = get_connection(db_path)
    try:
        if not args.skip_fetch:
            logger.info("Refreshing macro price data: %s", cfg.data.tickers)
            for ticker in cfg.data.tickers:
                fetch_and_store_prices(conn, ticker, period=f"{cfg.data.price_history_years}y")
            fetch_and_store_macro_series(conn, "DGS10")

            logger.info("Refreshing watchlist price data: %s", watchlist)
            for ticker in watchlist:
                if ticker not in cfg.data.tickers:
                    fetch_and_store_prices(conn, ticker, period=f"{cfg.data.price_history_years}y")

        results = []
        for ticker in watchlist:
            try:
                result = get_verdict(conn, ticker)
            except Exception as exc:
                # One bad ticker (missing data, a vendor hiccup) shouldn't
                # abort the whole scheduled run.
                logger.error("Error evaluating %s: %s", ticker, exc)
                result = {
                    "ticker": ticker, "verdict": "ERROR", "as_of_date": None,
                    "reject_layer": None, "reasoning": str(exc),
                }
            results.append(result)
    finally:
        conn.close()

    approved = [r for r in results if r["verdict"] == "APPROVE"]
    today = date.today().isoformat()
    out_dir = Path("data/scans")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"scan_{today}.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote %d results to %s (%d approved)", len(results), out_path, len(approved))

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(f"Daily scan {today}: {len(approved)}/{len(results)} approved")
        for r in approved:
            risk = r["risk"]
            print(
                f"  {r['ticker']}: {r['technical']['setup']} entry={risk['entry']:.2f} "
                f"stop={risk['stop']:.2f} shares={risk['shares']}"
            )


if __name__ == "__main__":
    main()
