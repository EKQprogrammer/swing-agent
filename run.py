"""CLI entry point for the swing trading agent.

Usage:
    python run.py TICKER [--as-of YYYY-MM-DD] [--json]
    python run.py --scan [--tickers T1 T2 ...] [--as-of YYYY-MM-DD] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys

from swing_agent.agents.orchestrator import get_verdict
from swing_agent.config import load_config
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_connection

logger = get_logger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("ticker", nargs="?", help="Single ticker to evaluate (omit when using --scan)")
    parser.add_argument("--scan", action="store_true", help="Scan a watchlist instead of a single ticker")
    parser.add_argument(
        "--tickers", nargs="+", default=None,
        help="Watchlist to scan (with --scan) or override for the single ticker; "
        "defaults to config.yaml's fundamental.universe when --scan is used alone",
    )
    parser.add_argument("--as-of", dest="as_of_date", default=None, help="Point-in-time date YYYY-MM-DD (default: latest)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable text")
    parser.add_argument("--db", default=None, help="Path to the SQLite DB (default: config.yaml's data.db_path)")
    parser.add_argument("--config", default="config.yaml")
    return parser


def _print_human(result: dict) -> None:
    verdict = result["verdict"]
    print(f"{result['ticker']} - {verdict} (as of {result.get('as_of_date')})")
    if verdict == "APPROVE":
        risk = result["risk"]
        technical = result["technical"]
        print(f"  Setup: {technical['setup']}")
        print(f"  Entry: {risk['entry']:.2f}  Stop: {risk['stop']:.2f}  Shares: {risk['shares']}")
        print(
            f"  Risk: ${risk['risk_dollars']:.2f}  "
            f"Targets: 2R={risk['targets']['2R']:.2f} 3R={risk['targets']['3R']:.2f}"
        )
    else:
        print(f"  Rejected at: {result.get('reject_layer', 'n/a')} layer")
        print(f"  Reason: {result.get('reasoning')}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.scan and not args.ticker:
        parser.error("Provide a TICKER or use --scan")

    cfg = load_config(args.config)
    db_path = args.db or cfg.data.db_path
    if args.scan:
        tickers = args.tickers or cfg.fundamental.universe
    else:
        tickers = [args.ticker]

    conn = get_connection(db_path)
    results = []
    try:
        for ticker in tickers:
            try:
                result = get_verdict(conn, ticker, as_of_date=args.as_of_date)
            except Exception as exc:
                # A single bad ticker (e.g. no price history fetched yet)
                # shouldn't abort a whole --scan run.
                logger.error("Error evaluating %s: %s", ticker, exc)
                result = {
                    "ticker": ticker, "verdict": "ERROR", "as_of_date": args.as_of_date,
                    "reject_layer": None, "reasoning": str(exc),
                }
            results.append(result)
    finally:
        conn.close()

    output = results if args.scan else results[0]
    if args.json:
        print(json.dumps(output, indent=2, default=str))
    elif args.scan:
        for result in results:
            _print_human(result)
    else:
        _print_human(results[0])

    return 0


if __name__ == "__main__":
    sys.exit(main())
