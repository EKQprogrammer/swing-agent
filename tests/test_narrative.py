from __future__ import annotations

from swing_agent.backtest.narrative import build_trade_narrative, build_trade_narratives


def _trade(**overrides) -> dict:
    base = {
        "ticker": "AAPL",
        "setup": "PULLBACK",
        "entry_date": "2021-03-04",
        "exit_date": "2021-03-22",
        "entry_price": 122.50,
        "shares": 20,
        "pnl": 0.0,
        "r_multiple": 0.0,
        "fills": [],
        "entry_reasoning": "Uptrend pullback to EMA20/50 on declining volume, resumed above prior high.",
        "macro_regime_at_entry": "BULLISH",
    }
    base.update(overrides)
    return base


def test_win_via_partials_and_trailing_stop() -> None:
    trade = _trade(
        pnl=410.0,
        r_multiple=2.6,
        fills=[
            {"date": "2021-03-10", "shares": 10, "price": 132.5, "reason": "2R"},
            {"date": "2021-03-15", "shares": 5, "price": 140.0, "reason": "3R"},
            {"date": "2021-03-22", "shares": 5, "price": 138.0, "reason": "stop"},
        ],
    )
    narrative = build_trade_narrative(trade)
    assert narrative.startswith("Entered AAPL 2021-03-04 via PULLBACK")
    assert "hit 2R" in narrative
    assert "hit 3R" in narrative
    assert "trailing stop" in narrative
    assert "runner exit after profits were already banked" in narrative
    assert "Result: WIN, +2.6R (~+$410)." in narrative


def test_straight_loss_no_partials() -> None:
    trade = _trade(
        pnl=-130.0,
        r_multiple=-1.0,
        fills=[{"date": "2021-03-08", "shares": 20, "price": 118.9, "reason": "stop"}],
    )
    narrative = build_trade_narrative(trade)
    assert "stopped out on 2021-03-08" in narrative
    assert "price reversed against the entry before reaching 1R" in narrative
    assert "Result: LOSS, -1.0R (~-$130)." in narrative


def test_time_stop_exit() -> None:
    trade = _trade(
        pnl=5.0,
        r_multiple=0.02,
        fills=[{"date": "2021-03-09", "shares": 20, "price": 122.75, "reason": "time_stop"}],
    )
    narrative = build_trade_narrative(trade)
    assert "timed out (no 1R move" in narrative
    assert "Result: BREAKEVEN" in narrative


def test_backtest_end_forced_close() -> None:
    trade = _trade(
        pnl=200.0,
        r_multiple=1.5,
        fills=[{"date": "2024-12-31", "shares": 20, "price": 132.5, "reason": "backtest_end"}],
    )
    narrative = build_trade_narrative(trade)
    assert "still open at the end of the backtest window" in narrative
    assert "Result: WIN" in narrative


def test_build_trade_narratives_is_non_destructive_and_adds_key() -> None:
    trades = [_trade(r_multiple=1.0, pnl=100.0, fills=[{"date": "d", "shares": 1, "price": 1.0, "reason": "stop"}])]
    enriched = build_trade_narratives(trades)
    assert "narrative" in enriched[0]
    assert "narrative" not in trades[0]  # original untouched
