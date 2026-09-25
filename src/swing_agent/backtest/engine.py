from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pandas as pd

from swing_agent.agents.macro_regime import MacroRegimeError, get_macro_regime
from swing_agent.agents.risk_manager import compute_trade_plan
from swing_agent.agents.technical import TechnicalError, get_technical_signal
from swing_agent.config import load_config
from swing_agent.logging_setup import get_logger

logger = get_logger(__name__)


def _load_full_history(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """Full ascending-date price history for `ticker`, plus a 10-day EMA and
    14-day ATR used only for the trailing-stop exit rule (kept local to the
    backtester rather than reusing agents/technical.py's compute_indicators,
    which uses the Layer-3 EMA20/50 pair, not the Exit Rules' 10-day EMA)."""
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
) -> dict:
    """Walk-forward simulation over [start_date, end_date] on the SPY trading
    calendar. Layers 1 (macro) and 3 (technical) are fully point-in-time
    backtestable from locally stored price/macro history.

    Layer 2 (fundamentals) is INTENTIONALLY NOT applied here: FMP's free tier
    only exposes current/TTM data, not a multi-year point-in-time history, so
    there is no $0-budget way to honor CLAUDE.md's "no look-ahead in
    backtests" rule for fundamentals (using today's cached fundamentals for a
    2020 entry decision would itself be a look-ahead violation). This
    backtests the macro+technical signal in isolation; a real trading
    decision should still require the Layer 2 PASS that run.py's live path
    enforces.
    """
    cfg = load_config()
    equity = account_equity if account_equity is not None else cfg.account.account_size

    histories = {t: _load_full_history(conn, t) for t in tickers}
    dates = _trading_dates(conn, start_date, end_date)

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
                try:
                    technical = get_technical_signal(conn, ticker, date)
                except TechnicalError:
                    continue
                if technical["verdict"] != "TRIGGER":
                    continue

                risk = compute_trade_plan(
                    account_equity=equity,
                    entry=technical["entry"],
                    stop=technical["stop"],
                    position_size_modifier=macro["position_size_modifier"],
                    half_size=technical["half_size"],
                    risk_per_trade_pct=cfg.account.risk_per_trade_pct,
                    current_open_positions=len(open_trades),
                    max_positions=cfg.account.max_positions,
                    reduce_size_after_losses=cfg.risk.reduce_size_after_losses,
                    reduce_size_multiplier=cfg.risk.reduce_size_multiplier,
                )
                if risk["verdict"] != "APPROVE":
                    continue

                open_trades[ticker] = _OpenTrade(
                    ticker=ticker, setup=technical["setup"], entry_date=date, entry_day_index=day_index,
                    entry_price=risk["entry"], current_stop=risk["stop"],
                    shares_original=risk["shares"], shares_remaining=risk["shares"],
                    risk_per_share=risk["risk_per_share"],
                    target_2r=risk["targets"]["2R"], target_3r=risk["targets"]["3R"],
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
