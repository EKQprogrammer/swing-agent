"""Fetches the DGS10 (10-year Treasury yield) FRED series into SQLite.

Usage:
    python scripts/fetch_macro.py [--db data/swing.db] [--config config.yaml]
"""
from __future__ import annotations

import argparse

from swing_agent.config import load_config
from swing_agent.data.macro import fetch_and_store_macro_series
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_connection

logger = get_logger(__name__)

SERIES_ID = "DGS10"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db_path = args.db or cfg.data.db_path

    conn = get_connection(db_path)
    try:
        rows = fetch_and_store_macro_series(conn, SERIES_ID)
        print(f"{SERIES_ID}: {rows} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
