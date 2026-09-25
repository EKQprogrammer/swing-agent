from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from swing_agent.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/swing.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _to_py(value):
    """Coerce pandas/numpy scalars (incl. NaN) to plain Python values for sqlite3."""
    if value is None:
        return None
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Opens a SQLite connection, creating the parent dir and schema if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Executes schema.sql (idempotent CREATE TABLE IF NOT EXISTS statements)."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


def upsert_prices(conn: sqlite3.Connection, ticker: str, df: pd.DataFrame) -> int:
    """INSERT OR REPLACE rows from a yfinance-shaped DataFrame (Open/High/Low/
    Close/Volume columns, DatetimeIndex). No-op on an empty/None DataFrame."""
    if df is None or df.empty:
        logger.warning("upsert_prices: empty DataFrame for ticker=%s, skipping", ticker)
        return 0

    rows = []
    for idx, row in df.iterrows():
        rows.append(
            (
                ticker,
                idx.strftime("%Y-%m-%d"),
                _to_py(row.get("Open")),
                _to_py(row.get("High")),
                _to_py(row.get("Low")),
                _to_py(row.get("Close")),
                _to_py(row.get("Volume")),
            )
        )

    conn.executemany(
        """
        INSERT OR REPLACE INTO prices (ticker, date, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    logger.info("upsert_prices: wrote %d rows for ticker=%s", len(rows), ticker)
    return len(rows)


def upsert_macro_series(conn: sqlite3.Connection, series_id: str, df: pd.DataFrame) -> int:
    """INSERT OR REPLACE rows from a (date, value) DataFrame. No-op on empty df."""
    if df is None or df.empty:
        logger.warning("upsert_macro_series: empty DataFrame for series_id=%s, skipping", series_id)
        return 0

    rows = []
    for _, row in df.iterrows():
        date = row["date"]
        date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)
        rows.append((series_id, date_str, _to_py(row.get("value"))))

    conn.executemany(
        """
        INSERT OR REPLACE INTO macro_series (series_id, date, value)
        VALUES (?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    logger.info("upsert_macro_series: wrote %d rows for series_id=%s", len(rows), series_id)
    return len(rows)


_FUNDAMENTALS_COLUMNS = [
    "ticker", "filed_date", "roic", "fcf", "fcf_margin", "revenue_growth_yoy",
    "earnings_growth_yoy", "relative_strength", "price", "avg_daily_volume",
]


def upsert_fundamentals(conn: sqlite3.Connection, row: dict) -> None:
    """INSERT OR REPLACE a single fundamentals row (see schema.sql). Missing
    keys in `row` (e.g. relative_strength, computed separately from local
    price data rather than fetched from FMP) default to None."""
    values = {col: row.get(col) for col in _FUNDAMENTALS_COLUMNS}
    conn.execute(
        """
        INSERT OR REPLACE INTO fundamentals
            (ticker, filed_date, roic, fcf, fcf_margin, revenue_growth_yoy,
             earnings_growth_yoy, relative_strength, price, avg_daily_volume)
        VALUES (:ticker, :filed_date, :roic, :fcf, :fcf_margin, :revenue_growth_yoy,
                :earnings_growth_yoy, :relative_strength, :price, :avg_daily_volume)
        """,
        values,
    )
    conn.commit()


def get_cached_fundamentals(conn: sqlite3.Connection, ticker: str, ttl_days: int) -> dict | None:
    """Returns the most recent fundamentals row for `ticker` (by filed_date) if
    it was fetched within the last `ttl_days`, else None (cache miss/stale)."""
    row = conn.execute(
        f"""
        SELECT {', '.join(_FUNDAMENTALS_COLUMNS)}, fetched_at
        FROM fundamentals
        WHERE ticker = ?
        ORDER BY filed_date DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        return None

    data = dict(zip(_FUNDAMENTALS_COLUMNS + ["fetched_at"], row))
    # fetched_at is a naive UTC string from SQLite's datetime('now'); compare
    # against a naive UTC "now" to avoid a naive/aware TypeError.
    fetched_at = datetime.fromisoformat(data["fetched_at"])
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    if now_utc - fetched_at > timedelta(days=ttl_days):
        return None
    return data


def get_fundamentals_as_of(conn: sqlite3.Connection, ticker: str, as_of_date: str) -> dict | None:
    """Point-in-time fundamentals lookup for BACKTESTING: the most recent
    row with filed_date <= as_of_date, regardless of fetched_at/TTL (unlike
    get_cached_fundamentals, which is for the live 7-day-TTL-cache path).
    Requires the fundamentals table to already hold historical rows -- see
    data/eodhd.py's fetch_and_store_historical_fundamentals(). Returns None
    if no fundamentals exist for the ticker as of that date yet (e.g.
    pre-IPO or before EODHD's earliest filing)."""
    row = conn.execute(
        f"""
        SELECT {', '.join(_FUNDAMENTALS_COLUMNS)}
        FROM fundamentals
        WHERE ticker = ? AND filed_date <= ?
        ORDER BY filed_date DESC LIMIT 1
        """,
        (ticker, as_of_date),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_FUNDAMENTALS_COLUMNS, row))


def upsert_earnings_dates(conn: sqlite3.Connection, ticker: str, report_dates: list[str]) -> int:
    """INSERT OR REPLACE one row per historical earnings report date."""
    if not report_dates:
        return 0
    rows = [(ticker, d) for d in report_dates]
    conn.executemany(
        "INSERT OR REPLACE INTO earnings_dates (ticker, report_date) VALUES (?, ?)", rows
    )
    conn.commit()
    return len(rows)


def get_earnings_dates(conn: sqlite3.Connection, ticker: str) -> list[str]:
    """All known report dates for `ticker`, ascending -- callers needing
    point-in-time behavior should bisect this list themselves (see
    backtest/engine.py's pattern for other precomputed per-ticker series)."""
    rows = conn.execute(
        "SELECT report_date FROM earnings_dates WHERE ticker = ? ORDER BY report_date ASC", (ticker,)
    ).fetchall()
    return [r[0] for r in rows]


def get_latest_date(conn: sqlite3.Connection, table: str, key_col: str, key_val: str) -> str | None:
    """Returns MAX(date) for the given key. `table`/`key_col` are restricted to
    an allow-list, never interpolated from arbitrary caller input, to avoid
    SQL injection via string-built queries."""
    allowed = {
        "prices": "ticker",
        "macro_series": "series_id",
    }
    if table not in allowed or allowed[table] != key_col:
        raise ValueError(f"Unsupported table/key_col combination: {table}/{key_col}")

    query = f"SELECT MAX(date) FROM {table} WHERE {key_col} = ?"  # nosec: table/key_col from allow-list only
    row = conn.execute(query, (key_val,)).fetchone()
    return row[0] if row else None
