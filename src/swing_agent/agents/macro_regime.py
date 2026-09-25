from __future__ import annotations

import sqlite3

from swing_agent.config import load_config
from swing_agent.logging_setup import get_logger

logger = get_logger(__name__)


class MacroRegimeError(RuntimeError):
    """Raised when required SPY/VIX price data is missing for the requested as_of_date."""


def _resolve_as_of_date(conn: sqlite3.Connection, as_of_date: str | None) -> str:
    if as_of_date is not None:
        return as_of_date
    row = conn.execute("SELECT MAX(date) FROM prices WHERE ticker = 'SPY'").fetchone()
    if row is None or row[0] is None:
        raise MacroRegimeError("No SPY price data found in database.")
    return row[0]


def get_spy_200ma(conn: sqlite3.Connection, as_of_date: str, window: int = 200) -> float:
    """Returns the SPY simple moving average over `window` trading days
    ending on or before as_of_date. Point-in-time: only rows with
    date <= as_of_date are considered. Raises MacroRegimeError if fewer
    than `window` rows are available (never averages a partial window)."""
    rows = conn.execute(
        """
        SELECT close FROM prices
        WHERE ticker = 'SPY' AND date <= ?
        ORDER BY date DESC LIMIT ?
        """,
        (as_of_date, window),
    ).fetchall()
    if len(rows) < window:
        raise MacroRegimeError(
            f"Insufficient SPY history for {window}-day MA as of {as_of_date}: "
            f"only {len(rows)} rows available."
        )
    closes = [r[0] for r in rows]
    return sum(closes) / len(closes)


def get_macro_regime(conn: sqlite3.Connection, as_of_date: str | None = None) -> dict:
    """Reads SPY and ^VIX prices and DGS10 macro data from SQLite, all
    point-in-time filtered (date <= as_of_date). Computes SPY's trailing
    N-day SMA (N from config), current VIX close, and the latest 10Y yield.

    Regime/position-size logic depends ONLY on SPY-vs-200MA and VIX
    thresholds; yield_10y is informational (returned/logged) and never
    gates the decision. Missing DGS10 data yields yield_10y=None without
    raising. Missing SPY or VIX price data raises MacroRegimeError.

    Threshold boundaries (not specified by the spec, fixed here): VIX equal
    to vix_calm_max is NOT calm (falls into CAUTIOUS); VIX equal to
    vix_panic_min IS panic (falls into BEARISH).
    """
    cfg = load_config().macro_regime
    resolved_date = _resolve_as_of_date(conn, as_of_date)

    spy_row = conn.execute(
        "SELECT close FROM prices WHERE ticker='SPY' AND date <= ? ORDER BY date DESC LIMIT 1",
        (resolved_date,),
    ).fetchone()
    if spy_row is None:
        raise MacroRegimeError(f"No SPY price data on or before {resolved_date}.")
    spy_close = spy_row[0]

    spy_200ma = get_spy_200ma(conn, resolved_date, window=cfg.spy_trend_ma_days)

    vix_row = conn.execute(
        "SELECT close FROM prices WHERE ticker='^VIX' AND date <= ? ORDER BY date DESC LIMIT 1",
        (resolved_date,),
    ).fetchone()
    if vix_row is None:
        raise MacroRegimeError(f"No VIX price data on or before {resolved_date}.")
    vix = vix_row[0]

    yield_row = conn.execute(
        """
        SELECT value FROM macro_series
        WHERE series_id='DGS10' AND date <= ? AND value IS NOT NULL
        ORDER BY date DESC LIMIT 1
        """,
        (resolved_date,),
    ).fetchone()
    yield_10y = yield_row[0] if yield_row else None

    spy_above_ma = spy_close > spy_200ma
    if spy_above_ma and vix < cfg.vix_calm_max:
        regime, modifier = "BULLISH", 1.0
    elif spy_above_ma and cfg.vix_calm_max <= vix < cfg.vix_panic_min:
        regime, modifier = "CAUTIOUS", 0.5
    else:
        regime, modifier = "BEARISH", 0.0

    reasoning = (
        f"SPY {'above' if spy_above_ma else 'below'} {cfg.spy_trend_ma_days}MA "
        f"(close={spy_close:.2f}, ma={spy_200ma:.2f}); VIX={vix:.2f} "
        f"(calm<{cfg.vix_calm_max}, panic>={cfg.vix_panic_min})."
    )
    logger.info("Macro regime as of %s: %s (modifier=%.1f)", resolved_date, regime, modifier)

    return {
        "regime": regime,
        "position_size_modifier": modifier,
        "spy_close": spy_close,
        "spy_200ma": spy_200ma,
        "vix": vix,
        "yield_10y": yield_10y,
        "reasoning": reasoning,
        "as_of_date": resolved_date,
    }
