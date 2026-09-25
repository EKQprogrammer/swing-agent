from __future__ import annotations

import sqlite3

import pandas as pd

from swing_agent.backtest.engine import run_backtest
from swing_agent.storage.db import upsert_fundamentals, upsert_prices

PASSING_FUNDAMENTALS = {
    "filed_date": "2023-06-01",
    "roic": 15.0,
    "fcf": 1_000_000.0,
    "fcf_margin": 10.0,
    "revenue_growth_yoy": 8.0,
    "earnings_growth_yoy": 12.0,
    "price": 50.0,
    "avg_daily_volume": 1_000_000,
}

N = 500
ALL_DATES = pd.date_range("2023-01-01", periods=N, freq="D")
LEAD = 248


def _price_df(dates, opens, highs, lows, closes, volumes) -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes}, index=dates
    )


def _seed_market(conn: sqlite3.Connection) -> None:
    """SPY bullish and VIX calm for the whole date range, so macro passes on
    every backtest day."""
    spy_closes = [100 + i * 0.5 for i in range(N)]
    upsert_prices(conn, "SPY", _price_df(ALL_DATES, spy_closes, spy_closes, spy_closes, spy_closes, [1_000_000] * N))
    upsert_prices(
        conn, "^VIX",
        _price_df(ALL_DATES, [14.0] * N, [14.0] * N, [14.0] * N, [14.0] * N, [1_000_000] * N),
    )


def _leadin_closes() -> tuple[list[float], float, float]:
    """Rising trend through day 247, a pullback dip on day 248, and the
    resume/trigger (entry) close on day 249 -- same recipe validated against
    agents/technical.py in test_technical.py's pullback test. Sawtooth (not
    a pure monotonic ramp): a monotonic ramp has zero losses so RSI pins
    near 100, which now fails the pullback_rsi_max overbought gate."""
    closes = []
    price = 100.0
    for i in range(LEAD):
        price += -0.6 if i % 3 == 2 else 1.0
        closes.append(price)
    dip = closes[-1] - 6
    closes.append(dip)
    entry_close = dip + 8
    closes.append(entry_close)
    return closes, dip, entry_close


def _disable_pivot_proximity(monkeypatch) -> None:
    """These tests exercise exit-rule/risk-gating mechanics, not the
    (already separately, directly tested) pivot-proximity gate -- disable
    it so fixtures predating that gate don't incidentally depend on where
    pivots happen to land."""
    import dataclasses
    from swing_agent.config import load_config as real_load_config

    def patched(*a, **k):
        cfg = real_load_config(*a, **k)
        cfg.technical = dataclasses.replace(cfg.technical, pivot_proximity_enabled=False)
        return cfg

    monkeypatch.setattr("swing_agent.backtest.engine.load_config", patched)


def test_full_cycle_partial_exits_and_forced_close(memory_conn: sqlite3.Connection, monkeypatch) -> None:
    _disable_pivot_proximity(monkeypatch)
    _seed_market(memory_conn)
    closes, dip, entry_close = _leadin_closes()
    jump_close = entry_close + 50  # blows well past both 2R and 3R targets
    closes.append(jump_close)
    closes.extend([jump_close] * 5)

    opens, highs, lows = list(closes), list(closes), list(closes)
    highs[LEAD] = dip + 1
    lows[LEAD] = dip - 1
    volumes = [1_000_000] * LEAD + [600_000, 1_800_000] + [1_000_000] * (len(closes) - LEAD - 2)
    upsert_prices(memory_conn, "TST", _price_df(ALL_DATES[: len(closes)], opens, highs, lows, closes, volumes))

    result = run_backtest(memory_conn, ["TST"], str(ALL_DATES[240].date()), str(ALL_DATES[254].date()))
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_date"] == str(ALL_DATES[249].date())
    reasons = [f["reason"] for f in trade["fills"]]
    assert "2R" in reasons
    assert "3R" in reasons
    assert trade["r_multiple"] > 3.0  # 2R + 3R partials plus a big final mark-to-market


def test_stop_out_loses_approximately_1r(memory_conn: sqlite3.Connection, monkeypatch) -> None:
    _disable_pivot_proximity(monkeypatch)
    _seed_market(memory_conn)
    closes, dip, entry_close = _leadin_closes()
    closes.append(entry_close - 30)  # gaps straight through the stop the next day

    opens, highs, lows = list(closes), list(closes), list(closes)
    highs[LEAD] = dip + 1
    lows[LEAD] = dip - 1
    lows[-1] = entry_close - 30
    volumes = [1_000_000] * LEAD + [600_000, 1_800_000] + [1_000_000]
    upsert_prices(memory_conn, "STP", _price_df(ALL_DATES[: len(closes)], opens, highs, lows, closes, volumes))

    result = run_backtest(memory_conn, ["STP"], str(ALL_DATES[240].date()), str(ALL_DATES[251].date()))
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["fills"][0]["reason"] == "stop"
    assert trade["r_multiple"] == -1.0


