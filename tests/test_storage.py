from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from swing_agent.storage.db import (
    get_connection,
    upsert_macro_series,
    upsert_prices,
)


def _price_df(dates: list[str], closes: list[float]) -> pd.DataFrame:
    idx = pd.to_datetime(dates)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        },
        index=idx,
    )


def test_init_schema_creates_tables(memory_conn: sqlite3.Connection) -> None:
    rows = memory_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {r[0] for r in rows}
    assert {"prices", "macro_series", "fundamentals"} <= table_names


def test_upsert_prices_idempotent(memory_conn: sqlite3.Connection) -> None:
    df = _price_df(["2024-01-02", "2024-01-03"], [100.0, 101.0])
    upsert_prices(memory_conn, "SPY", df)
    upsert_prices(memory_conn, "SPY", df)
    count = memory_conn.execute(
        "SELECT COUNT(*) FROM prices WHERE ticker='SPY'"
    ).fetchone()[0]
    assert count == 2


def test_upsert_prices_replace_updates_existing_row(memory_conn: sqlite3.Connection) -> None:
    df1 = _price_df(["2024-01-02"], [100.0])
    upsert_prices(memory_conn, "SPY", df1)
    df2 = _price_df(["2024-01-02"], [200.0])
    upsert_prices(memory_conn, "SPY", df2)
    close = memory_conn.execute(
        "SELECT close FROM prices WHERE ticker='SPY' AND date='2024-01-02'"
    ).fetchone()[0]
    assert close == 200.0


def test_upsert_prices_empty_dataframe_guard(memory_conn: sqlite3.Connection) -> None:
    empty = pd.DataFrame()
    written = upsert_prices(memory_conn, "SPY", empty)
    assert written == 0
    count = memory_conn.execute(
        "SELECT COUNT(*) FROM prices WHERE ticker='SPY'"
    ).fetchone()[0]
    assert count == 0


def test_upsert_macro_series_idempotent(memory_conn: sqlite3.Connection) -> None:
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "value": [4.1, 4.2],
        }
    )
    upsert_macro_series(memory_conn, "DGS10", df)
    upsert_macro_series(memory_conn, "DGS10", df)
    count = memory_conn.execute(
        "SELECT COUNT(*) FROM macro_series WHERE series_id='DGS10'"
    ).fetchone()[0]
    assert count == 2


def test_get_connection_creates_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "sub" / "test.db"
    conn = get_connection(db_path)
    try:
        assert db_path.exists()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"prices", "macro_series", "fundamentals"} <= tables
    finally:
        conn.close()
