from __future__ import annotations

import io
import sqlite3

import pandas as pd
import requests

from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import upsert_macro_series

logger = get_logger(__name__)

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"


def fetch_fred_series(series_id: str) -> pd.DataFrame:
    """Fetches a public FRED series via the free fredgraph.csv endpoint (no
    FRED_API_KEY required). This is an unofficial/undocumented endpoint with
    no published stability or rate-limit guarantee, but it keeps setup
    frictionless for a $0-budget project; switch to the official REST API
    (via FRED_API_KEY) if it proves unreliable.

    Returns a DataFrame with columns ['date', 'value'] (value may be NaN for
    holidays/missing observations, which FRED marks with '.').
    """
    url = FRED_CSV_URL.format(series_id=series_id)
    logger.info("Fetching FRED series %s from %s", series_id, url)
    resp = requests.get(url, headers={"User-Agent": "swing-agent/0.1"}, timeout=30)
    resp.raise_for_status()

    try:
        df = pd.read_csv(io.StringIO(resp.text), na_values=".")
    except Exception as exc:
        raise RuntimeError(f"Failed to parse FRED CSV response for series {series_id!r}") from exc

    if df.shape[1] < 2:
        raise RuntimeError(
            f"Unexpected FRED CSV shape for series {series_id!r}: columns={list(df.columns)}"
        )

    date_col, value_col = df.columns[0], df.columns[1]
    df = df.rename(columns={date_col: "date", value_col: "value"})
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "value"]]


def fetch_and_store_macro_series(conn: sqlite3.Connection, series_id: str) -> int:
    df = fetch_fred_series(series_id)
    return upsert_macro_series(conn, series_id, df)
