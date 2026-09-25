from __future__ import annotations


_REASON_LABELS = {
    "2R": "hit 2R (+50% off)",
    "3R": "hit 3R (+25% off)",
    "time_stop": "timed out (no 1R move within the time-stop window)",
    "backtest_end": "still open at the end of the backtest window (marked to market)",
}


def _describe_fill(fill: dict, entry_price: float, is_last_fill: bool, any_partial_taken_before: bool) -> str:
    reason = fill["reason"]
    date, price, shares = fill["date"], fill["price"], fill["shares"]

    if reason == "stop":
        if any_partial_taken_before:
            return (
                f"trailing stop on the remaining {shares} share(s) hit on {date} at ${price:.2f} "
                "(runner exit after profits were already banked)"
            )
        return (
            f"stopped out on {date} at ${price:.2f} for a loss -- price reversed against the "
            "entry before reaching 1R profit; the setup's thesis did not play out"
        )

    label = _REASON_LABELS.get(reason, reason)
    return f"{label} on {date}: {shares} share(s) at ${price:.2f}"


def build_trade_narrative(trade: dict) -> str:
    """Turns one closed-trade dict (as produced by backtest/engine.py's
    run_backtest -- ticker, setup, entry_date, entry_price, fills,
    entry_reasoning, macro_regime_at_entry, r_multiple, pnl) into a plain-
    English win/loss story: why it entered, what happened at each exit-rule
    fill, and the final result."""
    opening = (
        f"Entered {trade['ticker']} {trade['entry_date']} via {trade['setup']} "
        f"({trade.get('entry_reasoning', 'no reasoning recorded')}) at ${trade['entry_price']:.2f}"
    )
    macro = trade.get("macro_regime_at_entry")
    if macro:
        opening += f", macro regime {macro}."
    else:
        opening += "."

    fills = trade.get("fills", [])
    partial_taken = False
    clauses = []
    for fill in fills:
        clauses.append(_describe_fill(fill, trade["entry_price"], fill is fills[-1], partial_taken))
        if fill["reason"] in ("2R", "3R"):
            partial_taken = True

    body = " Then " + "; ".join(clauses) + "." if clauses else ""

    r = trade["r_multiple"]
    if r > 0.05:
        result = "WIN"
    elif r < -0.05:
        result = "LOSS"
    else:
        result = "BREAKEVEN"
    pnl = trade["pnl"]
    pnl_str = f"+${pnl:.0f}" if pnl >= 0 else f"-${abs(pnl):.0f}"
    closing = f" Result: {result}, {r:+.1f}R (~{pnl_str})."

    return opening + body + closing


def build_trade_narratives(trades: list[dict]) -> list[dict]:
    """Returns a new list of trade dicts, each with an added 'narrative' key.
    Non-destructive: input trade dicts are shallow-copied, not mutated."""
    result = []
    for trade in trades:
        enriched = dict(trade)
        enriched["narrative"] = build_trade_narrative(trade)
        result.append(enriched)
    return result
