"""Plain data types shared by every part of the bot."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


@dataclass(frozen=True)
class Bar:
    """One completed OHLCV bar. `ts` is the bar's START time, always UTC."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("Bar.ts must be timezone-aware (UTC)")
        if self.ts.utcoffset() != timedelta(0):
            object.__setattr__(self, "ts", self.ts.astimezone(UTC))


@dataclass(frozen=True)
class Candle:
    """A completed candle on some timeframe, built from bars."""

    tf: str
    start: datetime  # UTC, candle open time
    end: datetime  # UTC, nominal candle close time
    open: float
    high: float
    low: float
    close: float
    volume: float
