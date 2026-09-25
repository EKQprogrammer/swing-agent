from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass, field

import pandas as pd

from swing_agent.agents.fundamental import _evaluate_thresholds
from swing_agent.agents.macro_regime import MacroRegimeError, get_macro_regime
from swing_agent.agents.risk_manager import compute_trade_plan
from swing_agent.agents.technical import (
    _detect_breakout,
    _detect_failed_breakdown,
    _detect_pullback,
    compute_indicators,
)
from swing_agent.config import load_config
from swing_agent.logging_setup import get_logger
from swing_agent.storage.db import get_fundamentals_as_of

logger = get_logger(__name__)


def _load_full_history(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """Full ascending-date RAW price history for `ticker`, plus the 10-day
    EMA / 14-day ATR used only for the trailing-stop exit rule. Layer-3
    entry-signal indicators (ema_fast/ema_slow/rsi/atr/avg_volume) are
    deliberately NOT precomputed here -- see _scan_technical_signal_fast."""
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM prices WHERE ticker = ? ORDER BY date ASC",
        (ticker,),
    ).fetchall()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["ema10"] = df["close"].ewm(span=10, adjust=False).mean()
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    return df


def _scan_technical_signal_fast(raw_df: pd.DataFrame, technical_cfg, as_of_date: str) -> dict | None:
    """Point-in-time setup scan matching agents/technical.py's
    get_technical_signal() EXACTLY (same bounded lookback window, indicators
    recomputed fresh on that window, same detector priority order) -- this
    is a backtest-only performance path that avoids get_technical_signal's
    per-call cost (a fresh SQL query AND a fresh config.yaml/.env reload on
    every single call, which is correct and appropriately scoped for the
    live daily-scan use case but far too slow once a backtest walks
    thousands of trading days across dozens of tickers), while reusing its
    actual private detector functions so the live and backtest decisions
    never silently drift apart. The indicator window is recomputed fresh
    per call, same as live -- confirmed by direct comparison that a
    full-history "continuously computed" EMA differs numerically (by a
    fraction of a percent) from get_technical_signal's windowed recompute,
    which was enough to flip a handful of borderline setup triggers.
    """
    if raw_df.empty:
        return None
    lookback = max(technical_cfg.breakout_max_days, technical_cfg.failed_breakdown_support_window) + 60
    end = raw_df["date"].searchsorted(as_of_date, side="right")
    if end == 0:
        return None
    start = max(0, end - lookback)
    window_df = raw_df.iloc[start:end]
    if window_df.empty:
        return None

    indicator_df = compute_indicators(window_df, technical_cfg)
    match = (
        _detect_pullback(indicator_df, technical_cfg)
        or _detect_breakout(indicator_df, technical_cfg)
        or _detect_failed_breakdown(indicator_df, technical_cfg)
    )
    if match is None:
        return None

    today = indicator_df.iloc[-1]
    match["indicators"] = {
        "ema_fast": float(today["ema_fast"]) if pd.notna(today["ema_fast"]) else None,
        "ema_slow": float(today["ema_slow"]) if pd.notna(today["ema_slow"]) else None,
        "rsi": float(today["rsi"]) if pd.notna(today["rsi"]) else None,
        "atr": float(today["atr"]) if pd.notna(today["atr"]) else None,
    }
    return match


def _build_return_series(histories: dict[str, pd.DataFrame], lookback_days: int) -> dict[str, tuple[list, list]]:
    """Precomputes each ticker's trailing `lookback_days` % return series
    ONCE (vectorized pandas), for fast point-in-time relative-strength
    lookups during backtesting -- avoids agents/fundamental.py's
    calculate_relative_strength doing a fresh bounded SQL query per
    (ticker, universe-member, day), which would be prohibitively slow at
    30-ticker x multi-year backtest scale (the same class of problem
    _scan_technical_signal_fast already solves for Layer 3)."""
    result: dict[str, tuple[list, list]] = {}
    for ticker, df in histories.items():
        if df.empty:
            continue
        returns = (df["close"] / df["close"].shift(lookback_days) - 1.0)
        result[ticker] = (df["date"].tolist(), returns.tolist())
    return result


