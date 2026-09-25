from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class AccountConfig:
    account_size: float = 10000.0
    risk_per_trade_pct: float = 1.0
    max_positions: int = 6
    max_sector_pct: float = 30.0


@dataclass
class MacroRegimeConfig:
    vix_calm_max: float = 25.0
    vix_panic_min: float = 35.0
    spy_trend_ma_days: int = 200


@dataclass
class DataConfig:
    db_path: str = "data/swing.db"
    price_history_years: int = 3
    tickers: list[str] = field(default_factory=lambda: ["SPY", "^VIX"])


@dataclass
class FundamentalConfig:
    roic_min: float = 10.0
    fcf_margin_min: float = 5.0
    revenue_growth_min: float = 5.0
    earnings_growth_min: float = 10.0
    relative_strength_min: float = 70.0
    relative_strength_preferred: float = 80.0
    price_min: float = 10.0
    avg_volume_min: float = 500_000
    ttl_days: int = 7
    rs_lookback_days: int = 252
    # Refinement round 2 (train+validation, 2013-2022, 30-ticker universe,
    # fundamentals-gated): BREAKOUT setups showed consistent negative avg R
    # when combined with Layer 2 (train-only 45 trades: -0.167R; train+
    # validation 68 trades: -0.055R) while PULLBACK/FAILED_BREAKDOWN stayed
    # positive in both windows. Hypothesis: entering a fresh breakout on a
    # stock that already cleared relative_strength_min (i.e. already
    # strongly outperformed) is late-stage momentum with more reversal risk
    # than a pullback-in-uptrend entry. Applied to both the live orchestrator
    # and the backtest engine so they can't silently diverge.
    excluded_setups: list[str] = field(default_factory=lambda: ["BREAKOUT"])
    # Placeholder liquid large-cap universe for relative-strength ranking and
    # fetch_fundamentals.py's default ticker list. Expand/replace with the
    # actual scan watchlist in a later phase.
    universe: list[str] = field(
        default_factory=lambda: [
            "SPY", "AAPL", "MSFT", "GOOGL", "AMZN",
            "META", "NVDA", "JPM", "JNJ", "PG",
        ]
    )


@dataclass
class TechnicalConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    atr_stop_multiplier: float = 2.0
    volume_avg_window: int = 20
    pullback_tolerance_pct: float = 1.5
    # Refinement round 1 (train-period diagnostic, 2013-2019, 30-ticker
    # universe): PULLBACK trades with entry RSI >= 70 showed negative avg R
    # (-0.04 across 40 trades) vs +0.27 for the rest -- an overbought-chasing
    # failure mode. Gating on it is expected to trade away ~4% of PULLBACK
    # volume in exchange for removing its worst-performing slice.
    pullback_rsi_max: float = 70.0
    breakout_min_days: int = 15
    breakout_max_days: int = 40
    breakout_width_min_pct: float = 5.0
    breakout_width_max_pct: float = 15.0
    breakout_volume_multiplier: float = 1.5
    failed_breakdown_support_window: int = 20
    failed_breakdown_volume_multiplier: float = 1.5
    failed_breakdown_recovery_days: int = 2
    # Tier 1 item E: Gap Fade, adapted for daily bars (no premarket volume
    # data available from yfinance/EODHD -- that's a real-time/tick product,
    # not part of this project's free stack). Fades an overdone gap-down
    # panic: gap down >= gap_fade_down_pct on panic volume, but closes green
    # and in the upper half of the day's range (buyers absorbed the panic).
    gap_fade_down_pct: float = 3.0
    gap_fade_volume_multiplier: float = 1.5


@dataclass
class RiskConfig:
    reduce_size_after_losses: int = 3
    reduce_size_multiplier: float = 0.5
    breakeven_r: float = 1.0
    partial_exit_1_r: float = 2.0
    partial_exit_1_pct: float = 50.0
    partial_exit_2_r: float = 3.0
    partial_exit_2_pct: float = 25.0
    trail_remaining_pct: float = 25.0
    trail_ema_days: int = 10
    trail_atr_multiplier: float = 2.0
    time_stop_days: int = 5


@dataclass
class BacktestConfig:
    # Separate from fundamental.universe (which drives the LIVE relative-
    # strength percentile calc) so backtest universe changes never silently
    # reshape live RS scores.
    tickers: list[str] = field(
        default_factory=lambda: [
            "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AMD", "AVGO", "CRM", "ADBE",
            "JPM", "BAC", "V", "MA", "GS",
            "JNJ", "UNH", "LLY", "ABBV",
            "PG", "KO", "WMT", "COST", "HD", "NKE",
            "CAT", "HON", "UPS",
            "XOM", "CVX",
        ]
    )


@dataclass
class Config:
    account: AccountConfig = field(default_factory=AccountConfig)
    macro_regime: MacroRegimeConfig = field(default_factory=MacroRegimeConfig)
    data: DataConfig = field(default_factory=DataConfig)
    fundamental: FundamentalConfig = field(default_factory=FundamentalConfig)
    technical: TechnicalConfig = field(default_factory=TechnicalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)


def load_config(path: str | Path = "config.yaml") -> Config:
    """Loads .env (once) then config.yaml, mapping present keys onto the
    typed dataclasses above. Missing keys/sections fall back to defaults."""
    load_dotenv()

    path = Path(path)
    raw: dict = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    account = AccountConfig(**raw.get("account", {}))
    macro_regime = MacroRegimeConfig(**raw.get("macro_regime", {}))
    data = DataConfig(**raw.get("data", {}))
    fundamental = FundamentalConfig(**raw.get("fundamental", {}))
    technical = TechnicalConfig(**raw.get("technical", {}))
    risk = RiskConfig(**raw.get("risk", {}))
    backtest = BacktestConfig(**raw.get("backtest", {}))

    return Config(
        account=account,
        macro_regime=macro_regime,
        data=data,
        fundamental=fundamental,
        technical=technical,
        risk=risk,
        backtest=backtest,
    )
