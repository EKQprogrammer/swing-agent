from __future__ import annotations

from swing_agent.agents.risk_manager import compute_trade_plan


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        account_equity=10_000.0,
        entry=100.0,
        stop=95.0,  # risk_per_share = 5.0
        position_size_modifier=1.0,
        half_size=False,
        risk_per_trade_pct=1.0,  # base risk budget = $100
    )
    kwargs.update(overrides)
    return kwargs


def test_full_size_bullish_trade() -> None:
    result = compute_trade_plan(**_base_kwargs())
    assert result["verdict"] == "APPROVE"
    # $100 risk budget / $5 risk-per-share = 20 shares
    assert result["shares"] == 20
    assert result["targets"]["2R"] == 110.0
    assert result["targets"]["3R"] == 115.0


def test_cautious_regime_halves_size() -> None:
    result = compute_trade_plan(**_base_kwargs(position_size_modifier=0.5))
    assert result["verdict"] == "APPROVE"
    assert result["shares"] == 10  # $50 budget / $5


def test_half_size_on_failed_breakdown() -> None:
    result = compute_trade_plan(**_base_kwargs(half_size=True))
    assert result["shares"] == 10  # $100 * 0.5 / $5


def test_reduced_size_after_three_losses() -> None:
    result = compute_trade_plan(**_base_kwargs(consecutive_losses=3))
    assert result["shares"] == 10  # $100 * 0.5 / $5


def test_elevated_volatility_reduces_size() -> None:
    result = compute_trade_plan(**_base_kwargs(elevated_volatility=True, volatility_size_multiplier=0.5))
    assert result["shares"] == 10  # $100 * 0.5 / $5


def test_elevated_volatility_off_by_default_full_size() -> None:
    result = compute_trade_plan(**_base_kwargs())
    assert result["shares"] == 20  # unaffected unless elevated_volatility=True


def test_bearish_regime_rejects() -> None:
    result = compute_trade_plan(**_base_kwargs(position_size_modifier=0.0))
    assert result["verdict"] == "REJECT"


def test_invalid_stop_above_entry_rejects() -> None:
    result = compute_trade_plan(**_base_kwargs(stop=105.0))
    assert result["verdict"] == "REJECT"


def test_max_positions_reached_rejects() -> None:
    result = compute_trade_plan(**_base_kwargs(current_open_positions=6, max_positions=6))
    assert result["verdict"] == "REJECT"


def test_sector_exposure_limit_rejects() -> None:
    result = compute_trade_plan(**_base_kwargs(current_sector_exposure_pct=30.0, max_sector_pct=30.0))
    assert result["verdict"] == "REJECT"


def test_tiny_risk_budget_rounds_to_zero_shares_rejects() -> None:
    result = compute_trade_plan(**_base_kwargs(account_equity=10.0))  # $0.10 budget / $5
    assert result["verdict"] == "REJECT"