def test_time_stop_exits_flat_trade_after_five_days(memory_conn: sqlite3.Connection, monkeypatch) -> None:
    _disable_pivot_proximity(monkeypatch)
    _seed_market(memory_conn)
    closes, dip, entry_close = _leadin_closes()
    closes.extend([entry_close] * 8)  # flat -- never reaches 1R

    opens, highs, lows = list(closes), list(closes), list(closes)
    highs[LEAD] = dip + 1
    lows[LEAD] = dip - 1
    volumes = [1_000_000] * LEAD + [600_000, 1_800_000] + [1_000_000] * 8
    upsert_prices(memory_conn, "TSP", _price_df(ALL_DATES[: len(closes)], opens, highs, lows, closes, volumes))

    result = run_backtest(memory_conn, ["TSP"], str(ALL_DATES[240].date()), str(ALL_DATES[259].date()))
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["fills"][0]["reason"] == "time_stop"
    assert trade["r_multiple"] == 0.0


def test_no_trades_when_no_setup_ever_triggers(memory_conn: sqlite3.Connection) -> None:
    _seed_market(memory_conn)
    n = 300
    closes = [100 + i * 1.0 for i in range(n)]  # smooth rise, flat volume -> no setup fires
    volumes = [1_000_000] * n
    upsert_prices(memory_conn, "FLT", _price_df(ALL_DATES[:n], closes, closes, closes, closes, volumes))

    result = run_backtest(memory_conn, ["FLT"], str(ALL_DATES[240].date()), str(ALL_DATES[260].date()))
    assert result["trades"] == []
    assert len(result["equity_curve"]) > 0


def _seed_pullback_ticker(conn: sqlite3.Connection, ticker: str) -> None:
    closes, dip, entry_close = _leadin_closes()
    jump_close = entry_close + 50
    closes.append(jump_close)
    closes.extend([jump_close] * 5)
    opens, highs, lows = list(closes), list(closes), list(closes)
    highs[LEAD] = dip + 1
    lows[LEAD] = dip - 1
    volumes = [1_000_000] * LEAD + [600_000, 1_800_000] + [1_000_000] * (len(closes) - LEAD - 2)
    upsert_prices(conn, ticker, _price_df(ALL_DATES[: len(closes)], opens, highs, lows, closes, volumes))


def test_use_fundamentals_false_ignores_missing_fundamentals(memory_conn: sqlite3.Connection, monkeypatch) -> None:
    _disable_pivot_proximity(monkeypatch)
    _seed_market(memory_conn)
    _seed_pullback_ticker(memory_conn, "TST")
    # default use_fundamentals=False: same behavior as before this feature existed
    result = run_backtest(memory_conn, ["TST"], str(ALL_DATES[240].date()), str(ALL_DATES[254].date()))
    assert len(result["trades"]) == 1


def test_use_fundamentals_true_blocks_trade_without_data(memory_conn: sqlite3.Connection) -> None:
    _seed_market(memory_conn)
    _seed_pullback_ticker(memory_conn, "TST")
    # no fundamentals row seeded at all -> Layer 2 rejects every day
    result = run_backtest(
        memory_conn, ["TST"], str(ALL_DATES[240].date()), str(ALL_DATES[254].date()), use_fundamentals=True
    )
    assert result["trades"] == []


def _patch_small_rs_lookback(monkeypatch) -> None:
    """The pullback fixture's entry sits at day 249 of its own price series
    (only 250 rows exist by then), which isn't enough for the real
    config.yaml's 252-day RS lookback -- shrink it for these tests so the RS
    calc itself succeeds and the fundamentals threshold logic is what's
    actually being tested, not an incidental history-length failure."""
    from swing_agent.config import load_config as real_load_config

    def small_lookback(*a, **k):
        cfg = real_load_config(*a, **k)
        cfg.fundamental.rs_lookback_days = 10
        return cfg

    monkeypatch.setattr("swing_agent.backtest.engine.load_config", small_lookback)


def test_use_fundamentals_true_allows_trade_with_passing_fundamentals(memory_conn: sqlite3.Connection, monkeypatch) -> None:
    # Both _patch_small_rs_lookback and _disable_pivot_proximity monkeypatch
    # the same load_config attribute from scratch (via real_load_config), so
    # calling them back-to-back would have the second silently clobber the
    # first's change. Compose them into a single patched config instead.
    import dataclasses
    from swing_agent.config import load_config as real_load_config

    def combined(*a, **k):
        cfg = real_load_config(*a, **k)
        cfg.fundamental.rs_lookback_days = 10
        cfg.technical = dataclasses.replace(cfg.technical, pivot_proximity_enabled=False)
        return cfg

    monkeypatch.setattr("swing_agent.backtest.engine.load_config", combined)
    _seed_market(memory_conn)
    _seed_pullback_ticker(memory_conn, "TST")
    upsert_fundamentals(memory_conn, {**PASSING_FUNDAMENTALS, "ticker": "TST"})
    result = run_backtest(
        memory_conn, ["TST"], str(ALL_DATES[240].date()), str(ALL_DATES[254].date()), use_fundamentals=True
    )
    assert len(result["trades"]) == 1


def test_use_fundamentals_true_blocks_trade_with_failing_fundamentals(memory_conn: sqlite3.Connection, monkeypatch) -> None:
    _patch_small_rs_lookback(monkeypatch)
    _seed_market(memory_conn)
    _seed_pullback_ticker(memory_conn, "TST")
    weak = {**PASSING_FUNDAMENTALS, "roic": 2.0, "ticker": "TST"}  # fails roic_min
    upsert_fundamentals(memory_conn, weak)
    result = run_backtest(
        memory_conn, ["TST"], str(ALL_DATES[240].date()), str(ALL_DATES[254].date()), use_fundamentals=True
    )
    assert result["trades"] == []
