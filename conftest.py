from __future__ import annotations

import sqlite3

import pytest

from swing_agent.storage.db import init_schema


@pytest.fixture
def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    yield conn
    conn.close()
