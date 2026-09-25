from __future__ import annotations

from swing_agent.logging_setup import get_logger

logger = get_logger(__name__)


def compute_trade_plan(
    account_equity: float,
    entry: float,
    stop: float,
    position_size_modifier: float,
    half_size: bool,
    risk_per_trade_pct: float,
    consecutive_losses: int = 0,
    current_open_positions: int = 0,
    current_sector_exposure_pct: float = 0.0,
    max_positions: int = 6,
    max_sector_pct: float = 30.0,
    reduce_size_after_losses: int = 3,
    reduce_size_multiplier: float = 0.5,
    elevated_volatility: bool = False,
    volatility_size_multiplier: float = 0.5,
) -> dict:
    """Position sizing (Position Size = Account Equity x risk% / (Entry-Stop))
    plus the portfolio-level gates from CLAUDE.md's Position Sizing rules.

    `current_open_positions`, `current_sector_exposure_pct`, and
    `consecutive_losses` are NOT tracked anywhere yet in this project (no
    trade/portfolio log exists) — they default to a flat/fresh account (0)
    so this function is fully usable today, and are designed to be supplied
    by a future portfolio tracker (daily_scan.py / backtester) without an
    API change here.

    Returns a dict with verdict APPROVE/REJECT, share count, dollar risk,
    and the R-multiple exit levels from CLAUDE.md's Exit Rules.
    """
    if current_open_positions >= max_positions:
        return {
            "verdict": "REJECT",
            "reasoning": f"Max concurrent positions ({max_positions}) already open.",
        }
    if current_sector_exposure_pct >= max_sector_pct:
        return {
            "verdict": "REJECT",
            "reasoning": f"Sector exposure already at/above limit ({max_sector_pct}%).",
        }

    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return {
            "verdict": "REJECT",
            "reasoning": f"Entry ({entry}) must be above stop ({stop}) for a long setup.",
        }

    if position_size_modifier <= 0:
        return {
            "verdict": "REJECT",
            "reasoning": "Macro regime position-size modifier is 0 (no new longs).",
        }

    size_multiplier = position_size_modifier
    if half_size:
        size_multiplier *= 0.5
    if consecutive_losses >= reduce_size_after_losses:
        size_multiplier *= reduce_size_multiplier
    if elevated_volatility:
        # Tier 1 item D: this ticker's own ATR% is unusually high relative
        # to its trailing distribution (not just "high" in absolute terms).
        size_multiplier *= volatility_size_multiplier

    base_risk_dollars = account_equity * (risk_per_trade_pct / 100.0)
    risk_dollars = base_risk_dollars * size_multiplier
    shares = int(risk_dollars // risk_per_share)

    if shares <= 0:
        return {
            "verdict": "REJECT",
            "reasoning": (
                f"Computed position size is 0 shares (risk budget ${risk_dollars:.2f} "
                f"< risk/share ${risk_per_share:.2f})."
            ),
        }

    actual_risk_dollars = shares * risk_per_share

    result = {
        "verdict": "APPROVE",
        "shares": shares,
        "entry": entry,
        "stop": stop,
        "risk_per_share": risk_per_share,
        "risk_dollars": actual_risk_dollars,
        "size_multiplier": size_multiplier,
        "targets": {
            "breakeven": entry,
            "2R": entry + 2 * risk_per_share,
            "3R": entry + 3 * risk_per_share,
        },
        "exit_rules": [
            f"Initial stop {stop:.2f} (setup low or {risk_per_share:.2f} risk/share) = 1R loss.",
            f"Move stop to breakeven ({entry:.2f}) at 1R profit.",
            "Take 50% off at 2R.",
            "Take 25% off at 3R.",
            "Trail final 25% at 10-day EMA or 2x ATR.",
            "Time stop: exit if no 1R move within 5 trading days.",
            "Psychological stop: exit if you can't focus.",
        ],
        "reasoning": (
            f"{shares} shares at ${entry:.2f}, risking ${actual_risk_dollars:.2f} "
            f"({risk_per_trade_pct}% base x {size_multiplier:.2f} modifier)."
        ),
    }
    logger.info(
        "Risk-managed trade plan: %d shares, risk=$%.2f, multiplier=%.2f",
        shares, actual_risk_dollars, size_multiplier,
    )
    return result