def _fast_relative_strength(
    return_series: dict[str, tuple[list, list]], ticker: str, universe: list[str], as_of_date: str
) -> float | None:
    """Point-in-time RS percentile rank using the precomputed series above
    (bisect lookup, O(log n)) instead of a fresh SQL scan. Returns None
    (not a raise) if the ticker itself lacks enough history yet -- the
    backtest loop just skips that ticker for that day, same as any other
    missing-data case."""
    members = list(dict.fromkeys([ticker, *universe]))
    values: dict[str, float] = {}
    for member in members:
        series = return_series.get(member)
        if series is None:
            continue
        dates, returns = series
        idx = bisect.bisect_right(dates, as_of_date) - 1
        if idx < 0:
            continue
        v = returns[idx]
        if v == v:  # filter NaN
            values[member] = v

    if ticker not in values:
        return None
    if len(values) < 2:
        return 100.0
    sorted_vals = sorted(values.values())
    rank = sorted_vals.index(values[ticker])
    return rank / (len(sorted_vals) - 1) * 100


def _fundamental_verdict_fast(
    conn: sqlite3.Connection,
    ticker: str,
    as_of_date: str,
    return_series: dict[str, tuple[list, list]],
    rs_universe: list[str],
    fcfg,
) -> dict:
    """Backtest-only Layer 2 check: point-in-time fundamentals row (small,
    indexed table -- a per-call SQL lookup here is cheap, unlike Layer 3's
    indicator recomputation) + the fast RS lookup above, evaluated through
    agents/fundamental.py's actual _evaluate_thresholds so live and backtest
    can never silently disagree on what counts as a PASS. fcfg is passed in
    (not reloaded via load_config() per call) to avoid repeating the
    config.yaml/.env file-read cost on every ticker-day."""
    row = get_fundamentals_as_of(conn, ticker, as_of_date)
    if row is None:
        return {"verdict": "REJECT", "failed_checks": ["no_fundamentals_data"]}

    rs = _fast_relative_strength(return_series, ticker, rs_universe, as_of_date)
    if rs is None:
        return {"verdict": "REJECT", "failed_checks": ["insufficient_price_history_for_rs"]}

    verdict, failed, reasoning = _evaluate_thresholds(row, rs, fcfg)
    return {"verdict": verdict, "failed_checks": failed, "reasoning": reasoning}


def _trading_dates(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT date FROM prices WHERE ticker = 'SPY' AND date BETWEEN ? AND ? ORDER BY date ASC",
        (start_date, end_date),
    ).fetchall()
    return [r[0] for r in rows]


@dataclass
class _OpenTrade:
    ticker: str
    setup: str
    entry_date: str
    entry_day_index: int
    entry_price: float
    current_stop: float
    shares_original: int
    shares_remaining: int
    risk_per_share: float
    target_2r: float
    target_3r: float
    entry_reasoning: str = ""
    entry_indicators: dict = field(default_factory=dict)
    macro_regime_at_entry: str = ""
    breakeven_moved: bool = False
    partial_2r_taken: bool = False
    partial_3r_taken: bool = False
    realized_pnl: float = 0.0
    fills: list = field(default_factory=list)


def _close_trade_record(trade: _OpenTrade, exit_date: str, closed_trades: list[dict]) -> None:
    total_risk = trade.shares_original * trade.risk_per_share
    r_multiple = trade.realized_pnl / total_risk if total_risk > 0 else 0.0
    closed_trades.append(
        {
            "ticker": trade.ticker,
            "setup": trade.setup,
            "entry_date": trade.entry_date,
            "exit_date": exit_date,
            "entry_price": trade.entry_price,
            "shares": trade.shares_original,
            "pnl": trade.realized_pnl,
            "r_multiple": r_multiple,
            "fills": trade.fills,
            "entry_reasoning": trade.entry_reasoning,
            "entry_indicators": trade.entry_indicators,
            "macro_regime_at_entry": trade.macro_regime_at_entry,
        }
    )


