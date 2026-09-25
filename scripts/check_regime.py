"""Smoke test: opens the real data/swing.db and prints the current macro regime.

Usage:
    python scripts/check_regime.py
"""
from __future__ import annotations

import sys

from swing_agent.agents.macro_regime import MacroRegimeError, get_macro_regime
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_connection

logger = get_logger(__name__)


def main() -> None:
    conn = get_connection()
    try:
        result = get_macro_regime(conn)
    except MacroRegimeError as exc:
        logger.error("Could not compute macro regime: %s", exc)
        sys.exit(1)
    finally:
        conn.close()

    print(f"As of {result['as_of_date']}:")
    print(f"  Regime: {result['regime']} (position size modifier {result['position_size_modifier']}x)")
    print(f"  SPY close: {result['spy_close']:.2f} | 200MA: {result['spy_200ma']:.2f}")
    print(f"  VIX: {result['vix']:.2f}")
    yield_10y = result["yield_10y"]
    print(f"  10Y yield: {yield_10y:.2f}" if yield_10y is not None else "  10Y yield: N/A")
    print(f"  Reasoning: {result['reasoning']}")


if __name__ == "__main__":
    main()
