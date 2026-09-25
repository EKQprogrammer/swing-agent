from __future__ import annotations

import pytest

from swing_agent.backtest.metrics import compute_metrics


def test_no_trades_returns_empty_metrics() -> None:
    metrics = compute_metrics([], [], starting_equity=10_000.0)
    assert metrics["num_trades"] == 0
    assert metrics["win_rate"] is None
    assert metrics["final_equity"] == 10_000.0


def test_win_rate_and_avg_r() -> None:
    trades = [
        {"r_multiple": 2.0}, {"r_multiple": -1.0}, {"r_multiple": 3.0}, {"r_multiple": -1.0},
    ]
    equity_curve = [{"date": "d0", "equity": 10_000.0}, {"date": "d1", "equity": 10_300.0}]
    metrics = compute_metrics(trades, equity_curve, starting_equity=10_000.0)
    assert metrics["num_trades"] == 4
    assert metrics["win_rate"] == 50.0
    assert metrics["avg_r"] == pytest.approx(0.75)
    assert metrics["expectancy_r"] == pytest.approx(0.75)


def test_max_drawdown_from_equity_curve() -> None:
    equity_curve = [
        {"date": "d0", "equity": 10_000.0},
        {"date": "d1", "equity": 11_000.0},
        {"date": "d2", "equity": 9_900.0},  # 10% drawdown from the 11,000 peak
        {"date": "d3", "equity": 10_500.0},
    ]
    metrics = compute_metrics([{"r_multiple": 1.0}], equity_curve, starting_equity=10_000.0)
    assert metrics["max_drawdown_pct"] == pytest.approx(10.0)


def test_total_return_and_final_equity() -> None:
    equity_curve = [{"date": "d0", "equity": 10_000.0}, {"date": "d1", "equity": 12_000.0}]
    metrics = compute_metrics([{"r_multiple": 1.0}], equity_curve, starting_equity=10_000.0)
    assert metrics["final_equity"] == 12_000.0
    assert metrics["total_return_pct"] == pytest.approx(20.0)