def _manage_trade(trade: _OpenTrade, bar: pd.Series, date: str, day_index: int, cfg) -> float:
    """Applies one day's OHLC to an open trade per CLAUDE.md's Exit Rules, in
    priority order: stop-out, 2R partial, breakeven move, 3R partial,
    trailing-stop update, time stop. Returns this bar's realized pnl delta
    (0.0 if nothing fired). The psychological stop ("exit if you can't
    focus") is a human-judgment rule with no numeric trigger and is not
    simulated."""
    realized = 0.0

    if bar["low"] <= trade.current_stop:
        shares = trade.shares_remaining
        pnl = (trade.current_stop - trade.entry_price) * shares
        trade.fills.append({"date": date, "shares": shares, "price": trade.current_stop, "reason": "stop"})
        trade.realized_pnl += pnl
        trade.shares_remaining = 0
        return pnl

    if not trade.partial_2r_taken and bar["high"] >= trade.target_2r:
        shares = min(int(trade.shares_original * (cfg.risk.partial_exit_1_pct / 100.0)), trade.shares_remaining)
        if shares > 0:
            pnl = (trade.target_2r - trade.entry_price) * shares
            trade.fills.append({"date": date, "shares": shares, "price": trade.target_2r, "reason": "2R"})
            trade.realized_pnl += pnl
            trade.shares_remaining -= shares
            realized += pnl
        trade.partial_2r_taken = True

    target_1r = trade.entry_price + trade.risk_per_share
    if not trade.breakeven_moved and bar["high"] >= target_1r:
        trade.current_stop = max(trade.current_stop, trade.entry_price)
        trade.breakeven_moved = True

    if (
        trade.partial_2r_taken
        and not trade.partial_3r_taken
        and trade.shares_remaining > 0
        and bar["high"] >= trade.target_3r
    ):
        shares = min(int(trade.shares_original * (cfg.risk.partial_exit_2_pct / 100.0)), trade.shares_remaining)
        if shares > 0:
            pnl = (trade.target_3r - trade.entry_price) * shares
            trade.fills.append({"date": date, "shares": shares, "price": trade.target_3r, "reason": "3R"})
            trade.realized_pnl += pnl
            trade.shares_remaining -= shares
            realized += pnl
        trade.partial_3r_taken = True

    if trade.partial_3r_taken and trade.shares_remaining > 0:
        candidates = [trade.current_stop]
        if pd.notna(bar.get("ema10")):
            candidates.append(bar["ema10"])
        if pd.notna(bar.get("atr14")):
            candidates.append(bar["close"] - cfg.risk.trail_atr_multiplier * bar["atr14"])
        # "Trail at 10-day EMA or 2x ATR": tighter (higher, more profit-protective) wins;
        # stop only ever ratchets up.
        trade.current_stop = max(candidates)

    if not trade.breakeven_moved and (day_index - trade.entry_day_index) >= cfg.risk.time_stop_days:
        shares = trade.shares_remaining
        if shares > 0:
            pnl = (bar["close"] - trade.entry_price) * shares
            trade.fills.append({"date": date, "shares": shares, "price": bar["close"], "reason": "time_stop"})
            trade.realized_pnl += pnl
            trade.shares_remaining = 0
            realized += pnl

    return realized


