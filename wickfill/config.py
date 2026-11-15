"""Configuration: typed defaults, overridden by a YAML file.

Unknown keys are an error, so a typo in config.yaml fails loudly instead of
being silently ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class StrategyConfig:
    timeframes: list[str] = field(default_factory=lambda: ["5m", "15m", "60m", "1d", "1w"])
    # Wick-zone qualification
    wick_min_ratio: float = 0.50  # (high-close)/(high-low)
    wick_min_pct: float = 0.04  # (high-close)/close
    volume_mult: float = 2.0  # candle volume vs average of prior candles
    volume_lookback: int = 20
    volume_min_prior: int = 5
    intraday_expiry_days: int = 3  # calendar days, 5m/15m/60m zones
    htf_expiry_days: int = 120  # calendar days, daily/weekly zones
    max_target_above_price: float = 2.0  # ignore zones with target > price * (1 + this)
    # State machine
    reclaim_closes: int = 2
    retest_tolerance: float = 0.01  # low <= level * 1.01
    # Signals
    enable_buy_b: bool = False  # fresh-reclaim entries (riskier)
    signal_start_et: str = "04:00"
    signal_end_et: str = "16:00"
    min_day_gain: float = 0.10
    min_rr: float = 1.5
    max_progress: float = 0.60  # block if price is this far from level to target
    max_failed_attempts: int = 0  # block if the zone failed more than this many times
    min_avg_dollar_volume: float = 20000.0  # per 5m bar
    dollar_volume_lookback: int = 20
    # Risk plan
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    pct_stop: float = 0.015  # stop no higher than price * (1 - this)
    t1_fraction: float = 0.5


@dataclass
class RiskConfig:
    equity: float = 1000.0
    risk_pct: float = 0.01
    buying_power: float | None = None  # None = same as equity (cash, no margin)
    # Hard limits
    max_position_value: float = 1000.0
    max_shares: int = 5000
    # Enforced by the paper broker / backtester (Phase 3 onward)
    max_trades_per_day: int = 3
    max_daily_loss_pct: float = 0.03
    time_exit_bars: int = 60
    flat_by_et: str = "16:00"


@dataclass
class DisplayConfig:
    local_tz: str = "America/Chicago"


@dataclass
class Config:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)


def _apply(obj: Any, data: dict[str, Any], path: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"Unknown config key '{path}{key}'. Check for typos.")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"Config section '{path}{key}' must be a mapping")
            _apply(current, value, f"{path}{key}.")
        else:
            if isinstance(current, float) and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            setattr(obj, key, value)


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    if path is not None:
        data = yaml.safe_load(Path(path).read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError("Config file must contain a YAML mapping")
        _apply(cfg, data, "")
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    allowed = {"5m", "15m", "60m", "1d", "1w"}
    bad = set(cfg.strategy.timeframes) - allowed
    if bad:
        raise ValueError(f"Unsupported timeframes: {sorted(bad)}")
    if not 0 < cfg.risk.risk_pct <= 0.05:
        raise ValueError("risk.risk_pct must be between 0 and 0.05 (5%)")
    if cfg.risk.equity <= 0:
        raise ValueError("risk.equity must be positive")
    if cfg.risk.buying_power is not None and cfg.risk.buying_power > cfg.risk.equity:
        raise ValueError("risk.buying_power cannot exceed equity (no margin or leverage)")
