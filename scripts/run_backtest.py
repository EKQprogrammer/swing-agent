"""Runs a walk-forward backtest over a date range for a ticker universe.

Usage:
    python scripts/run_backtest.py --start 2022-01-01 --end 2024-12-31 [--tickers AAPL MSFT]
        [--account-equity 13615] [--narratives] [--json]

NOTE: Layer 2 (fundamentals) is intentionally not applied in backtests --
see swing_agent.backtest.engine.run_backtest's docstring for why (no $0
point-in-time historical fundamentals source exists). This backtests the
macro + technical signal in isolation.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from swing_agent.backtest.engine import run_backtest
from swing_agent.backtest.metrics import compute_metrics
from swing_agent.backtest.narrative import build_trade_narratives
from swing_agent.config import load_config
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_connection

logger = get_logger(__name__)


def _breakdown_by_setup(trades: list[dict]) -> dict[str, dict]:
    by_setup: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_setup[t["setup"]].append(t)
    return {
        setup: compute_metrics(setup_trades, [], starting_equity=0.0)
        for setup, setup_trades in by_setup.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--account-equity", type=float, default=None, help="Starting equity in USD (default: config.yaml's account.account_size)")
    parser.add_argument("--narratives", action="store_true", help="Add a plain-English 'why did I win/lose' narrative to each trade")
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tickers = args.tickers or cfg.backtest.tickers
    db_path = args.db or cfg.data.db_path

    conn = get_connection(db_path)
    try:
        result = run_backtest(conn, tickers, args.start, args.end, account_equity=args.account_equity)
    finally:
        conn.close()

    trades = result["trades"]
    if args.narratives:
        trades = build_trade_narratives(trades)

    metrics = compute_metrics(trades, result["equity_curve"], result["starting_equity"])
    by_setup = _breakdown_by_setup(result["trades"])

    if args.json:
        output = {
            "metrics": metrics,
            "by_setup": by_setup,
            "trades": trades,
            "equity_curve": result["equity_curve"],
        }
        print(json.dumps(output, indent=2, default=str))
        return

    print(f"Backtest {args.start} to {args.end} over {len(tickers)} tickers (starting equity ${result['starting_equity']:,.2f}):")
    print(f"  Trades: {metrics['num_trades']}")
    if metrics["num_trades"]:
        print(f"  Win rate: {metrics['win_rate']:.1f}%")
        print(f"  Avg R: {metrics['avg_r']:.2f}")
        print(f"  Expectancy: {metrics['expectancy_r']:.2f}R")
        print(f"  Max drawdown: {metrics['max_drawdown_pct']:.1f}%")
        print(f"  Sharpe: {metrics['sharpe']:.2f}" if metrics["sharpe"] is not None else "  Sharpe: N/A")
        print(f"  Final equity: ${metrics['final_equity']:,.2f} ({metrics['total_return_pct']:+.1f}%)")
        print("  By setup:")
        for setup, m in by_setup.items():
            print(f"    {setup}: {m['num_trades']} trades, {m['win_rate']:.1f}% win rate, {m['avg_r']:.2f} avg R")

    if args.narratives:
        print("\nTrade narratives:")
        for t in trades:
            print(f"  - {t['narrative']}")


if __name__ == "__main__":
    main()
