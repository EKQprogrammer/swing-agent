from __future__ import annotations

import sqlite3

import pandas as pd

from swing_agent.config import load_config
from swing_agent.logging_setup import get_logger

logger = get_logger(__name__)


class TechnicalError(RuntimeError):
    """Raised when there isn't enough point-in-time price history to compute
    indicators / evaluate setups for the requested ticker/as_of_date."""


# --- indicator computation ---------------------------------------------------


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.where(avg_loss != 0, 100.0)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _load_price_history(
    conn: sqlite3.Connection, ticker: str, as_of_date: str | None, lookback_days: int
) -> pd.DataFrame:
    """Point-in-time price load: only rows with date <= as_of_date (or the
    ticker's latest date if as_of_date is None), most recent `lookback_days`
    rows, returned in ascending date order for indicator computation."""
    if as_of_date is None:
        row = conn.execute("SELECT MAX(date) FROM prices WHERE ticker = ?", (ticker,)).fetchone()
        if row is None or row[0] is None:
            raise TechnicalError(f"No price data found for ticker={ticker!r}.")
        as_of_date = row[0]

    rows = conn.execute(
        """
        SELECT date, open, high, low, close, volume FROM prices
        WHERE ticker = ? AND date <= ?
        ORDER BY date DESC LIMIT ?
        """,
        (ticker, as_of_date, lookback_days),
    ).fetchall()
    if not rows:
        raise TechnicalError(f"No price data on or before {as_of_date} for ticker={ticker!r}.")

    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    return df.iloc[::-1].reset_index(drop=True)


def _compute_weekly_pivots(df: pd.DataFrame) -> pd.DataFrame:
    """Tier 1, item A: classic floor-trader pivot points (PP/R1/S1/R2/S2),
    computed from the PRIOR completed week's H/L/C and carried forward onto
    every day of the following week -- no look-ahead (a week's pivots only
    ever derive from a week that has already fully closed). Weekly (not
    daily) pivots, since daily pivots are an intraday tool and this system
    holds 3 days to 3 weeks. Informational only for now (see config.py) --
    not gating any entry -- exposed on the result dict for diagnosis."""
    out = df.copy()
    dt = pd.to_datetime(out["date"])
    weekly = (
        out.assign(_dt=dt)
        .set_index("_dt")
        .resample("W-FRI")
        .agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    )
    weekly = weekly.shift(1).dropna()
    pp = (weekly["high"] + weekly["low"] + weekly["close"]) / 3
    weekly_pivots = pd.DataFrame(
        {
            "pivot_pp": pp,
            "pivot_r1": 2 * pp - weekly["low"],
            "pivot_s1": 2 * pp - weekly["high"],
            "pivot_r2": pp + (weekly["high"] - weekly["low"]),
            "pivot_s2": pp - (weekly["high"] - weekly["low"]),
        }
    ).reset_index().rename(columns={"_dt": "week_end"})

    left = out.assign(_dt=dt)
    if weekly_pivots.empty:
        for col in ("pivot_pp", "pivot_r1", "pivot_s1", "pivot_r2", "pivot_s2"):
            left[col] = float("nan")
        return left.drop(columns=["_dt"])

    merged = pd.merge_asof(
        left.sort_values("_dt"), weekly_pivots.sort_values("week_end"),
        left_on="_dt", right_on="week_end", direction="backward",
    )
    return merged.drop(columns=["_dt", "week_end"])


