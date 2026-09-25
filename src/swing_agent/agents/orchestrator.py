from __future__ import annotations

import sqlite3
from typing import Callable

from swing_agent.agents.fundamental import get_fundamental_verdict
from swing_agent.agents.macro_regime import get_macro_regime
from swing_agent.agents.risk_manager import compute_trade_plan
from swing_agent.agents.technical import get_technical_signal
from swing_agent.config import load_config
from swing_agent.data.fundamentals import fetch_and_store_fundamentals
from swing_agent.logging_setup import get_logger

logger = get_logger(__name__)


def get_verdict(
    conn: sqlite3.Connection,
    ticker: str,
    as_of_date: str | None = None,
    fetch_fn: Callable[[sqlite3.Connection, str, str], dict] = fetch_and_store_fundamentals,
    portfolio_state: dict | None = None,
) -> dict:
    """Chains Layer 1 (macro regime) -> Layer 2 (fundamental quality) ->
    Layer 3 (technical trigger) -> risk-managed position sizing. A REJECT
    verdict/failure at any layer short-circuits and names the failing layer
    in `reject_layer`; only a pass at every layer reaches APPROVE.

    `portfolio_state` (optional: {"consecutive_losses", "open_positions",
    "sector_exposure_pct"}) carries live account context the risk manager
    needs but this project doesn't persist yet (no trade/portfolio log) —
    defaults assume a flat/fresh account. `fetch_fn` is the same injectable
    FMP-fetch hook used by get_fundamental_verdict, so this whole chain is
    testable offline.
    """
    portfolio_state = portfolio_state or {}
    cfg = load_config()

    macro = get_macro_regime(conn, as_of_date)
    if macro["position_size_modifier"] <= 0:
        return _reject(ticker, macro["as_of_date"], "macro", macro["reasoning"], macro=macro)

    fundamental = get_fundamental_verdict(conn, ticker, as_of_date, fetch_fn=fetch_fn)
    if fundamental["verdict"] != "PASS":
        return _reject(
            ticker, fundamental["as_of_date"], "fundamental", fundamental["reasoning"],
            macro=macro, fundamental=fundamental,
        )

    technical = get_technical_signal(conn, ticker, as_of_date)
    if technical["verdict"] != "TRIGGER":
        return _reject(
            ticker, technical["as_of_date"], "technical", technical["reasoning"],
            macro=macro, fundamental=fundamental, technical=technical,
        )

    risk = compute_trade_plan(
        account_equity=cfg.account.account_size,
        entry=technical["entry"],
        stop=technical["stop"],
        position_size_modifier=macro["position_size_modifier"],
        half_size=technical["half_size"],
        risk_per_trade_pct=cfg.account.risk_per_trade_pct,
        consecutive_losses=portfolio_state.get("consecutive_losses", 0),
        current_open_positions=portfolio_state.get("open_positions", 0),
        current_sector_exposure_pct=portfolio_state.get("sector_exposure_pct", 0.0),
        max_positions=cfg.account.max_positions,
        max_sector_pct=cfg.account.max_sector_pct,
        reduce_size_after_losses=cfg.risk.reduce_size_after_losses,
        reduce_size_multiplier=cfg.risk.reduce_size_multiplier,
    )
    if risk["verdict"] != "APPROVE":
        return _reject(
            ticker, technical["as_of_date"], "risk", risk["reasoning"],
            macro=macro, fundamental=fundamental, technical=technical, risk=risk,
        )

    logger.info("Verdict for %s: APPROVE (%s setup, %d shares)", ticker, technical["setup"], risk["shares"])
    return {
        "ticker": ticker,
        "as_of_date": technical["as_of_date"],
        "verdict": "APPROVE",
        "reject_layer": None,
        "macro": macro,
        "fundamental": fundamental,
        "technical": technical,
        "risk": risk,
    }


def _reject(ticker: str, as_of_date: str, layer: str, reasoning: str, **layers) -> dict:
    logger.info("Verdict for %s: REJECT at %s layer (%s)", ticker, layer, reasoning)
    return {
        "ticker": ticker,
        "as_of_date": as_of_date,
        "verdict": "REJECT",
        "reject_layer": layer,
        "reasoning": reasoning,
        "macro": layers.get("macro"),
        "fundamental": layers.get("fundamental"),
        "technical": layers.get("technical"),
        "risk": layers.get("risk"),
    }
