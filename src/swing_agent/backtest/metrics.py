from __future__ import annotations

import math


def compute_metrics(trades: list[dict], equity_curve: list[dict], starting_equity: float) -> dict:
    """Win rate, average R, expectancy (in R), max drawdown, and a simplified
    Sharpe ratio (day-over-day realized-equity returns, no risk-free rate
    subtraction, annualized with sqrt(252))."""
    if not trades:
        return {
            "num_trades": 0,
            "win_rate": None,
            "avg_r": None,
            "expectancy_r": None,
            "max_drawdown_pct": None,
            "sharpe": None,
            "final_equity": starting_equity,
            "total_return_pct": 0.0,
        }

    wins = [t for t in trades if t["r_multiple"] > 0]
    losses = [t for t in trades if t["r_multiple"] <= 0]
    win_rate_frac = len(wins) / len(trades)
    avg_r = sum(t["r_multiple"] for t in trades) / len(trades)
    avg_win_r = sum(t["r_multiple"] for t in wins) / len(wins) if wins else 0.0
    avg_loss_r = sum(t["r_multiple"] for t in losses) / len(losses) if losses else 0.0
    expectancy_r = win_rate_frac * avg_win_r + (1 - win_rate_frac) * avg_loss_r

    equities = [e["equity"] for e in equity_curve] or [starting_equity]
    peak = equities[0]
    max_dd = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100)

    daily_returns = [
        (curr - prev) / prev for prev, curr in zip(equities, equities[1:]) if prev != 0
    ]
    if len(daily_returns) > 1:
        mean_r = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std_r = math.sqrt(variance)
        sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else None
    else:
        sharpe = None

    final_equity = equities[-1]
    total_return_pct = (final_equity - starting_equity) / starting_equity * 100 if starting_equity else 0.0

    return {
        "num_trades": len(trades),
        "win_rate": win_rate_frac * 100,
        "avg_r": avg_r,
        "expectancy_r": expectancy_r,
        "max_drawdown_pct": max_dd,
        "sharpe": sharpe,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
    }