def compute_indicators(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Adds ema_fast/ema_slow/rsi/atr/avg_volume/pivot_* columns to an
    ascending-date OHLCV DataFrame. RSI is computed and reported (the
    "timing" leg of the 3-indicator stack) but does not gate setup detection
    below — CLAUDE.md's setup definitions reference EMA/volume/price action
    only, with no explicit RSI threshold, unlike Layer 1's explicit VIX
    thresholds. Weekly pivots (Tier 1 item A) are likewise informational
    only for now -- see _compute_weekly_pivots."""
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=cfg.ema_fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=cfg.ema_slow, adjust=False).mean()
    out["rsi"] = _rsi(out["close"], cfg.rsi_period)
    out["atr"] = _atr(out, cfg.atr_period)
    out["avg_volume"] = out["volume"].rolling(window=cfg.volume_avg_window).mean()
    out = _compute_weekly_pivots(out)
    return out


def _initial_stop(entry: float, setup_low: float, atr: float, cfg) -> float:
    """'Setup low or 2x ATR, whichever tighter' -> for a long, the tighter
    stop is the one closer to entry (smaller loss), i.e. the higher price."""
    atr_stop = entry - cfg.atr_stop_multiplier * atr
    return max(setup_low, atr_stop)


# --- setup detection -----------------------------------------------------------


def _detect_pullback(df: pd.DataFrame, cfg) -> dict | None:
    """Uptrend (price and fast EMA above slow EMA) pulls back to within
    pullback_tolerance_pct of either EMA on below-average volume, then
    resumes with today's close above yesterday's high on above-average
    volume. Entries with RSI >= pullback_rsi_max are rejected as chasing an
    already-overbought move (added after train-period backtest analysis
    showed this specific slice has negative expectancy -- see config.py)."""
    if len(df) < max(cfg.ema_slow, cfg.volume_avg_window) + 2:
        return None

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    uptrend = today["ema_fast"] > today["ema_slow"] and today["close"] > today["ema_slow"]
    if not uptrend:
        return None

    if pd.notna(today["rsi"]) and today["rsi"] >= cfg.pullback_rsi_max:
        return None

    tol = cfg.pullback_tolerance_pct / 100.0
    near_fast = yesterday["low"] <= yesterday["ema_fast"] * (1 + tol)
    near_slow = yesterday["low"] <= yesterday["ema_slow"] * (1 + tol)
    pulled_back = near_fast or near_slow
    declining_volume = pd.notna(yesterday["avg_volume"]) and yesterday["volume"] < yesterday["avg_volume"]

    resumed = today["close"] > yesterday["high"]
    above_avg_volume = pd.notna(today["avg_volume"]) and today["volume"] > today["avg_volume"]

    if not (pulled_back and declining_volume and resumed and above_avg_volume):
        return None

    setup_low = df["low"].iloc[-3:].min()
    entry = today["close"]
    stop = _initial_stop(entry, setup_low, today["atr"], cfg)
    return {
        "setup": "PULLBACK",
        "entry": float(entry),
        "stop": float(stop),
        "half_size": False,
        "reasoning": (
            f"Uptrend pullback to EMA{cfg.ema_fast}/{cfg.ema_slow} on declining volume, "
            f"resumed above prior high ({yesterday['high']:.2f}) on volume "
            f"{today['volume']:.0f} vs avg {today['avg_volume']:.0f}."
        ),
    }


def _detect_breakout(df: pd.DataFrame, cfg) -> dict | None:
    """3-8 week (breakout_min_days..breakout_max_days) consolidation, 5-15%
    wide, that breaks out today on 1.5x+ its own average volume."""
    if len(df) < cfg.breakout_max_days + 1:
        return None

    today = df.iloc[-1]
    history = df.iloc[:-1]  # everything strictly before today

    for window in range(cfg.breakout_min_days, cfg.breakout_max_days + 1):
        if len(history) < window:
            break
        segment = history.iloc[-window:]
        seg_high = segment["high"].max()
        seg_low = segment["low"].min()
        if seg_low <= 0:
            continue
        width_pct = (seg_high - seg_low) / seg_low * 100
        if not (cfg.breakout_width_min_pct <= width_pct <= cfg.breakout_width_max_pct):
            continue

        seg_avg_volume = segment["volume"].mean()
        breaks_out = today["close"] > seg_high
        volume_surge = seg_avg_volume > 0 and today["volume"] >= cfg.breakout_volume_multiplier * seg_avg_volume
        if breaks_out and volume_surge:
            entry = today["close"]
            stop = _initial_stop(entry, seg_low, today["atr"], cfg)
            return {
                "setup": "BREAKOUT",
                "entry": float(entry),
                "stop": float(stop),
                "half_size": False,
                "reasoning": (
                    f"{window}-day consolidation ({width_pct:.1f}% wide) broke out above "
                    f"{seg_high:.2f} on volume {today['volume']:.0f} vs "
                    f"{cfg.breakout_volume_multiplier}x avg {seg_avg_volume:.0f}."
                ),
            }
    return None


def _detect_failed_breakdown(df: pd.DataFrame, cfg) -> dict | None:
    """Price broke below a recent support level on panic volume within the
    last `failed_breakdown_recovery_days` bars, then closed back above
    support today. Half size per CLAUDE.md's exit-rules section."""
    window = cfg.failed_breakdown_support_window
    recovery = cfg.failed_breakdown_recovery_days
    if len(df) < window + recovery + 1:
        return None

    today = df.iloc[-1]
    for i in range(1, recovery + 1):
        breakdown_idx = len(df) - 1 - i
        if breakdown_idx < window:
            continue
        support_segment = df.iloc[breakdown_idx - window:breakdown_idx]
        support = support_segment["low"].min()
        breakdown_bar = df.iloc[breakdown_idx]

        avg_volume = breakdown_bar["avg_volume"]
        panic_volume = (
            pd.notna(avg_volume)
            and avg_volume > 0
            and breakdown_bar["volume"] >= cfg.failed_breakdown_volume_multiplier * avg_volume
        )
        broke_support = breakdown_bar["low"] < support
        recovered = today["close"] > support

        if broke_support and panic_volume and recovered:
            entry = today["close"]
            stop = _initial_stop(entry, breakdown_bar["low"], today["atr"], cfg)
            return {
                "setup": "FAILED_BREAKDOWN",
                "entry": float(entry),
                "stop": float(stop),
                "half_size": True,
                "reasoning": (
                    f"Support {support:.2f} broke on panic volume "
                    f"{breakdown_bar['volume']:.0f} ({i} bar(s) ago), recovered to "
                    f"close {today['close']:.2f} above support today."
                ),
            }
    return None


def _detect_gap_fade(df: pd.DataFrame, cfg) -> dict | None:
    """Tier 1 item E: Gap Fade, adapted for daily bars (no premarket-volume
    data available -- see TechnicalConfig's comment). Today gaps down
    gap_fade_down_pct+ vs yesterday's close on panic volume, but closes
    green and in the upper half of its own day's range -- the panic gap was
    overdone and buyers absorbed it. Half size: a counter-trend reversal
    play, same precedent as FAILED_BREAKDOWN."""
    if len(df) < cfg.volume_avg_window + 2:
        return None

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    if yesterday["close"] <= 0:
        return None
    gap_pct = (today["open"] - yesterday["close"]) / yesterday["close"] * 100
    gapped_down = gap_pct <= -cfg.gap_fade_down_pct

    avg_volume = today["avg_volume"]
    panic_volume = (
        pd.notna(avg_volume) and avg_volume > 0
        and today["volume"] >= cfg.gap_fade_volume_multiplier * avg_volume
    )

    day_range = today["high"] - today["low"]
    closed_green = today["close"] > today["open"]
    closed_upper_half = day_range > 0 and today["close"] >= today["low"] + day_range / 2

    if not (gapped_down and panic_volume and closed_green and closed_upper_half):
        return None

    entry = today["close"]
    stop = _initial_stop(entry, today["low"], today["atr"], cfg)
    return {
        "setup": "GAP_FADE",
        "entry": float(entry),
        "stop": float(stop),
        "half_size": True,
        "reasoning": (
            f"Gapped down {gap_pct:.1f}% on panic volume {today['volume']:.0f} vs avg "
            f"{avg_volume:.0f}, closed green in the upper half of the day's range "
            f"(overdone panic absorbed by buyers)."
        ),
    }


def get_technical_signal(
    conn: sqlite3.Connection, ticker: str, as_of_date: str | None = None
) -> dict:
    """Layer 3 — Technical Trigger. Loads point-in-time price history, computes
    the EMA/RSI/ATR/volume indicator stack, and checks the four setups in
    the order PULLBACK, BREAKOUT, FAILED_BREAKDOWN, GAP_FADE, returning the
    first match. If none match, verdict is NO_SETUP (not an error) with
    indicators still reported for visibility.
    """
    cfg = load_config().technical
    lookback = max(cfg.breakout_max_days, cfg.failed_breakdown_support_window) + 60

    raw = _load_price_history(conn, ticker, as_of_date, lookback_days=lookback)
    df = compute_indicators(raw, cfg)
    resolved_date = df["date"].iloc[-1]
    today = df.iloc[-1]

    match = (
        _detect_pullback(df, cfg)
        or _detect_breakout(df, cfg)
        or _detect_failed_breakdown(df, cfg)
        or _detect_gap_fade(df, cfg)
    )

    result = {
        "ticker": ticker,
        "as_of_date": resolved_date,
        "close": float(today["close"]),
        "ema_fast": float(today["ema_fast"]),
        "ema_slow": float(today["ema_slow"]),
        "rsi": float(today["rsi"]) if pd.notna(today["rsi"]) else None,
        "atr": float(today["atr"]) if pd.notna(today["atr"]) else None,
        "pivot_pp": float(today["pivot_pp"]) if pd.notna(today["pivot_pp"]) else None,
        "pivot_s1": float(today["pivot_s1"]) if pd.notna(today["pivot_s1"]) else None,
        "pivot_r1": float(today["pivot_r1"]) if pd.notna(today["pivot_r1"]) else None,
    }

    if match is None:
        logger.info("Technical signal for %s as of %s: NO_SETUP", ticker, resolved_date)
        result.update({"verdict": "NO_SETUP", "setup": None, "entry": None, "stop": None,
                        "half_size": False, "reasoning": "No PULLBACK/BREAKOUT/FAILED_BREAKDOWN/GAP_FADE setup detected."})
        return result

    logger.info("Technical signal for %s as of %s: %s", ticker, resolved_date, match["setup"])
    result.update({"verdict": "TRIGGER", **match})
    return result


def get_volatility_percentile(
    conn: sqlite3.Connection, ticker: str, as_of_date: str | None = None, lookback_days: int = 252
) -> float | None:
    """Tier 1 item D (live path): percentile rank (0-100) of TODAY's ATR%
    (atr/close*100) within `ticker`'s own trailing `lookback_days`
    distribution of the same metric -- a ticker-specific volatility-regime
    signal, distinct from the macro layer's VIX-driven CAUTIOUS/BEARISH
    regime. None if there isn't enough history yet. See
    backtest/engine.py's _fast_volatility_percentile for the backtest-fast
    equivalent (same _atr formula, precomputed once per ticker there)."""
    cfg = load_config().technical
    try:
        raw = _load_price_history(conn, ticker, as_of_date, lookback_days=lookback_days + cfg.atr_period + 5)
    except TechnicalError:
        return None
    if len(raw) < cfg.atr_period + 2:
        return None
    df = raw.copy()
    df["atr"] = _atr(df, cfg.atr_period)
    atr_pct = (df["atr"] / df["close"] * 100).dropna()
    if atr_pct.empty:
        return None
    current = atr_pct.iloc[-1]
    if len(atr_pct) < 2:
        return 100.0
    rank = (atr_pct < current).sum()
    return float(rank / (len(atr_pct) - 1) * 100)