def run_backtest(
    conn: sqlite3.Connection,
    tickers: list[str],
    start_date: str,
    end_date: str,
    account_equity: float | None = None,
    use_fundamentals: bool = False,
    fundamentals_universe: list[str] | None = None,
) -> dict:
    """Walk-forward simulation over [start_date, end_date] on the SPY trading
    calendar. Layers 1 (macro) and 3 (technical) are always point-in-time
    backtestable from locally stored price/macro history.

    Layer 2 (fundamentals) defaults to OFF (`use_fundamentals=False`) for
    backward compatibility with earlier backtests/tests that never seeded
    fundamentals data -- with it off, every ticker would otherwise get
    silently REJECTed at Layer 2 (no data = fail) and no trades would ever
    open. Pass `use_fundamentals=True` once point-in-time fundamentals have
    actually been backfilled (scripts/fetch_fundamentals.py --historical,
    via EODHD -- FMP's free tier only exposes current/TTM data, so this
    wasn't possible before). `fundamentals_universe` sets the relative-
    strength comparison set; defaults to `tickers` itself (the backtest
    universe) when omitted.
    """
    cfg = load_config()
    equity = account_equity if account_equity is not None else cfg.account.account_size

    histories = {t: _load_full_history(conn, t) for t in tickers}
    dates = _trading_dates(conn, start_date, end_date)

    return_series = {}
    rs_universe = fundamentals_universe if fundamentals_universe is not None else tickers
    if use_fundamentals:
        return_series = _build_return_series(histories, cfg.fundamental.rs_lookback_days)

    open_trades: dict[str, _OpenTrade] = {}
    closed_trades: list[dict] = []
    equity_curve: list[dict] = []

    for day_index, date in enumerate(dates):
        for ticker in list(open_trades.keys()):
            trade = open_trades[ticker]
            hist = histories[ticker]
            bar_rows = hist[hist["date"] == date]
            if bar_rows.empty:
                continue
            bar = bar_rows.iloc[0]
            equity += _manage_trade(trade, bar, date, day_index, cfg)
            if trade.shares_remaining <= 0:
                _close_trade_record(trade, date, closed_trades)
                del open_trades[ticker]

        try:
            macro = get_macro_regime(conn, date)
        except MacroRegimeError:
            equity_curve.append({"date": date, "equity": equity})
            continue

        if macro["position_size_modifier"] > 0 and len(open_trades) < cfg.account.max_positions:
            for ticker in tickers:
                if ticker in open_trades or len(open_trades) >= cfg.account.max_positions:
                    continue
                hist = histories.get(ticker)
                if hist is None or hist.empty:
                    continue

                if use_fundamentals:
                    fnd = _fundamental_verdict_fast(conn, ticker, date, return_series, rs_universe, cfg.fundamental)
                    if fnd["verdict"] != "PASS":
                        continue

                match = _scan_technical_signal_fast(hist, cfg.technical, date)
                if match is None:
                    continue
                if use_fundamentals and match["setup"] in cfg.fundamental.excluded_setups:
                    # Data-driven exclusion (config.fundamental.excluded_setups),
                    # applied identically to the live orchestrator -- see
                    # FundamentalConfig's comment for the backtest evidence.
                    continue

                risk = compute_trade_plan(
                    account_equity=equity,
                    entry=match["entry"],
                    stop=match["stop"],
                    position_size_modifier=macro["position_size_modifier"],
                    half_size=match["half_size"],
                    risk_per_trade_pct=cfg.account.risk_per_trade_pct,
                    current_open_positions=len(open_trades),
                    max_positions=cfg.account.max_positions,
                    reduce_size_after_losses=cfg.risk.reduce_size_after_losses,
                    reduce_size_multiplier=cfg.risk.reduce_size_multiplier,
                )
                if risk["verdict"] != "APPROVE":
                    continue

                open_trades[ticker] = _OpenTrade(
                    ticker=ticker, setup=match["setup"], entry_date=date, entry_day_index=day_index,
                    entry_price=risk["entry"], current_stop=risk["stop"],
                    shares_original=risk["shares"], shares_remaining=risk["shares"],
                    risk_per_share=risk["risk_per_share"],
                    target_2r=risk["targets"]["2R"], target_3r=risk["targets"]["3R"],
                    entry_reasoning=match["reasoning"],
                    entry_indicators=match["indicators"],
                    macro_regime_at_entry=macro["regime"],
                )

        equity_curve.append({"date": date, "equity": equity})

    # mark remaining open trades closed at the last available bar (mark-to-market)
    for ticker, trade in list(open_trades.items()):
        hist = histories[ticker]
        remaining = hist[hist["date"] <= end_date]
        if remaining.empty:
            continue
        last_bar = remaining.iloc[-1]
        shares = trade.shares_remaining
        pnl = (last_bar["close"] - trade.entry_price) * shares
        trade.fills.append({"date": last_bar["date"], "shares": shares, "price": last_bar["close"], "reason": "backtest_end"})
        trade.realized_pnl += pnl
        trade.shares_remaining = 0
        equity += pnl
        _close_trade_record(trade, last_bar["date"], closed_trades)

    return {"trades": closed_trades, "equity_curve": equity_curve, "starting_equity": account_equity or cfg.account.account_size}
