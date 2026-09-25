"""Runs a walk-forward backtest over a date range for a ticker universe.

Usage:
    python scripts/run_backtest.py --start 2022-01-01 --end 2024-12-31 [--tickers AAPL MSFT] [--json]

NOTE: Layer 2 (fundamentals) is intentionally not applied in backtests --
see swing_agent.backtest.engine.run_backtest's docstring for why (no $0
point-in-time historical fundamentals source exists). This backtests the
macro + technical signal in isolation.
"""
from __future__ import annotations

import argparse
import json

from swing_agent.backtest.engine import run_backtest
from swing_agent.backtest.metrics import compute_metrics
from swing_agent.config import load_config
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_connection

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tickers = args.tickers or cfg.fundamental.universe
    db_path = args.db or cfg.data.db_path

    conn = get_connection(db_path)
    try:
        result = run_backtest(conn, tickers, args.start, args.end)
    finally:
        conn.close()

    metrics = compute_metrics(result["trades"], result["equity_curve"], result["starting_equity"])

    if args.json:
        print(json.dumps({"metrics": metrics, "trades": result["trades"]}, indent=2, default=str))
        return

    print(f"Backtest {args.start} to {args.end} over {len(tickers)} tickers:")
    print(f"  Trades: {metrics['num_trades']}")
    if metrics["num_trades"]:
        print(f"  Win rate: {metrics['win_rate']:.1f}%")
        print(f"  Avg R: {metrics['avg_r']:.2f}")
        print(f"  Expectancy: {metrics['expectancy_r']:.2f}R")
        print(f"  Max drawdown: {metrics['max_drawdown_pct']:.1f}%")
        print(f"  Sharpe: {metrics['sharpe']:.2f}" if metrics["sharpe"] is not None else "  Sharpe: N/A")
        print(f"  Final equity: ${metrics['final_equity']:.2f} ({metrics['total_return_pct']:+.1f}%)")


if __name__ == "__main__":
    main()
