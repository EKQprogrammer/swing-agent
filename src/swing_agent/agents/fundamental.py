from __future__ import annotations

import os
import sqlite3
from typing import Callable

from swing_agent.config import load_config
from swing_agent.data.eodhd import fetch_and_store_fundamentals
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_cached_fundamentals

logger = get_logger(__name__)


class FundamentalError(RuntimeError):
    """Raised when fundamentals data cannot be obtained (no fresh cache and no
    successful live fetch) or when relative strength cannot be computed."""


def calculate_relative_strength(
    conn: sqlite3.Connection,
    ticker: str,
    universe: list[str],
    as_of_date: str,
    lookback_days: int = 252,
) -> float:
    """IBD-style relative strength: percentile rank (0-100) of `ticker`'s
    trailing `lookback_days` price return among `universe`. Point-in-time:
    only uses price rows with date <= as_of_date. Universe members with
    insufficient history are excluded from the ranking rather than treated
    as a zero return. Computed entirely from the local `prices` table — no
    network call.
    """
    members = list(dict.fromkeys([ticker, *universe]))  # de-dup, preserve order
    returns: dict[str, float] = {}
    for member in members:
        rows = conn.execute(
            """
            SELECT close FROM prices
            WHERE ticker = ? AND date <= ?
            ORDER BY date DESC LIMIT ?
            """,
            (member, as_of_date, lookback_days + 1),
        ).fetchall()
        if len(rows) < lookback_days + 1:
            continue
        latest_close = rows[0][0]
        oldest_close = rows[-1][0]
        if oldest_close:
            returns[member] = (latest_close - oldest_close) / oldest_close

    if ticker not in returns:
        raise FundamentalError(
            f"Insufficient price history for {ticker} to compute relative strength "
            f"(need {lookback_days + 1} rows as of {as_of_date})."
        )
    if len(returns) < 2:
        return 100.0  # nothing to rank against

    sorted_returns = sorted(returns.values())
    rank = sorted_returns.index(returns[ticker])
    return rank / (len(sorted_returns) - 1) * 100


def get_fundamental_verdict(
    conn: sqlite3.Connection,
    ticker: str,
    as_of_date: str | None = None,
    fetch_fn: Callable[[sqlite3.Connection, str, str], dict] = fetch_and_store_fundamentals,
) -> dict:
    """Layer 2 — Fundamental Quality.

    Reads cached fundamentals (7-day TTL, see config.fundamental.ttl_days)
    from SQLite; on a cache miss/stale entry, calls `fetch_fn` (default: live
    EODHD fetch, requires EODHD_API_KEY -- see data/eodhd.py) to refresh it.
    Relative strength is computed locally from the `prices` table against
    the configured universe (no network call). Verdict is PASS only if
    every Layer 2 threshold from config.yaml passes; otherwise REJECT, with
    the failing checks listed.

    `fetch_fn` is injectable so tests/offline runs can seed the cache
    directly or supply a fake fetch function instead of hitting the live
    API — real API calls belong behind @pytest.mark.live. An FMP-based
    fetch_fn (data/fundamentals.py, current/TTM data only, no point-in-time
    history) remains available and can be passed explicitly if needed.
    """
    cfg = load_config()
    fcfg = cfg.fundamental

    cached = get_cached_fundamentals(conn, ticker, ttl_days=fcfg.ttl_days)
    if cached is None:
        api_key = os.environ.get("EODHD_API_KEY", "")
        cached = fetch_fn(conn, ticker, api_key)

    resolved_date = as_of_date or cached.get("filed_date")
    if resolved_date is None:
        raise FundamentalError(f"No as_of_date available for {ticker} (missing filed_date).")

    relative_strength = calculate_relative_strength(
        conn, ticker, fcfg.universe, resolved_date, lookback_days=fcfg.rs_lookback_days
    )

    checks = {
        "roic": (cached.get("roic"), lambda v: v is not None and v > fcfg.roic_min),
        "fcf_positive": (cached.get("fcf"), lambda v: v is not None and v > 0),
        "fcf_margin": (cached.get("fcf_margin"), lambda v: v is not None and v > fcfg.fcf_margin_min),
        "revenue_growth_yoy": (
            cached.get("revenue_growth_yoy"),
            lambda v: v is not None and v > fcfg.revenue_growth_min,
        ),
        "earnings_growth_yoy": (
            cached.get("earnings_growth_yoy"),
            lambda v: v is not None and v > fcfg.earnings_growth_min,
        ),
        "relative_strength": (relative_strength, lambda v: v > fcfg.relative_strength_min),
        "price": (cached.get("price"), lambda v: v is not None and v > fcfg.price_min),
        "avg_daily_volume": (
            cached.get("avg_daily_volume"),
            lambda v: v is not None and v > fcfg.avg_volume_min,
        ),
    }

    failed = [name for name, (value, predicate) in checks.items() if not predicate(value)]
    verdict = "PASS" if not failed else "REJECT"
    reasoning = (
        "All fundamental checks passed."
        if verdict == "PASS"
        else f"Failed checks: {', '.join(failed)}."
    )
    logger.info("Fundamental verdict for %s: %s (%s)", ticker, verdict, reasoning)

    return {
        "ticker": ticker,
        "verdict": verdict,
        "roic": cached.get("roic"),
        "fcf": cached.get("fcf"),
        "fcf_margin": cached.get("fcf_margin"),
        "revenue_growth_yoy": cached.get("revenue_growth_yoy"),
        "earnings_growth_yoy": cached.get("earnings_growth_yoy"),
        "relative_strength": relative_strength,
        "price": cached.get("price"),
        "avg_daily_volume": cached.get("avg_daily_volume"),
        "failed_checks": failed,
        "reasoning": reasoning,
        "as_of_date": resolved_date,
    }
