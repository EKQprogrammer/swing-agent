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
class Config:
    account: AccountConfig = field(default_factory=AccountConfig)
    macro_regime: MacroRegimeConfig = field(default_factory=MacroRegimeConfig)
    data: DataConfig = field(default_factory=DataConfig)


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

    return Config(account=account, macro_regime=macro_regime, data=data)
